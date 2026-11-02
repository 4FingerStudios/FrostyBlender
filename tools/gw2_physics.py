"""Garden Warfare 1 and 2 collision (GW1 is at the end of this file). GW2: a static model's cooked Havok physics
resource (hk_2013 packfile version 11, 32-bit, little endian), read as a collision mesh. The cooked packfiles keep their virtual fixups (every object
and its class) but not their pointer fixups, so each shape is found by class and read at fixed offsets:

    hknpConvexPolytopeShape, hknpSphereShape, hknpCapsuleShape, hknpConvexShape
          +20  f32 convex radius
          +48  u16 vertex count, u16 offset of the vertices from this field; f32 xyzw each (w packs an index)
    hknpStaticCompoundShape
          +100 u32 instance count
          +176 instances, 128 bytes each: three rotation rows, the translation, the scale (each a float4 whose
               fourth component packs other data); instance i places the i-th shape in file order

    hknpCompressedMeshShapeData
          +0x14 u32 top tree node count (5 bytes each), +0x20 the mesh's bounds (two float4)
          +0x50 +0x5c +0x68 +0x74 +0x80 +0x8c u32 counts: sections, primitives, shared vertex indices, packed
               vertices, shared vertices, data runs (each array's pointer is the field before its count)
          +0xe0 the arrays, in that order after the top nodes, each aligned to 16:
               sections (0x60 bytes each, followed by each one's tree nodes, 4 bytes each); u8 primitives[4];
               u16 shared vertex indices; u32 packed vertices; u64 shared vertices; u32 data runs
          section: +0x04 node count, +0x30 six f32 (offset xyz, scale xyz), +0x48 first packed vertex,
               +0x4c shared indices (first << 8), +0x50 primitives (first << 8 | count), +0x58 u8 packed count,
               u8 shared count
          primitive: four indices into the section's packed then shared vertices, a triangle when the last two match
          packed vertex: x 11, y 11, z 10 bits times the section's scale, plus its offset
          shared vertex: x 21, y 21, z 22 bits across the mesh's bounds

A sphere has one vertex and a capsule two (its ends), the shape's radius around them. A compound's instance count
matches the convex shapes alone: compressed meshes (buildings, large rocks) stand outside it, in the model's space.
"""
import struct

import numpy as np

import bfn_physics

PACKFILE = b'\x57\xe0\xe0\x57'
CONVEX = ('hknpConvexPolytopeShape', 'hknpConvexShape', 'hknpSphereShape', 'hknpCapsuleShape')
INSTANCE = 128
BOUNDS_FIT = 0.1  # m: how far a GW1 compound's placed shapes may miss its stored bounds
PARKED = 500.0  # m: shapes this far from the model's origin are parked out of the way, not collision


def objects(data, metadata):
    """(offset in the data section, class name) of every object in the first (32-bit) packfile, and the data
    section's absolute start; None when the resource holds no packfile this reader knows."""
    if len(metadata) != 16:
        return None
    pack = struct.unpack('<4I', bytes(metadata))[0] & 0xffffff
    if pack + 256 > len(data) or data[pack:pack + 4] != PACKFILE or struct.unpack_from('<I', data, pack + 12)[0] != 11:
        return None
    if data[pack + 80:pack + 94] != b'__classnames__' or data[pack + 208:pack + 216] != b'__data__':
        return None
    names = pack + struct.unpack_from('<I', data, pack + 100)[0]
    start, virtual, end = (struct.unpack_from('<I', data, pack + at)[0] for at in (228, 240, 244))
    base = pack + start
    found = []
    for at in range(base + virtual, min(base + end, len(data) - 12), 12):
        offset, _, name = struct.unpack_from('<3I', data, at)
        if offset == 0xffffffff:
            break
        stop = data.find(b'\0', names + name)
        found.append((offset, data[names + name:stop].decode('ascii', 'replace')))
    return found, base


def _vertices(data, at):
    count, offset = struct.unpack_from('<2H', data, at + 48)
    first = at + 48 + offset
    if not 0 < count <= 4096 or first + count * 16 > len(data):
        return None
    points = np.frombuffer(data, '<f4', count * 4, first).reshape(-1, 4)[:, :3].astype(np.float64)
    return points if np.isfinite(points).all() else None


