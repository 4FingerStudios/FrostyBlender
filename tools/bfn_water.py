"""Battle for Neighborville water surfaces.

BFN's WaterEntityData carries only physics (a Havok shape and buoyancy). The water you see is
ordinary static meshes (levels/*/lake/*) whose materials use the art/Shaders/Water/GW3_Water_*
shader graphs, set up by colour parameters instead of textures. Their sections get a
'surface': 'water' material, which the add-on builds with water_materials.py like GW water.
"""
import hashlib
import json

WATER_GRAPH = 'art/shaders/water/'
# The shared wave normal of BFN's water shaders, when the level exports it.
NORMAL_TEXTURES = ('art/textures/water/watertile_t01_nm',)


def graph_name(converter, mat_doc, mat):
    """The ShaderGraph a material draws with, through any SurfaceShaderPresets."""
    found, depth = converter.resolve(mat_doc, (mat.get('Shader') or {}).get('Shader')), 0
    while found and found[1].get('$type') == 'SurfaceShaderPreset' and depth < 4:
        found, depth = converter.resolve(found[0], (found[1].get('ShaderPreset') or {}).get('Shader')), depth + 1
    return (found[1].get('Name') or '') if found else ''


def is_water(name):
    return WATER_GRAPH in name.lower()


def _vectors(mat):
    out = {}
    for p in (mat.get('Shader') or {}).get('VectorParameters', []) or []:
        v = p.get('Value') or {}
        out[p.get('ParameterName') or ''] = [v.get(k, 0.0) for k in 'xyzw']
    return out


def material(converter, mat, graph, section_name):
    """A water material JSON for one water section, cached by its parameters."""
    vectors = _vectors(mat)
    shallow, deep = vectors.get('Color_Shallow'), vectors.get('Color_Deep')
    if shallow and deep:
        # The surface reads mostly as the shallow colour pulled towards the deep one.
        color = [shallow[i] * .6 + deep[i] * .4 for i in range(3)] + [1]
        source = 'Color_Shallow and Color_Deep'
    elif shallow or deep:
        color, source = list((shallow or deep)[:3]) + [1], 'Color_Shallow' if shallow else 'Color_Deep'
    else:
        color, source = [.04, .18, .24, 1], 'Blender fallback'
    textures, bindings = {}, {}
    for name in NORMAL_TEXTURES:
        entry = converter.by_name.get(name)
        result = converter.texture_object(entry[1]['objects'][0])[0] if entry and entry[1]['objects'] else None
        if result:
            textures['normal'], bindings['normal'] = result[0], name
            break
    content = {'format': 'aura-frostbite-material', 'version': 1, 'profile': 'bfn', 'surface': 'water',
               'name': section_name or 'Water surface', 'shader_name': graph,
               'source_shader': (mat.get('Shader') or {}), 'source_water': {},
               'water_color': color, 'water_color_source': source,
               'water_parameters': vectors, 'native_shader_databases': [],
               'double_sided': True, 'alpha_cutoff': -1, 'surface_position': 'mesh',
               'surface_approximation': 'The authored water mesh with animated Blender refraction and normals. '
                   'Native depth colouring, shore foam and flow are not executed.',
               'textures': textures, 'texture_sources': bindings}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:20]
    key = 'water:' + digest
    if key not in converter.materials:
        rel = 'materials/frostbite/water_' + digest + '.json'
        path = converter.assets / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content, indent=2), encoding='utf-8')
        converter.materials[key] = rel
    return converter.materials[key]


# ---- the sea: WaterEntityData's physics shape ----
# Open sea has no mesh in BFN (Pressure Pier's coast): only the water entity's Havok shape knows where it is.
def shape_points(data):
    """Points of a BFN water physics resource: its 64-byte records are an index, a NaN, 0x80, then a
    position (and a frame). Only the positions are read; the convex pieces they belong to are not decoded."""
    import struct
    out, at = [], 0
    while True:
        at = data.find(b'\xff\xff\xff\x7f\x80\x00\x00\x00', at)
        if at < 4 or at + 20 > len(data):
            break
        x, y, z = struct.unpack_from('<3f', data, at + 8)
        if all(abs(v) < 1e5 for v in (x, y, z)):
            out.append((x, y, z))
        at += 8
    return out


def sea_level(points, minimum=8):
    """The height most of the shape's points share (the open sea), or None."""
    import collections
    if not points:
        return None
    counts = collections.Counter(round(p[1] * 2) / 2 for p in points)
    level, count = counts.most_common(1)[0]
    if count < minimum:
        return None
    near = [p[1] for p in points if abs(p[1] - level) <= .5]
    return sum(near) / len(near)


