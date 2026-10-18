"""Read-only Plants vs. Zombies: Battle for Neighborville archive support. Game files are never modified.

BFN keeps its bundle manifests in binary superbundle TOCs, its payloads in Oodle-compressed
install-chunk CAS archives, and its EBX type descriptors in an AES-encrypted initfs. EBX
partitions are version 4: classes are named only by TypeInfo GUID and laid out from the
shared type descriptors, so a class/field name table is required (frosty_types.ps1 writes
one from a Frosty Editor PVZ3SDK.dll).

The initfs key is the first 16 bytes of a Frosty key file, given with --key or
AURA_BFN_KEY as hex. Windows only: AES uses CNG (bcrypt.dll) and Oodle is the game's
own oo2core DLL.
"""
import ctypes
import json
import os
import re
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path

from gw2 import Reader

LIMIT = 512 * 1024 * 1024
EXE = 'PVZBattleforNeighborville.exe'
EBX_V4 = 0x0FB4D1CE
TEXTURE_TYPE, MESHSET_TYPE, TERRAIN_TREE_TYPE = 0x6BDE20BA, 0x49B156D4, 0x22FE8AC8


def is_bfn(game):
    return (Path(game) / EXE).is_file()


# ---------------- crypto and compression ----------------
def key_from(value=None):
    text = (value or os.environ.get('AURA_BFN_KEY') or '').strip()
    if len(text) < 260 and Path(text).is_file():
        raw = Path(text).read_bytes()
        text = raw.decode('ascii', 'ignore').strip() if all(c in b'0123456789abcdefABCDEF \r\n' for c in raw[:64]) else raw[:16].hex()
    text = re.sub(r'\s+', '', text)
    if len(text) < 32 or not re.fullmatch(r'[0-9a-fA-F]+', text):
        raise ValueError('Battle for Neighborville needs its initfs key (32+ hex digits)')
    return bytes.fromhex(text[:32])


def aes_cbc_decrypt(key, data):
    """AES-128-CBC with the key as IV, as BFN encrypts its initfs."""
    if len(data) % 16:
        raise ValueError('Encrypted initfs is not block aligned')
    bcrypt = ctypes.WinDLL('bcrypt')
    alg, handle = ctypes.c_void_p(), ctypes.c_void_p()
    if bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(alg), 'AES', None, 0):
        raise OSError('AES provider unavailable')
    try:
        mode = ctypes.create_unicode_buffer('ChainingModeCBC')
        bcrypt.BCryptSetProperty(alg, 'ChainingMode', mode, ctypes.sizeof(mode), 0)
        if bcrypt.BCryptGenerateSymmetricKey(alg, ctypes.byref(handle), None, 0, key, len(key), 0):
            raise OSError('AES key rejected')
        out, size = ctypes.create_string_buffer(len(data)), wintypes.ULONG()
        status = bcrypt.BCryptDecrypt(handle, data, len(data), None, ctypes.create_string_buffer(key, 16), 16,
                                      out, len(data), ctypes.byref(size), 0)
        bcrypt.BCryptDestroyKey(handle)
        if status:
            raise OSError('AES decrypt failed 0x%08x' % (status & 0xffffffff))
        return out.raw[:size.value]
    finally:
        bcrypt.BCryptCloseAlgorithmProvider(alg, 0)


def read_db(path):
    data = Path(path).read_bytes()
    if data[:3] != b'\0\xd1\xce' or len(data) < 0x22c:
        raise ValueError('Unrecognized BFN DB envelope: %s' % path)
    return Reader(data[0x22c:]).db()[1]


def initfs(game, key):
    """Files of the Data initfs, overlaid with the Patch initfs."""
    files = {}
    for folder in ('Data', 'Patch'):
        path = Path(game) / folder / 'initfs_Win32'
        if not path.is_file():
            continue
        doc = read_db(path)
        plain = aes_cbc_decrypt(key, doc['encrypted']) if 'encrypted' in doc else None
        entries = Reader(plain).db()[1] if plain is not None else doc
        try:
            for entry in entries:
                item = entry.get('$file') or entry.get('file') or entry
                files[item['name']] = item['payload']
        except (TypeError, KeyError, AttributeError) as error:
            raise ValueError('The initfs key is wrong for this game version') from error
    if 'SharedTypeDescriptors.ebx' not in files:
        raise ValueError('The initfs key is wrong for this game version (no type descriptors)')
    return files


