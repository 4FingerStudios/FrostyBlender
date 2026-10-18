"""Water entity export and lossless native shader sidecars.

The projected/simulated game surface becomes a bounded plane at the entity
origin. Blender reconstructs an animated surface; native GPU programs and
simulation settings are retained for inspection, not executed by Blender.
"""
import hashlib
import json
import math
import struct

import numpy as np

from convert import write_mesh

SHADER_DATABASE = 0x36F3F2C0


def water_constants(data):
    """Read GW1 Xenon v5 constant tables. Unknown layouts remain raw sidecars."""
    if len(data) < 28 or struct.unpack_from('>2I', data) != (1, 5):
        return []
    count = struct.unpack_from('>I', data, 24)[0]
    if count > 100000:
        raise ValueError('Invalid shader constant table count')
    at, result = 28, []
    for _ in range(count):
        if at + 48 > len(data):
            raise ValueError('Truncated shader constant table')
        size, *offsets = struct.unpack_from('>6I', data, at)
        if size < 48 or at + size > len(data) or offsets != sorted(offsets) or not all(48 <= o <= size for o in offsets):
            raise ValueError('Invalid shader constant table range')
        nt, nv = data[at+29:at+31]
        if offsets[1] + nt*140 > offsets[2] or offsets[2] + nv*60 > offsets[3]:
            raise ValueError('Shader constant entries exceed table')
        textures = []
        for i in range(nt):
            p = at + offsets[1] + i*140
            name = data[p+4:p+132].split(b'\0', 1)[0].decode('utf-8')
            textures.append({'slot': data[p], 'name': name})
        if any('water' in t['name'].lower() or 'ocean' in t['name'].lower() for t in textures):
            vectors = {}
            for i in range(nv):
                p = at + offsets[2] + i*60
                name = data[p:p+32].split(b'\0', 1)[0].decode('utf-8')
                value = list(struct.unpack_from('>4f', data, p+44))
                if all(math.isfinite(v) for v in value):
                    vectors[name] = value
            result.append({'offset': at, 'size': size, 'textures': textures, 'vectors': vectors})
        at += size
    return result


def shader_sources(converter):
    if hasattr(converter, '_water_shader_sources'):
        return converter._water_shader_sources
    sources, tables = [], []
    # Anonymous shader databases share resource ID zero. Iterate assets, never
    # the resource-ID dictionary, to preserve every level's native shader data.
    for asset in converter.archive.assets:
        if asset.get('resource_type') != SHADER_DATABASE or 'bytes' not in asset:
            continue
        raw = converter.archive.data(asset)
        digest = hashlib.sha256(raw).hexdigest()
        rel = 'shaders/frostbite/' + digest[:20] + '.shaderdb'
        target = converter.assets / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        source = {'name': asset['name'], 'path': rel, 'sha256': digest,
                  'resource_type': SHADER_DATABASE, 'metadata': asset.get('metadata', [])}
        try:
            decoded = water_constants(raw)
        except (ValueError, UnicodeError, struct.error) as error:
            decoded = []
            source['decode_warning'] = str(error)
        tables.extend(dict(table, database=rel) for table in decoded)
        sources.append(source)
    converter._water_shader_sources = sources, tables
    return sources, tables


def convert_water(converter, doc, obj):
    archive = converter.archive
    graph = archive.resolve(doc, obj.get('Shader'))
    databases, tables = shader_sources(converter)
    document_name = next((name for name, (_, candidate) in archive.documents.items()
                          if candidate is doc), '')
    matching = [s for s in databases if document_name.startswith(s['name'].rsplit('/', 1)[0] + '/')]
    if matching:
        closest = max(matching, key=lambda s: len(s['name']))['path']
        tables = [t for t in tables if t['database'] == closest]
    content = {'format': 'aura-frostbite-material', 'version': 1,
               'profile': archive.inventory.get('profile', 'gw1'), 'surface': 'water',
               'name': 'Water surface', 'shader_name': graph[1].get('Name', '') if graph else '',
               'source_shader': graph[1] if graph else {}, 'source_water': obj,
               'source_shader_document': dict(graph[0]) if graph else None,
               'native_shader_databases': databases, 'water_constant_tables': tables,
               'double_sided': True, 'alpha_cutoff': -1,
               'surface_approximation': 'Bounded plane at entity origin; animated Blender refraction and normals. '
                   'Native projected-grid simulation, shore depth and interactive waves are not executed.',
               'shader_binding_note': 'Constant tables selected by water texture names; '
                   'compiled graph-to-table linkage is not decoded.'}
    # Constants embedded in the compiled program are absent from the EBX graph.
    # Use an unambiguous water colour when present, while retaining provenance.
    colors = {tuple(t['vectors']['Color4'][:3]) for t in tables if 'Color4' in t['vectors']}
    content['water_color'] = list(next(iter(colors))) + [1] if len(colors) == 1 else [.04, .18, .24, 1]
    content['water_color_source'] = 'water constant tables: Color4' if len(colors) == 1 else 'Blender fallback'
    names = sorted({t['name'].lower() for table in tables for t in table['textures']})
    textures, bindings = {}, {}
    for role, test in [('normal', lambda n: n.endswith('_n') and 'water' in n),
                       ('foam_normal', lambda n: n.endswith('_nm') and 'water' in n),
                       ('foam', lambda n: 'foam' in n and n.endswith('_d'))]:
        for name in names:
            if not test(name):
                continue
            found = archive.documents.get(name)
            if found:
                path = converter.texture(found[0]['guid'])
                if path:
                    textures[role], bindings[role] = path, name
                    break
    content['textures'], content['texture_sources'] = textures, bindings
    digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:20]
    material = 'materials/frostbite/water_' + digest + '.json'
    target = converter.assets / material
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(content, indent=2), encoding='utf-8')
    converter.materials['water:'+digest] = material
    rel = 'meshes/frostbite/water_plane.amsh'
    if not hasattr(converter, '_water_plane'):
        section = {'name': 'Water surface', 'positions': np.array([[-.5, 0, -.5], [-.5, 0, .5], [.5, 0, .5], [.5, 0, -.5]], np.float32),
                   'normals': np.tile([0, 1, 0], (4, 1)), 'uv': np.array([[0, 0], [0, 1], [1, 1], [1, 0]], np.float32),
                   'indices': np.array([0, 1, 2, 0, 2, 3], np.uint32), 'color': np.ones((4, 3)),
                   'palette': np.zeros(4), 'material_id': 0}
        vertices, triangles = write_mesh(converter.assets / rel, [section])
        converter.counts['vertices'] += vertices
        converter.counts['triangles'] += triangles
        converter._water_plane = rel
    return rel, material
