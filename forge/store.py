"""In-memory deterministic store. Repositories are keyed strictly by
(owner, name) under the token owner; any other owner is a 404, never a
cross-owner hit. Git state is the source of truth for file contents:
contents are read from the branch tip's tree, so REST, GraphQL and git
always agree.
"""
import base64
import difflib
import hashlib
import threading

from forge.gitproto import (
    ZERO_OID, hash_object, parse_commit, parse_tree, build_tree,
    build_commit, flatten_tree, reachable_objects, merge_base,
)
from forge.scenario import VIEWER_LOGIN


def _ts(seq):
    # Deterministic timestamp: base epoch + seq seconds, ISO-8601 UTC.
    base = 1735689600 + seq
    import datetime
    return datetime.datetime.fromtimestamp(base, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Repo:
    def __init__(self, repo_id, owner, name, private, description):
        self.id = repo_id
        self.owner = owner
        self.name = name
        self.full_name = '%s/%s' % (owner, name)
        self.private = private
        self.description = description or ''
        self.default_branch = 'main'
        self.created_at = '2026-01-01T00:00:00Z'
        self.objects = {}          # sha -> (type, data)
        self.refs = {}             # 'refs/heads/x' -> sha
        self.issues = {}           # number -> dict
        self.issue_counter = 1
        self.pulls = {}            # number -> dict
        self.pr_counter = 1
        self.labels = {}           # name -> {'name','color'}
        self.comment_counter = 1
        self.seq = 0               # deterministic clock / ordering counter
        self.pack_cache = {}       # (wants, depth, haves) -> (pack_bytes, shallow)
        self.pack_lock = threading.Lock()
        self.version = 0           # bumped on any git mutation (cache key)

    def next_seq(self):
        self.seq += 1
        return self.seq

    def head_ref(self):
        return 'refs/heads/%s' % self.default_branch

    def branch_tip(self, branch):
        return self.refs.get('refs/heads/%s' % branch)

    def store_object(self, obj_type, data):
        sha = hash_object(obj_type, data)
        if sha not in self.objects:
            self.objects[sha] = (obj_type, data)
        return sha

    def tree_of(self, commit_sha):
        hit = self.objects.get(commit_sha)
        if not hit or hit[0] != 'commit':
            return {}
        tree_sha = parse_commit(hit[1])['tree']
        return flatten_tree(self.objects, tree_sha) if tree_sha else {}

    def build_tree_from_flat(self, flat):
        """flat: {path: (mode, sha)} -> root tree sha (stores intermediates)."""
        # Build nested structure.
        root = {}

        def insert(node, parts, mode, sha):
            if len(parts) == 1:
                node[parts[0]] = (mode, sha)
                return
            sub = node.get(parts[0])
            if not isinstance(sub, dict):
                sub = {}
                node[parts[0]] = sub
            insert(sub, parts[1:], mode, sha)

        for path, (mode, sha) in flat.items():
            insert(root, path.split('/'), mode, sha)

        def build(node):
            entries = []
            for name, val in node.items():
                if isinstance(val, dict):
                    entries.append((b'40000', name.encode('utf-8'), build(val)))
                else:
                    entries.append((val[0], name.encode('utf-8'), val[1]))
            data = build_tree(entries)
            return self.store_object('tree', data)

        return build(root)

    def commit_file(self, branch, path, content, message, expected_sha=None):
        """Create a commit on `branch` setting `path` to `content` (bytes).
        If the branch does not exist it is created from the default branch
        tip (or an empty tree). Returns (commit_sha, blob_sha, is_update,
        error_status) where error_status is None or 409."""
        tip = self.branch_tip(branch)
        if tip is None and branch != self.default_branch:
            tip = self.branch_tip(self.default_branch)
        flat = self.tree_of(tip) if tip else {}
        existing = flat.get(path)
        if expected_sha is not None:
            current_sha = existing[1] if existing else None
            if current_sha != expected_sha:
                return None, None, False, 409
        is_update = existing is not None
        blob_sha = self.store_object('blob', content)
        flat[path] = (b'100644', blob_sha)
        tree_sha = self.build_tree_from_flat(flat)
        parents = [tip] if tip else []
        commit_sha = self.store_object(
            'commit', build_commit(tree_sha, parents, message.encode('utf-8')))
        self.refs['refs/heads/%s' % branch] = commit_sha
        self.version += 1
        self.pack_cache.clear()
        return commit_sha, blob_sha, is_update, None

    def delete_file(self, branch, path, message, expected_sha=None):
        tip = self.branch_tip(branch)
        if tip is None:
            return None, 404
        flat = self.tree_of(tip)
        existing = flat.get(path)
        if existing is None:
            return None, 404
        if expected_sha is not None and existing[1] != expected_sha:
            return None, 409
        del flat[path]
        tree_sha = self.build_tree_from_flat(flat)
        commit_sha = self.store_object(
            'commit', build_commit(tree_sha, [tip], message.encode('utf-8')))
        self.refs['refs/heads/%s' % branch] = commit_sha
        self.version += 1
        self.pack_cache.clear()
        return commit_sha, None

    def commits_from(self, start):
        """Commits reachable from a branch name or sha, newest first (BFS
        by generation: parents after children)."""
        tip = self.branch_tip(start) if not self.objects.get(start) else start
        if tip is None:
            tip = self.refs.get('refs/heads/%s' % start)
        if tip is None or tip == ZERO_OID:
            return []
        out = []
        seen = set()
        queue = [tip]
        while queue:
            sha = queue.pop(0)
            if sha in seen or sha == ZERO_OID:
                continue
            hit = self.objects.get(sha)
            if not hit or hit[0] != 'commit':
                continue
            seen.add(sha)
            c = parse_commit(hit[1])
            out.append((sha, c))
            queue.extend(c['parents'])
        return out

    # -- pull-request helpers ------------------------------------------------

    def pr_files(self, pr):
        """Diff merge-base tree -> head tree. Returns list of file dicts."""
        head_sha = pr['head']['sha']
        base_sha = pr['base']['sha']
        mb = merge_base(self.objects, head_sha, base_sha) if head_sha != ZERO_OID and base_sha != ZERO_OID else None
        base_flat = self.tree_of(mb) if mb else {}
        head_flat = self.tree_of(head_sha) if head_sha != ZERO_OID else {}
        files = []
        for path in sorted(set(base_flat) | set(head_flat)):
            old = base_flat.get(path)
            new = head_flat.get(path)
            if old == new:
                continue
            if old is None:
                status = 'added'
                old_lines, new_lines = [], self._blob_lines(new[1])
            elif new is None:
                status = 'removed'
                old_lines, new_lines = self._blob_lines(old[1]), []
            else:
                status = 'modified'
                old_lines, new_lines = self._blob_lines(old[1]), self._blob_lines(new[1])
            diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=''))
            additions = sum(1 for l in diff if l.startswith('+') and not l.startswith('+++'))
            deletions = sum(1 for l in diff if l.startswith('-') and not l.startswith('---'))
            files.append({
                'filename': path, 'status': status,
                'additions': additions, 'deletions': deletions,
                'changes': additions + deletions,
            })
        return files

    def _blob_lines(self, sha):
        hit = self.objects.get(sha)
        if not hit:
            return []
        try:
            return hit[1].decode('utf-8', 'replace').splitlines()
        except Exception:
            return []

    def merge_pr(self, pr):
        """Three-way squash merge of pr['head'] into pr['base'].
        Returns (status, merge_sha_or_None): status 200 or 405."""
        head_sha = pr['head']['sha']
        base_ref = 'refs/heads/%s' % pr['base']['ref']
        base_tip = self.refs.get(base_ref)
        if not head_sha or head_sha == ZERO_OID or not base_tip:
            return 405, None
        mb = merge_base(self.objects, head_sha, base_tip)
        base_flat = self.tree_of(mb) if mb else {}
        head_flat = self.tree_of(head_sha)
        target_flat = self.tree_of(base_tip)
        merged = dict(target_flat)
        for path in sorted(set(base_flat) | set(head_flat) | set(target_flat)):
            b = base_flat.get(path)
            h = head_flat.get(path)
            t = target_flat.get(path)
            if h == b:
                continue  # head unchanged: keep target
            if t == b:
                if h is None:
                    merged.pop(path, None)
                else:
                    merged[path] = h
                continue
            if h == t:
                continue
            return 405, None  # both sides changed differently
        tree_sha = self.build_tree_from_flat(merged)
        msg = ('Merge pull request #%d from %s\n\n%s' % (
            pr['number'], pr['head']['ref'], pr['title'])).encode('utf-8')
        merge_sha = self.store_object(
            'commit', build_commit(tree_sha, [base_tip], msg))
        self.refs[base_ref] = merge_sha
        self.version += 1
        self.pack_cache.clear()
        return 200, merge_sha


