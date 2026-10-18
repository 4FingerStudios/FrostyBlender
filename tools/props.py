"""Single props (static meshes) from the maps of Garden Warfare 1 and 2 and Battle for Neighborville.

Props are listed per map from the mesh assets in that map's bundles. An import takes only the
chosen meshes, what they import (materials, textures, geometry) and, from the map's mesh
variation databases, just the entries for those meshes, so it does not pull in the whole map.
Imported props are laid out in a row along X, each in its own collection.

    python props.py discover --game GAME --out props.json
    python props.py export --game GAME --items items.json --out EXPORT [--names BFN_TYPES]
    python props.py convert --export EXPORT --textures TEXTURES --assets ASSETS --out scene.json

BFN also needs AURA_BFN_KEY, as for maps. Game files are only read.
"""
import argparse
import json
import os
import re
import struct
import sys
from pathlib import Path

import numpy as np

import gw2
from cosmetics import game_kind, title

# Mesh assets that are not props: effects, light helpers, characters, collision and proxies.
NOT_PROPS = re.compile(r'(meshparticle|/fx/|(^|/|_)vfx_|shadowcaster|/lighting/|animations/characters|/characters/|_proxy|proxy_|'
                       r'collision|occluder|/csg/|meshscatter|scattermesh|_lod\d|skybox|skydome|cloudsgeo|water\.mesh|'
                       r'/gameplay/|/ui/)')
MESH_TYPES = ('RigidMeshAsset', 'CompositeMeshAsset', 'SkinnedMeshAsset')


def _prop(name, bundle, sb=''):
    leaf = name.split('/')[-1]
    leaf = re.sub(r'_mesh$', '', leaf, flags=re.I)
    group = '/'.join(name.split('/')[-3:-1]) or 'props'
    return {'name': title(leaf), 'group': group, 'asset': name, 'bundle': bundle, 'sb': sb}


def _gw_level_meshes(game, package):
    """Mesh EBX names in a GW1/GW2 level's bundles, read from bundle headers only."""
    game = Path(game)
    out = {}
    for root in gw2.data_roots(game):
        toc = root / (package + '.toc')
        if not toc.is_file():
            continue
        with toc.with_suffix('.sb').open('rb') as stream:
            for b in gw2.read_db(toc).get('bundles', []):
                if b.get('delta') or b.get('base'):
                    continue  # patch deltas need their base; the base TOC already lists those bundles
                try:
                    stream.seek(b['offset'])
                    size = struct.unpack('>I', stream.read(4))[0] + 4
                    if not 36 <= size <= min(gw2.LIMIT, b['size']):
                        continue
                    stream.seek(b['offset'])
                    assets, _ = gw2.bundle(stream.read(size))
                except (ValueError, OSError, struct.error):
                    continue
                for a in assets:
                    if a['kind'] == 'ebx' and a['name'].lower().endswith('_mesh'):
                        out.setdefault(a['name'].lower(), (a['name'], b['id']))
    return out


def discover(game):
    kind = game_kind(game)
    maps = []
    if kind == 'bfn':
        import bfn
        archive = bfn.Archive(game)
        for entry in bfn.discover(game):
            meshes, dbs = {}, set()
            for bundle in bfn.level_bundles(archive, entry['package']):
                try:
                    listed = archive.bundle(bundle)
                except (ValueError, struct.error, IndexError):
                    continue
                for a in listed:
                    low = a['name'].lower() if a['kind'] == 'ebx' else ''
                    if low.endswith('_mesh'):
                        meshes.setdefault(low, (a['name'], bundle[0]))
                    elif low.endswith('meshvariationdb_win32'):
                        dbs.add(a['name'])
            maps.append((entry, meshes, sorted(dbs)))
    else:
        module = __import__('gw1' if kind == 'gw1' else 'gw2')
        for entry in module.discover(game):
            meshes = _gw_level_meshes(game, entry['package'])
            # EBX names drop the bundle ID's platform folder.
            dbs = sorted({re.sub(r'^(?:win32|xenon)/', '', b, flags=re.I) + '/meshvariationdb_' + b.split('/')[0].lower()
                          for _, b in meshes.values()})
            maps.append((entry, meshes, dbs))
    result = []
    for entry, meshes, dbs in maps:
        props = [_prop(name, bundle) for low, (name, bundle) in meshes.items() if not NOT_PROPS.search(low)]
        if props:
            props.sort(key=lambda p: (p['group'], p['name']))
            result.append({'name': entry['name'], 'package': entry['package'], 'props': props, 'variation_dbs': dbs})
    result.sort(key=lambda m: m['name'])
    return {'game': kind, 'maps': result}


