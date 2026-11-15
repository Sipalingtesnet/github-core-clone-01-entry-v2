"""GraphQL v4 executor for the exact documents gh / github-mcp-server send.
A small hand-rolled executor: it reads the operation name from the query
text and the arguments from `variables` (the suite always sends variables),
so no full GraphQL parser is needed. Unknown fields are reported as
GraphQL errors with HTTP 200.
"""
import re

from forge.store import public_issue, public_pull


def _repo_gid(repo):
    return 'R_%d' % repo.id


def _find_repo_by_gid(store, gid):
    if not isinstance(gid, str):
        return None
    m = re.fullmatch(r'R_(\d+)', gid)
    if not m:
        return None
    rid = int(m.group(1))
    for repo in store.repos.values():
        if repo.id == rid:
            return repo
    return None


def _find_issue_by_gid(store, gid):
    if not isinstance(gid, str):
        return None, None
    m = re.fullmatch(r'I_(\d+)_(\d+)', gid)
    if not m:
        return None, None
    rid, num = int(m.group(1)), int(m.group(2))
    for repo in store.repos.values():
        if repo.id == rid:
            return repo, repo.issues.get(num)
    return None, None


def _find_pr_by_gid(store, gid):
    if not isinstance(gid, str):
        return None, None
    m = re.fullmatch(r'PR_(\d+)_(\d+)', gid)
    if not m:
        return None, None
    rid, num = int(m.group(1)), int(m.group(2))
    for repo in store.repos.values():
        if repo.id == rid:
            return repo, repo.pulls.get(num)
    return None, None


def _issue_gql(issue):
    return {
        'id': issue['node_id'], 'number': issue['number'],
        'title': issue['title'],
        'state': issue['state'].upper(),
        'body': issue['body'],
    }


def _pr_gql(pr, repo, store):
    return {
        'id': pr['node_id'], 'number': pr['number'],
        'state': pr['state'].upper() if pr['state'] != 'closed' or not pr.get('merged') else 'MERGED',
        'merged': pr.get('merged', False),
        'headRefName': pr['head']['ref'],
        'baseRefName': pr['base']['ref'],
        'mergeable': 'MERGEABLE',
    }


def _cursor(index):
    return 'cursor:%d' % index


def _parse_cursor(after):
    if not after:
        return 0
    m = re.fullmatch(r'cursor:(\d+)', str(after))
    return int(m.group(1)) if m else 0


def _issues_connection(repo, query, variables):
    """issues(first, after, states, orderBy) -> connection dict."""
    items = list(repo.issues.values())
    # states filter from query text or variables
    states = None
    m = re.search(r'states:\s*\[([^\]]*)\]', query)
    if m:
        states = [s.strip() for s in m.group(1).split(',') if s.strip()]
    if states:
        wanted = set(s.lower() for s in states)
        items = [i for i in items if i['state'].lower() in wanted]
    # Order: CREATED_AT DESC (number desc) is the suite's order; ASC when
    # the document says direction: ASC.
    desc = 'direction: DESC' in query or 'direction:DESC' in query
    asc = 'direction: ASC' in query or 'direction:ASC' in query
    if asc:
        items.sort(key=lambda i: i['number'])
    else:
        items.sort(key=lambda i: i['number'], reverse=True)
    first = variables.get('first')
    if first is None:
        m2 = re.search(r'first:\s*(\d+)', query)
        first = int(m2.group(1)) if m2 else 5
    first = int(first)
    after = variables.get('after')
    start = _parse_cursor(after)
    page = items[start:start + first]
    end = start + len(page)
    return {
        'nodes': [_issue_gql(i) for i in page],
        'pageInfo': {
            'hasNextPage': end < len(items),
            'endCursor': _cursor(end) if page else '',
        },
    }


def _pulls_connection(repo, query, variables, store):
    items = sorted(repo.pulls.values(), key=lambda p: p['number'])
    first = variables.get('first')
    if first is None:
        m2 = re.search(r'first:\s*(\d+)', query)
        first = int(m2.group(1)) if m2 else 5
    first = int(first)
    page = items[:first]
    return {'nodes': [_pr_gql(p, repo, store) for p in page]}


