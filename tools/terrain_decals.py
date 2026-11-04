"""Garden Warfare 1/2 terrain decals: the roads, road lines and puddles laid over the terrain (<terrain>.decals).

The resource (type 0x15e1f32e) is a header (f32, u32, u32, u8) then groups. A group is a u32 record count, the
records, u32 vertex bytes, u32 index bytes, the vertices and u32 indices; an empty group is only its count. A record is a zero-terminated shader
name, 3 bytes (2 in Garden Warfare 1), u32 first index, u32 triangle count, 13 f32 (its bounds, the terrain tile
it lies in and a scale), u32 vertex byte offset, u32 vertex bytes and a u8. Indices count from the record's
first vertex.

Vertices start with the position: f32 x, z for shaders ending _2D, which the game draws into the terrain
texture, and f32 x, y, z for the rest. Garden Warfare 2 follows it with half floats: normal (4), tangent (4),
texture coordinates (4 holding u and v, or 2) and colour (4); Garden Warfare 1 with f32 u, v and half colour (4).
Most _2D decals have a twin _3D decal with heights; the _3D one is used. The game projects decals onto the ground,
so a _2D one without a twin is laid on the terrain, and none sink below it.
"""
import hashlib
import json
import struct
import numpy as np
from convert import write_mesh

RESOURCE_TYPE = 0x15e1f32e
LIFT = .02  # above the ground they lie on, so they don't flicker into it


