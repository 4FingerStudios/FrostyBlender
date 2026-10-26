"""Battle for Neighborville collision: the game's own physics resources (a PhysicsAsset's .physics resource,
resource type 0x41759364), read as collision meshes. Frostbite's physics here replaced Havok; layout (little
endian, version 2 with no extra sections, which is most props and every static model group):

    0x00  u32 version (2), u32 extra sections (0 here), u32 shape count, u32 instance count, u32 ?,
          u32 total hull vertices, u32 total hull edge indices, u32 ?, u32 ?, u32 total hull faces, ...
    0x30  shapes, 0x38 bytes each: a runtime pointer block, then at +0x28 u32 vertex count, edge index count,
          face count
          (16-byte aligned)
          instances, 0x40 bytes each: f32 position xyz (w 1), f32 rotation quaternion xyzw, then f32 radius at
          +0x20, u32 shape kind at +0x24 (0 sphere, 1 capsule, 2 and 3 convex hull), u32 shape index at +0x34;
          a capsule keeps its half length in the position's w and runs along its local Y
          hull vertices, f32 xyzw each, shape after shape
    then each hull's faces (8 bytes each) and edges (two u32 index lists), which the hull rebuilds from its
    points instead.

Instances are in the owner's space: a static model's own, or for a static model group the group's.
"""
import functools
import itertools
import struct

import numpy as np

PHYSICS_RESOURCE = 0x41759364
SPHERE, CAPSULE = 0, 1
HULLS = (2, 3)


def _quaternion(x, y, z, w):
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def round_mesh(radius, half=0.0, rings=6, segments=10):
    """A sphere (half 0) or a capsule along Y with hemispheres half above and below the centre, as
    (vertices, outward triangles): latitude rows from the top pole, the equator row once per hemisphere."""
    rows = [np.array([[0.0, radius + half, 0.0]])]
    phi = 2 * np.pi * np.arange(segments) / segments
    for k in range(1, rings):
        theta = np.pi * k / rings
        for shift in ((half,) if k < rings / 2 else (-half,) if k > rings / 2 else (half, -half) if half else (0.0,)):
            rows.append(np.c_[radius * np.sin(theta) * np.cos(phi), np.full(segments, radius * np.cos(theta) + shift),
                              radius * np.sin(theta) * np.sin(phi)])
    rows.append(np.array([[0.0, -radius - half, 0.0]]))
    vertices = np.concatenate(rows)
    starts = np.cumsum([0] + [len(r) for r in rows])
    triangles = []
    for r in range(len(rows) - 1):
        a, b = starts[r], starts[r + 1]
        for j in range(segments):
            k = (j + 1) % segments
            if len(rows[r]) == 1:
                triangles.append((a, b + k, b + j))
            elif len(rows[r + 1]) == 1:
                triangles.append((a + j, a + k, b))
            else:
                triangles += [(a + j, a + k, b + k), (a + j, b + k, b + j)]
    return vertices, np.array(triangles, np.int64)


@functools.lru_cache(maxsize=128)
def _triples(n):
    return np.array(list(itertools.combinations(range(n), 3)), np.int64)


def _cross(a, b):
    """np.cross for (N, 3) rows, without its axis shuffling (most of a small hull's time)."""
    return np.stack((a[:, 1] * b[:, 2] - a[:, 2] * b[:, 1], a[:, 2] * b[:, 0] - a[:, 0] * b[:, 2],
                     a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]), axis=1)