class Oodle:
    def __init__(self, game):
        dll = next(iter(sorted(Path(game).glob('oo2core_*_win64.dll'), reverse=True)), None)
        if not dll:
            raise FileNotFoundError('No oo2core_*_win64.dll in ' + str(game))
        self.lib = ctypes.WinDLL(str(dll))
        self.fn = self.lib.OodleLZ_Decompress
        self.fn.restype = ctypes.c_longlong
        self.fn.argtypes = [ctypes.c_char_p, ctypes.c_longlong, ctypes.c_void_p, ctypes.c_longlong] + [ctypes.c_int] * 3 + \
                           [ctypes.c_void_p, ctypes.c_longlong] + [ctypes.c_void_p] * 3 + [ctypes.c_longlong, ctypes.c_int]

    def cas(self, data):
        """Decode a stream of CAS blocks."""
        out, at = bytearray(), 0
        while at < len(data):
            if at + 8 > len(data):
                raise ValueError('Truncated CAS block header')
            size = struct.unpack_from('>I', data, at)[0] & 0xffffff
            code = struct.unpack_from('<H', data, at + 4)[0]
            packed = struct.unpack_from('>H', data, at + 6)[0] | ((code >> 8 & 15) << 16)
            kind = code & 0x7f
            at += 8
            if len(out) + size > LIMIT:
                raise ValueError('CAS output exceeds limit')
            if kind == 0:
                out += data[at:at + size]
                at += size
            elif kind in (0x11, 0x15, 0x19):
                buf = ctypes.create_string_buffer(size)
                if self.fn(data[at:at + packed], packed, buf, size, 1, 0, 0, None, 0, None, None, None, 0, 3) != size:
                    raise ValueError('Oodle CAS decode failed')
                out += buf.raw
                at += packed
            else:
                raise ValueError('Unsupported CAS codec 0x%x' % kind)
        return bytes(out)


# ---------------- archive layout ----------------
def cstring(data, at):
    end = data.index(b'\0', at)
    return data[at:end].decode('utf-8', 'replace')


def guid_big(data):
    return (data[:4][::-1] + data[4:6][::-1] + data[6:8][::-1] + data[8:]).hex()


def cas_ref(enc, wide):
    if wide:
        return ((enc >> 48) & 0xff) != 0, (enc >> 16) & 0xffffffff, enc & 0xffff
    return (enc & 0xff0000) != 0, (enc >> 8) & 0xff, enc & 0xff


def manifest(d):
    """Assets of a binary bundle manifest: ebx, then resources, then chunks."""
    for endian in ('>', '<'):
        magic = struct.unpack_from(endian + 'I', d, 4)[0] ^ 0x7065636E
        if magic in (0xED1CEDB8, 0xED1CEDBB):
            break
    else:
        raise ValueError('Unsupported bundle manifest magic')
    total, ne, nr, nc, strings = struct.unpack_from(endian + '5I', d, 8)
    strings += 4
    if ne + nr + nc != total or total * 20 > len(d):
        raise ValueError('Invalid bundle manifest counts')
    at = 36
    sha = [d[at + 20 * i:at + 20 * i + 20].hex() for i in range(total)]
    at += 20 * total
    assets = []
    for i in range(ne + nr):
        name, size = struct.unpack_from(endian + 'II', d, at)
        at += 8
        assets.append({'kind': 'ebx' if i < ne else 'resource', 'name': cstring(d, strings + name),
                       'original_size': size, 'sha1': sha[i]})
    res = assets[ne:]
    for a in res:
        a['resource_type'] = struct.unpack_from(endian + 'I', d, at)[0]; at += 4
    for a in res:
        a['metadata'] = list(d[at:at + 16]); at += 16
    for a in res:
        a['resource_id'] = str(struct.unpack_from(endian + 'Q', d, at)[0]); at += 8
    for i in range(nc):
        guid = guid_big(d[at:at + 16]) if endian == '>' else d[at:at + 16].hex()
        lo, size = struct.unpack_from(endian + 'II', d, at + 16)
        at += 24
        assets.append({'kind': 'chunk', 'guid': guid, 'name': guid, 'original_size': (lo & 0xffff) | size,
                       'sha1': sha[ne + nr + i]})
    return assets


