"""Read-only Garden Warfare 2 archive support. Game files are never modified.

Legacy DB/SB layouts cross-checked against FrostyToolsuite 1.0.6.3's
DbReader, PVZDeobfuscator and BaseBinarySbReader. This module does not use
Skate's RIFF EBX reader or its Oodle DLL.
"""
import struct
import zlib
import json
import mmap
import threading
from concurrent.futures import ThreadPoolExecutor
from archive_parallel import worker_count, ordered_map
from pathlib import Path
try:
    import lz4.block as fast_lz4
except ImportError:
    fast_lz4 = None

LIMIT = 512 * 1024 * 1024


class Reader:
    def __init__(self, data, endian=None):
        self.data = data
        self.at = 0
        self.endian = endian

    def take(self, size):
        if size < 0 or size > len(self.data) - self.at:
            raise ValueError('Truncated GW2 archive')
        value = self.data[self.at:self.at + size]
        self.at += size
        return value

    def number(self, fmt):
        if self.endian and fmt[0] in '<>':
            fmt = self.endian + fmt[1:]
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def varint(self):
        value = 0
        for shift in range(0, 64, 7):
            byte = self.number('B')
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
        raise ValueError('Invalid GW2 variable integer')

    def string(self):
        end = self.data.find(b'\0', self.at)
        if end < 0 or end - self.at > 1048576:
            raise ValueError('Unterminated GW2 string')
        return self.take(end - self.at + 1)[:-1].decode('utf-8')

    def db(self, depth=0):
        if depth > 64:
            raise ValueError('GW2 DB nesting limit exceeded')
        tag = self.number('B')
        kind = tag & 31
        if not kind:
            return '', None
        name = '' if tag & 128 else self.string()
        if kind in (1, 2):
            size = self.varint()
            if size > len(self.data) - self.at:
                raise ValueError('GW2 DB container overrun')
            end = self.at + size
            value = [] if kind == 1 else {}
            while self.at < end:
                key, item = self.db(depth + 1)
                if self.at > end:
                    raise ValueError('GW2 DB child overrun')
                if item is None:
                    break
                if kind == 1:
                    value.append(item)
                else:
                    if key in value:
                        raise ValueError('Duplicate GW2 DB field')
                    value[key] = item
            self.at = end
        elif kind == 6:
            value = bool(self.number('B'))
        elif kind == 7:
            value = self.take(self.varint()).rstrip(b'\0').decode('utf-8')
        elif kind in (8, 9, 11, 12):
            value = self.number({8: '<i', 9: '<q', 11: '<f', 12: '<d'}[kind])
        elif kind in (15, 16):
            value = self.take(16 if kind == 15 else 20).hex()
        elif kind == 19:
            value = self.take(self.varint())
        else:
            raise ValueError(f'Unsupported GW2 DB type {kind}')
        return name, value


def read_db(path):
    data = Path(path).read_bytes()
    if len(data) > LIMIT:
        raise ValueError('GW2 DB exceeds input limit')
    magic = struct.unpack_from('<I', data)[0] if len(data) >= 4 else 0
    if magic in (0x01CED100, 0x03CED100):
        if len(data) < 0x22c:
            raise ValueError('Truncated GW2 DB envelope')
        key = bytes(b ^ 123 for b in data[0x128:0x229]) if magic == 0x01CED100 else None
        data = data[0x22c:]
        if key:
            data = bytes(b ^ key[i % 257] for i, b in enumerate(data))
    return Reader(data).db()[1]


def discover(game):
    game = Path(game)
    if not (game / 'GW2.Main_Win64_Retail.exe').is_file():
        return []
    maps = {}
    for data in data_roots(game):
        levels = data / 'Win32/levels'
        if not levels.is_dir():
            continue
        for toc in sorted(levels.rglob('*.toc')):
            if toc.stem == 'level_fe_splash':
                continue  # Front-end loading scene, not a static world.
            doc = read_db(toc)
            if not doc.get('bundles'):
                continue
            package = toc.relative_to(data).with_suffix('').as_posix()
            titles={'level_fe_hub':'Backyard Battleground','level_hub_gnomeunderworld':'Gnome Underworld',
                    'level_hub_tacobandits':'Taco Bandits Hub'}
            maps[package.lower()] = {
                'name': _title(game, toc.stem) or titles.get(toc.stem, toc.stem.replace('level_', '').replace('_', ' ').title()),
                'package': package, 'mod': str(game), 'profile': 'gw2', 'level': toc.stem,
            }
    import variants
    return sorted(variants.expand(game, list(maps.values())), key=lambda item: item['name'])


def _title(game, stem):
    """The level's name in the game's own English text, or None (an unreadable string table never stops
    the list)."""
    try:
        import loc
        return loc.level_title(game, stem)
    except Exception:  # noqa: BLE001 - names are a nicety
        return None


def guid_big(data):
    # Binary SB GUIDs are big endian; EBX and texture GUIDs use native order.
    return (data[:4][::-1] + data[4:6][::-1] + data[6:8][::-1] + data[8:]).hex()