def sea_section(level, box, terrain, cell=4.0):
    """Quads of a 'cell'-metre grid at `level` over `box` (min x, min z, max x, max z) wherever the terrain
    dips below the sea or ends (the open sea past the island), so the coast follows the shore and rooms
    under higher ground stay dry. `terrain` is a list of world-space vertex arrays."""
    import numpy as np
    x0, z0, x1, z1 = box
    nx, nz = max(1, int(np.ceil((x1 - x0) / cell))), max(1, int(np.ceil((z1 - z0) / cell)))
    low = np.full((nz, nx), np.inf)
    for world in terrain:
        ix = np.floor((world[:, 0] - x0) / cell).astype(int)
        iz = np.floor((world[:, 2] - z0) / cell).astype(int)
        ok = (ix >= 0) & (ix < nx) & (iz >= 0) & (iz < nz)
        np.minimum.at(low, (iz[ok], ix[ok]), world[ok, 1])
    wet = (low < level) | np.isinf(low)
    if not wet.any():
        return None
    iz, ix = np.nonzero(wet)
    corners = np.array([[0, 0], [0, 1], [1, 1], [1, 0]])
    xs = (x0 + (ix[:, None] + corners[None, :, 0]) * cell).reshape(-1)
    zs = (z0 + (iz[:, None] + corners[None, :, 1]) * cell).reshape(-1)
    positions = np.stack([xs, np.full_like(xs, level), zs], axis=1)
    base = (np.arange(len(ix)) * 4)[:, None]
    indices = (base + np.array([0, 1, 2, 0, 2, 3])[None, :]).reshape(-1).astype(np.uint32)
    count = len(positions)
    return {'name': 'Sea surface', 'positions': positions.astype(np.float32),
            'normals': np.tile(np.array([0, 1, 0], np.float32), (count, 1)),
            'uv': np.stack([(xs - x0) / 64, (zs - z0) / 64], axis=1).astype(np.float32),
            'indices': indices, 'color': np.ones((count, 3), np.float32), 'palette': np.zeros(count, np.float32),
            'material_id': 0}, int(wet.sum())


def sea(converter, entities, places, box):
    """'frostbite_mesh' places for the sea of each WaterEntityData, at the level its physics shape
    mostly sits at, over the terrain inside `box` (or all the terrain when None)."""
    import numpy as np
    from convert import write_mesh
    terrain = []
    for p in places:
        entry = converter.meshes.get(p['tags'].get('mesh', '').rsplit('/', 1)[-1][:-len('.amsh')])
        if p.get('kind') == 'frostbite_mesh' and entry and p['tags']['mesh'].split('/')[-1].startswith('bfn_terrain_'):
            m = np.array(p['matrix'])
            terrain.append(entry[2] @ m[:3, :3].T + m[:3, 3])
    if not terrain:
        return []
    if box is None:
        allpts = np.concatenate(terrain)
        box = (*allpts[:, [0, 2]].min(axis=0).tolist(), *allpts[:, [0, 2]].max(axis=0).tolist())
    out, done = [], set()
    for doc, obj, matrix in entities:
        for pointer in obj.get('Components', []) or []:
            component = converter.resolve(doc, pointer)
            asset = converter.resolve(component[0], component[1].get('Asset')) if component else None
            rid = ((asset[1].get('Resource') or {}).get('$resource')) if asset else None
            res = converter.archive.resources.get(rid)
            if not res or rid in done:
                continue
            done.add(rid)
            points = [tuple((matrix @ np.array([*q, 1]))[:3]) for q in shape_points(converter.archive.data(res))]
            level = sea_level(points)
            if level is None:
                continue
            built = sea_section(level, box, terrain)
            if not built:
                continue
            section, cells = built
            # The colour of an authored water mesh lying at the same level (a lake that is part of the sea).
            material = None
            for p in places:
                entry = converter.meshes.get(p['tags'].get('mesh', '').rsplit('/', 1)[-1][:-len('.amsh')])
                if p['tags'].get('surface') == 'water' and entry:
                    m = np.array(p['matrix'])
                    if abs(float((entry[2] @ m[:3, :3].T + m[:3, 3])[:, 1].mean()) - level) < 1:
                        material = p['tags']['material_0']
                        break
            material = material or globals()['material'](converter, {}, 'sea', 'Sea surface')
            centre = section['positions'].mean(axis=0)
            centre[1] = level
            section['positions'] -= centre
            key = 'bfn_sea_' + str(rid)
            rel = 'meshes/frostbite/' + key + '.amsh'
            vertices, triangles = write_mesh(converter.assets / rel, [section])
            converter.counts['vertices'] += vertices
            converter.counts['triangles'] += triangles
            converter.meshes[key] = (rel, [material], section['positions'], section['indices'].reshape(-1, 3))
            placement = np.eye(4)
            placement[:3, 3] = centre
            out.append({'kind': 'frostbite_mesh', 'name': 'Sea surface', 'matrix': placement.tolist(),
                        'tags': {'mesh': rel, 'material_0': material, 'surface': 'water', 'sea_level': round(level, 3),
                                 'sea_cells': cells, 'source': (asset[1].get('Name') or '')},
                        'values': {'collision': 0}})
    return out
