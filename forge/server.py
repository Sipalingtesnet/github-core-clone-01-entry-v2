"""HTTP server: git smart HTTP, REST v3, GraphQL v4, MCP. One process,
stdlib only, HTTP/1.1 keep-alive, threaded.
"""
import base64
import json
import os
import re
import traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

from forge.store import Store, public_repo, public_issue, public_pull
from forge.scenario import VIEWER_LOGIN
from forge.gitproto import (
    ZERO_OID, pkt_line, pkt_flush, pkt_delim, pkt_sideband,
    sideband_stream, parse_pkt_lines, parse_pkt_text_lines,
    parse_packfile, build_pack, reachable_objects, parse_commit,
    parse_tree, parse_tag, flatten_tree,
)
from forge.graphql import execute_graphql
from forge.mcp import handle_mcp_rpc

ZERO = ZERO_OID


class ForgeHTTPRequestHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    store = Store()
    viewer_login = VIEWER_LOGIN

    def setup(self):
        super().setup()
        try:
            import socket as _socket
            self.request.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
        except Exception:
            pass
        self._body_cache = None

    def log_message(self, *args):
        pass

    # ------------------------------------------------------------------ IO

    def send_json(self, status, data, extra_headers=None):
        try:
            body = (json.dumps(data) + '\n').encode('utf-8')
        except Exception:
            body = b'{"message": "serialization error"}\n'
            status = 500
        self.send_response(status)
        self.send_header('content-type', 'application/json; charset=utf-8')
        self.send_header('content-length', str(len(body)))
        self.send_header('x-github-request-id', 'req-1')
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def send_bytes(self, status, body, content_type, extra_headers=None):
        self.send_response(status)
        self.send_header('content-type', content_type)
        self.send_header('content-length', str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def send_no_content(self, status):
        self.send_response(status)
        self.send_header('content-length', '0')
        self.end_headers()

    def check_auth(self):
        auth = self.headers.get('authorization', '')
        if not auth.startswith('Bearer '):
            return False
        return len(auth[7:].strip()) > 0

    def read_body(self):
        # Cached per request: a second read must not block on the
        # keep-alive stream waiting for the *next* request's bytes.
        if getattr(self, '_body_cache', None) is not None:
            return self._body_cache
        try:
            length = int(self.headers.get('content-length', 0) or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._body_cache = b''
            return b''
        self._body_cache = self.rfile.read(length)
        return self._body_cache

    def read_json(self):
        raw = self.read_body()
        if not raw:
            return {}, None
        try:
            return json.loads(raw.decode('utf-8')), None
        except Exception:
            return None, 'Malformed JSON'

    # ------------------------------------------------------------------ git

    def _git_repo(self, owner, repo_name):
        if owner.lower() != self.viewer_login.lower():
            return None
        return self.store.get_repo(owner, repo_name)

    def _peeled(self, repo, sha):
        """Resolve a tag object chain to its commit sha (or None)."""
        seen = set()
        cur = sha
        while cur and cur not in seen:
            seen.add(cur)
            hit = repo.objects.get(cur)
            if not hit:
                return None
            if hit[0] == 'commit':
                return cur
            if hit[0] == 'tag':
                cur = parse_tag(hit[1])['object']
            else:
                return None
        return None

    def _ref_lines_v0(self, repo):
        """(first-line caps, ref lines) for a v0 advertisement."""
        caps = 'side-band-64k ofs-delta shallow'
        head_ref = repo.head_ref()
        head_sha = repo.refs.get(head_ref)
        lines = []
        if not repo.refs:
            lines.append((ZERO, 'capabilities^{}', caps))
            return caps, lines
        first = True
        for name in sorted(repo.refs):
            sha = repo.refs[name]
            c = caps
            if first:
                c += ' symref=HEAD:%s' % head_ref
                first = False
            lines.append((sha, name, c))
            peeled = self._peeled(repo, sha)
            if name.startswith('refs/tags/') and peeled and peeled != sha:
                lines.append((peeled, name + '^{}', None))
        return caps, lines

    def _handle_info_refs(self, path, query):
        parts = path.strip('/').split('/')
        # /{owner}/{repo}.git/info/refs
        if len(parts) != 4 or parts[2] != 'info' or parts[3] != 'refs':
            self.send_json(404, {'message': 'Not Found'})
            return
        owner = unquote(parts[0])
        repo_name = parts[1][:-4] if parts[1].endswith('.git') else parts[1]
        service = query.get('service', [''])[0]
        repo = self._git_repo(owner, repo_name)
        if repo is None:
            self.send_json(404, {'message': 'Repository not found'})
            return
        is_v2 = (self.headers.get('git-protocol', '') or '').strip() == 'version=2'
        if service == 'git-upload-pack' and is_v2:
            body = bytearray()
            body += pkt_line(b'# service=git-upload-pack\n')
            body += pkt_flush()
            body += pkt_line(b'version 2\n')
            body += pkt_line(b'agent=git/makefun\n')
            body += pkt_line(b'ls-refs=unborn\n')
            body += pkt_line(b'fetch=shallow\n')
            body += pkt_line(b'side-band-64k\n')
            body += pkt_line(b'object-format=sha1\n')
            body += pkt_flush()
            self.send_bytes(200, bytes(body),
                            'application/x-git-upload-pack-advertisement')
            return
        if service == 'git-upload-pack':
            body = bytearray()
            body += pkt_line(b'# service=git-upload-pack\n')
            body += pkt_flush()
            caps, lines = self._ref_lines_v0(repo)
            for i, (sha, name, c) in enumerate(lines):
                if i == 0 and c:
                    body += pkt_line(('%s %s\0%s\n' % (sha, name, c)).encode('ascii'))
                else:
                    body += pkt_line(('%s %s\n' % (sha, name)).encode('ascii'))
            body += pkt_flush()
            self.send_bytes(200, bytes(body),
                            'application/x-git-upload-pack-advertisement')
            return
        if service == 'git-receive-pack':
            body = bytearray()
            body += pkt_line(b'# service=git-receive-pack\n')
            body += pkt_flush()
            caps = 'report-status side-band-64k delete-refs object-format=sha1'
            head_ref = repo.head_ref()
            if not repo.refs:
                body += pkt_line(
                    ('%s capabilities^{}\0%s symref=HEAD:%s\n' % (ZERO, caps, head_ref)).encode('ascii'))
            else:
                first = True
                for name in sorted(repo.refs):
                    sha = repo.refs[name]
                    c = caps
                    if first:
                        c += ' symref=HEAD:%s' % head_ref
                        first = False
                    body += pkt_line(('%s %s\0%s\n' % (sha, name, c)).encode('ascii'))
            body += pkt_flush()
            self.send_bytes(200, bytes(body),
                            'application/x-git-receive-pack-advertisement')
            return
        self.send_json(404, {'message': 'Not Found'})

    def _ls_refs_lines(self, repo, prefixes, want_peel, want_symrefs, want_unborn):
        """Lines for a v2 ls-refs response (already server-filtered)."""
        head_ref = repo.head_ref()
        head_sha = repo.refs.get(head_ref)
        out = []

        def keep(name):
            return not prefixes or any(name.startswith(p) for p in prefixes)

        if not repo.refs:
            if want_unborn and keep('HEAD'):
                out.append('unborn HEAD symref-target:%s\n' % head_ref)
            return out
        if keep('HEAD') and head_sha:
            line = '%s HEAD' % head_sha
            if want_symrefs:
                line += ' symref-target:%s' % head_ref
            out.append(line + '\n')
        for name in sorted(repo.refs):
            if not keep(name):
                continue
            sha = repo.refs[name]
            line = '%s %s' % (sha, name)
            if want_peel:
                peeled = self._peeled(repo, sha)
                if name.startswith('refs/tags/') and peeled and peeled != sha:
                    line += ' peeled:%s' % peeled
            out.append(line + '\n')
        return out

    def _fetch_pack(self, repo, wants, haves, depth, shallow_in):
        """Return (pack_bytes, shallow_list, unshallow_list). Cached per
        (repo version, wants, haves, depth, shallow_in)."""
        key = (repo.version, tuple(sorted(wants)), tuple(sorted(haves)),
               depth, tuple(sorted(shallow_in)))
        cached = repo.pack_cache.get(key)
        if cached is not None:
            return cached
        # Serialize pack builds for one repo: 50 concurrent clones must
        # not each rebuild the same pack under the GIL.
        with repo.pack_lock:
            cached = repo.pack_cache.get(key)
            if cached is not None:
                return cached
            result = self._build_fetch_pack(repo, wants, haves, depth, shallow_in)
            repo.pack_cache[key] = result
            return result

    def _build_fetch_pack(self, repo, wants, haves, depth, shallow_in):
        from forge.gitproto import shallow_have_reach
        have_reach = set()
        if haves:
            have_reach = shallow_have_reach(repo.objects, haves, shallow_in)
        included, boundary = reachable_objects(repo.objects, wants, depth=depth)
        included -= have_reach
        shallow = sorted(boundary)
        unshallow = sorted(
            b for b in shallow_in
            if b in reachable_objects(repo.objects, wants, depth=depth)[0]
            and b not in boundary)
        items = sorted(included)
        pack = build_pack([(sha, repo.objects[sha][0], repo.objects[sha][1])
                           for sha in items if sha in repo.objects])
        return (pack, shallow, unshallow)

    def _handle_upload_pack(self, path, raw_body):
        parts = path.strip('/').split('/')
        if len(parts) != 3:
            self.send_json(404, {'message': 'Not Found'})
            return
        owner = unquote(parts[0])
        repo_name = parts[1][:-4] if parts[1].endswith('.git') else parts[1]
        repo = self._git_repo(owner, repo_name)
        if repo is None:
            self.send_json(404, {'message': 'Repository not found'})
            return
        is_v2 = (self.headers.get('git-protocol', '') or '').strip() == 'version=2'
        lines = parse_pkt_text_lines(raw_body)
        command = None
        for ln in lines:
            if ln.startswith('command='):
                command = ln.split('=', 1)[1].strip()
                break
        if is_v2 and command == 'ls-refs':
            prefixes = []
            want_peel = False
            want_symrefs = False
            want_unborn = False
            for ln in lines:
                s = ln.strip()
                if s == 'peel':
                    want_peel = True
                elif s == 'symrefs':
                    want_symrefs = True
                elif s == 'unborn':
                    want_unborn = True
                elif s.startswith('ref-prefix '):
                    prefixes.append(s[len('ref-prefix '):].strip())
            out = self._ls_refs_lines(repo, prefixes, want_peel,
                                      want_symrefs, want_unborn)
            body = bytearray()
            for ln in out:
                body += pkt_line(ln.encode('ascii'))
            body += pkt_flush()
            self.send_bytes(200, bytes(body),
                            'application/x-git-upload-pack-result')
            return
        # fetch (v2 command=fetch, or v0 want/have lines)
        wants, haves, shallow_in, depth = [], [], [], None
        for ln in lines:
            s = ln.strip()
            if s.startswith('want '):
                wants.append(s[5:].split()[0])
            elif s.startswith('have '):
                haves.append(s[5:].split()[0])
            elif s.startswith('shallow '):
                shallow_in.append(s[8:].split()[0])
            elif s.startswith('deepen '):
                try:
                    depth = int(s[7:].split()[0])
                except ValueError:
                    pass
        if is_v2 and command not in (None, 'fetch'):
            self.send_bytes(400, b'', 'application/x-git-upload-pack-result')
            return
        if not wants:
            # No wants (e.g. an empty clone): v2 answers an empty fetch
            # result; v0 likewise.
            if is_v2:
                body = pkt_line(b'packfile\n') + sideband_stream(1, build_pack([])) + pkt_flush()
            else:
                body = sideband_stream(1, build_pack([])) + pkt_flush()
            self.send_bytes(200, body, 'application/x-git-upload-pack-result')
            return
        pack, shallow, unshallow = self._fetch_pack(repo, wants, haves,
                                                    depth, shallow_in)
        if is_v2:
            body = bytearray()
            if haves:
                body += pkt_line(b'acknowledgments\n')
                body += pkt_line(b'NAK\n')
                body += pkt_delim()
            if depth is not None and (shallow or unshallow):
                body += pkt_line(b'shallow-info\n')
                for oid in shallow:
                    body += pkt_line(('shallow %s\n' % oid).encode('ascii'))
                for oid in unshallow:
                    body += pkt_line(('unshallow %s\n' % oid).encode('ascii'))
                body += pkt_delim()
            body += pkt_line(b'packfile\n')
            body += sideband_stream(1, pack)
            body += pkt_flush()
            self.send_bytes(200, bytes(body),
                            'application/x-git-upload-pack-result')
            return
        body = bytearray()
        if haves:
            body += pkt_line(b'NAK\n')
        if depth is not None:
            for oid in shallow:
                body += pkt_line(('shallow %s\n' % oid).encode('ascii'))
            for oid in unshallow:
                body += pkt_line(('unshallow %s\n' % oid).encode('ascii'))
        body += sideband_stream(1, pack)
        body += pkt_flush()
        self.send_bytes(200, bytes(body),
                        'application/x-git-upload-pack-result')

    def _handle_receive_pack(self, path, raw_body):
        parts = path.strip('/').split('/')
        if len(parts) != 3:
            self.send_json(404, {'message': 'Not Found'})
            return
        owner = unquote(parts[0])
        repo_name = parts[1][:-4] if parts[1].endswith('.git') else parts[1]
        repo = self._git_repo(owner, repo_name)
        if repo is None:
            self.send_json(404, {'message': 'Repository not found'})
            return
        packets, consumed = parse_pkt_lines(raw_body)
        pack_data = raw_body[consumed:] if consumed < len(raw_body) else b''
        commands = []
        for pkt in packets:
            if pkt in (b'0000', b'0001', b'0002'):
                continue
            line = pkt.decode('utf-8', errors='replace')
            if '\0' in line:
                line = line.split('\0')[0]
            line = line.strip()
            if not line:
                continue
            bits = line.split()
            if len(bits) >= 3:
                commands.append((bits[0], bits[1], bits[2]))
        # Unpack (a delete-only push carries no pack at all).
        if pack_data:
            ok, detail, resolved = parse_packfile(pack_data, repo.objects)
        else:
            ok, detail, resolved = True, 'unpack ok', []
        status_lines = []
        if ok:
            for sha, typ, data in resolved:
                if sha not in repo.objects:
                    repo.objects[sha] = (typ, data)
            status_lines.append(b'unpack ok\n')
            for old_oid, new_oid, ref_name in commands:
                ok_ref, reason = self._apply_ref_update(repo, old_oid, new_oid, ref_name)
                if ok_ref:
                    status_lines.append(('ok %s\n' % ref_name).encode('ascii'))
                else:
                    status_lines.append(('ng %s %s\n' % (ref_name, reason)).encode('ascii'))
            repo.version += 1
            repo.pack_cache.clear()
        else:
            status_lines.append(('unpack %s\n' % detail).encode('ascii'))
        resp = bytearray()
        resp += pkt_sideband(2, b'Unpacking objects: %d, done.\n' % len(resolved))
        payload = b''.join(status_lines)
        # Band-1 report is itself pkt-lined text; chunk it if large.
        for i in range(0, len(payload), 60000):
            resp += pkt_sideband(1, payload[i:i + 60000])
        resp += pkt_flush()
        self.send_bytes(200, bytes(resp),
                        'application/x-git-receive-pack-result')

    def _apply_ref_update(self, repo, old_oid, new_oid, ref_name):
        current = repo.refs.get(ref_name)
        if new_oid == ZERO:
            if current is None:
                return False, 'remote ref does not exist'
            if old_oid != ZERO and old_oid != current:
                return False, 'non-fast-forward'
            del repo.refs[ref_name]
            return True, ''
        # create or update
        if current is None:
            if old_oid != ZERO:
                return False, 'non-fast-forward'
            if new_oid not in repo.objects:
                return False, 'object not found'
            repo.refs[ref_name] = new_oid
            return True, ''
        if old_oid != current:
            return False, 'non-fast-forward'
        if new_oid == current:
            return True, ''
        if new_oid not in repo.objects:
            return False, 'object not found'
        hit = repo.objects[new_oid]
        if ref_name.startswith('refs/heads/'):
            if hit[0] != 'commit':
                return False, 'not a commit'
            from forge.gitproto import is_ancestor
            if not is_ancestor(repo.objects, current, new_oid):
                return False, 'non-fast-forward'
        repo.refs[ref_name] = new_oid
        return True, ''

    # ------------------------------------------------------------------ REST

    def _repo_route(self, parts):
        """parts after /api/v3/repos/: return (repo, rest_parts) or (None, err)."""
        owner = unquote(parts[0])
        name = unquote(parts[1])
        repo = self.store.get_repo(owner, name)
        return repo, parts[2:]

    def _sorted_issues(self, repo, state_filter, sort, direction):
        items = list(repo.issues.values())
        if state_filter != 'all':
            items = [i for i in items if i['state'] == state_filter]
        if sort == 'created':
            items.sort(key=lambda i: i['_seq'], reverse=(direction == 'desc'))
        else:
            items.sort(key=lambda i: i['number'])
        return items

    def do_GET(self):
        self._body_cache = None
        try:
            self.read_body()
        except Exception:
            pass
        try:
            self._get()
        except Exception as e:
            traceback.print_exc()
            try:
                self.send_json(500, {'message': 'Internal Server Error: %s' % e})
            except Exception:
                pass

    def _get(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == '/healthz':
            self.send_json(200, {'ok': True})
            return

        if path.endswith('/info/refs'):
            self._handle_info_refs(path, query)
            return

        if path in ('/api/v3', '/api/v3/'):
            self.send_json(200, {'current_user_url': '/api/v3/user',
                                 'rate_limit_url': '/api/v3/rate_limit'},
                           {'x-oauth-scopes': 'repo, read:org'})
            return
        if path == '/api/v3/meta':
            self.send_json(200, {'installed_version': '1.0.0'})
            return
        if path == '/api/v3/user':
            if not self.check_auth():
                self.send_json(401, {'message': 'Requires authentication'})
                return
            self.send_json(200, {'login': self.viewer_login})
            return
        if path == '/api/v3/user/repos':
            if not self.check_auth():
                self.send_json(401, {'message': 'Requires authentication'})
                return
            repos = self.store.list_user_repos(self.viewer_login)
            per_page = int(query.get('per_page', ['30'])[0])
            page = int(query.get('page', ['1'])[0])
            start = (page - 1) * per_page
            end = start + per_page
            extra = {}
            if len(repos) > end:
                host = self.headers.get('host', '127.0.0.1')
                extra['link'] = ('<http://%s/api/v3/user/repos?per_page=%d&page=%d>; rel="next"'
                                 % (host, per_page, page + 1))
            self.send_json(200, [public_repo(r) for r in repos[start:end]], extra)
            return

        if path.startswith('/api/v3/repos/'):
            if not self.check_auth():
                self.send_json(401, {'message': 'Requires authentication'})
                return
            parts = [unquote(p) for p in path.strip('/').split('/')]
            if len(parts) < 5:
                self.send_json(404, {'message': 'Not Found'})
                return
            repo, rest = self._repo_route(parts[3:])
            if repo is None:
                self.send_json(404, {'message': 'Repository not found'})
                return
            if not rest:
                self.send_json(200, public_repo(repo))
                return
            sub = rest[0]
            if sub == 'branches':
                if len(rest) == 1:
                    out = [{'name': name[11:],
                            'commit': {'sha': sha}}
                           for name, sha in sorted(repo.refs.items())
                           if name.startswith('refs/heads/')]
                    self.send_json(200, out)
                    return
                if len(rest) == 2:
                    sha = repo.branch_tip(rest[1])
                    if sha is None:
                        self.send_json(404, {'message': 'Not Found'})
                        return
                    self.send_json(200, {'name': rest[1],
                                         'commit': {'sha': sha}})
                    return
            if sub == 'commits':
                start = query.get('sha', [repo.default_branch])[0]
                commits = repo.commits_from(start)
                out = []
                for sha, c in commits:
                    msg = c['message'].decode('utf-8', 'replace')
                    out.append({'sha': sha,
                                'commit': {'message': msg}})
                self.send_json(200, out)
                return
            if sub == 'contents':
                self._get_contents(repo, rest[1:], query)
                return
            if sub == 'git' and len(rest) >= 3 and rest[1] == 'refs':
                ref = '/'.join(rest[2:])
                sha = repo.refs.get(ref)
                if sha is None:
                    self.send_json(404, {'message': 'Not Found'})
                    return
                self.send_json(200, {'ref': ref, 'node_id': 'REF_%d' % repo.id,
                                     'object': {'type': 'commit', 'sha': sha}})
                return
            if sub == 'issues':
                if len(rest) == 1:
                    state_filter = query.get('state', ['open'])[0]
                    sort = query.get('sort', ['created'])[0]
                    direction = query.get('direction', ['desc'])[0]
                    items = self._sorted_issues(repo, state_filter, sort, direction)
                    per_page = int(query.get('per_page', ['30'])[0])
                    page = int(query.get('page', ['1'])[0])
                    items = items[(page - 1) * per_page:page * per_page]
                    self.send_json(200, [public_issue(i) for i in items])
                    return
                if len(rest) == 2:
                    try:
                        num = int(rest[1])
                    except ValueError:
                        self.send_json(404, {'message': 'Issue not found'})
                        return
                    iss = repo.issues.get(num)
                    if iss is None:
                        self.send_json(404, {'message': 'Issue not found'})
                        return
                    self.send_json(200, public_issue(iss))
                    return
                if len(rest) == 3 and rest[2] == 'comments':
                    try:
                        num = int(rest[1])
                    except ValueError:
                        self.send_json(404, {'message': 'Issue not found'})
                        return
                    iss = repo.issues.get(num)
                    if iss is None:
                        self.send_json(404, {'message': 'Issue not found'})
                        return
                    self.send_json(200, list(iss.get('comments', [])))
                    return
            if sub == 'pulls':
                if len(rest) == 1:
                    state_filter = query.get('state', ['open'])[0]
                    items = list(repo.pulls.values())
                    if state_filter != 'all':
                        items = [p for p in items if p['state'] == state_filter]
                    items.sort(key=lambda p: p['number'])
                    per_page = int(query.get('per_page', ['30'])[0])
                    page = int(query.get('page', ['1'])[0])
                    items = items[(page - 1) * per_page:page * per_page]
                    self.send_json(200, [public_pull(p) for p in items])
                    return
                if len(rest) == 2:
                    try:
                        num = int(rest[1])
                    except ValueError:
                        self.send_json(404, {'message': 'Pull request not found'})
                        return
                    pr = repo.pulls.get(num)
                    if pr is None:
                        self.send_json(404, {'message': 'Pull request not found'})
                        return
                    self.send_json(200, public_pull(pr))
                    return
                if len(rest) == 3 and rest[2] == 'files':
                    try:
                        num = int(rest[1])
                    except ValueError:
                        self.send_json(404, {'message': 'Pull request not found'})
                        return
                    pr = repo.pulls.get(num)
                    if pr is None:
                        self.send_json(404, {'message': 'Pull request not found'})
                        return
                    self.send_json(200, repo.pr_files(pr))
                    return
        self.send_json(404, {'message': 'Not Found'})

    def _get_contents(self, repo, rest, query):
        file_path = '/'.join(rest)
        branch = query.get('ref', [query.get('branch', [repo.default_branch])[0]])[0]
        tip = repo.branch_tip(branch)
        flat = repo.tree_of(tip) if tip else {}
        if not file_path:
            listing = []
            for fp in sorted(flat):
                mode, sha = flat[fp]
                listing.append({'type': 'file', 'name': fp.split('/')[-1],
                                'path': fp, 'sha': sha,
                                'size': len(repo.objects[sha][1]) if sha in repo.objects else 0})
            self.send_json(200, listing)
            return
        if file_path in flat:
            mode, sha = flat[file_path]
            data = repo.objects[sha][1]
            self.send_json(200, {
                'type': 'file', 'name': file_path.split('/')[-1],
                'path': file_path, 'encoding': 'base64',
                'content': base64.b64encode(data).decode('ascii'),
                'sha': sha, 'size': len(data)})
            return
        prefix = file_path + '/'
        children = {}
        for fp in sorted(flat):
            if fp.startswith(prefix):
                rel = fp[len(prefix):]
                first = rel.split('/')[0]
                if first not in children:
                    mode, sha = flat[fp]
                    children[first] = {
                        'type': 'dir' if '/' in rel else 'file',
                        'name': first, 'path': file_path + '/' + first,
                        'sha': sha, 'size': 0}
        if children:
            self.send_json(200, list(children.values()))
            return
        self.send_json(404, {'message': 'Not Found'})

    def do_POST(self):
        self._body_cache = None
        try:
            self.read_body()
        except Exception:
            pass
        try:
            self._post()
        except Exception as e:
            traceback.print_exc()
            try:
                self.send_json(500, {'message': 'Internal Server Error: %s' % e})
            except Exception:
                pass

    def _post(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.endswith('/git-upload-pack'):
            try:
                raw = self.read_body()
            except Exception:
                raw = b''
            self._handle_upload_pack(path, raw)
            return
        if path.endswith('/git-receive-pack'):
            try:
                raw = self.read_body()
            except Exception:
                raw = b''
            self._handle_receive_pack(path, raw)
            return

        if path == '/api/graphql':
            if not self.check_auth():
                self.send_json(401, {'message': 'Requires authentication'})
                return
            body, err = self.read_json()
            if err:
                self.send_json(400, {'message': 'Malformed JSON'})
                return
            if isinstance(body, list):
                results = [execute_graphql(q.get('query', ''), q.get('variables'),
                                           self.viewer_login, self.store)
                           for q in body if isinstance(q, dict)]
                self.send_json(200, results)
                return
            if not isinstance(body, dict):
                self.send_json(400, {'message': 'Malformed JSON'})
                return
            res = execute_graphql(body.get('query', ''), body.get('variables'),
                                  self.viewer_login, self.store)
            self.send_json(200, res)
            return

        if path == '/mcp':
            if not self.check_auth():
                self.send_json(401, {'message': 'Requires authentication'})
                return
            raw = self.read_body()
            try:
                body = json.loads(raw.decode('utf-8')) if raw else {}
            except Exception:
                self.send_json(400, {'jsonrpc': '2.0', 'id': None,
                                     'error': {'code': -32700, 'message': 'Parse error'}})
                return
            if isinstance(body, list):
                results = [handle_mcp_rpc(item, self.viewer_login, self.store)
                           for item in body if isinstance(item, dict)]
                self.send_json(200, results)
                return
            res = handle_mcp_rpc(body, self.viewer_login, self.store)
            if res == {}:
                self.send_json(200, {})
                return
            self.send_json(200, res)
            return

        if path == '/api/v3/user/repos':
            if not self.check_auth():
                self.send_json(401, {'message': 'Requires authentication'})
                return
            body, err = self.read_json()
            if err:
                self.send_json(400, {'message': 'Malformed JSON'})
                return
            name = body.get('name', 'repo')
            repo = self.store.create_repo(self.viewer_login, name,
                                          body.get('private', True),
                                          body.get('description', ''))
            if repo is None:
                self.send_json(422, {'message': 'Repository already exists'})
                return
            self.send_json(201, public_repo(repo))
            return

        parts = [unquote(p) for p in path.strip('/').split('/')]
        if len(parts) >= 6 and parts[0] == 'api' and parts[1] == 'v3' and parts[2] == 'repos':
            if not self.check_auth():
                self.send_json(401, {'message': 'Requires authentication'})
                return
            repo, rest = self._repo_route(parts[3:])
            if repo is None:
                self.send_json(404, {'message': 'Repository not found'})
                return
            body, err = self.read_json()
            if err:
                self.send_json(400, {'message': 'Malformed JSON'})
                return
            sub = rest[0] if rest else ''
            if sub == 'labels':
                label_name = body.get('name', 'bug')
                color = body.get('color', 'ededed')
                repo.labels[label_name] = {'name': label_name, 'color': color}
                self.send_json(201, {'name': label_name, 'color': color})
                return
            if sub == 'issues' and len(rest) == 3 and rest[2] == 'comments':
                try:
                    num = int(rest[1])
                except ValueError:
                    self.send_json(404, {'message': 'Issue not found'})
                    return
                iss = repo.issues.get(num)
                if iss is None:
                    self.send_json(404, {'message': 'Issue not found'})
                    return
                cid = repo.comment_counter
                repo.comment_counter += 1
                comment = {'id': cid, 'body': body.get('body', '')}
                iss.setdefault('comments', []).append(comment)
                self.send_json(201, comment)
                return
            if sub == 'issues' and len(rest) == 1:
                labels = body.get('labels', []) or []
                for lbl in labels:
                    if lbl not in repo.labels:
                        self.send_json(422, {'message': 'Validation Failed: unknown label'})
                        return
                issue = self.store.create_issue(repo, body.get('title', ''),
                                                body.get('body', ''), labels)
                self.send_json(201, public_issue(issue))
                return
            if sub == 'pulls' and len(rest) == 1:
                head = body.get('head', 'feature')
                base = body.get('base', repo.default_branch)
                for pr in repo.pulls.values():
                    if pr['state'] == 'open' and pr['head']['ref'] == head and pr['base']['ref'] == base:
                        self.send_json(422, {'message': 'A pull request already exists'})
                        return
                pr = self.store.create_pull(repo, body.get('title', 'PR'),
                                            body.get('body', ''), head, base)
                self.send_json(201, public_pull(pr))
                return
            if sub == 'git' and len(rest) == 2 and rest[1] == 'refs':
                ref_name = body.get('ref', 'refs/heads/main')
                sha = body.get('sha', ZERO)
                if ref_name in repo.refs:
                    self.send_json(422, {'message': 'Reference already exists'})
                    return
                if sha not in repo.objects and sha != ZERO:
                    self.send_json(422, {'message': 'Object does not exist'})
                    return
                repo.refs[ref_name] = sha
                repo.version += 1
                repo.pack_cache.clear()
                self.send_json(201, {'ref': ref_name, 'node_id': 'REF_%d' % repo.id,
                                     'object': {'type': 'commit', 'sha': sha}})
                return
        self.send_json(404, {'message': 'Not Found'})

    def do_PATCH(self):
        self._body_cache = None
        try:
            self.read_body()
        except Exception:
            pass
        try:
            self._patch()
        except Exception as e:
            traceback.print_exc()
            try:
                self.send_json(500, {'message': 'Internal Server Error: %s' % e})
            except Exception:
                pass

    def _patch(self):
        parsed = urlparse(self.path)
        path = parsed.path
        parts = [unquote(p) for p in path.strip('/').split('/')]
        if len(parts) >= 5 and parts[0] == 'api' and parts[1] == 'v3' and parts[2] == 'repos':
            if not self.check_auth():
                self.send_json(401, {'message': 'Requires authentication'})
                return
            repo, rest = self._repo_route(parts[3:])
            if repo is None:
                self.send_json(404, {'message': 'Repository not found'})
                return
            body, err = self.read_json()
            if err:
                self.send_json(400, {'message': 'Malformed JSON'})
                return
            if not rest:
                if 'description' in body:
                    repo.description = body['description']
                self.send_json(200, public_repo(repo))
                return
            if rest[0] == 'issues' and len(rest) == 2:
                try:
                    num = int(rest[1])
                except ValueError:
                    self.send_json(404, {'message': 'Issue not found'})
                    return
                iss = repo.issues.get(num)
                if iss is None:
                    self.send_json(404, {'message': 'Issue not found'})
                    return
                if 'state' in body:
                    iss['state'] = body['state']
                if 'title' in body:
                    iss['title'] = body['title']
                if 'body' in body:
                    iss['body'] = body['body']
                self.send_json(200, public_issue(iss))
                return
            if rest[0] == 'pulls' and len(rest) == 2:
                try:
                    num = int(rest[1])
                except ValueError:
                    self.send_json(404, {'message': 'Pull request not found'})
                    return
                pr = repo.pulls.get(num)
                if pr is None:
                    self.send_json(404, {'message': 'Pull request not found'})
                    return
                if 'state' in body:
                    pr['state'] = body['state']
                self.send_json(200, public_pull(pr))
                return
        self.send_json(404, {'message': 'Not Found'})

    def do_PUT(self):
        self._body_cache = None
        try:
            self.read_body()
        except Exception:
            pass
        try:
            self._put()
        except Exception as e:
            traceback.print_exc()
            try:
                self.send_json(500, {'message': 'Internal Server Error: %s' % e})
            except Exception:
                pass

    def _put(self):
        parsed = urlparse(self.path)
        path = parsed.path
        parts = [unquote(p) for p in path.strip('/').split('/')]
        if len(parts) >= 6 and parts[0] == 'api' and parts[1] == 'v3' and parts[2] == 'repos':
            if not self.check_auth():
                self.send_json(401, {'message': 'Requires authentication'})
                return
            repo, rest = self._repo_route(parts[3:])
            if repo is None:
                self.send_json(404, {'message': 'Repository not found'})
                return
            body, err = self.read_json()
            if err:
                self.send_json(400, {'message': 'Malformed JSON'})
                return
            if rest and rest[0] == 'contents':
                file_path = '/'.join(rest[1:])
                branch = body.get('branch', repo.default_branch)
                msg = body.get('message', 'Update file')
                content_b64 = body.get('content', '')
                try:
                    content = base64.b64decode(content_b64)
                except Exception:
                    content = content_b64.encode('utf-8')
                expected = body.get('sha')
                commit_sha, blob_sha, is_update, status = repo.commit_file(
                    branch, file_path, content, msg, expected_sha=expected)
                if status == 409:
                    self.send_json(409, {'message': 'Conflict: sha does not match'})
                    return
                self.send_json(201 if not is_update else 200, {
                    'content': {'name': file_path.split('/')[-1], 'path': file_path,
                                'sha': blob_sha, 'size': len(content),
                                'encoding': 'base64', 'content': content_b64},
                    'commit': {'sha': commit_sha, 'message': msg}})
                return
            if rest and rest[0] == 'pulls' and len(rest) == 3 and rest[2] == 'merge':
                try:
                    num = int(rest[1])
                except ValueError:
                    self.send_json(404, {'message': 'Pull request not found'})
                    return
                pr = repo.pulls.get(num)
                if pr is None:
                    self.send_json(404, {'message': 'Pull request not found'})
                    return
                if pr['state'] == 'closed':
                    if pr.get('merged'):
                        self.send_json(200, {'sha': pr.get('merge_commit_sha') or ZERO,
                                             'merged': True,
                                             'message': 'Pull request successfully merged'})
                        return
                    self.send_json(405, {'message': 'Pull request is closed'})
                    return
                status, merge_sha = repo.merge_pr(pr)
                if status != 200:
                    self.send_json(405, {'message': 'Merge conflict'})
                    return
                pr['merged'] = True
                pr['state'] = 'closed'
                pr['merged_at'] = '2026-01-01T00:00:00Z'
                pr['merge_commit_sha'] = merge_sha
                self.send_json(200, {'sha': merge_sha, 'merged': True,
                                     'message': 'Pull request successfully merged'})
                return
        self.send_json(404, {'message': 'Not Found'})

    def do_DELETE(self):
        self._body_cache = None
        try:
            self.read_body()
        except Exception:
            pass
        try:
            self._delete()
        except Exception as e:
            traceback.print_exc()
            try:
                self.send_json(500, {'message': 'Internal Server Error: %s' % e})
            except Exception:
                pass

    def _delete(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == '/mcp':
            self.send_json(200, {})
            return
        parts = [unquote(p) for p in path.strip('/').split('/')]
        if len(parts) >= 5 and parts[0] == 'api' and parts[1] == 'v3' and parts[2] == 'repos':
            if not self.check_auth():
                self.send_json(401, {'message': 'Requires authentication'})
                return
            owner = parts[3]
            name = parts[4]
            repo, rest = self._repo_route(parts[3:])
            if rest and rest[0] == 'contents':
                if repo is None:
                    self.send_json(404, {'message': 'Repository not found'})
                    return
                file_path = '/'.join(rest[1:])
                body, _err = self.read_json()
                branch = (body or {}).get('branch', repo.default_branch)
                msg = (body or {}).get('message', 'Delete file')
                expected = (body or {}).get('sha')
                commit_sha, status = repo.delete_file(branch, file_path, msg, expected)
                if status == 404:
                    self.send_json(404, {'message': 'Not Found'})
                    return
                if status == 409:
                    self.send_json(409, {'message': 'Conflict: sha does not match'})
                    return
                self.send_json(200, {'commit': {'sha': commit_sha, 'message': msg}})
                return
            if repo is None:
                self.send_json(404, {'message': 'Repository not found'})
                return
            if not rest:
                if self.store.delete_repo(owner, name):
                    self.send_no_content(204)
                else:
                    self.send_json(404, {'message': 'Repository not found'})
                return
        self.send_json(404, {'message': 'Not Found'})