def mesh_chunk_guids(data):
    """Geometry references in PC 64-bit and Xenon 32-bit MeshSet headers."""
    if len(data) < 40:
        return []
    lod = struct.unpack_from('<Q', data, 32)[0]
    if 0 < lod and lod + 112 <= len(data):
        return [data[lod + offset:lod + offset + 16].hex() for offset in (96, 88)]
    lod = struct.unpack_from('>I', data, 32)[0]
    return [guid_big(data[lod + 68:lod + 84])] if 0 < lod and lod + 84 <= len(data) else []


def bundle(data):
    r = Reader(data)
    header = [r.number('>I') for _ in range(9)]
    offset, magic, total, ne, nr, nc, strings, meta, meta_size = header
    if magic ^ 0x7065636e not in (0xed1cedbb, 0xed1cedb8):
        raise ValueError(f'Unsupported GW2 bundle magic {magic:08x}')
    if total != ne + nr + nc or total > 1000000 or offset + 4 > len(data):
        raise ValueError('Invalid GW2 bundle counts or size')
    hashes = [r.take(20).hex() for _ in range(total)]
    assets = []
    for kind, count in (('ebx', ne), ('resource', nr)):
        for _ in range(count):
            name_offset, size = r.number('>I'), r.number('>I')
            names = Reader(data)
            names.at = strings + 4 + name_offset
            name = names.string()
            assets.append({'kind': kind, 'name': name, 'original_size': size,
                           'sha1': hashes[len(assets)]})
    for asset in assets[ne:]:
        asset['resource_type'] = r.number('>I')
    for asset in assets[ne:]:
        asset['metadata'] = list(r.take(16))
    for asset in assets[ne:]:
        asset['resource_id'] = str(r.number('>Q'))
    for _ in range(nc):
        guid = guid_big(r.take(16))
        lo, size = r.number('>I'), r.number('>I')
        assets.append({'kind': 'chunk', 'guid': guid, 'name': guid,
                       # Xenon mip tails can start halfway through a 64 KiB block. The
                       # prefix and logical size may overlap in bit positions, so add them.
                       'original_size': (lo & 65535) + size,
                       'sha1': hashes[len(assets)]})
    r.at = offset + 4
    return assets, r


def asset_key(asset):
    """Anonymous resources (notably per-bundle shader DBs) share ID zero."""
    if asset['kind'] == 'resource':
        identity = asset.get('resource_id')
        if identity in (None, '', 0, '0'):
            identity = (asset.get('resource_type'), asset['name'])
    else:
        identity = asset.get('guid', asset['name'])
    return asset['kind'], identity


