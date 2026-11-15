"""Git object model and smart-HTTP packfile handling, written from the
protocol (pkt-line framing, pack v2, side-band-64k) and the git object
format. No deltas are ever emitted; incoming deltas (OFS/REF) are resolved,
including REF_DELTA bases that already live in the store (thin packs).
"""
import hashlib
import struct
import zlib

ZERO_OID = '0' * 40
TYPE_CODE = {'commit': 1, 'tree': 2, 'blob': 3, 'tag': 4}
CODE_TYPE = {1: 'commit', 2: 'tree', 3: 'blob', 4: 'tag'}


def hash_object(obj_type, data):
    header = ('%s %d\0' % (obj_type, len(data))).encode('ascii')
    return hashlib.sha1(header + data).hexdigest()


# ---------------------------------------------------------------- pkt-line

def pkt_line(data):
    return ('%04x' % (len(data) + 4)).encode('ascii') + data


def pkt_flush():
    return b'0000'


def pkt_delim():
    return b'0001'


def pkt_sideband(band, data):
    """One sideband pkt-line; caller must keep data small enough."""
    return pkt_line(bytes([band]) + data)


def sideband_stream(band, data, chunk=65515):
    out = bytearray()
    if not data:
        return bytes(out)
    for i in range(0, len(data), chunk):
        out += pkt_line(bytes([band]) + data[i:i + chunk])
    return bytes(out)


def parse_pkt_lines(buf):
    """Return (packets, consumed). packets: list of bytes payloads, or
    b'0000' for flush / b'0001' for delim. Stops at the first flush or at
    the end of the buffer; consumed is the offset just past the last
    complete packet parsed."""
    packets = []
    i = 0
    n = len(buf)
    while i + 4 <= n:
        head = buf[i:i + 4]
        if head == b'0000':
            packets.append(b'0000')
            return packets, i + 4
        if head == b'0001':
            packets.append(b'0001')
            i += 4
            continue
        if head == b'0002':
            packets.append(b'0002')
            i += 4
            continue
        try:
            length = int(head.decode('ascii'), 16)
        except ValueError:
            break
        if length < 4 or i + length > n:
            break
        packets.append(buf[i + 4:i + length])
        i += length
    return packets, i


def parse_pkt_text_lines(buf):
    """All text pkt-lines of a request body (ignores flush/delim)."""
    packets, _ = parse_pkt_lines(buf)
    out = []
    for p in packets:
        if p in (b'0000', b'0001', b'0002'):
            continue
        out.append(p.decode('utf-8', errors='replace'))
    return out


# ---------------------------------------------------------------- object parsing

def parse_commit(data):
    """Return {'tree': hex, 'parents': [hex], 'message': bytes}."""
    tree = None
    parents = []
    i = 0
    n = len(data)
    while i < n:
        j = data.find(b'\n', i)
        if j < 0:
            break
        line = data[i:j]
        if line == b'':
            i = j + 1
            break
        if line.startswith(b'tree '):
            tree = line[5:].decode('ascii')
        elif line.startswith(b'parent '):
            parents.append(line[7:].decode('ascii'))
        i = j + 1
    return {'tree': tree, 'parents': parents, 'message': data[i:] if i < n else b''}


def parse_tree(data):
    """Return list of (mode_bytes, name_bytes, sha_hex)."""
    entries = []
    i = 0
    n = len(data)
    while i < n:
        sp = data.find(b' ', i)
        if sp < 0:
            break
        mode = data[i:sp]
        nul = data.find(b'\0', sp)
        if nul < 0 or nul + 21 > n:
            break
        name = data[sp + 1:nul]
        sha = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, sha))
        i = nul + 21
    return entries


def parse_tag(data):
    obj = None
    typ = None
    i = 0
    n = len(data)
    while i < n:
        j = data.find(b'\n', i)
        if j < 0:
            break
        line = data[i:j]
        if line == b'':
            break
        if line.startswith(b'object '):
            obj = line[7:].decode('ascii')
        elif line.startswith(b'type '):
            typ = line[5:].decode('ascii')
        i = j + 1
    return {'object': obj, 'type': typ}


def build_tree(entries):
    """entries: list of (mode_bytes, name_bytes, sha_hex). Sorted git-style:
    by name, directories (mode 40000) sorting as if name ends with '/'."""
    def sort_key(e):
        mode, name, _ = e
        key = name + b'/' if mode == b'40000' else name
        return key
    out = bytearray()
    for mode, name, sha in sorted(entries, key=sort_key):
        out += mode + b' ' + name + b'\0' + bytes.fromhex(sha)
    return bytes(out)


def build_commit(tree_sha, parents, message, author=b'makefun <makefun@example.invalid>'):
    lines = [b'tree ' + tree_sha.encode('ascii')]
    for p in parents:
        lines.append(b'parent ' + p.encode('ascii'))
    lines.append(b'author ' + author + b' 1735689600 +0000')
    lines.append(b'committer ' + author + b' 1735689600 +0000')
    return b'\n'.join(lines) + b'\n\n' + message