class Store:
    def __init__(self):
        self.repos = {}            # (owner.lower, name.lower) -> Repo
        self.repo_id_counter = 1

    def viewer(self):
        return VIEWER_LOGIN

    def get_repo(self, owner, name):
        if not isinstance(owner, str) or not isinstance(name, str):
            return None
        if owner.lower() != VIEWER_LOGIN.lower():
            return None
        return self.repos.get((owner.lower(), name.lower()))

    def create_repo(self, owner, name, private=True, description=''):
        key = (owner.lower(), name.lower())
        if key in self.repos:
            return None
        repo = Repo(self.repo_id_counter, owner, name, private, description)
        self.repo_id_counter += 1
        self.repos[key] = repo
        return repo

    def delete_repo(self, owner, name):
        repo = self.get_repo(owner, name)
        if repo is None:
            return False
        del self.repos[(owner.lower(), name.lower())]
        return True

    def list_user_repos(self, owner):
        if owner.lower() != VIEWER_LOGIN.lower():
            return []
        return [r for (o, _n), r in sorted(self.repos.items()) if o == owner.lower()]

    # -- issues ---------------------------------------------------------------

    def create_issue(self, repo, title, body, labels):
        num = repo.issue_counter
        repo.issue_counter += 1
        seq = repo.next_seq()
        issue = {
            'id': num, 'number': num, 'title': title or '', 'body': body or '',
            'state': 'open',
            'labels': [{'name': l} for l in labels],
            'comments': [], '_seq': seq,
            'created_at': _ts(seq), 'updated_at': _ts(seq),
            'node_id': 'I_%d_%d' % (repo.id, num),
        }
        repo.issues[num] = issue
        return issue

    def create_pull(self, repo, title, body, head, base):
        head_sha = repo.branch_tip(head) or ZERO_OID
        base_sha = repo.branch_tip(base) or ZERO_OID
        num = repo.pr_counter
        repo.pr_counter += 1
        seq = repo.next_seq()
        pr = {
            'id': num, 'number': num, 'title': title or '', 'body': body or '',
            'state': 'open', 'merged': False, 'mergeable': True,
            'head': {'ref': head, 'sha': head_sha},
            'base': {'ref': base, 'sha': base_sha},
            'merged_at': None, 'merge_commit_sha': None,
            '_seq': seq, 'created_at': _ts(seq),
            'node_id': 'PR_%d_%d' % (repo.id, num),
        }
        repo.pulls[num] = pr
        return pr