def lz4(data, size):
    if fast_lz4 is not None:
        try:
            result = fast_lz4.decompress(data, uncompressed_size=size)
        except fast_lz4.LZ4BlockError as error:
            raise ValueError('Invalid GW2 LZ4 block') from error
        if len(result) != size:
            raise ValueError('GW2 LZ4 output size mismatch')
        return result
    r = Reader(data)
    out = bytearray()
    def length(value):
        if value == 15:
            while True:
                extra = r.number('B')
                value += extra
                if extra < 255:
                    break
        return value
    while r.at < len(data):
        token = r.number('B')
        out.extend(r.take(length(token >> 4)))
        if len(out) > size:
            raise ValueError('GW2 LZ4 output overrun')
        if r.at == len(data):
            break
        distance = r.number('<H')
        count = length(token & 15) + 4
        if not distance or distance > len(out) or count > size - len(out):
            raise ValueError('Invalid GW2 LZ4 match')
        # LZ4 matches overlap: repeat the previous distance-byte sequence in
        # native bytearray operations instead of appending one byte in Python.
        pattern = out[-distance:]
        out.extend((pattern * ((count + distance - 1) // distance))[:count])
    if len(out) != size:
        raise ValueError('GW2 LZ4 output size mismatch')
    return bytes(out)


def decompress(r, size):
    if size > LIMIT:
        raise ValueError('GW2 asset exceeds output limit')
    out = bytearray()
    while len(out) < size:
        decoded = r.number('>I') & 0xffffff
        compression = r.number('<H')
        packed = r.number('>H') | ((compression >> 8 & 15) << 16)
        kind = compression & 127
        if not decoded or decoded > size - len(out):
            raise ValueError('Invalid GW2 compression block size')
        raw = r.take(decoded if kind == 0 else packed)
        if kind == 0:
            block = raw
        elif kind == 2:
            decoder = zlib.decompressobj()
            block = decoder.decompress(raw, decoded + 1)
            if not decoder.eof or decoder.unused_data:
                raise ValueError('Invalid GW2 zlib block')
        elif kind == 9:
            block = lz4(raw, decoded)
        else:
            raise ValueError(f'Unsupported GW2 compression codec {kind}')
        if len(block) != decoded:
            raise ValueError('GW2 decompressed size mismatch')
        out.extend(block)
    return bytes(out)


def block(r):
    if len(r.data) - r.at < 8:
        raise ValueError('Truncated GW2 patch compression block')
    size = struct.unpack_from('>I', r.data, r.at)[0] & 0xffffff
    # Patch edits can delete a range by inserting an empty raw CAS block.
    # Even with no payload, its eight-byte header must advance the stream.
    if size == 0:
        header = r.take(8)
        compression = struct.unpack_from('<H', header, 4)[0]
        packed = struct.unpack_from('>H', header, 6)[0] | ((compression >> 8 & 15) << 16)
        if compression & 127 or packed:
            raise ValueError('Invalid GW2 empty patch block')
        return b''
    return decompress(r, size)


def patch_bundle(base, delta):
    """Apply GW2's bounded metadata edits and CAS block delta instructions."""
    b, d = Reader(base), Reader(delta)
    d.take(8)
    command_size, data_size = d.number('>I'), d.number('>I')
    metadata = bytearray(d.take(4))
    if base:
        b.take(4)
    while d.at <= command_size + 15:
        tag = d.number('>I')
        kind, count = tag >> 24, tag & 0xffffff
        if kind == 0:
            metadata.extend(b.take(count))
        elif kind == 0x40:
            b.take(count)
        elif kind == 0x80:
            metadata.extend(d.take(count))
        else:
            raise ValueError('Unsupported GW2 metadata delta instruction')
        if len(metadata) > LIMIT:
            raise ValueError('GW2 patch metadata exceeds limit')
    if len(metadata) != struct.unpack_from('>I', metadata)[0] + 4:
        raise ValueError('GW2 patched metadata size mismatch')
    out = bytearray()
    while d.at < len(delta):
        tag = d.number('>I')
        kind, count = tag >> 28, tag & 0xfffffff
        if count > LIMIT:
            raise ValueError('GW2 patch instruction exceeds limit')
        if kind in (0, 3, 4):
            for _ in range(count):
                value = block(d if kind == 3 else b)
                if kind != 4:
                    out.extend(value)
        elif kind in (1, 2):
            original = Reader(block(b))
            edited = bytearray()
            if kind == 1:
                for _ in range(count):
                    offset, skip = d.number('>H'), d.number('>H')
                    edited.extend(original.take(offset - original.at))
                    edited.extend(block(d))
                    original.take(skip)
                edited.extend(original.take(len(original.data) - original.at))
            else:
                expected = d.number('>H') + 1
                end = d.at + count
                while d.at < end:
                    offset, skip, add = d.number('>H'), d.number('B'), d.number('B')
                    edited.extend(original.take(offset - original.at))
                    original.take(skip)
                    edited.extend(d.take(add))
                if d.at != end:
                    raise ValueError('GW2 byte delta instruction overrun')
                edited.extend(original.take(expected - len(edited)))
                if len(edited) != expected:
                    raise ValueError('GW2 byte delta output mismatch')
            out.extend(edited)
        else:
            raise ValueError('Unsupported GW2 CAS delta instruction')
        if len(metadata) + len(out) > LIMIT:
            raise ValueError('GW2 patched bundle exceeds limit')
    while b.at < len(base):
        out.extend(block(b))
        if len(metadata) + len(out) > LIMIT:
            raise ValueError('GW2 patched bundle exceeds limit')
    return bytes(metadata + out)


_PLATFORMS = {}


def _platform(toc):
    """'win32' or 'xenon' from a superbundle TOC's first bundle name (None for chunk-only TOCs)."""
    toc = Path(toc)
    if toc not in _PLATFORMS:
        try:
            bundles = (read_db(toc) or {}).get('bundles', []) if toc.is_file() else []
        except Exception:
            bundles = []
        _PLATFORMS[toc] = bundles[0]['id'].split('/')[0].lower() if bundles else None
    return _PLATFORMS[toc]


def patch_applies(game, toc):
    """False when the Update/Patch copy of a superbundle was made for another platform than its base, as in
    hybrid GW1 builds whose Xenon superbundles (Coastal, Characters, UI) sit beside PC ones under a PC patch:
    the patch names bundles 'win32/...' where the base has 'Xenon/...', and applying it would fail them.
    Chunk-only TOCs always apply (chunks are found by GUID)."""
    toc = Path(toc)
    try:
        relative = toc.relative_to(Path(game) / 'Data')
    except ValueError:
        relative = toc.relative_to(Path(game) / 'Update/Patch/Data')
    base, patch = _platform(Path(game) / 'Data' / relative), _platform(Path(game) / 'Update/Patch/Data' / relative)
    return not (base and patch and base != patch)


def data_roots(game):
    """The Data folders to read: the base and Update/Patch (check patch_applies per superbundle)."""
    game = Path(game)
    return [game / 'Data'] + ([game / 'Update/Patch/Data'] if (game / 'Update/Patch/Data').is_dir() else [])


def bundles_with_patches(game, toc):
    base_description = read_db(toc)
    entries = {entry['id'].lower(): entry for entry in base_description.get('bundles', [])}
    patch_toc = Path(game) / 'Update/Patch/Data' / toc.relative_to(Path(game) / 'Data')
    patched = read_db(patch_toc) if patch_toc.exists() and patch_applies(game, toc) else None
    descriptions = patched.get('bundles', []) if patched else list(entries.values())
    def read_entry(path, entry):
        if entry['offset'] < 0 or not 0 < entry['size'] <= LIMIT:
            raise ValueError('Invalid GW2 bundle file range')
        with path.with_suffix('.sb').open('rb') as stream:
            stream.seek(entry['offset'])
            data = stream.read(entry['size'])
        if len(data) != entry['size']:
            raise ValueError('Truncated GW2 bundle file range')
        return data
    for entry in descriptions:
        old = entries.get(entry['id'].lower())
        if entry.get('base') or not patched:
            if not old:
                raise ValueError('GW2 base bundle reference missing')
            yield entry, read_entry(toc, old), False
        elif entry.get('delta'):
            yield entry, patch_bundle(read_entry(toc, old) if old else b'', read_entry(patch_toc, entry)), True
        else:
            yield entry, read_entry(patch_toc, entry), False


def _index_toc(toc):
    indexes = {'ebx': {}, 'resource': {}, 'chunk': {}}
    guid_by_hash = {}
    doc = read_db(toc)
    descriptions = doc.get('bundles', [])
    if not descriptions:
        return indexes
    source = toc.with_suffix('.sb')
    with source.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
        for description in descriptions:
            start, size = description['offset'], description['size']
            if start < 0 or size < 36 or start + size > len(mapping):
                raise ValueError('Invalid GW2 dependency bundle range')
            metadata_size = struct.unpack_from('>I', mapping, start)[0] + 4
            if not 36 <= metadata_size <= min(LIMIT, size):
                raise ValueError('Invalid GW2 dependency metadata range')
            assets, metadata = bundle(mapping[start:start + metadata_size])
            at = start + metadata.at
            for asset in assets:
                begin, remaining = at, asset['original_size']
                while remaining:
                    if at + 8 > start + size:
                        raise ValueError('Truncated GW2 dependency block')
                    decoded = struct.unpack_from('>I', mapping, at)[0] & 0xffffff
                    kind = struct.unpack_from('<H', mapping, at + 4)[0]
                    packed = struct.unpack_from('>H', mapping, at + 6)[0] | ((kind >> 8 & 15) << 16)
                    if not decoded or decoded > remaining:
                        raise ValueError(f'Invalid dependency block length in {source.name}: {asset["name"]} '
                                         f'at {at:#x}, decoded {decoded}, remaining {remaining}')
                    at += 8 + (decoded if kind & 127 == 0 else packed)
                    remaining -= decoded
                    if at > start + size:
                        raise ValueError('GW2 dependency block overrun')
                kind = asset['kind']
                if kind == 'ebx':
                    sha = asset['sha1']
                    if sha not in guid_by_hash:
                        first = block(Reader(mapping[begin:at])) if at > begin else b''
                        guid_by_hash[sha] = (guid_big(first[40:56]) if first[:4] in (b'\x0f\xb2\xd1\xce', b'\x0f\xb4\xd1\xce')
                                             else first[40:56].hex()) if len(first) >= 56 else ''
                    key = guid_by_hash[sha]
                else:
                    key = asset.get('resource_id', asset.get('guid'))
                    if kind == 'resource' and key == '0':
                        key = 'anonymous:%08x:%s' % (asset['resource_type'], asset['name'])
                if not key:
                    continue
                old = indexes[kind].get(key)
                if old is None or asset['original_size'] > old[3]['original_size']:
                    indexes[kind][key] = (source, begin, at - begin, asset)
    return indexes


def dependency_index(game, jobs=None):
    """Scan independent superbundle mappings concurrently, merge in TOC order."""
    indexes = {'ebx': {}, 'resource': {}, 'chunk': {}}
    tocs = sorted((Path(game) / 'Data/Win32').rglob('*.toc'))
    for partial in ordered_map(_index_toc, tocs, jobs):
        for kind, entries in partial.items():
            for key, value in entries.items():
                old = indexes[kind].get(key)
                if old is None or value[3]['original_size'] > old[3]['original_size']:
                    indexes[kind][key] = value
    print('GW2 shared dependency index:', {key: len(value) for key, value in indexes.items()}, flush=True)
    return indexes


_DEPENDENCY_MEMORY = {}


def cached_dependency_index(game, jobs=None):
    """Index cached in memory and on disk. TOC and SB size/time changes rebuild
    it. Set AURA_IMPORT_CACHE to choose its disk folder."""
    import hashlib
    import os
    import pickle
    tocs = sorted((Path(game) / 'Data/Win32').rglob('*.toc'))
    files = tocs + [t.with_suffix('.sb') for t in tocs if t.with_suffix('.sb').is_file()]
    key = hashlib.sha256(repr(['parallel-v4', str(Path(game).resolve())] + [(str(t), t.stat().st_size, t.stat().st_mtime_ns)
                                                              for t in files]).encode()).hexdigest()[:24]
    if key in _DEPENDENCY_MEMORY:
        return _DEPENDENCY_MEMORY[key]
    folder = Path(os.environ.get('AURA_IMPORT_CACHE') or Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'aura-frostbite')
    path = folder / ('dependencies_%s.pickle' % key)
    try:
        with path.open('rb') as stream:
            indexes = pickle.load(stream)
        _DEPENDENCY_MEMORY.clear()
        _DEPENDENCY_MEMORY[key] = indexes
        print('GW2 shared dependency index (cached):', {k: len(v) for k, v in indexes.items()}, flush=True)
        return indexes
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError):
        pass
    indexes = dependency_index(game, jobs)
    _DEPENDENCY_MEMORY.clear()
    _DEPENDENCY_MEMORY[key] = indexes
    temporary = None
    try:
        import uuid
        folder.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        with temporary.open('wb') as stream:
            pickle.dump(indexes, stream, protocol=pickle.HIGHEST_PROTOCOL)
        temporary.replace(path)
    except OSError:
        pass  # a cache that cannot be written only costs time
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return indexes


def complete_dependencies(game, output, inventory, documents=None, resource_data=None, jobs=None):
    existing = {a.get('guid') for a in inventory['assets'] if 'document' in a}
    if documents is None:
        def read_document(asset):
            return asset, json.loads((Path(output) / asset['document']).read_text())
        documents = list(ordered_map(read_document, (a for a in inventory['assets'] if 'document' in a), jobs))
    follow = {'WorldPartData', 'ObjectBlueprint', 'SpatialPrefabBlueprint', 'LogicPrefabBlueprint', 'SubWorldData',
              'RigidMeshAsset', 'CompositeMeshAsset', 'SkinnedMeshAsset', 'MeshVariationDatabase', 'TextureAsset',
              'AtlasTextureAsset',
              'TerrainData'}
    resources = {a['resource_id'] for a in inventory['assets'] if a['kind'] == 'resource'}
    # Texture assets import nothing, so a missing resource also needs the index.
    missing = any(doc['root_type'] in ('TextureAsset', 'AtlasTextureAsset') and (doc['objects'][0].get('Resource') or {}).get('$resource') not in resources
                  for asset, doc in documents if doc['objects'])
    if not missing and not any(ref['file'] not in existing for asset, doc in documents
                               if doc['root_type'] in follow for ref in doc['imports']):
        return
    indexes = cached_dependency_index(game, jobs)
    chunks = {a['guid'] for a in inventory['assets'] if a['kind'] == 'chunk'}
    def obtain(kind, key):
        location = indexes[kind].get(key)
        if location is None:
            return None
        path, offset, size, source = location
        with path.open('rb') as stream:
            stream.seek(offset)
            content = decompress(Reader(stream.read(size)), source['original_size'])
        asset = dict(source)
        asset['path'] = f"{kind}/{source['sha1']}.bin"
        target = Path(output) / asset['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        if asset.get('resource_type') == 0x6bde20ba and len(content) == 132:
            content = content[:28] + b'\0' * 4 + content[28:] + b'\0' * 8
            if struct.unpack_from('<I', content, 12)[0] == 63:
                asset['source_format'] = 63
                content = content[:12] + struct.pack('<I', 64) + content[16:]
        target.write_bytes(content)
        asset['bytes'] = len(content)
        if kind == 'resource':
            cache_resource(resource_data, asset, content)
        inventory['assets'].append(asset)
        return asset, content
    tried = set(existing)
    for asset, doc in documents:
        if doc['root_type'] not in follow:
            continue
        for ref in doc['imports']:
            guid = ref['file']
            if guid in tried:
                continue
            tried.add(guid)
            found = obtain('ebx', guid)
            if found:
                item, content = found
                decoded = Ebx(content).document()
                item['document'] = str(Path(item['path']).with_suffix('.json')).replace('\\', '/')
                (Path(output) / item['document']).write_text(json.dumps(decoded), encoding='utf-8')
                item['root_type'], item['guid'] = decoded['root_type'], decoded['guid']
                documents.append((item, decoded))
        root = doc['objects'][0]
        field = 'Resource' if doc['root_type'] in ('TextureAsset', 'AtlasTextureAsset') else 'TerrainStreamingTreeResource' if doc['root_type'] == 'TerrainData' else 'MeshSetResource'
        resource = root.get(field, {}).get('$resource')
        if resource and resource not in resources:
            resources.add(resource)
            found = obtain('resource', resource)
            if found and doc['root_type'] in ('RigidMeshAsset', 'CompositeMeshAsset', 'SkinnedMeshAsset'):
                mesh = found[1]
                candidates = mesh_chunk_guids(mesh)
                guid = next((g for g in candidates if g in indexes['chunk'] or g in chunks), '')
                if guid and guid not in chunks:
                    chunks.add(guid)
                    obtain('chunk', guid)
    print('GW2 referenced assets:', len(inventory['assets']), flush=True)


class Ebx:
    """Self-describing legacy EBX v2/v4 (GW2 uses v2)."""
    def __init__(self, data):
        self.endian = '>' if data[:4] in (b'\x0f\xb2\xd1\xce', b'\x0f\xb4\xd1\xce') else '<'
        self.r = Reader(data, self.endian)
        r = self.r
        magic = r.number('<I')
        if magic not in (0x0fb2d1ce, 0x0fb4d1ce):
            raise ValueError('Unsupported GW2 EBX version')
        self.version = magic
        self.strings, length, imports = (r.number('<I') for _ in range(3))
        instances, exported, unique, classes, fields, names_size = (r.number('<H') for _ in range(6))
        strings_size, arrays, data_size = (r.number('<I') for _ in range(3))
        self.arrays_offset = self.strings + strings_size + data_size
        self.file_guid = self.guid(r.take(16))
        if magic == 0x0fb4d1ce:
            r.take(8)
        else:
            self.align(16)
        if imports > len(data) // 32 or self.strings + length > len(data):
            raise ValueError('Invalid GW2 EBX lengths')
        self.imports = [{'file': self.guid(r.take(16)), 'object': self.guid(r.take(16))} for _ in range(imports)]
        names = {}
        end = r.at + names_size
        while r.at < end:
            name = r.string()
            h = 5381
            for b in name.encode('utf-8'):
                h = ((h * 33) ^ b) & 0xffffffff
            names[h] = name
        self.fields = []
        for _ in range(fields):
            h, flags, ref, offset, extra = struct.unpack(self.endian + 'IHHII', r.take(16))
            kind = ((flags >> (1 if magic == 0x0fb4d1ce else 0)) >> 4) & 31
            self.fields.append((names.get(h, f'field_{h:08x}'), kind, ref, offset))
        self.classes = []
        for _ in range(classes):
            h, index, count, alignment, flags, size, extra = struct.unpack(self.endian + 'IIBBHHH', r.take(16))
            alignment &= 127  # High bit marks descriptors such as GW2 enums.
            if not alignment or alignment & (alignment - 1) or index + count > fields:
                raise ValueError('Invalid GW2 EBX class descriptor')
            self.classes.append((names.get(h, f'class_{h:08x}'), index, count, alignment, size))
        self.instances = [(r.number('<H'), r.number('<H'), i < exported) for i in range(instances)]
        self.align(16)
        self.arrays = [struct.unpack(self.endian + 'III', r.take(12)) for _ in range(arrays)]
        r.at = self.strings + strings_size
        self.objects = []
        for ref, count, is_exported in self.instances:
            for _ in range(count):
                cls = self.classes[ref]
                self.align(cls[3])
                guid = self.guid(r.take(16)) if is_exported else '0' * 32
                if cls[3] != 4:
                    r.take(8)
                base = r.at - 8
                obj = self.object(ref, base, 0)
                obj['$guid'] = guid
                self.objects.append(obj)
                r.at = base + cls[4]
                self.align(cls[3])

    def guid(self, data):
        return guid_big(data) if self.endian == '>' else data.hex()

    def align(self, alignment):
        self.r.at = (self.r.at + alignment - 1) & -alignment

    def object(self, ref, base, depth):
        if depth > 64 or ref >= len(self.classes):
            raise ValueError('Invalid GW2 EBX class reference')
        name, first, count, alignment, size = self.classes[ref]
        obj = {'$type': name}
        for field in self.fields[first:first + count]:
            key, kind, target, offset = field
            if kind == 0:
                inherited = self.object(target, base, depth + 1)
                inherited.pop('$type', None)
                obj.update(inherited)
            else:
                self.r.at = base + offset
                obj[key] = self.value(kind, target, depth + 1)
        self.r.at = base + size
        return obj

    def value(self, kind, ref, depth):
        r = self.r
        if kind == 2:
            self.align(self.classes[ref][3])
            return self.object(ref, r.at, depth)
        if kind == 3:
            index = r.number('<I')
            if index & 0x80000000:
                return {'$pointer': 2, 'index': index & 0x7fffffff}
            return {'$pointer': 1, 'index': index - 1} if index else {'$pointer': 0}
        if kind == 4:
            index = r.number('<I')
            if index >= len(self.arrays):
                raise ValueError('Invalid GW2 EBX array index')
            offset, count, target = self.arrays[index]
            if count > 1000000:
                raise ValueError('GW2 EBX array count exceeds limit')
            saved = r.at
            r.at = self.arrays_offset + offset
            cls = self.classes[ref]
            field = self.fields[cls[1]]
            result = [self.value(field[1], field[2], depth + 1) for _ in range(count)]
            r.at = saved
            return result
        if kind == 6:
            return r.take(32).split(b'\0')[0].decode('utf-8')
        if kind in (7, 9, 25):
            offset = r.number('<Q' if kind == 9 else '<I')
            if offset == 0xffffffff or offset == 0xffffffffffffffff:
                return ''
            saved = r.at
            r.at = self.strings + (offset & 0xffffffff)
            value = r.string()
            r.at = saved
            return value
        if kind == 23:
            return {'$resource': str(r.number('<Q'))}
        if kind in (21, 22):
            return self.guid(r.take(16)) if kind == 21 else r.take(20).hex()
        if kind == 26:
            return {'$boxed': r.take(16).hex()}
        formats = {8: '<i', 10: 'B', 11: 'b', 12: 'B', 13: '<h', 14: '<H', 15: '<i',
                   16: '<I', 17: '<Q', 18: '<q', 19: '<f', 20: '<d'}
        if kind not in formats:
            raise ValueError(f'Unsupported GW2 EBX field type {kind}')
        value = r.number(formats[kind])
        return bool(value) if kind == 10 else value

    def document(self):
        return {'guid': self.file_guid, 'endian': self.endian, 'root_type': self.objects[0]['$type'] if self.objects else '',
                'imports': self.imports, 'objects': self.objects}


def skip_asset(reader, size, decoded=False):
    """Advance without inflating duplicate data; validate every block's range."""
    if not 0 <= size <= LIMIT:
        raise ValueError('GW2 asset exceeds output limit')
    if decoded:
        if size > len(reader.data) - reader.at:
            raise ValueError('Truncated decoded GW2 asset')
        reader.at += size
        return
    remaining = size
    while remaining:
        if reader.at + 8 > len(reader.data):
            raise ValueError('Truncated GW2 compression header')
        decoded_size = struct.unpack_from('>I', reader.data, reader.at)[0] & 0xffffff
        compression = struct.unpack_from('<H', reader.data, reader.at + 4)[0]
        packed = struct.unpack_from('>H', reader.data, reader.at + 6)[0] | ((compression >> 8 & 15) << 16)
        if not decoded_size or decoded_size > remaining or compression & 127 not in (0, 2, 9):
            raise ValueError('Invalid GW2 compression block')
        end = reader.at + 8 + (decoded_size if compression & 127 == 0 else packed)
        if end > len(reader.data):
            raise ValueError('Truncated GW2 compressed payload')
        reader.at = end
        remaining -= decoded_size


_RESOURCE_HEADERS = {0x6bde20ba, 0x49b156d4, 0x22fe8ac8, 0x5c4954a6, 0xacd91fe8}


def cache_resource(cache, asset, content):
    if cache is not None and asset.get('resource_type') in _RESOURCE_HEADERS and len(content) <= 4*1024*1024:
        cache[asset['path']] = content


def _export_asset(task):
    output, asset, bundle_id, raw, start, decoded, lock = task
    reader = Reader(raw)
    reader.at = start
    content = reader.take(asset['original_size']) if decoded else decompress(reader, asset['original_size'])
    item = dict(asset)
    if asset['kind'] == 'resource' and asset.get('resource_type') == 0x6bde20ba and len(content) == 132:
        content = content[:28] + b'\0'*4 + content[28:] + b'\0'*8
        if struct.unpack_from('<I', content, 12)[0] == 63:
            item['source_format'] = 63
            content = content[:12] + struct.pack('<I', 64) + content[16:]
    name = asset['sha1']
    item.update(path=f"{asset['kind']}/{name}.bin", bytes=len(content), bundle=bundle_id)
    document = None
    if asset['kind'] == 'ebx':
        try:
            document = Ebx(content).document()
            item.update(document=f'ebx/{name}.json', root_type=document['root_type'], guid=document['guid'])
        except (ValueError, IndexError, struct.error, UnicodeError) as error:
            item['error'] = str(error)
    # Distinct anonymous assets can share a payload hash/path. Serialize those
    # writes, retaining every inventory entry and deterministic source priority.
    with lock:
        (output / item['path']).write_bytes(content)
        if document is not None:
            (output / item['document']).write_text(json.dumps(document), encoding='utf-8')
    cached = content if item.get('resource_type') in _RESOURCE_HEADERS and len(content) <= 4*1024*1024 else None
    return item, document, cached


def export(game, package, output, jobs=None, resource_data=None):
    game, output = Path(game), Path(output)
    data_root = (game / 'Data').resolve()
    toc = (data_root / (package + '.toc')).resolve()
    if not toc.is_relative_to(data_root) or not toc.is_file():
        raise ValueError('Invalid GW2 map package')
    output.mkdir(parents=True, exist_ok=True)
    for kind in ('ebx', 'resource', 'chunk'):
        (output / kind).mkdir(exist_ok=True)
    inventory = {'profile': 'gw2', 'game': str(game), 'package': package,
                 'assets': [], 'errors': [], 'archive_policy': 'base-and-installed-patch'}
    seen, locks, documents = set(), {}, []
    resource_data = resource_data if resource_data is not None else {}
    workers = worker_count(jobs)
    print('GW2 archive workers:', workers, flush=True)
    duplicates = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='frostbite-archive') as pool:
        for source in [game / 'Data/Win32/globals.toc', game / 'Data/Win32/shared_levels.toc', toc]:
            if not source.is_file():
                continue
            for description, raw, decoded in bundles_with_patches(game, source):
                assets, reader = bundle(raw)
                def tasks():
                    nonlocal duplicates
                    for asset in assets:
                        start = reader.at
                        skip_asset(reader, asset['original_size'], decoded)
                        key = asset_key(asset)
                        if key in seen:
                            duplicates += 1
                            continue
                        seen.add(key)
                        lock = locks.setdefault((asset['kind'], asset['sha1']), threading.Lock())
                        yield output, asset, description['id'], raw, start, decoded, lock
                for item, document, content in ordered_map(_export_asset, tasks(), workers, pool):
                    inventory['assets'].append(item)
                    if document is not None:
                        documents.append((item, document))
                    if content is not None:
                        cache_resource(resource_data, item, content)
                    if 'error' in item:
                        inventory['errors'].append({'name': item['name'], 'stage': 'ebx', 'error': item['error']})
                print('GW2 bundle:', description['id'], 'unique assets:', len(inventory['assets']), flush=True)
    print('GW2 duplicate assets skipped without decompression:', duplicates, flush=True)
    complete_dependencies(game, output, inventory, documents, resource_data, workers)
    complete_texture_chunks(game, output, inventory, resource_data=resource_data, jobs=workers)
    (output / 'inventory.json').write_text(json.dumps(inventory, indent=2), encoding='utf-8')
    return inventory


def complete_texture_chunks(game, output, inventory, terrain_only=False, resource_data=None, jobs=None):
    """Replace bundle low-mip data with full chunks from the base streaming SBs."""
    index = {}
    for base in (root / 'Win32' for root in data_roots(game)):
        for toc in sorted(base.glob('*chunks.toc')):
            for chunk in read_db(toc).get('chunks', []):
                index[chunk['id']] = (toc.with_suffix('.sb'), chunk)
    chunks = {a['guid']: a for a in inventory['assets'] if a['kind'] == 'chunk'}
    wanted = set()
    for asset in inventory['assets']:
        if asset.get('resource_type') == 0x22fe8ac8:
            from gw2_terrain import terrain_tree
            try:
                tree = terrain_tree(resource_data[asset['path']] if resource_data is not None and asset['path'] in resource_data else (Path(output) / asset['path']).read_bytes())
            except (ValueError, IndexError, struct.error) as error:
                # An unreadable terrain tree is reported, not fatal; the rest of the map still imports.
                inventory['errors'].append({'name': asset['name'], 'stage': 'terrain', 'error': str(error)})
                continue
            wanted.update(tile['guid'] for tile in tree['tiles'] if tile['size'])
        elif terrain_only:
            continue
        elif asset.get('resource_type') == 0x6bde20ba:
            header = resource_data[asset['path']] if resource_data is not None and asset['path'] in resource_data else (Path(output) / asset['path']).read_bytes()
            if len(header) == 144:
                wanted.add(header[32:48].hex())
        elif asset.get('resource_type') == 0x49b156d4:
            mesh = resource_data[asset['path']] if resource_data is not None and asset['path'] in resource_data else (Path(output) / asset['path']).read_bytes()
            if len(mesh) >= 128:
                lod = struct.unpack_from('<Q', mesh, 32)[0]
                if lod and lod + 112 <= len(mesh):
                    wanted.add(mesh[lod + 96:lod + 112].hex())
    for guid in sorted(wanted):
        if guid not in index:
            continue
        source, entry = index[guid]
        if entry['offset'] < 0 or not 0 < entry['size'] <= LIMIT:
            raise ValueError('Invalid GW2 streaming chunk range')
        with source.open('rb') as stream:
            stream.seek(entry['offset'])
            data = stream.read(entry['size'])
        r = Reader(data)
        total = 0
        while r.at < len(data):
            decoded = r.number('>I') & 0xffffff
            kind = r.number('<H')
            packed = r.number('>H') | ((kind >> 8 & 15) << 16)
            r.take(decoded if kind & 127 == 0 else packed)
            total += decoded
            if total > LIMIT:
                raise ValueError('GW2 streaming chunk exceeds output limit')
        content = decompress(Reader(data), total)
        item = chunks.get(guid)
        if item is None:
            item = {'kind': 'chunk', 'name': guid, 'guid': guid,
                    'path': f'chunk/stream-{guid}.bin'}
            inventory['assets'].append(item)
        target = Path(output) / item['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        item['bytes'] = item['original_size'] = len(content)
        item['streaming_source'] = str(source)
    print('GW2 complete streaming chunks:', len(wanted & index.keys()), flush=True)
    # Chunks of shared textures (and props) pulled in as dependencies are kept in bundles, not
    # streaming superbundles: take them from the dependency index.
    missing = sorted(g for g in wanted - index.keys() if 'bytes' not in chunks.get(g, {}))
    if missing:
        shared = cached_dependency_index(game, jobs)['chunk']
        found = 0
        for guid in missing:
            location = shared.get(guid)
            if location is None:
                continue
            path, offset, size, source = location
            with path.open('rb') as stream:
                stream.seek(offset)
                content = decompress(Reader(stream.read(size)), source['original_size'])
            item = chunks.get(guid)
            if item is None:
                item = chunks[guid] = {'kind': 'chunk', 'name': guid, 'guid': guid, 'path': f'chunk/{guid}.bin'}
                inventory['assets'].append(item)
            target = Path(output) / item['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            item['bytes'] = item['original_size'] = len(content)
            found += 1
        print('GW2 bundle chunks from the shared index:', found, flush=True)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--game', required=True)
    parser.add_argument('--map')
    parser.add_argument('--out')
    parser.add_argument('--jobs', type=int, help='Archive workers (default: AURA_IMPORT_JOBS or up to 8)')
    args = parser.parse_args()
    if args.map and args.out:
        result = export(args.game, args.map, args.out, args.jobs)
        print('GW2 assets:', len(result['assets']), 'EBX errors:', len(result['errors']))
    else:
        print(json.dumps(discover(args.game), indent=2))