# ---------------------------------------------------------------- delta

def _decode_varint(data, pos):
    size = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        size |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return size, pos


def apply_delta(base, delta):
    base_size, pos = _decode_varint(delta, 0)
    _target_size, pos = _decode_varint(delta, pos)
    out = bytearray()
    while pos < len(delta):
        cmd = delta[pos]
        pos += 1
        if cmd & 0x80:
            cp_off = 0
            cp_size = 0
            if cmd & 0x01:
                cp_off = delta[pos]; pos += 1
            if cmd & 0x02:
                cp_off |= delta[pos] << 8; pos += 1
            if cmd & 0x04:
                cp_off |= delta[pos] << 16; pos += 1
            if cmd & 0x08:
                cp_off |= delta[pos] << 24; pos += 1
            if cmd & 0x10:
                cp_size = delta[pos]; pos += 1
            if cmd & 0x20:
                cp_size |= delta[pos] << 8; pos += 1
            if cmd & 0x40:
                cp_size |= delta[pos] << 16; pos += 1
            if cp_size == 0:
                cp_size = 0x10000
            out += base[cp_off:cp_off + cp_size]
        elif cmd:
            out += delta[pos:pos + cmd]
            pos += cmd
        else:
            raise ValueError('delta opcode 0')
    return bytes(out)


# ---------------------------------------------------------------- packfile parsing

def parse_packfile(pack_data, store_objects):
    """Parse a packfile. store_objects: dict sha -> (type, data) of objects
    already in the repo (for thin-pack REF_DELTA bases).
    Returns (ok, detail, [(sha, type, data), ...]) with objects in pack order
    (deltas resolved). An empty pack (0 objects) is ok. Missing/invalid
    header is an error."""
    if not pack_data:
        return True, 'unpack ok', []
    if len(pack_data) < 12 or pack_data[:4] != b'PACK':
        return False, 'Invalid pack header', []
    version, num_objects = struct.unpack('!II', pack_data[4:12])
    if version != 2:
        return False, 'Invalid pack version', []
    entries = []  # (type_code, data, ref) ref: hex sha | int offset | None
    pos = 12
    end = len(pack_data) - 20 if len(pack_data) >= 20 else len(pack_data)
    try:
        for _ in range(num_objects):
            if pos >= end:
                return False, 'Unexpected end of packfile', []
            byte = pack_data[pos]
            pos += 1
            obj_type = (byte >> 4) & 7
            size = byte & 15
            shift = 4
            while byte & 0x80:
                if pos >= len(pack_data):
                    return False, 'Unexpected end of packfile', []
                byte = pack_data[pos]
                pos += 1
                size += (byte & 0x7F) << shift
                shift += 7
            ref = None
            if obj_type == 7:  # REF_DELTA
                if pos + 20 > len(pack_data):
                    return False, 'Unexpected end of packfile', []
                ref = pack_data[pos:pos + 20].hex()
                pos += 20
            elif obj_type == 6:  # OFS_DELTA
                c = pack_data[pos]
                pos += 1
                offset = c & 0x7F
                while c & 0x80:
                    if pos >= len(pack_data):
                        return False, 'Unexpected end of packfile', []
                    c = pack_data[pos]
                    pos += 1
                    offset = ((offset + 1) << 7) + (c & 0x7F)
                ref = offset
            decomp = zlib.decompressobj()
            uncompressed = decomp.decompress(pack_data[pos:])
            pos = len(pack_data) - len(decomp.unused_data)
            if len(uncompressed) != size:
                return False, 'Object size mismatch', []
            entries.append((obj_type, uncompressed, ref))
    except Exception as e:
        return False, 'Invalid packfile: %s' % e, []

    by_index = {}
    by_sha = dict(store_objects)  # sha -> (type, data); pack entries override
    resolved = []
    pending = list(enumerate(entries))
    # Non-deltas first, then deltas iteratively (handles chains).
    remaining = []
    for idx, (ot, data, ref) in pending:
        if ot in CODE_TYPE:
            t = CODE_TYPE[ot]
            sha = hash_object(t, data)
            by_index[idx] = (t, data)
            by_sha[sha] = (t, data)
            resolved.append((sha, t, data))
        else:
            remaining.append((idx, ot, data, ref))
    for _ in range(20):
        if not remaining:
            break
        nxt = []
        progressed = False
        for idx, ot, data, ref in remaining:
            base = None
            base_type = None
            if ot == 7:
                hit = by_sha.get(ref)
                if hit:
                    base_type, base = hit
            elif ot == 6:
                hit = by_index.get(idx - ref)
                if hit:
                    base_type, base = hit
            if base is None:
                nxt.append((idx, ot, data, ref))
                continue
            try:
                target = apply_delta(base, data)
            except Exception:
                return False, 'Invalid delta', []
            sha = hash_object(base_type, target)
            by_index[idx] = (base_type, target)
            by_sha[sha] = (base_type, target)
            resolved.append((sha, base_type, target))
            progressed = True
        remaining = nxt
        if not progressed:
            break
    if remaining:
        return False, 'Unresolved delta base', []
    return True, 'unpack ok', resolved