class Archive:
    def __init__(self, game):
        self.game = Path(game)
        if not is_bfn(game):
            raise ValueError('Not a Battle for Neighborville installation: ' + str(game))
        self.sbs, self.install = [], {}
        for folder in ('Patch', 'Data'):
            layout = self.game / folder / 'layout.toc'
            if not layout.is_file():
                continue
            doc = read_db(layout)
            for sb in doc.get('superBundles', []):
                if sb['name'].lower() not in (s.lower() for s in self.sbs):
                    self.sbs.append(sb['name'])
            self.install[folder] = [c['name'] for c in doc.get('installManifest', {}).get('installChunks', [])]
        self.install.setdefault('Patch', self.install.get('Data', []))
        self.tocs, self.handles, self.lock = {}, {}, threading.Lock()
        self.read_locks = {}
        self.oodle = Oodle(game)

    def read(self, path, offset, size):
        with self.lock:
            handle = self.handles.get(path)
            if handle is None:
                handle = self.handles[path] = open(path, 'rb')
                self.read_locks[path] = threading.Lock()
            read_lock = self.read_locks[path]
        # Protect seek/read on each handle, not every CAS file in the archive.
        # Parallel readers of independent files can now make progress together.
        with read_lock:
            handle.seek(offset)
            data = handle.read(size)
        if len(data) != size:
            raise ValueError('Short read from %s' % path)
        return data

    def parse_toc(self, path):
        body = path.read_bytes()[0x22c:]
        h = struct.unpack_from('>12I', body, 0)
        bundle_at, bundles, chunk_guids, chunk_count, names, chunk_data, words, flags = \
            h[1], h[2], h[4], h[5], h[8], h[9], h[10], h[11]
        if flags & 4:
            raise ValueError('Compressed TOC names are not supported: %s' % path)
        out = {'sb': path.with_suffix('.sb'), 'body': body, 'bundles': {}, 'chunks': {}}
        for i in range(bundles):
            name_at, size_flag, offset = struct.unpack_from('>IIQ', body, bundle_at + 16 * i)
            name = cstring(body, names + name_at)
            out['bundles'][name.lower()] = (name, offset, size_flag & 0x3fffffff, size_flag >> 30 == 1, out)
        table = struct.unpack_from('>%dI' % words, body, chunk_data) if words else ()
        for i in range(chunk_count):
            at = chunk_guids + 20 * i
            guid = body[at:at + 16][::-1].hex()
            index = struct.unpack_from('>i', body, at + 16)[0]
            if index == -1:
                continue
            kind, w = (index >> 24) & 0xff, index & 0xffffff
            if kind == 1 and w + 3 <= len(table):
                out['chunks'][guid] = cas_ref(table[w], False) + (table[w + 1], table[w + 2])
            elif kind == 0x80 and w + 4 <= len(table):
                out['chunks'][guid] = cas_ref(table[w] << 32 | table[w + 1], True) + (table[w + 2], table[w + 3])
        return out

    def toc(self, sb):
        """A superbundle as patched: the Patch TOC overrides bundles and chunks of the Data TOC."""
        key = sb.lower()
        if key not in self.tocs:
            view = {'bundles': {}, 'chunks': {}}
            for folder in ('Data', 'Patch'):
                path = self.game / folder / (sb + '.toc')
                if path.is_file():
                    t = self.parse_toc(path)
                    view['bundles'].update(t['bundles'])
                    view['chunks'].update(t['chunks'])
            self.tocs[key] = view
        return self.tocs[key]

    def cas_path(self, ref):
        patch, install, archive = ref[:3]
        folder = 'Patch' if patch else 'Data'
        names = self.install[folder]
        if install >= len(names):
            raise ValueError('CAS install chunk out of range')
        return self.game / folder / 'Win32' / names[install] / ('cas_%02d.cas' % archive)

    def fetch(self, ref):
        return self.oodle.cas(self.read(self.cas_path(ref), ref[3], ref[4]))

    def bundle(self, entry):
        """Assets of one bundle, each with its CAS reference."""
        name, offset, size, in_toc, source = entry
        region = source['body'][offset:offset + size] if in_toc else self.read(source['sb'], offset, size)
        manifest_at, manifest_size, flags_at, total, data_at = struct.unpack_from('>5I', region, 0)
        assets = manifest(region[manifest_at:manifest_at + manifest_size])
        if len(assets) != total:
            raise ValueError('Bundle payload count differs from its manifest: ' + name)
        at, current = data_at, None
        for i, asset in enumerate(assets):
            flag = region[flags_at + i]
            if flag == 1:
                current = struct.unpack_from('>I', region, at)[0], False; at += 4
            elif flag:
                current = struct.unpack_from('>Q', region, at)[0], True; at += 8
            if current is None:
                raise ValueError('Bundle payload has no CAS archive: ' + name)
            offset, size = struct.unpack_from('>II', region, at); at += 8
            asset['ref'] = cas_ref(*current) + (offset, size)
            asset['bundle'] = name
        return assets

    def chunk(self, guid):
        for sb in self.sbs:
            ref = self.toc(sb)['chunks'].get(guid)
            if ref:
                return ref
        return None

    def levels(self):
        found = {}
        for sb in self.sbs:
            if '/loc/' in sb.lower():
                continue
            for lower, entry in self.toc(sb)['bundles'].items():
                parts = lower.split('/')
                if len(parts) == 4 and parts[:2] == ['win32', 'levels'] and parts[2] == parts[3]:
                    found.setdefault(parts[2], sb)
        return found


def discover(game):
    archive = Archive(game)
    maps = []
    for leaf, sb in archive.levels().items():
        if leaf.endswith('_splash') or leaf == 'dsub_podiumcelebration':
            continue
        title = ' '.join(w.capitalize() for w in re.sub(r'^(level|dsub)_', '', leaf).split('_'))
        maps.append({'name': title, 'package': 'Win32/levels/' + leaf, 'mod': str(game), 'profile': 'bfn'})
    return sorted(maps, key=lambda item: item['name'])