def _mesh(data, at, kind):
    """A convex shape's (vertices, triangles) in its own space."""
    points = _vertices(data, at)
    if points is None:
        return None
    radius = struct.unpack_from('<f', data, at + 20)[0]
    if kind == 'hknpSphereShape' and radius > 0:
        v, t = bfn_physics.round_mesh(radius)
        return v + points[0], t
    if kind == 'hknpCapsuleShape' and radius > 0 and len(points) >= 2:
        a, b = points[0], points[1]
        axis = b - a
        half = float(np.linalg.norm(axis)) / 2
        v, t = bfn_physics.round_mesh(radius, half)
        y = axis / (2 * half) if half > 1e-9 else np.array([0.0, 1.0, 0.0])
        x = np.cross(y, [0.0, 0.0, 1.0] if abs(y[2]) < 0.9 else [1.0, 0.0, 0.0])
        x /= np.linalg.norm(x)
        z = np.cross(x, y)
        return v @ np.array([x, y, z]) + (a + b) / 2, t
    return bfn_physics.convex_hull(points)


def _aligned(at):
    return (at + 15) // 16 * 16


def compressed_mesh(data, at, nodes=0xe0):
    """(vertices, triangles) of the compressed mesh tree at this absolute offset; nodes is where its arrays start."""
    top, sections, primitives, indices, packed, shared, runs = (
        struct.unpack_from('<I', data, at + field)[0] for field in (0x14, 0x50, 0x5c, 0x68, 0x74, 0x80, 0x8c))
    low, high = np.frombuffer(data, '<f4', 8, at + 0x20).reshape(2, 4)[:, :3].astype(np.float64)
    first_section = _aligned(at + nodes + top * 5)
    next_at = first_section + sections * 0x60
    for i in range(sections):
        next_at = _aligned(next_at + 4 * struct.unpack_from('<I', data, first_section + i * 0x60 + 4)[0])
    primitive_at = next_at
    index_at = _aligned(primitive_at + primitives * 4)
    packed_at = _aligned(index_at + indices * 2)
    shared_at = _aligned(packed_at + packed * 4)
    if _aligned(shared_at + shared * 8) + runs * 4 > len(data):
        raise ValueError('GW2 compressed mesh runs past its resource')
    primitive = np.frombuffer(data, '<u1', primitives * 4, primitive_at).reshape(-1, 4).astype(np.int64)
    index = np.frombuffer(data, '<u2', indices, index_at)
    packed_vertices = np.frombuffer(data, '<u4', packed, packed_at)
    bits = np.frombuffer(data, '<u8', shared, shared_at)
    shared_vertices = np.c_[bits & 0x1fffff, (bits >> np.uint64(21)) & 0x1fffff, bits >> np.uint64(42)]         * ((high - low) / [2 ** 21 - 1, 2 ** 21 - 1, 2 ** 22 - 1]) + low
    vertices, triangles, start = [], [], 0
    for i in range(sections):
        section = first_section + i * 0x60
        parameters = np.frombuffer(data, '<f4', 6, section + 0x30).astype(np.float64)
        first_packed, shared_range, primitive_range = struct.unpack_from('<3I', data, section + 0x48)
        packed_count, shared_count = struct.unpack_from('<2B', data, section + 0x58)
        chunk = packed_vertices[first_packed:first_packed + packed_count]
        own = np.c_[chunk & 0x7ff, (chunk >> 11) & 0x7ff, chunk >> 22] * parameters[3:] + parameters[:3]
        first_shared = shared_range >> 8
        points = np.r_[own, shared_vertices[index[first_shared:first_shared + shared_count]]]
        first_primitive = primitive_range >> 8
        polygons = primitive[first_primitive:first_primitive + (primitive_range & 0xff)]
        quads = polygons[polygons[:, 2] != polygons[:, 3]]
        faces = np.r_[polygons[:, :3], quads[:, [0, 2, 3]]]
        faces = faces[faces.max(axis=1) < len(points)]  # the odd filler primitive names no real vertices
        vertices.append(points)
        triangles.append(faces + start)
        start += len(points)
    if not vertices:
        return np.zeros((0, 3)), np.zeros((0, 3), np.int64)
    return np.concatenate(vertices), np.concatenate(triangles)