def build_pack(objects):
    """objects: iterable of (sha, type, data). No deltas. Returns pack bytes
    with a valid SHA-1 trailer."""
    items = list(objects)
    out = bytearray()
    out += b'PACK'
    out += struct.pack('!II', 2, len(items))
    for _sha, obj_type, data in items:
        code = TYPE_CODE[obj_type]
        size = len(data)
        byte = ((code & 7) << 4) | (size & 15)
        size >>= 4
        if size:
            byte |= 0x80
        out.append(byte)
        while size:
            b = size & 0x7F
            size >>= 7
            if size:
                b |= 0x80
            out.append(b)
        out += zlib.compress(data, 1)
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


# ---------------------------------------------------------------- reachability

def reachable_objects(objects, tips, depth=None):
    """objects: dict sha -> (type, data). Walk commits/trees/tags from tips.
    depth: if not None, only commits within <depth> generations of a tip
    are included (their trees/blobs included; the boundary commits are
    returned separately). Returns (included_set, boundary_set)."""
    included = set()
    boundary = set()
    stack = [(t, 0) for t in tips]
    seen_commits = set()
    while stack:
        sha, d = stack.pop()
        if sha in included or sha == ZERO_OID:
            continue
        if depth is not None and d >= depth:
            continue
        hit = objects.get(sha)
        if hit is None:
            continue
        typ, data = hit
        included.add(sha)
        if typ == 'commit':
            if depth is not None and d == depth - 1:
                # This commit is in the pack but its parents are cut off:
                # it is the shallow boundary (git records the fetched tip
                # itself as shallow for a depth-1 clone).
                boundary.add(sha)
                if sha in seen_commits:
                    continue
                seen_commits.add(sha)
                c = parse_commit(data)
                if c['tree']:
                    stack.append((c['tree'], d))
                continue
            if sha in seen_commits:
                continue
            seen_commits.add(sha)
            c = parse_commit(data)
            if c['tree']:
                stack.append((c['tree'], d))
            for p in c['parents']:
                stack.append((p, d + 1))
        elif typ == 'tree':
            for _mode, _name, child in parse_tree(data):
                stack.append((child, d))
        elif typ == 'tag':
            t = parse_tag(data)
            if t['object']:
                stack.append((t['object'], d))
    return included, boundary


def flatten_tree(objects, tree_sha):
    """Return {path: (mode_bytes, sha)} for a tree, recursively."""
    out = {}
    def walk(sha, prefix):
        hit = objects.get(sha)
        if hit is None or hit[0] != 'tree':
            return
        for mode, name, child in parse_tree(hit[1]):
            path = prefix + name.decode('utf-8', 'replace')
            if mode == b'40000':
                walk(child, path + '/')
            else:
                out[path] = (mode, child)
    walk(tree_sha, '')
    return out


def shallow_have_reach(objects, haves, shallow_boundaries):
    """Objects a client already holds, given its `have` tips and its
    shallow boundaries: walk each have's commit/tree/blobs, but never
    traverse the parents of a shallow boundary commit (a shallow clone
    does not hold its boundary's ancestry)."""
    shallow = set(shallow_boundaries or ())
    held = set()
    stack = list(haves)
    seen = set()
    while stack:
        sha = stack.pop()
        if sha in seen or sha == ZERO_OID:
            continue
        seen.add(sha)
        hit = objects.get(sha)
        if hit is None:
            continue
        typ, data = hit
        held.add(sha)
        if typ == 'commit':
            c = parse_commit(data)
            if c['tree']:
                stack.append(c['tree'])
            if sha not in shallow:
                stack.extend(c['parents'])
        elif typ == 'tree':
            for _mode, _name, child in parse_tree(data):
                stack.append(child)
        elif typ == 'tag':
            t = parse_tag(data)
            if t['object']:
                stack.append(t['object'])
    return held


def commit_ancestors(objects, tip):
    """All commit shas reachable from tip (following parents)."""
    out = set()
    stack = [tip]
    while stack:
        sha = stack.pop()
        if sha in out or sha == ZERO_OID:
            continue
        hit = objects.get(sha)
        if hit is None or hit[0] != 'commit':
            continue
        out.add(sha)
        stack.extend(parse_commit(hit[1])['parents'])
    return out


def is_ancestor(objects, ancestor, tip):
    if ancestor == tip:
        return True
    return ancestor in commit_ancestors(objects, tip)


def merge_base(objects, a, b):
    """First common ancestor of a and b (BFS from a, then BFS from b)."""
    anc_a = commit_ancestors(objects, a)
    stack = [b]
    seen = set()
    while stack:
        sha = stack.pop()
        if sha in seen or sha == ZERO_OID:
            continue
        seen.add(sha)
        if sha in anc_a:
            return sha
        hit = objects.get(sha)
        if hit and hit[0] == 'commit':
            stack.extend(parse_commit(hit[1])['parents'])
    return None