# ---------------- export ----------------
def _trim_variations(document, meshes):
    """Keep only the database entries for `meshes` (file GUIDs), blanking the imports they don't use."""
    root = document['objects'][0]
    keep = []
    for entry in root.get('Entries', []) or []:
        ref = entry.get('Mesh') or {}
        if ref.get('$pointer') == 2 and document['imports'][ref['index']]['file'] in meshes:
            keep.append(entry)
    root['Entries'] = keep
    used = set()

    def walk(value):
        if isinstance(value, dict):
            if value.get('$pointer') == 2:
                used.add(value['index'])
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(keep)
    document['imports'] = [imp if i in used else {'file': '0' * 32, 'object': '0' * 32}
                           for i, imp in enumerate(document['imports'])]
    return len(keep)


def gw_export(game, kind, items, variation_dbs, output):
    game, output = Path(game), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    index = gw2.cached_dependency_index(game)
    by_name = {location[3]['name'].lower(): location for location in index['ebx'].values()}
    inventory = {'profile': kind, 'game': str(game), 'package': 'props', 'assets': [], 'errors': [],
                 'archive_policy': 'base-only shared index'}

    def obtain(name):
        location = by_name.get(name.lower())
        if location is None:
            inventory['errors'].append({'name': name, 'stage': 'ebx', 'error': 'Not in the shared index'})
            return None
        path, offset, size, source = location
        with path.open('rb') as stream:
            stream.seek(offset)
            content = gw2.decompress(gw2.Reader(stream.read(size)), source['original_size'])
        document = gw2.Ebx(content).document()
        asset = dict(source)
        asset['path'] = 'ebx/%s.bin' % source['sha1']
        asset['document'] = 'ebx/%s.json' % source['sha1']
        asset['bytes'] = len(content)
        asset['root_type'], asset['guid'] = document['root_type'], document['guid']
        (output / 'ebx').mkdir(exist_ok=True)
        (output / asset['path']).write_bytes(content)
        return asset, document

    meshes = set()
    for item in items:
        found = obtain(item['asset'])
        if found:
            asset, document = found
            asset['item'] = item['name']
            (output / asset['document']).write_text(json.dumps(document), encoding='utf-8')
            inventory['assets'].append(asset)
            meshes.add(asset['guid'])
    for name in variation_dbs:
        found = obtain(name)
        if found and _trim_variations(found[1], meshes):
            (output / found[0]['document']).write_text(json.dumps(found[1]), encoding='utf-8')
            inventory['assets'].append(found[0])
    gw2.complete_dependencies(game, output, inventory)
    if kind == 'gw1':
        import gw1
        gw1.finish(game, output, inventory)
    else:
        gw2.complete_texture_chunks(game, output, inventory)
    inventory['items'] = items
    (output / 'inventory.json').write_text(json.dumps(inventory, indent=1), encoding='utf-8')
    return inventory


def export(game, items, output, names=None, variation_dbs=()):
    kind = game_kind(game)
    if kind == 'bfn':
        import bfn
        if not names:
            raise ValueError('Battle for Neighborville needs its type name table (--names)')
        inventory = bfn.export(game, 'props', output, bfn.key_from(None), names,
                               int(os.environ.get('AURA_IMPORT_JOBS', '4')), bundles=[],
                               extra_ebx=[i['asset'] for i in items], variation_dbs=variation_dbs)
        for a in inventory['assets']:
            for i in items:
                if a.get('name', '').lower() == i['asset'].lower():
                    a['item'] = i['name']
        inventory['items'] = items
        (Path(output) / 'inventory.json').write_text(json.dumps(inventory, indent=1), encoding='utf-8')
        return inventory
    return gw_export(game, kind, items, variation_dbs, output)


