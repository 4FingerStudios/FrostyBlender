"""Read-only Plants vs. Zombies: Garden Warfare (2014) support. Game files are never modified.

Garden Warfare 1 shares Garden Warfare 2's archive family (DB TOCs, inline binary
superbundles, Update/Patch delta bundles, self-describing EBX v2), so gw2.export reads it.
What differs is the texture resource: an older 128-byte header (resource type 0x5c4954a6)
with its own format ordinals. This module rewrites those headers into the reader's
144-byte v11 layout and fetches their full-resolution chunks from GW1's *Chunks TOCs.
Terrain trees parse with gw2_terrain; their heightfield tile chunks are fetched here too.
"""
import json
import re
import struct
from pathlib import Path

import gw2

EXE = 'PVZ.Main_Win64_Retail.exe'
GW1_TEXTURE, TEXTURE, MESHSET, TERRAIN_TREE = 0x5C4954A6, 0x6BDE20BA, 0x49B156D4, 0x22FE8AC8
# GW1 format ordinal -> the reader's (Skate) ordinal, from per-format bytes per pixel and texture names.
FORMATS = {0: 54, 3: 60, 4: 62, 19: 64, 20: 54, 11: 18}


def is_gw1(game):
    return (Path(game) / EXE).is_file()


def discover(game):
    game = Path(game)
    if not is_gw1(game):
        return []
    maps = {}
    for data in gw2.data_roots(game):
        levels = data / 'Win32/_pvz/Levels'
        if not levels.is_dir():
            continue
        for toc in sorted(levels.rglob('*.toc')):
            leaf = toc.stem.lower()
            if 'frontend' in leaf or not gw2.read_db(toc).get('bundles'):
                continue
            package = toc.relative_to(data).with_suffix('').as_posix()
            title = ' '.join(w.capitalize() for w in re.sub(r'^level_', '', leaf).split('_'))
            maps[package.lower()] = {'name': title, 'package': package,
                                     'mod': str(game), 'profile': 'gw1'}
    return sorted(maps.values(), key=lambda item: item['name'])


def texture_header(header, chunk_size):
    """The 128-byte GW1 header in the reader's 144-byte v11 layout, describing the mips in the chunk."""
    h = bytearray(header)
    fmt = struct.unpack_from('<I', h, 12)[0]
    width, height = struct.unpack_from('<HH', h, 18)
    mips, first = h[26], h[27]
    sizes = list(struct.unpack_from('<15I', h, 44))
    if chunk_size != sum(sizes[:mips]):
        # Bundles often carry only the low mips; describe what is there.
        for skip in range(1, mips):
            if chunk_size == sum(sizes[skip:mips]):
                width, height, mips = max(1, width >> skip), max(1, height >> skip), mips - skip
                sizes = sizes[skip:] + [0] * skip
                break
        else:
            raise ValueError('GW1 texture chunk size matches no mip tail')
    if fmt not in FORMATS:
        raise ValueError('Unsupported GW1 texture format %d' % fmt)
    struct.pack_into('<I', h, 12, FORMATS[fmt])
    struct.pack_into('<HH', h, 18, width, height)
    h[26], h[27] = mips, 0
    struct.pack_into('<15I', h, 44, *sizes)
    struct.pack_into('<I', h, 104, sum(sizes[:mips]))
    return bytes(h[:28]) + b'\0' * 4 + bytes(h[28:]) + b'\0' * 12


def stream_size(data):
    """Decoded size of a stream of compression blocks, from their headers."""
    at = total = 0
    while at + 8 <= len(data):
        decoded = struct.unpack_from('>I', data, at)[0] & 0xffffff
        kind = struct.unpack_from('<H', data, at + 4)[0]
        packed = struct.unpack_from('>H', data, at + 6)[0] | ((kind >> 8 & 15) << 16)
        at += 8 + (decoded if kind & 127 == 0 else packed)
        total += decoded
        if total > gw2.LIMIT:
            raise ValueError('GW1 streaming chunk exceeds output limit')
    return total