# ---------------- EBX version 4 ----------------
INHERITED, STRUCT, POINTER, ARRAY, CSTRING, ENUM, FILEREF, RESOURCE = 0x00, 0x02, 0x03, 0x04, 0x07, 0x08, 0x09, 0x17
SCALARS = {0x0A: '<B', 0x0B: '<b', 0x0C: '<B', 0x0D: '<h', 0x0E: '<H', 0x0F: '<i', 0x10: '<I', 0x11: '<Q',
           0x12: '<q', 0x13: '<f', 0x14: '<d', ENUM: '<i'}
SIZES = {0x0A: 1, 0x0B: 1, 0x0C: 1, 0x0D: 2, 0x0E: 2, 0x0F: 4, 0x10: 4, 0x13: 4, ENUM: 4, POINTER: 4, ARRAY: 4,
         CSTRING: 4, FILEREF: 4, 0x11: 8, 0x12: 8, 0x14: 8, RESOURCE: 8, 0x15: 16, 0x16: 20}


class Types:
    """SharedTypeDescriptors.ebx and its patch, named from a Frosty SDK name table."""
    def __init__(self, base, patch, names):
        self.fields, self.classes, self.by_guid, self.redirect = [], [], {}, []
        self.names = names
        self.read(base, False)
        if patch:
            self.read(patch, True)
        for f in self.fields:
            ref = f[2]
            if 0 <= ref < len(self.classes) and self.redirect[ref] >= 0:
                f[2] = self.redirect[ref]
            elif not 0 <= ref < len(self.classes):
                f[2] = -1

    def read(self, data, patch):
        magic, class_count, field_count = struct.unpack_from('<IHH', data, 0)
        if magic != EBX_V4:
            raise ValueError('Shared type descriptors have an unknown version')
        field_base, at = len(self.fields), 8
        raw = []
        for _ in range(field_count):
            h, flags, ref, offset, _ = struct.unpack_from('<IHHII', data, at)
            raw.append((h, (flags >> 5) & 0x1F, ref, offset))
            at += 16
        owner = [None] * field_count
        for _ in range(class_count):
            guid, body = data[at:at + 16], at + 16
            _, field_index, count, align, flags, size = struct.unpack_from('<IiBBHH', data, body)
            index = len(self.classes)
            self.redirect.append(-1)
            if patch and data[body:body + 16] == guid:
                # A patch entry that only names an unchanged base class.
                if guid.hex() not in self.by_guid:
                    raise ValueError('Patch type descriptor names an unknown base class')
                self.redirect[-1] = self.by_guid[guid.hex()]
                self.classes.append(self.classes[self.redirect[-1]])
                at += 32
                continue
            # The field index is a byte distance back from the class record to its first field.
            first = (body - field_index - 8) // 16
            if first < 0 or first + count > field_count:
                raise ValueError('Type descriptor fields out of range')
            for k in range(count):
                owner[first + k] = index
            name = self.names['classes'].get(guid.hex(), guid.hex())
            self.classes.append({'name': name, 'first': field_base + first, 'count': count,
                                 'align': max(align, 1), 'size': size, 'type': (flags >> 5) & 0x1F})
            self.by_guid[guid.hex()] = index
            at += 32
        per_class, generic = self.names['class_fields'], self.names['fields']
        for i, (h, kind, ref, offset) in enumerate(raw):
            name, cls = '%08x' % h, owner[i]
            if cls is not None:
                name = per_class.get(self.classes[cls]['name'], {}).get(str(h)) or generic.get(str(h)) or name
                # References are relative to the owning class, counted through both descriptor files.
                ref = cls + struct.unpack('<h', struct.pack('<H', ref))[0]
            self.fields.append([name, kind, ref, offset])