def collision_sections(data, metadata):
    """write_mesh sections for a GW2 physics resource, or None when it holds no readable shapes."""
    found = objects(data, metadata)
    if not found:
        return gw1_collision_sections(data, metadata)
    listed, base = found
    shapes = [(base + offset, name) for offset, name in listed if name in CONVEX]
    compound = next((base + offset for offset, name in listed if name == 'hknpStaticCompoundShape'), None)
    placed = []
    if compound is not None:
        count = struct.unpack_from('<I', data, compound + 100)[0]
        if count != len(shapes) or compound + 176 + count * INSTANCE > len(data):
            return None  # a model group's compound, its instances naming shapes this reader can't follow
        for i, (at, name) in enumerate(shapes):
            rows = np.frombuffer(data, '<f4', 20, compound + 176 + i * INSTANCE).reshape(5, 4)[:, :3].astype(np.float64)
            rotation, position, scale = rows[:3], rows[3], rows[4]
            if not np.isfinite(rows).all() or np.abs(rotation @ rotation.T - np.eye(3)).max() > 1e-3:
                return None
            placed.append((at, name, rotation.T * scale[None, :], position))
    else:
        placed = [(at, name, np.eye(3), np.zeros(3)) for at, name in shapes]
    vertices, triangles, start = [], [], 0
    for at, name, linear, position in placed:
        mesh = _mesh(data, at, name)
        if mesh is None:
            continue
        v, t = mesh
        v = v @ linear.T + position
        if np.abs(v).max() > PARKED:
            continue  # a destructible prop's broken pieces, parked far below until it breaks
        vertices.append(v)
        triangles.append(t + start)
        start += len(v)
    for offset, name in listed:
        if name == 'hknpCompressedMeshShapeData':
            v, t = compressed_mesh(data, base + offset)
            if len(t):
                vertices.append(v)
                triangles.append(t + start)
                start += len(v)
    if not vertices:
        return None
    return bfn_physics.sections(np.concatenate(vertices), np.concatenate(triangles))


def _box(half):
    corners = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], np.float64) * half
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1), (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4),
             (1, 5, 7), (1, 7, 3)]
    return corners, np.array(faces, np.int64)


def _cylinder(a, b, radius, sides=16):
    axis = b - a
    length = np.linalg.norm(axis)
    if length < 1e-9:
        raise ValueError('GW1 cylinder has no length')
    axis = axis / length
    x = np.cross(axis, [0.0, 0.0, 1.0] if abs(axis[2]) < 0.9 else [1.0, 0.0, 0.0])
    x /= np.linalg.norm(x)
    y = np.cross(axis, x)
    angles = np.arange(sides) * 2 * np.pi / sides
    ring = radius * (np.cos(angles)[:, None] * x + np.sin(angles)[:, None] * y)
    vertices = np.r_[ring + a, ring + b, [a, b]]
    faces = []
    for i in range(sides):
        j = (i + 1) % sides
        faces += [(i, j, sides + j), (i, sides + j, sides + i), (2 * sides, j, i), (2 * sides + 1, sides + i, sides + j)]
    return vertices, np.array(faces, np.int64)


def _capsule(a, b, radius):
    axis = b - a
    half = float(np.linalg.norm(axis)) / 2
    v, t = bfn_physics.round_mesh(radius, half)
    y = axis / (2 * half) if half > 1e-9 else np.array([0.0, 1.0, 0.0])
    x = np.cross(y, [0.0, 0.0, 1.0] if abs(y[2]) < 0.9 else [1.0, 0.0, 0.0])
    x /= np.linalg.norm(x)
    return v @ np.array([x, y, np.cross(x, y)]) + (a + b) / 2, t


def _quaternion(q):
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def _floats(data, at, count):
    return np.frombuffer(data, '<f4', count, at).astype(np.float64)