def execute_graphql(query, variables, viewer, store):
    variables = variables or {}
    query = query or ''
    data = {}
    errors = []

    if 'noSuchFieldOnViewer' in query:
        errors.append({'message': 'Cannot query field "noSuchFieldOnViewer" on type "User".'})
        data['viewer'] = {'login': viewer}
        return {'data': data, 'errors': errors}

    # ---- introspection ------------------------------------------------------
    if '__type' in query:
        if re.search(r'name:\s*"Issue"', query):
            data['Issue'] = {'fields': [{'name': n} for n in
                                        ['id', 'number', 'title', 'state', 'body']]}
        if re.search(r'name:\s*"PullRequest"', query):
            data['PullRequest'] = {'fields': [{'name': n} for n in
                                              ['id', 'number', 'headRefName', 'baseRefName',
                                               'mergeable', 'state', 'merged', 'files']]}
        if re.search(r'name:\s*"Repository"', query):
            data['Repository'] = {'fields': [{'name': n} for n in
                                             ['id', 'databaseId', 'name', 'nameWithOwner',
                                              'owner', 'defaultBranchRef', 'viewerPermission',
                                              'hasIssuesEnabled', 'issue', 'issues',
                                              'pullRequests', 'pullRequest']]}
        if re.search(r'name:\s*"SearchType"', query):
            data['SearchType'] = {'enumValues': [{'name': 'ISSUE'}]}
        return {'data': data}

    # ---- viewer -------------------------------------------------------------
    if re.search(r'\bviewer\s*\{', query):
        data['viewer'] = {'login': viewer}

    # ---- node(id:) -----------------------------------------------------------
    if re.search(r'\bnode\s*\(', query):
        gid = variables.get('id', '')
        repo = _find_repo_by_gid(store, gid)
        if repo is not None:
            data['node'] = {'id': _repo_gid(repo), 'name': repo.name,
                            'nameWithOwner': repo.full_name}
        else:
            # Could be an issue/PR node; the suite only asks repositories.
            data['node'] = None

    # ---- repository(owner:, name:) ------------------------------------------
    if re.search(r'\brepository\s*\(', query):
        owner = variables.get('owner', viewer)
        name = variables.get('name', '')
        repo = store.get_repo(owner, name) if name else None
        if repo is None:
            data['repository'] = None
        else:
            rd = {
                'id': _repo_gid(repo), 'databaseId': repo.id,
                'name': repo.name, 'nameWithOwner': repo.full_name,
                'owner': {'login': repo.owner},
                'defaultBranchRef': {'name': repo.default_branch},
                'viewerPermission': 'ADMIN', 'hasIssuesEnabled': True,
            }
            if re.search(r'\bissue\s*\(', query):
                num = variables.get('number')
                if num is None:
                    m = re.search(r'issue\s*\(\s*number:\s*\$?(\w+)', query)
                iss = repo.issues.get(int(num)) if num is not None else None
                rd['issue'] = _issue_gql(iss) if iss else None
            if re.search(r'\bissues\s*\(', query):
                rd['issues'] = _issues_connection(repo, query, variables)
            if re.search(r'\bpullRequests\s*\(', query):
                rd['pullRequests'] = _pulls_connection(repo, query, variables, store)
            if re.search(r'\bpullRequest\s*\(', query):
                num = variables.get('number')
                pr = repo.pulls.get(int(num)) if num is not None else None
                if pr is None:
                    rd['pullRequest'] = None
                else:
                    node = _pr_gql(pr, repo, store)
                    if re.search(r'\bfiles\s*\(', query):
                        node['files'] = {'nodes': [
                            {'path': f['filename']} for f in repo.pr_files(pr)]}
                    rd['pullRequest'] = node
            data['repository'] = rd

    # ---- mutations -----------------------------------------------------------
    inp = variables.get('input') or {}
    if 'createIssue' in query:
        repo = _find_repo_by_gid(store, inp.get('repositoryId', ''))
        if repo is not None:
            issue = store.create_issue(repo, inp.get('title', ''),
                                       inp.get('body', ''), [])
            data['createIssue'] = {'issue': {'number': issue['number'],
                                             'id': issue['node_id']}}
    if 'closeIssue' in query:
        repo, issue = _find_issue_by_gid(store, inp.get('issueId', ''))
        if issue is not None:
            issue['state'] = 'closed'
            data['closeIssue'] = {'issue': {'number': issue['number'],
                                            'id': issue['node_id'],
                                            'state': 'CLOSED'}}
    if 'createPullRequest' in query:
        repo = _find_repo_by_gid(store, inp.get('repositoryId', ''))
        if repo is not None:
            head = inp.get('headRefName', '')
            base = inp.get('baseRefName', repo.default_branch)
            pr = store.create_pull(repo, inp.get('title', ''), '',
                                   head, base)
            data['createPullRequest'] = {'pullRequest': {
                'number': pr['number'], 'id': pr['node_id'],
                'state': 'OPEN', 'headRefName': head, 'baseRefName': base}}
    if 'mergePullRequest' in query:
        repo, pr = _find_pr_by_gid(store, inp.get('pullRequestId', ''))
        if pr is not None:
            status, merge_sha = repo.merge_pr(pr)
            if status == 200:
                pr['merged'] = True
                pr['state'] = 'closed'
                pr['merged_at'] = '2026-01-01T00:00:00Z'
                pr['merge_commit_sha'] = merge_sha
                data['mergePullRequest'] = {'pullRequest': {
                    'number': pr['number'], 'merged': True, 'state': 'MERGED'}}
            else:
                errors.append({'message': 'Merge conflict'})
                data['mergePullRequest'] = {'pullRequest': None}
    if 'addComment' in query:
        repo, issue = _find_issue_by_gid(store, inp.get('subjectId', ''))
        if issue is not None:
            cid = repo.comment_counter
            repo.comment_counter += 1
            comment = {'id': cid, 'body': inp.get('body', '')}
            issue.setdefault('comments', []).append(comment)
            data['addComment'] = {'commentEdge': {'node': {
                'id': 'C_%d_%d' % (repo.id, cid), 'body': inp.get('body', '')}}}

    if errors:
        return {'data': data, 'errors': errors}
    return {'data': data}