class Ebx:
    def __init__(self, data, types):
        self.d, self.types = data, types
        magic, self.strings, _, imports = struct.unpack_from('<4I', data, 0)
        if magic != EBX_V4:
            raise ValueError('Not a version 4 EBX partition')
        instances, exported, _, classes, _, _ = struct.unpack_from('<6H', data, 16)
        strings_size, arrays, data_size = struct.unpack_from('<3I', data, 28)
        self.guid = data[40:56].hex()
        at = 0x40
        self.imports = []
        for _ in range(imports):
            self.imports.append({'file': data[at:at + 16].hex(), 'object': data[at + 16:at + 32].hex()})
            at += 32
        self.local = []
        for _ in range(classes):
            g = data[at:at + 16].hex()
            if g not in types.by_guid:
                raise ValueError('EBX class %s is not in the type descriptors' % g)
            self.local.append(types.by_guid[g])
            at += 16
        groups = [struct.unpack_from('<HH', data, at + 4 * i) for i in range(instances)]
        at = (at + 4 * instances + 15) & ~15
        self.arrays = [struct.unpack_from('<3I', data, at + 12 * i) for i in range(arrays)]
        self.data_at = self.strings + strings_size
        self.arrays_at = self.data_at + data_size
        if self.arrays_at > len(data):
            raise ValueError('EBX sections exceed the partition')
        self.count = sum(n for _, n in groups)
        self.objects = []
        at = self.data_at
        for g, (cls, n) in enumerate(groups):
            if cls >= len(self.local):
                raise ValueError('EBX instance class out of range')
            klass = self.local[cls]
            c = types.classes[klass]
            for _ in range(n):
                at = (at + c['align'] - 1) // c['align'] * c['align']
                guid = None
                if g < exported:
                    guid = data[at:at + 16].hex()
                    at += 16
                # The 8-byte DataContainer header overlaps the instance GUID.
                if c['align'] != 4:
                    at += 8
                base = at - 8
                obj = self.object(klass, base, 0)
                obj['$guid'] = guid
                self.objects.append(obj)
                at = base + c['size']

    def text(self, offset):
        return '' if offset == 0xFFFFFFFF else cstring(self.d, self.strings + offset)

    def size(self, kind, ref):
        if kind == STRUCT:
            return self.types.classes[ref]['size']
        if kind not in SIZES:
            raise ValueError('EBX array of unsupported element type %d' % kind)
        return SIZES[kind]

    def value(self, kind, ref, at, depth):
        if depth > 48:
            raise ValueError('EBX nesting exceeded')
        d = self.d
        if kind in SCALARS:
            v = struct.unpack_from(SCALARS[kind], d, at)[0]
            return bool(v) if kind == 0x0A else v
        if kind in (CSTRING, FILEREF):
            return self.text(struct.unpack_from('<I', d, at)[0])
        if kind in (0x15, 0x16):
            return d[at:at + (16 if kind == 0x15 else 20)].hex()
        if kind == RESOURCE:
            return {'$resource': str(struct.unpack_from('<Q', d, at)[0])}
        if kind == POINTER:
            v = struct.unpack_from('<I', d, at)[0]
            if v >> 31:
                return {'$pointer': 2, 'index': v & 0x7FFFFFFF}
            if not v or v > self.count:
                return None
            return {'$pointer': 1, 'index': v - 1}
        if kind == STRUCT:
            return self.object(ref, at, depth + 1)
        if kind == ARRAY:
            index = struct.unpack_from('<I', d, at)[0]
            if index >= len(self.arrays):
                raise ValueError('EBX array index out of range')
            holder = self.types.classes[ref]
            if not holder['count']:
                return []
            _, mkind, mref, _ = self.types.fields[holder['first']]
            stride = self.size(mkind, mref)
            start, count = self.arrays_at + self.arrays[index][0], self.arrays[index][1]
            if count and start + count * stride > len(d):
                raise ValueError('EBX array exceeds partition')
            return [self.value(mkind, mref, start + i * stride, depth + 1) for i in range(count)]
        return None  # type references, boxed values, delegates: unused by the converter

    def fill(self, klass, at, out, depth):
        if depth > 48:
            raise ValueError('EBX inheritance too deep')
        c = self.types.classes[klass]
        for name, kind, ref, offset in self.types.fields[c['first']:c['first'] + c['count']]:
            if kind == INHERITED:
                if ref >= 0:
                    self.fill(ref, at, out, depth + 1)
                continue
            out[name] = self.value(kind, ref, at + offset, depth)

    def object(self, klass, at, depth):
        out = {'$type': self.types.classes[klass]['name']}
        self.fill(klass, at, out, depth)
        return out

    def document(self):
        return {'guid': self.guid, 'root_type': self.objects[0]['$type'] if self.objects else '',
                'imports': self.imports, 'objects': self.objects}


def load_types(game, key, names_path):
    files = initfs(game, key)
    names = json.loads(Path(names_path).read_text(encoding='utf-8-sig'))
    return Types(files['SharedTypeDescriptors.ebx'], files.get('SharedTypeDescriptors_patch.ebx'), names)


# ---------------- export ----------------
SHADER_DEPOT = 0x73312045
# BFN texture format ordinals that differ from the reader's (Skate) table: 63 is BC5 here,
# and 64 is BC6H, which the reader does not decode.
FORMATS = {63: 64, 64: 0xFFFF}


def texture_header(header, chunk_size):
    """A BFN 132-byte v12 texture header in the reader's 144-byte v12 layout, describing
    exactly the mips present in the chunk (bundles often carry only the lower mips)."""
    h = bytearray(header)
    fmt = struct.unpack_from('<I', h, 12)[0]
    width, height = struct.unpack_from('<HH', h, 22)
    mips, first = h[30], h[31]
    sizes = list(struct.unpack_from('<15I', h, 48))
    if chunk_size != sum(sizes[:mips]):
        if not first or chunk_size != sum(sizes[first:mips]):
            raise ValueError('Texture chunk size matches neither all mips nor the streamed tail')
        width, height, mips = max(1, width >> first), max(1, height >> first), mips - first
        sizes = sizes[first:] + [0] * first
    struct.pack_into('<I', h, 12, FORMATS.get(fmt, fmt))
    struct.pack_into('<HH', h, 22, width, height)
    h[30], h[31] = mips, 0
    struct.pack_into('<15I', h, 48, *sizes)
    struct.pack_into('<I', h, 108, sum(sizes[:mips]))
    return bytes(h[:32]) + b'\0' * 4 + bytes(h[32:]) + b'\0' * 8


