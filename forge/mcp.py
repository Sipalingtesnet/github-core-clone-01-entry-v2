"""MCP (JSON-RPC 2.0, Streamable HTTP) for the 2026-07-28 era, with the
2025-11-25 handshake era also answered. Tools act on the same store as
REST, so objects read back identically.
"""
import json

from forge.store import public_repo, public_issue, public_pull

SERVER_NAME = 'github-core-clone-mcp'
SERVER_VERSION = '1.0.0'
PROTOCOL_VERSION = '2026-07-28'
SUPPORTED_VERSIONS = ['2026-07-28', '2025-11-25']

TOOLS = [
    {
        'name': 'create_repo',
        'description': 'Create a new repository',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string'},
                'name': {'type': 'string'},
                'description': {'type': 'string'},
                'private': {'type': 'boolean'},
                'full_name': {'type': 'string'},
            },
            'required': ['name'],
        },
    },
    {
        'name': 'get_repo',
        'description': 'Get repository details',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string'},
                'repo': {'type': 'string'},
                'full_name': {'type': 'string'},
            },
            'required': ['owner', 'repo'],
        },
    },
    {
        'name': 'create_issue',
        'description': 'Create an issue',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string'},
                'repo': {'type': 'string'},
                'title': {'type': 'string'},
                'body': {'type': 'string'},
                'full_name': {'type': 'string'},
            },
            'required': ['owner', 'repo', 'title'],
        },
    },
    {
        'name': 'list_issues',
        'description': 'List repository issues',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string'},
                'repo': {'type': 'string'},
                'state': {'type': 'string'},
                'full_name': {'type': 'string'},
            },
            'required': ['owner', 'repo'],
        },
    },
    {
        'name': 'create_pull_request',
        'description': 'Create a pull request',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string'},
                'repo': {'type': 'string'},
                'title': {'type': 'string'},
                'body': {'type': 'string'},
                'head': {'type': 'string'},
                'base': {'type': 'string'},
                'full_name': {'type': 'string'},
            },
            'required': ['owner', 'repo', 'title', 'head', 'base'],
        },
    },
    {
        'name': 'merge_pull_request',
        'description': 'Merge a pull request',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string'},
                'repo': {'type': 'string'},
                'pull_number': {'type': 'integer'},
                'number': {'type': 'integer'},
                'full_name': {'type': 'string'},
            },
            'required': ['owner', 'repo', 'pull_number'],
        },
    },
]

_TOOL_BY_NAME = {t['name']: t for t in TOOLS}


def _result(msg_id, result):
    return {'jsonrpc': '2.0', 'id': msg_id, 'result': result}


def _error(msg_id, code, message):
    return {'jsonrpc': '2.0', 'id': msg_id,
            'error': {'code': code, 'message': message}}


def _tool_result(msg_id, obj, is_error=False):
    text = obj if isinstance(obj, str) else json.dumps(obj)
    res = {'content': [{'type': 'text', 'text': text}]}
    if not isinstance(obj, str):
        res['structuredContent'] = obj
    if is_error:
        res['isError'] = True
    return _result(msg_id, res)


def _resolve_owner_repo(args, viewer):
    owner = args.get('owner') or args.get('owner_login') or viewer
    repo_name = args.get('repo') or args.get('repository') or args.get('name') or ''
    full = args.get('full_name') or args.get('repo_full_name')
    if full and '/' in str(full):
        owner, repo_name = str(full).split('/', 1)
    return owner, repo_name