# ---------------- conversion ----------------
def convert(export_dir, textures, assets, out, gap=1.0):
    export_dir, out = Path(export_dir), Path(out)
    inventory = json.loads((export_dir / 'inventory.json').read_text(encoding='utf-8'))
    kind = inventory.get('profile')
    wanted = {i['asset'].lower(): i for i in inventory.get('items', [])}
    places, errors = [], []
    if kind == 'bfn':
        from convert import Archive
        import convert_legacy
        archive = Archive(export_dir)
        converter = convert_legacy.LegacyConverter(archive, textures, assets, 'bfn')

        def mesh_of(doc, obj):
            rel, materials, positions = converter.mesh(doc, obj)[:3]
            return rel, materials, positions
    else:
        import gw2_convert
        archive = gw2_convert.Gw2Archive(export_dir)
        converter = gw2_convert.Gw2Converter(archive, textures, assets)
        converter.skinned = True

        def mesh_of(doc, obj):
            rel, materials = converter.convert_static((doc, obj), 0)
            resource = archive.resources[obj['MeshSetResource']['$resource']]
            positions = np.concatenate([s['positions'] for s in gw2_convert.mesh_sections(archive, resource, True)])
            return rel, materials, positions
    cursor = 0.0
    for name, (asset, doc) in archive.documents.items():
        if name.lower() not in wanted or asset.get('root_type') not in MESH_TYPES:
            continue
        item = wanted[name.lower()]
        obj = doc['objects'][0]
        try:
            rel, materials, positions = mesh_of(doc, obj)
        except Exception as error:  # one bad prop must not lose the others
            errors.append({'stage': 'mesh', 'name': name, 'error': str(error)})
            continue
        lo, hi = positions.min(axis=0), positions.max(axis=0)
        # Frostbite is Y-up: lay props out along X, resting on Y = 0.
        x = cursor - float(lo[0])
        cursor += float(hi[0] - lo[0]) + gap
        matrix = [[1, 0, 0, x], [0, 1, 0, -float(lo[1])], [0, 0, 1, 0], [0, 0, 0, 1]]
        tags = {'mesh': rel, 'source': obj.get('Name', name), 'level_instance': item['name'], 'profile': kind}
        tags.update({'material_%d' % i: m for i, m in enumerate(materials)})
        places.append({'kind': 'frostbite_mesh', 'name': name.split('/')[-1], 'matrix': matrix, 'tags': tags,
                       'values': {'collision': 0}})
    if hasattr(converter, 'finish_textures'):
        converter.finish_textures()
    if not places:
        raise ValueError('No props converted' + (': ' + errors[0]['error'] if errors else ''))
    places.insert(0, {'kind': 'frostbite_environment', 'name': 'Props lighting', 'at': [0, 0, 0], 'tags': {},
                      'values': {'sun_intensity': 2.4}})
    scene = {'format': 'aura-frostbite-scene', 'version': 1, 'name': 'Props', 'places': places}
    report = {'profile': kind, 'placements': len(places), 'meshes': len(places) - 1, 'errors': errors}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(scene, indent=1), encoding='utf-8')
    out.with_suffix('.report.json').write_text(json.dumps(report, indent=1), encoding='utf-8')
    return scene, report


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('action', choices=('discover', 'export', 'convert'))
    p.add_argument('--game')
    p.add_argument('--out', required=True)
    p.add_argument('--items', help='JSON {"props": [...], "variation_dbs": [...]} or a list of props')
    p.add_argument('--names', help='BFN type name table')
    p.add_argument('--export')
    p.add_argument('--textures')
    p.add_argument('--assets')
    a = p.parse_args()
    if a.action == 'discover':
        Path(a.out).write_text(json.dumps(discover(a.game), indent=1), encoding='utf-8')
    elif a.action == 'export':
        request = json.loads(Path(a.items).read_text(encoding='utf-8'))
        if isinstance(request, list):
            request = {'props': request, 'variation_dbs': []}
        export(a.game, request['props'], a.out, a.names, request.get('variation_dbs', []))
    else:
        scene, report = convert(a.export, a.textures, a.assets, a.out)
        print('Props:', report['meshes'], 'errors:', len(report['errors']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