def _pointers(value):
    """Every pointer object inside an EBX value."""
    if isinstance(value, dict):
        if '$pointer' in value:
            yield value
        for v in value.values():
            yield from _pointers(v)
    elif isinstance(value, list):
        for v in value:
            yield from _pointers(v)


# Documents whose imports and resources a level needs from other superbundles.
FOLLOW = {'RigidMeshAsset', 'CompositeMeshAsset', 'SkinnedMeshAsset', 'SurfaceShaderPreset', 'MeshVariationDatabase', 'TextureAsset', 'ObjectBlueprint',
          'SpatialPrefabBlueprint', 'PrefabBlueprint', 'LayerData', 'SubWorldData', 'DetachedSubWorldData',
          'WorldPartData', 'TerrainData'}


def shared_index(archive, jobs=4):
    """Every bundle's EBX by file GUID, resources by id, and chunks by GUID. An EBX file's GUID
    sits in its first CAS block, so only that block is decoded."""
    ebx, resources, chunks = {}, {}, {}
    for sb in archive.sbs:
        if '/loc/' in sb.lower():
            continue
        for entry in archive.toc(sb)['bundles'].values():
            try:
                listed = archive.bundle(entry)
            except (ValueError, struct.error, IndexError):
                continue
            for a in listed:
                if a['kind'] == 'ebx':
                    ebx.setdefault(a['sha1'], a)
                elif a['kind'] == 'resource':
                    resources.setdefault(a['resource_id'], a)
                else:
                    chunks.setdefault(a['guid'], a['ref'])

    def guid(a):
        ref = a['ref']
        path = archive.cas_path(ref)
        try:
            head = archive.read(path, ref[3], 8)
            size = struct.unpack_from('>I', head)[0] & 0xffffff
            code = struct.unpack_from('<H', head, 4)[0]
            packed = struct.unpack_from('>H', head, 6)[0] | ((code >> 8 & 15) << 16)
            first = archive.oodle.cas(archive.read(path, ref[3], 8 + (packed if code & 0x7f else size)))
        except (ValueError, OSError, struct.error):
            return None, a
        return (first[40:56].hex() if len(first) >= 56 and struct.unpack_from('<I', first)[0] == EBX_V4 else None), a
    by_guid = {}
    with ThreadPoolExecutor(max(1, min(8, jobs))) as pool:
        for g, a in pool.map(guid, sorted(ebx.values(), key=lambda a: a['ref'])):
            if g:
                by_guid.setdefault(g, a)
    print('BFN shared index: %d documents, %d resources, %d chunks' % (len(by_guid), len(resources), len(chunks)), flush=True)
    return {'ebx': by_guid, 'resources': resources, 'chunks': chunks}


def level_bundles(archive, package):
    leaf = package.rstrip('/').split('/')[-1].lower()
    sb = archive.levels().get(leaf)
    if not sb:
        raise ValueError('No Battle for Neighborville level named ' + package)
    prefix = 'win32/levels/%s/' % leaf
    return [e for k, e in sorted(archive.toc(sb)['bundles'].items()) if k.startswith(prefix)]