def public_repo(repo):
    return {
        'id': repo.id, 'databaseId': repo.id, 'node_id': 'R_%d' % repo.id,
        'owner': {'login': repo.owner},
        'name': repo.name, 'full_name': repo.full_name,
        'nameWithOwner': repo.full_name,
        'private': repo.private, 'description': repo.description,
        'default_branch': repo.default_branch,
        'hasIssuesEnabled': True, 'viewerPermission': 'ADMIN',
        'created_at': repo.created_at,
    }


def public_issue(issue):
    return {
        'id': issue['id'], 'number': issue['number'],
        'title': issue['title'], 'body': issue['body'],
        'state': issue['state'],
        'labels': list(issue.get('labels', [])),
        'comments': list(issue.get('comments', [])),
        'created_at': issue.get('created_at'),
        'updated_at': issue.get('updated_at'),
        'node_id': issue.get('node_id'),
    }


def public_pull(pr):
    out = {
        'id': pr['id'], 'number': pr['number'],
        'title': pr['title'], 'body': pr['body'],
        'state': pr['state'], 'merged': pr.get('merged', False),
        'head': dict(pr['head']), 'base': dict(pr['base']),
        'created_at': pr.get('created_at'),
        'node_id': pr.get('node_id'),
    }
    if pr.get('merged_at'):
        out['merged_at'] = pr['merged_at']
    if pr.get('merge_commit_sha'):
        out['merge_commit_sha'] = pr['merge_commit_sha']
    return out


def b64encode(data):
    return base64.b64encode(data).decode('ascii')