def export(game, package, output):
    game, output = Path(game), Path(output)
    return finish(game, output, gw2.export(game, package, output))


def finish(game, output, inventory):
    game, output = Path(game), Path(output)
    inventory['profile'] = 'gw1'
    from gw1_xenon import TEXTURE as XENON_TEXTURE, texture as xenon_texture
    xenon = any(a.get('resource_type') == XENON_TEXTURE or a.get('bundle', '').lower().startswith('xenon/')
                for a in inventory['assets'])
    inventory['platform'] = 'xenon' if xenon else 'win32'
    if xenon:
        # Cooked terrain shaders and character defaults live in anonymous shader
        # databases, not the stripped ShaderGraph EBX. Keep each database by name.
        existing = {a['name'] for a in inventory['assets'] if a.get('resource_type') == 0x36f3f2c0}
        for source, offset, size, asset in gw2.cached_dependency_index(game)['resource'].values():
            if asset.get('resource_type') != 0x36f3f2c0 or asset['name'] in existing:
                continue
            with source.open('rb') as stream:
                stream.seek(offset)
                content = gw2.decompress(gw2.Reader(stream.read(size)), asset['original_size'])
            entry = dict(asset, path='resource/%s.bin' % asset['sha1'], bytes=len(content))
            (output / entry['path']).parent.mkdir(parents=True, exist_ok=True)
            (output / entry['path']).write_bytes(content)
            inventory['assets'].append(entry)
            existing.add(asset['name'])
    index = {}
    for base in (root / 'Win32' for root in gw2.data_roots(game)):
        for toc in sorted(base.glob('*chunks.toc')):
            for chunk in gw2.read_db(toc).get('chunks', []):
                index[chunk['id']] = (toc.with_suffix('.sb'), chunk)
    chunks = {a['guid']: a for a in inventory['assets'] if a['kind'] == 'chunk'}

    shared = {}

    def fetch(guid):
        """A chunk from GW1's streaming TOCs, or failing that from any superbundle's bundles,
        added to (or replacing) the export's copy."""
        if guid in index:
            source, entry = index[guid]
            if entry['offset'] < 0 or not 0 < entry['size'] <= gw2.LIMIT:
                raise ValueError('Invalid GW1 streaming chunk range')
            with source.open('rb') as stream:
                stream.seek(entry['offset'])
                data = stream.read(entry['size'])
            content = gw2.decompress(gw2.Reader(data), stream_size(data))
        else:
            if not shared:
                shared.update(gw2.cached_dependency_index(game))
            location = shared['chunk'].get(guid)
            if location is None:
                raise ValueError('GW1 chunk %s is in no TOC or bundle' % guid)
            path, offset, size, source = location
            with path.open('rb') as stream:
                stream.seek(offset)
                content = gw2.decompress(gw2.Reader(stream.read(size)), source['original_size'])
        item = chunks.get(guid)
        if item is None:
            item = chunks[guid] = {'kind': 'chunk', 'name': guid, 'guid': guid, 'path': 'chunk/stream-%s.bin' % guid}
            inventory['assets'].append(item)
        (output / item['path']).parent.mkdir(parents=True, exist_ok=True)
        (output / item['path']).write_bytes(content)
        item['bytes'] = len(content)
        return item

    # Geometry: GW1 MeshSets name their chunk at LOD + 88 (GW2's reader looks at + 96).
    meshes = 0
    for asset in inventory['assets']:
        if asset.get('resource_type') != MESHSET or 'bytes' not in asset:
            continue
        mesh = (output / asset['path']).read_bytes()
        candidates = gw2.mesh_chunk_guids(mesh)
        if not candidates:
            continue
        guid = candidates[-1]
        if guid != '0' * 32 and 'bytes' not in chunks.get(guid, {}):
            try:
                fetch(guid)
                meshes += 1
            except (ValueError, OSError, struct.error) as error:
                inventory['errors'].append({'name': asset['name'], 'stage': 'mesh chunk', 'error': str(error)})
    print('GW1 mesh chunks:', meshes, flush=True)
    # Terrain: heightfield tile chunks named by each streaming tree's tile tree.
    from gw2_terrain import terrain_tree
    tiles = 0
    for asset in inventory['assets']:
        if asset.get('resource_type') != TERRAIN_TREE or 'bytes' not in asset:
            continue
        try:
            tree = terrain_tree((output / asset['path']).read_bytes())
        except (ValueError, struct.error) as error:
            inventory['errors'].append({'name': asset['name'], 'stage': 'terrain', 'error': str(error)})
            continue
        # Parsed now, so drop the archive reader's earlier complaint about this tree.
        inventory['errors'] = [e for e in inventory['errors'] if not (e.get('stage') == 'terrain' and e.get('name') == asset['name'])]
        for tile in tree['tiles']:
            item = chunks.get(tile['guid'], {})
            if not tile['size'] or item.get('bytes') == tile['size']:
                continue
            try:
                fetch(tile['guid'])
                tiles += 1
            except (ValueError, OSError, struct.error) as error:
                inventory['errors'].append({'name': tile['guid'], 'stage': 'terrain chunk', 'error': str(error)})
    print('GW1 terrain chunks:', tiles, flush=True)
    fetched = 0
    for asset in inventory['assets']:
        if asset.get('resource_type') not in (GW1_TEXTURE, XENON_TEXTURE) or 'bytes' not in asset:
            continue
        header = (output / asset['path']).read_bytes()
        is_xenon = asset['resource_type'] == XENON_TEXTURE
        guid = gw2.guid_big(header[28:44]) if is_xenon else header[28:44].hex()
        item = chunks.get(guid)
        full = sum(struct.unpack_from(('>' if is_xenon else '<') + '15I', header, 44)[:min(header[26], 15)])
        try:
            if item is None or item.get('bytes') != full:
                try:  # streaming TOCs first, then any bundle (menu art lives only in bundles)
                    item = fetch(guid)
                    fetched += 1
                except ValueError:
                    if item is None:
                        raise
            if item is None or 'bytes' not in item:
                raise ValueError('GW1 texture chunk unavailable')
            # The original header stays in place, so the fix-up can run again on the same export.
            if is_xenon:
                converted, linear = xenon_texture(header, (output / item['path']).read_bytes())
                # Keep original tiled chunks intact, so normalization can be rerun safely.
                item = dict(item, path='chunk/linear-%s.bin' % guid, bytes=len(linear))
                (output / item['path']).write_bytes(linear)
                chunks[guid] = item
                inventory['assets'].append(item)
            else:
                converted = texture_header(header, item['bytes'])
            asset['path'] = asset['path'][:-len('.bin')] + '.texture.bin'
            (output / asset['path']).write_bytes(converted)
            asset['resource_type'] = TEXTURE
            if is_xenon:
                asset['metadata'] = list(struct.pack('<4I', 11, 0, 0, 0))
                asset['bytes'] = len(converted)
            asset['source_format'] = struct.unpack_from(('>' if is_xenon else '<') + 'I', header, 12)[0]
        except (ValueError, OSError, struct.error) as error:
            asset['error'] = str(error)
            inventory['errors'].append({'name': asset['name'], 'stage': 'texture', 'error': str(error)})
    print('GW1 full texture chunks:', fetched, flush=True)
    (output / 'inventory.json').write_text(json.dumps(inventory, indent=2), encoding='utf-8')
    return inventory


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Garden Warfare 1 map export')
    parser.add_argument('--game', required=True)
    parser.add_argument('--map')
    parser.add_argument('--out')
    args = parser.parse_args()
    if args.map and args.out:
        result = export(args.game, args.map, args.out)
        print('GW1 assets:', len(result['assets']), 'errors:', len(result['errors']))
    else:
        print(json.dumps(discover(args.game), indent=2))