def export(game, package, output, key, names, jobs=4, bundles=None, extra_ebx=(), variation_dbs=()):
    """Export a level, or with `bundles` (bundle names) just those bundles, e.g. cosmetics.
    `extra_ebx` names further EBX files to take from whichever bundle lists them.
    `variation_dbs` names MeshVariationDatabases to add, trimmed to the exported meshes' entries
    (single props take their textures from their map's database)."""
    game, output = Path(game), Path(output)
    archive = Archive(game)
    types = load_types(game, key, names)
    output.mkdir(parents=True, exist_ok=True)
    inventory = {'profile': 'bfn', 'game': 'bfn', 'game_root': str(game), 'package': package,
                 'assets': [], 'errors': [], 'archive_policy': 'base-and-installed-patch'}
    seen, assets, chunk_refs = set(), [], {}
    if bundles is None:
        entries = level_bundles(archive, package)
    else:
        found = {k: e for sb in archive.sbs for k, e in archive.toc(sb)['bundles'].items()}
        entries = [found[b.lower()] for b in bundles if b.lower() in found]
        if not entries and not extra_ebx:
            raise ValueError('None of the requested bundles exist')
    for entry in entries:
        try:
            for asset in archive.bundle(entry):
                if asset['kind'] == 'chunk':
                    chunk_refs.setdefault(asset['guid'], asset['ref'])
                    continue
                key_ = (asset['kind'], asset.get('resource_id') or asset['name'].lower())
                if key_ in seen or asset.get('resource_type') == SHADER_DEPOT:
                    continue
                seen.add(key_)
                assets.append(asset)
        except (ValueError, struct.error, IndexError) as error:
            inventory['errors'].append({'bundle': entry[0], 'stage': 'bundle', 'error': str(error)})
    extra = ({n.lower() for n in extra_ebx} | {n.lower() for n in variation_dbs}) - {k[1] for k in seen if k[0] == 'ebx'}
    for sb in archive.sbs if extra else ():
        for entry in archive.toc(sb)['bundles'].values():
            if not extra:
                break
            try:
                listed = archive.bundle(entry)
            except (ValueError, struct.error, IndexError):
                continue
            for asset in listed:
                if asset['kind'] == 'ebx' and asset['name'].lower() in extra:
                    extra.discard(asset['name'].lower())
                    seen.add(('ebx', asset['name'].lower()))
                    assets.append(asset)
    for name in sorted(extra):
        inventory['errors'].append({'name': name, 'stage': 'ebx', 'error': 'Not listed in any bundle'})
    trimmed = {n.lower() for n in variation_dbs}
    later = [a for a in assets if a['kind'] == 'ebx' and a['name'].lower() in trimmed]
    assets = [a for a in assets if not (a['kind'] == 'ebx' and a['name'].lower() in trimmed)]
    print('BFN bundles read; assets:', len(assets), flush=True)

    wanted = {}  # chunk guid -> reason
    lock = threading.Lock()

    def work(asset):
        item = {k: v for k, v in asset.items() if k not in ('ref', 'original_size')}
        try:
            content = archive.fetch(asset['ref'])
            if len(content) != asset['original_size']:
                raise ValueError('Decoded size differs from the bundle manifest')
            if asset['kind'] == 'ebx':
                document = Ebx(content, types).document()
                item['document'] = 'ebx/%s.json' % asset['sha1']
                (output / item['document']).write_text(json.dumps(document), encoding='utf-8')
                item.update(root_type=document['root_type'], guid=document['guid'], bytes=len(content))
                return item
            rtype = asset.get('resource_type')
            if rtype == TEXTURE_TYPE and len(content) == 132:
                with lock:
                    wanted[content[32:48].hex()] = 'texture'
            elif rtype == MESHSET_TYPE and len(content) >= 0x50:
                lod = next((v for v in struct.unpack_from('<6Q', content, 0x20) if v), 0)
                if lod and lod + 0x70 <= len(content):
                    with lock:
                        wanted[content[lod + 0x60:lod + 0x70].hex()] = 'mesh'
            elif rtype == TERRAIN_TREE_TYPE:
                # Streamed terrain keeps its height pages in chunks the tile tree names.
                import bfn_terrain
                try:
                    tree = bfn_terrain.terrain_tree(content)
                except (ValueError, struct.error) as error:
                    with lock:
                        inventory['errors'].append({'name': asset['name'], 'stage': 'terrain', 'error': str(error)})
                else:
                    pending = [tree['root']]
                    while pending:
                        node = pending.pop()
                        pending.extend(node['children'])
                        if node.get('guid'):
                            with lock:
                                wanted[node['guid']] = 'terrain'
            item['path'] = 'resource/%s.bin' % asset['sha1']
            (output / item['path']).write_bytes(content)
            item['bytes'] = len(content)
        except (ValueError, struct.error, IndexError, KeyError, UnicodeError, OSError) as error:
            item['error'] = str(error)
            item.pop('bytes', None)
            with lock:
                inventory['errors'].append({'name': asset['name'], 'stage': asset['kind'], 'error': str(error)})
        return item

    for folder in ('ebx', 'resource', 'chunk'):
        (output / folder).mkdir(exist_ok=True)
    with ThreadPoolExecutor(max(1, min(8, jobs))) as pool:
        for i, item in enumerate(pool.map(work, assets)):
            inventory['assets'].append(item)
            if i % 2000 == 1999:
                print('BFN assets decoded:', i + 1, flush=True)

    # Variation databases, cut down to the exported meshes so they do not pull in a whole map.
    if later:
        meshes = {i['guid'] for i in inventory['assets'] if i.get('root_type') in ('RigidMeshAsset', 'CompositeMeshAsset', 'SkinnedMeshAsset')}
        for asset in later:
            try:
                document = Ebx(archive.fetch(asset['ref']), types).document()
            except (ValueError, struct.error, IndexError, KeyError) as error:
                inventory['errors'].append({'name': asset['name'], 'stage': 'ebx', 'error': str(error)})
                continue
            root = document['objects'][0]
            keep = []
            for entry in root.get('Entries', []) or []:
                ref = entry.get('Mesh') or {}
                if ref.get('$pointer') == 2 and document['imports'][ref['index']]['file'] in meshes:
                    keep.append(entry)
            root['Entries'] = keep
            used = {json.dumps(v) for e in keep for v in _pointers(e)}
            # Imports stay indexed by position; unused ones are blanked so they are not followed.
            document['imports'] = [imp if json.dumps({'$pointer': 2, 'index': i}) in used else {'file': '0' * 32, 'object': '0' * 32}
                                   for i, imp in enumerate(document['imports'])]
            item = {k: v for k, v in asset.items() if k not in ('ref', 'original_size')}
            item['document'] = 'ebx/%s.json' % asset['sha1']
            (output / item['document']).write_text(json.dumps(document), encoding='utf-8')
            item.update(root_type=document['root_type'], guid=document['guid'], bytes=asset['original_size'])
            inventory['assets'].append(item)

    # Shared dependencies: meshes, textures and prefabs a level imports from other superbundles.
    shared = None
    followed = set()
    for _ in range(8):
        have = {i['guid'] for i in inventory['assets'] if 'document' in i}
        need, resources = set(), set()
        for item in inventory['assets']:
            if 'document' not in item or item['guid'] in followed or item.get('root_type') not in FOLLOW:
                continue
            followed.add(item['guid'])
            document = json.loads((output / item['document']).read_text(encoding='utf-8'))
            need.update(i['file'] for i in document['imports'] if i['file'] not in have)
            root = document['objects'][0] if document['objects'] else {}
            for field in ('Resource', 'MeshSetResource', 'TerrainStreamingTreeResource'):
                rid = (root.get(field) or {}).get('$resource') if isinstance(root.get(field), dict) else None
                if rid and rid != '0':
                    resources.add(rid)
        resources -= {a.get('resource_id') for a in assets}
        if not need and not resources:
            break
        if shared is None:
            shared = shared_index(archive, jobs)
            chunk_refs.update((g, r) for g, r in shared['chunks'].items() if g not in chunk_refs)
        extra = [shared['ebx'][g] for g in sorted(need) if g in shared['ebx']]
        extra += [shared['resources'][r] for r in sorted(resources) if r in shared['resources']]
        extra = [a for a in extra if (a['kind'], a.get('resource_id') or a['name'].lower()) not in seen]
        if not extra:
            break
        for a in extra:
            seen.add((a['kind'], a.get('resource_id') or a['name'].lower()))
        assets.extend(extra)
        with ThreadPoolExecutor(max(1, min(8, jobs))) as pool:
            inventory['assets'].extend(pool.map(work, extra))
        print('BFN shared dependencies added:', len(extra), flush=True)

    # Geometry and full-resolution texture chunks: the TOC's streamed chunk when present,
    # otherwise the bundle's copy.
    def chunk_work(guid):
        ref = archive.chunk(guid) or chunk_refs.get(guid)
        if not ref:
            return guid, None, 'Chunk not found in any superbundle'
        try:
            data = archive.fetch(ref)
        except (ValueError, OSError) as error:
            return guid, None, str(error)
        return guid, data, None

    textures = {i['resource_id']: i for i in inventory['assets'] if i.get('resource_type') == TEXTURE_TYPE and 'bytes' in i}
    by_chunk = {}
    for item in textures.values():
        header = (output / item['path']).read_bytes()
        by_chunk.setdefault(header[32:48].hex(), []).append((item, header))
    with ThreadPoolExecutor(max(1, min(8, jobs))) as pool:
        for guid, data, error in pool.map(chunk_work, sorted(wanted)):
            if data is None:
                inventory['errors'].append({'name': guid, 'stage': 'chunk', 'error': error})
                continue
            path = 'chunk/%s.bin' % guid
            (output / path).write_bytes(data)
            inventory['assets'].append({'kind': 'chunk', 'name': guid, 'guid': guid, 'path': path, 'bytes': len(data)})
            for item, header in by_chunk.get(guid, []):
                try:
                    (output / item['path']).write_bytes(texture_header(header, len(data)))
                except ValueError as err:
                    item['error'] = str(err)
                    item.pop('bytes', None)
                    inventory['errors'].append({'name': item['name'], 'stage': 'texture', 'error': str(err)})
    print('BFN chunks:', len(wanted), flush=True)
    (output / 'inventory.json').write_text(json.dumps(inventory, indent=1), encoding='utf-8')
    return inventory


def main():
    import argparse
    p = argparse.ArgumentParser(description='Battle for Neighborville map export')
    p.add_argument('--game', required=True)
    p.add_argument('--map')
    p.add_argument('--out')
    p.add_argument('--key', help='initfs key: hex, or a Frosty key file (default: AURA_BFN_KEY)')
    p.add_argument('--names', help='class/field name table from frosty_types.ps1 (default: AURA_BFN_TYPES)')
    a = p.parse_args()
    if not (a.map and a.out):
        print(json.dumps(discover(a.game), indent=2))
        return 0
    names = a.names or os.environ.get('AURA_BFN_TYPES')
    if not names or not Path(names).is_file():
        raise SystemExit('Battle for Neighborville needs a type name table (--names, from frosty_types.ps1)')
    jobs = int(os.environ.get('AURA_IMPORT_JOBS', '4'))
    result = export(a.game, a.map, a.out, key_from(a.key), names, jobs)
    print('BFN assets:', len(result['assets']), 'errors:', len(result['errors']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