def parse(data, name_padding=3):
    """Every decal piece: [{'shader', 'flat', 'box' (min xyz, max xyz), 'positions' (n, 3; y is nan when flat),
    'uv', 'colour', 'indices'}]."""
    pieces = []
    at = 13
    while at + 4 <= len(data):
        count = struct.unpack_from('<I', data, at)[0]
        at += 4
        if count == 0:  # an empty group is just its count
            continue
        if count >= 100000:
            raise ValueError('Invalid terrain decal group')
        records = []
        for _ in range(count):
            end = data.index(b'\0', at)
            shader = data[at:end].decode('ascii', 'replace')
            at = end + 1 + name_padding
            first, triangles = struct.unpack_from('<II', data, at)
            box = struct.unpack_from('<6f', data, at + 8)
            offset, size = struct.unpack_from('<II', data, at + 60)
            at += 69
            records.append((shader, first, triangles, offset, size, box))
        vertex_bytes, index_bytes = struct.unpack_from('<II', data, at)
        at += 8
        vertices = data[at:at + vertex_bytes]
        indices = np.frombuffer(data, '<u4', index_bytes // 4, at + vertex_bytes)
        at += vertex_bytes + index_bytes
        if len(vertices) != vertex_bytes or at > len(data):
            raise ValueError('Truncated terrain decal group')
        for shader, first, triangles, offset, size, box in records:
            idx = indices[first:first + triangles * 3]
            if not len(idx) or offset + size > len(vertices):
                continue
            n = int(idx.max()) + 1
            stride = size // n
            flat = shader.lower().endswith('_2d')
            position_bytes = 8 if flat else 12
            rest = stride - position_bytes
            if size != stride * n or rest not in (16, 28, 32):
                continue
            raw = np.frombuffer(vertices, np.uint8, size, offset).reshape(n, stride)
            floats = raw[:, :position_bytes].copy().view('<f4')
            positions = np.full((n, 3), np.nan, np.float32)
            if flat:
                positions[:, 0], positions[:, 2] = floats[:, 0], floats[:, 1]
            else:
                positions[:] = floats
            if rest == 16:  # Garden Warfare 1: f32 uv, half colour
                uv = raw[:, position_bytes:position_bytes + 8].copy().view('<f4')
                colour = raw[:, position_bytes + 8:].copy().view('<f2').astype(np.float32)[:, :3]
            else:
                uv_halves = (rest - 24) // 2
                halves = raw[:, position_bytes:].copy().view('<f2').astype(np.float32)
                uv = halves[:, 8:8 + uv_halves]
                uv = uv[:, [1, 3]] if uv_halves == 4 else uv
                colour = halves[:, 8 + uv_halves:8 + uv_halves + 3]
            pieces.append({'shader': shader, 'flat': flat, 'box': box, 'positions': positions, 'uv': uv,
                           'colour': colour, 'indices': idx.reshape(-1, 3).astype(np.uint32)})
    return pieces


def bounds_key(piece):
    """The ground area a piece covers, the same for a _2D decal and its _3D twin."""
    x0, _, z0, x1, _, z1 = piece['box']
    return tuple(round(v, 1) for v in (x0, z0, x1, z1))


def convert(converter, terrain, ground=None):
    """The terrain's decals as meshes in terrain space: [(mesh path, material path)]. ground(x, z) gives the
    terrain height for decals stored without one, or raises ValueError."""
    archive = converter.archive
    name = (terrain[1].get('Name') or '').lower() + '.decals'
    resource = next((a for a in archive.assets if a.get('resource_type') == RESOURCE_TYPE and a.get('bytes')
                     and (a.get('name') or '').lower() == name), None)
    if resource is None:
        return []
    gw1 = archive.inventory.get('profile', 'gw2') == 'gw1'
    pieces = parse(archive.data(resource), 2 if gw1 else 3)
    raised = {(p['shader'].lower()[:-3], bounds_key(p)) for p in pieces if p['shader'].lower().endswith('_3d')}
    by_shader = {}
    for piece in pieces:
        if piece['flat'] and (piece['shader'].lower()[:-3], bounds_key(piece)) in raised:
            continue
        # The game projects decals onto the ground: _2D ones without a height lie on the terrain, and the rest
        # stay where they are but never under it (some water is stored below the ground it covers).
        heights = piece['positions'][:, 1]
        for i, (x, y, z) in enumerate(piece['positions']):
            try:
                floor = ground(x, z) if ground else None
            except ValueError:
                floor = None
            if floor is not None:
                heights[i] = floor if piece['flat'] or not y >= floor else y
        if np.isnan(heights).any():
            continue
        by_shader.setdefault(piece['shader'], []).append(piece)
    out = []
    for shader, group in sorted(by_shader.items()):
        sections = []
        for i, piece in enumerate(group):
            positions = piece['positions'].copy()
            positions[:, 1] += LIFT
            count = len(positions)
            sections.append({'name': shader.rsplit('/', 1)[-1], 'positions': positions,
                             'normals': np.tile([0, 1, 0], (count, 1)), 'uv': piece['uv'],
                             'color': np.clip(piece['colour'], 0, 1), 'palette': np.zeros(count),
                             'indices': piece['indices'].ravel(), 'material_id': 0})
        digest = hashlib.sha256((name + shader).encode()).hexdigest()[:16]
        rel = f'meshes/frostbite/terrain_decal_{digest}.amsh'
        vertices, triangles = write_mesh(converter.assets / rel, sections)
        converter.counts['vertices'] += vertices
        converter.counts['triangles'] += triangles
        out.append((rel, material(converter, shader)))
    return out


def material(converter, shader):
    """Road decals are alpha blended with the textures named after their shader (Road_Hub_RoadLinesA_3D ->
    Road_Hub_RoadLinesA_Color); water decals use the water material."""
    archive = converter.archive
    base = shader.rsplit('/', 1)[-1]
    stem = base[:-3] if base.lower().endswith(('_2d', '_3d')) else base
    digest = hashlib.sha256(shader.encode()).hexdigest()[:20]
    rel = f'materials/frostbite/terrain_decal_{digest}.json'
    if 'water' in shader.lower():
        content = {'format': 'aura-frostbite-material', 'version': 1, 'profile': 'gw2', 'surface': 'water',
                   'name': stem, 'shader_name': shader, 'water_color': [.04, .18, .24, 1], 'textures': {},
                   'double_sided': True, 'alpha_cutoff': -1,
                   'surface_approximation': 'Terrain water decal (puddle, stream) with the generic water look.'}
    else:
        textures = {}
        wanted = {stem.lower() + suffix: role for role, suffixes in
                  (('base', ('_color', '_col', '_d', '_diffuse')), ('normal', ('_normal', '_n')))
                  for suffix in suffixes}
        for doc_name, (asset, _) in archive.documents.items():
            role = wanted.get(doc_name.rsplit('/', 1)[-1])
            if role and role not in textures and asset.get('root_type') == 'TextureAsset':
                try:
                    path = converter.texture(asset['guid'])
                except OSError:
                    path = None
                if path:
                    textures[role] = path
        content = {'format': 'aura-frostbite-material', 'version': 1, 'profile': 'gw2', 'surface': 'decal',
                   'name': stem, 'textures': textures, 'tint': [1, 1, 1, 1], 'texture_extension': 'REPEAT',
                   'parameters': [[1, 1, 1, 1], [.6, 0, 1 if 'normal' in textures else 0, 1], [1, 1, 0, 0],
                                  [0, 0, 0, 0]],
                   'double_sided': True, 'blend': 'alpha', 'alpha_cutoff': .1,
                   'source_decal_template': shader,
                   'surface_approximation': "Terrain decal mesh, alpha blended over the ground; the game's "
                                            'blend into the terrain texture is not reproduced.'}
    path = converter.assets / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, indent=2), encoding='utf-8')
    converter.materials['terrain decal:' + digest] = rel
    return rel