def convex_hull(points):
    """Triangles (outward) of the convex hull of a few dozen points: every plane through three points with all
    points on one side is a face; each face's points are ordered around it and fanned. Returns (vertices,
    triangles) or None when the points are flat."""
    p = np.unique(np.round(np.asarray(points, np.float64), 5), axis=0)
    if len(p) < 4:
        return None
    size = float(np.ptp(p, axis=0).max()) or 1.0
    eps = 1e-4 * size
    if np.linalg.matrix_rank(p - p.mean(axis=0), tol=eps) < 3:
        return None
    planes = []
    combos = _triples(len(p))
    for chunk in (np.array_split(combos, len(combos) // 20000) if len(combos) > 40000 else (combos,)):
        a, b, c = p[chunk[:, 0]], p[chunk[:, 1]], p[chunk[:, 2]]
        n = _cross(b - a, c - a)
        length = np.sqrt((n * n).sum(axis=1))
        ok = length > 1e-9 * size * size
        n, a = n[ok] / length[ok, None], a[ok]
        d = (n * a).sum(axis=1)
        dist = p @ n.T - d
        below, above = (dist <= eps).all(axis=0), (dist >= -eps).all(axis=0)
        planes.append(np.c_[n[below], d[below]])
        planes.append(np.c_[-n[above], -d[above]])
    planes = np.concatenate(planes)
    if not len(planes):
        return None
    keys = np.round(planes / np.array([1e-3, 1e-3, 1e-3, 1e-3 * size]))
    _, first = np.unique(keys, axis=0, return_index=True)
    planes = planes[np.sort(first)]
    on = np.abs(p @ planes[:, :3].T - planes[:, 3]) <= eps  # points x faces
    vertices, triangles = [], []
    for f in range(len(planes)):
        face = p[on[:, f]]
        if len(face) < 3:
            continue
        nx, ny, nz = planes[f, :3]
        rel = face - face.mean(axis=0)
        u = rel[0] / (np.sqrt(rel[0] @ rel[0]) or 1.0)
        v = np.array((ny * u[2] - nz * u[1], nz * u[0] - nx * u[2], nx * u[1] - ny * u[0]))
        order = np.argsort(np.arctan2(rel @ v, rel @ u))
        base = len(vertices)
        vertices.extend(face[order])
        triangles.extend((base, base + i, base + i + 1) for i in range(1, len(face) - 1))
    if not triangles:
        return None
    return np.array(vertices), np.array(triangles, np.int64)


def shapes(data):
    """The physics resource's collision shapes as (local convex points, rotation, position, cache key) in the
    owner's space, or None when the layout is one this reader doesn't know (extra sections). Instances of one
    hull or one primitive size share a key, so their hull is built once."""
    if len(data) < 0x30:
        return None
    version, extra, count, instances = struct.unpack_from('<4I', data, 0)
    total = struct.unpack_from('<I', data, 0x14)[0]
    if version != 2 or extra != 0:
        return None
    counts = [struct.unpack_from('<I', data, 0x30 + i * 0x38 + 0x28)[0] for i in range(count)]
    if sum(counts) != total:
        return None
    at = (0x30 + count * 0x38 + 15) & ~15
    vertex_start = at + instances * 0x40
    if vertex_start + total * 16 > len(data):
        return None
    points = np.frombuffer(data, '<f4', total * 4, vertex_start).reshape(-1, 4)[:, :3].astype(np.float64)
    hulls, start = [], 0
    for n in counts:
        hulls.append(points[start:start + n])
        start += n
    out = []
    for i in range(instances):
        e = at + i * 0x40
        px, py, pz, pw, qx, qy, qz, qw, radius = struct.unpack_from('<9f', data, e)
        kind, = struct.unpack_from('<I', data, e + 0x24)
        index, = struct.unpack_from('<I', data, e + 0x34)
        rotation, position = _quaternion(qx, qy, qz, qw), np.array([px, py, pz])
        if not (np.isfinite(rotation).all() and np.isfinite(position).all()):
            continue
        if kind in HULLS and index < len(hulls) and len(hulls[index]) >= 4:
            out.append((hulls[index], rotation, position, ('hull', index)))
        elif kind in (SPHERE, CAPSULE) and radius > 0:
            half = max(pw, 0.0) if kind == CAPSULE else 0.0
            out.append((None, rotation, position, ('round', round(radius, 4), round(half, 4))))
    return out


def collision_sections(data):
    """write_mesh sections for a physics resource: one section of all its shapes, or None."""
    found = shapes(data)
    if not found:
        return None
    built, vertices, triangles, base = {}, [], [], 0
    for local, rotation, position, key in found:
        if key not in built:
            built[key] = round_mesh(key[1], key[2]) if key[0] == 'round' else convex_hull(local)
        if built[key] is None:
            continue
        v, t = built[key]
        vertices.append(v @ rotation.T + position)
        triangles.append(t + base)
        base += len(v)
    if not vertices:
        return None
    pos = np.concatenate(vertices).astype(np.float32)
    idx = np.concatenate(triangles).reshape(-1).astype(np.uint32)
    tri = idx.reshape(-1, 3)
    normals = np.zeros_like(pos)
    face = np.cross(pos[tri[:, 1]] - pos[tri[:, 0]], pos[tri[:, 2]] - pos[tri[:, 0]])
    for j in range(3):
        np.add.at(normals, tri[:, j], face)
    normals /= np.maximum(np.linalg.norm(normals, axis=1), 1e-12)[:, None]
    return [{'name': 'collision', 'positions': pos, 'normals': normals, 'uv': np.zeros((len(pos), 2), np.float32),
             'color': np.ones((len(pos), 3), np.float32), 'palette': np.zeros(len(pos), np.float32), 'indices': idx}]
