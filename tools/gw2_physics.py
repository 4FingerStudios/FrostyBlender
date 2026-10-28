"""Garden Warfare 2 collision: a static model's cooked Havok physics resource (hk_2013 packfile version 11,
32-bit, little endian), read as a collision mesh. The cooked packfiles keep their virtual fixups (every object
and its class) but not their pointer fixups, so each shape is found by class and read at fixed offsets:

    hknpConvexPolytopeShape, hknpSphereShape, hknpCapsuleShape, hknpConvexShape
          +20  f32 convex radius
          +48  u16 vertex count, u16 offset of the vertices from this field; f32 xyzw each (w packs an index)
    hknpStaticCompoundShape
          +100 u32 instance count
          +176 instances, 128 bytes each: three rotation rows, the translation, the scale (each a float4 whose
               fourth component packs other data); instance i places the i-th shape in file order

A sphere has one vertex and a capsule two (its ends), the shape's radius around them. Compressed triangle
meshes (hknpCompressedMeshShape, buildings and large rocks) are not read: shapes() returns None for a
resource holding one, and the caller keeps the render mesh as collision.
"""
import struct

import numpy as np

import bfn_physics

PACKFILE = b'\x57\xe0\xe0\x57'
CONVEX = ('hknpConvexPolytopeShape', 'hknpConvexShape', 'hknpSphereShape', 'hknpCapsuleShape')
INSTANCE = 128
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


def collision_sections(data, metadata):
    """write_mesh sections for a GW2 physics resource, or None when it holds no readable shapes or a compressed
    triangle mesh (whose collision the render mesh stands in for)."""
    found = objects(data, metadata)
    if not found:
        return None
    listed, base = found
    if any(name == 'hknpCompressedMeshShape' for _, name in listed):
        return None
    shapes = [(base + offset, name) for offset, name in listed if name in CONVEX]
    compound = next((base + offset for offset, name in listed if name == 'hknpStaticCompoundShape'), None)
    placed = []
    if compound is not None:
        count = struct.unpack_from('<I', data, compound + 100)[0]
        if count != len(shapes) or compound + 176 + count * INSTANCE > len(data):
            return None
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
    if not vertices:
        return None
    return bfn_physics.sections(np.concatenate(vertices), np.concatenate(triangles))