def _gw1_shape(data, listed, index):
    """(list of (vertices, triangles), the next object's index) for the shape object at index; a wrapper's children
    follow it in file order. None when a shape can't be read."""
    if index >= len(listed):
        return None
    at, name = listed[index]
    radius = struct.unpack_from('<f', data, at + 16)[0]
    if name == 'hkpBoxShape':
        return [_box(_floats(data, at + 32, 3) + radius)], index + 1
    if name == 'hkpSphereShape':
        return [bfn_physics.round_mesh(radius)], index + 1
    if name == 'hkpCapsuleShape':
        return [_capsule(_floats(data, at + 32, 3), _floats(data, at + 48, 3), radius)], index + 1
    if name == 'hkpCylinderShape':
        cylinder_radius = struct.unpack_from('<f', data, at + 20)[0]
        return [_cylinder(_floats(data, at + 32, 3), _floats(data, at + 48, 3), cylinder_radius + radius)], index + 1
    if name == 'hkpConvexVerticesShape':
        blocks, count = struct.unpack_from('<I', data, at + 68)[0], struct.unpack_from('<I', data, at + 76)[0]
        if not 0 < count <= blocks * 4 or at + 112 + blocks * 48 > len(data):
            return None
        rows = _floats(data, at + 112, blocks * 12).reshape(blocks, 3, 4)
        points = rows.transpose(0, 2, 1).reshape(-1, 3)[:count]
        center, half = _floats(data, at + 48, 3), _floats(data, at + 32, 3)
        if np.abs(points - center).max(initial=0) > half.max() + radius + 1e-3:
            return None
        mesh = bfn_physics.convex_hull(points)
        return ([mesh] if mesh else []), index + 1
    if name == 'hkpBvCompressedMeshShape':
        return [compressed_mesh(data, at + 0x40, 0xb0)], index + 1
    if name in ('hkpConvexTranslateShape', 'hkpConvexTransformShape'):
        child = _gw1_shape(data, listed, index + 1)
        if child is None:
            return None
        meshes, following = child
        position = _floats(data, at + 32, 3)
        linear = np.eye(3)
        if name == 'hkpConvexTransformShape':
            linear = _quaternion(_floats(data, at + 48, 4)) * _floats(data, at + 64, 3)
        return [(v @ linear.T + position, t) for v, t in meshes], following
    if name == 'hkpListShape':
        meshes, following = [], index + 1
        for _ in range(struct.unpack_from('<I', data, at + 28)[0]):
            child = _gw1_shape(data, listed, following)
            if child is None:
                return None
            meshes += child[0]
            following = child[1]
        return meshes, following
    if name == 'hkpStaticCompoundShape':
        count = struct.unpack_from('<I', data, at + 36)[0]
        meshes, following = [], index + 1
        # Instances don't say which shape they use (the pointers are stripped): follow them in file order when each
        # has its own, or all onto a lone shape; with any other sharing the caller keeps the render mesh.
        shapes = []
        while following < len(listed):
            child = _gw1_shape(data, listed, following)
            if child is None:
                return None
            shapes.append(child[0])
            following = child[1]
        if len(shapes) != count and len(shapes) != 1:
            return None
        for i in range(count):
            child = (shapes[i if len(shapes) == count else 0], following)
            row = at + 128 + i * 64
            position, rotation, scale = _floats(data, row, 3), _floats(data, row + 16, 4), _floats(data, row + 32, 3)
            if abs(float(rotation @ rotation) - 1) > 1e-3:
                return None
            linear = _quaternion(rotation) * scale
            meshes += [(v @ linear.T + position, t) for v, t in child[0]]
            following = child[1]
        # The compound stores its bounds: a wrong instance order wouldn't reproduce them.
        if meshes:
            points = np.concatenate([v for v, _ in meshes])
            low, high = _floats(data, at + 0x50, 3), _floats(data, at + 0x60, 3)
            if max(np.abs(points.min(axis=0) - low).max(), np.abs(points.max(axis=0) - high).max()) > BOUNDS_FIT:
                return None
        return meshes, following
    return None


def gw1_collision_sections(data, metadata):
    """write_mesh sections for a Garden Warfare 1 physics resource (hk_2012 packfile version 9, 32-bit), or None when
    it holds a shape this reader doesn't know. Objects: hkRootLevelContainer, HavokPhysicsContainer (+12 root shape
    count), then each root shape followed by its children, depth first."""
    if len(metadata) != 16:
        return None
    pack = struct.unpack('<4I', bytes(metadata))[0] & 0xffffff
    if pack + 256 > len(data) or data[pack:pack + 4] != PACKFILE or struct.unpack_from('<I', data, pack + 12)[0] != 9:
        return None
    if data[pack + 64:pack + 78] != b'__classnames__' or data[pack + 160:pack + 168] != b'__data__':
        return None
    names = pack + struct.unpack_from('<I', data, pack + 84)[0]
    base = pack + struct.unpack_from('<I', data, pack + 180)[0]
    virtual, _, _, end = struct.unpack_from('<4I', data, pack + 192)
    listed = []
    for at in range(base + virtual, min(base + end, len(data) - 12), 12):
        offset, _, name = struct.unpack_from('<3I', data, at)
        if offset == 0xffffffff:
            break
        listed.append((base + offset, data[names + name:data.find(b'\0', names + name)].decode('ascii', 'replace')))
    if len(listed) < 3 or listed[1][1] != 'HavokPhysicsContainer':
        return None
    roots = struct.unpack_from('<I', data, listed[1][0] + 12)[0]
    meshes, index = [], 2
    for _ in range(roots):
        shape = _gw1_shape(data, listed, index)
        if shape is None:
            return None
        meshes += shape[0]
        index = shape[1]
    vertices, triangles, start = [], [], 0
    for v, t in meshes:
        if len(t) and np.abs(v).max() <= PARKED:
            vertices.append(v)
            triangles.append(t + start)
            start += len(v)
    if not vertices:
        return None
    return bfn_physics.sections(np.concatenate(vertices), np.concatenate(triangles))