def handle_mcp_rpc(req, viewer, store):
    if not isinstance(req, dict):
        return _error(None, -32600, 'Invalid Request')
    method = req.get('method')
    msg_id = req.get('id')
    params = req.get('params') or {}

    if method == 'server/discover':
        return _result(msg_id, {
            'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
            '_meta': {'io.modelcontextprotocol/serverInfo':
                      {'name': SERVER_NAME, 'version': SERVER_VERSION}},
            'capabilities': {'tools': {}},
            'supportedVersions': list(SUPPORTED_VERSIONS),
        })

    if method == 'initialize':
        return _result(msg_id, {
            'protocolVersion': PROTOCOL_VERSION,
            'capabilities': {'tools': {}},
            'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
        })

    if method == 'notifications/initialized':
        # Notification: no id, no result expected; answer an empty result.
        return {}

    if method == 'tools/list':
        return _result(msg_id, {'tools': TOOLS})

    if method == 'tools/call':
        name = params.get('name') or ''
        args = params.get('arguments') or {}
        if not isinstance(args, dict):
            args = {}
        # Match by exact name, case-insensitive suffix (server prefixes).
        tool = None
        lname = str(name).lower()
        for t in TOOLS:
            if lname == t['name'] or lname.endswith('__' + t['name']) or lname.endswith(t['name']):
                tool = t
                break
        if tool is None:
            return _error(msg_id, -32601, 'Tool not found: %s' % name)
        tname = tool['name']
        owner, repo_name = _resolve_owner_repo(args, viewer)

        def missing(*keys):
            return [k for k in keys if args.get(k) in (None, '')]

        if tname == 'create_repo':
            if missing('name'):
                return _tool_result(msg_id, 'Missing required argument: name', True)
            repo = store.create_repo(owner, args['name'],
                                     args.get('private', True),
                                     args.get('description', ''))
            if repo is None:
                repo = store.get_repo(owner, args['name'])
            return _tool_result(msg_id, public_repo(repo))

        if tname == 'get_repo':
            if missing('owner', 'repo') and not args.get('full_name'):
                return _tool_result(msg_id, 'Missing required argument: owner/repo', True)
            repo = store.get_repo(owner, repo_name)
            if repo is None:
                return _tool_result(msg_id, 'Repository not found', True)
            return _tool_result(msg_id, public_repo(repo))

        if tname == 'create_issue':
            miss = missing('title')
            if not repo_name or not owner:
                miss.append('repo')
            if miss:
                return _tool_result(msg_id, 'Missing required argument: %s' % ', '.join(miss), True)
            repo = store.get_repo(owner, repo_name)
            if repo is None:
                return _tool_result(msg_id, 'Repository not found', True)
            issue = store.create_issue(repo, args.get('title', ''),
                                       args.get('body', ''), [])
            return _tool_result(msg_id, public_issue(issue))

        if tname == 'list_issues':
            if not repo_name:
                return _tool_result(msg_id, 'Missing required argument: repo', True)
            repo = store.get_repo(owner, repo_name)
            if repo is None:
                return _tool_result(msg_id, 'Repository not found', True)
            state = args.get('state')
            items = [public_issue(i) for i in repo.issues.values()]
            if state and state != 'all':
                items = [i for i in items if i['state'] == state]
            return _tool_result(msg_id, items)

        if tname == 'create_pull_request':
            miss = missing('title', 'head', 'base')
            if not repo_name:
                miss.append('repo')
            if miss:
                return _tool_result(msg_id, 'Missing required argument: %s' % ', '.join(miss), True)
            repo = store.get_repo(owner, repo_name)
            if repo is None:
                return _tool_result(msg_id, 'Repository not found', True)
            for pr in repo.pulls.values():
                if pr['state'] == 'open' and pr['head']['ref'] == args['head']:
                    return _tool_result(msg_id, 'A pull request already exists for this head branch', True)
            pr = store.create_pull(repo, args.get('title', ''),
                                   args.get('body', ''), args['head'], args['base'])
            return _tool_result(msg_id, public_pull(pr))

        if tname == 'merge_pull_request':
            num = args.get('pull_number') or args.get('number')
            if num is None or not repo_name:
                return _tool_result(msg_id, 'Missing required argument: pull_number', True)
            repo = store.get_repo(owner, repo_name)
            if repo is None:
                return _tool_result(msg_id, 'Repository not found', True)
            pr = repo.pulls.get(int(num))
            if pr is None:
                return _tool_result(msg_id, 'Pull request not found', True)
            status, merge_sha = repo.merge_pr(pr)
            if status != 200:
                return _tool_result(msg_id, 'Merge conflict', True)
            pr['merged'] = True
            pr['state'] = 'closed'
            pr['merged_at'] = '2026-01-01T00:00:00Z'
            pr['merge_commit_sha'] = merge_sha
            return _tool_result(msg_id, public_pull(pr))

    return _error(msg_id, -32601, 'Method not found: %s' % method)
