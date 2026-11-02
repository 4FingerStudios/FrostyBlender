"""Garden Warfare 2 Havok collision (tools/gw2_physics.py) on a hand-built packfile; no game data."""
import struct
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import gw2_physics

CUBE = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], float)


def packfile(shapes, compound=None):
    """A version 11 packfile holding convex shapes (each a list of points) and optionally a static compound whose
    instances (translations) place them in order."""
    names = b''.join(b'\0\0\0\0\x09' + n + b'\0' for n in (b'hknpConvexPolytopeShape', b'hknpStaticCompoundShape'))
    convex_name, compound_name = 5, 5 + len(b'hknpConvexPolytopeShape') + 1 + 5
    body, listed = bytearray(), []
    if compound is not None:
        at = len(body)
        record = bytearray(176 + len(compound) * gw2_physics.INSTANCE)
        struct.pack_into('<I', record, 100, len(compound))
        for i, t in enumerate(compound):
            rows = np.zeros((5, 4), '<f4'); rows[:3, :3] = np.eye(3); rows[3, :3] = t; rows[4, :3] = 1
            record[176 + i * 128:176 + i * 128 + 80] = rows.tobytes()
        body += record
        listed.append((at, compound_name))
    for points in shapes:
        at = len(body)
        shape = bytearray(64)
        struct.pack_into('<f', shape, 20, 0.01)
        struct.pack_into('<2H', shape, 48, len(points), 16)
        body += shape + np.c_[points, np.zeros(len(points))].astype('<f4').tobytes()
        listed.append((at, convex_name))
    virtual = len(body)
    for at, name in listed:
        body += struct.pack('<3I', at, 0, name)
    body += b'\xff' * 12
    header = bytearray(272)
    header[0:16] = struct.pack('<4I', 0x57e0e057, 0x10c0c010, 0, 11)
    header[80:94] = b'__classnames__'
    header[208:216] = b'__data__'
    struct.pack_into('<I', header, 100, 272)
    data_start = 272 + len(names)
    struct.pack_into('<I', header, 228, data_start)
    struct.pack_into('<2I', header, 240, virtual, len(body))
    return bytes(header) + names + bytes(body), [0] * 16


def compressed(packed, shared, primitives, shared_index=(), low=(0, 0, 0), high=(10, 10, 10)):
    """A hknpCompressedMeshShapeData with one section whose scale is 1 and offset 0."""
    head = bytearray(0xe0)
    for field, count in ((0x14, 1), (0x50, 1), (0x5c, len(primitives)), (0x68, len(shared_index)), (0x74, len(packed)),
                         (0x80, len(shared))):
        struct.pack_into('<I', head, field, count)
    struct.pack_into('<8f', head, 0x20, *low, 0, *high, 0)
    section = bytearray(0x60)
    struct.pack_into('<6f', section, 0x30, 0, 0, 0, 1, 1, 1)
    struct.pack_into('<3I', section, 0x48, 0, len(packed), len(primitives))
    struct.pack_into('<2B', section, 0x58, len(packed), len(shared_index))
    def padded(raw):
        return raw + bytes(-len(raw) % 16)
    return bytes(head) + bytes(16) + bytes(section) + padded(bytes(b for p in primitives for b in p)) + padded(
        np.array(shared_index, '<u2').tobytes()) + padded(
        np.array([x | y << 11 | z << 22 for x, y, z in packed], '<u4').tobytes()) + padded(
        np.array([x | y << 21 | z << 42 for x, y, z in shared], '<u8').tobytes())


class Gw2PhysicsChecks(unittest.TestCase):
    def test_compressed_mesh_reads_packed_and_shared_vertices_and_quads(self):
        top = 2 ** 21 - 1
        data = compressed([(1, 2, 3), (4, 2, 3), (4, 5, 3)], [(top, 0, 0)], [(0, 1, 2, 3), (0, 1, 2, 2)], shared_index=[0])
        vertices, triangles = gw2_physics.compressed_mesh(data, 0)
        self.assertTrue(np.allclose(vertices, [(1, 2, 3), (4, 2, 3), (4, 5, 3), (10, 0, 0)]))
        self.assertEqual(sorted(map(tuple, triangles.tolist())), [(0, 1, 2), (0, 1, 2), (0, 2, 3)])

    def test_compressed_mesh_drops_a_primitive_naming_no_vertex(self):
        data = compressed([(1, 2, 3), (4, 2, 3), (4, 5, 3)], [], [(0, 1, 2, 2), (222, 173, 222, 173)])
        self.assertEqual(len(gw2_physics.compressed_mesh(data, 0)[1]), 1)

    def test_lone_hull_stays_in_model_space(self):
        data, metadata = packfile([CUBE])
        positions = gw2_physics.collision_sections(data, metadata)[0]['positions']
        self.assertTrue(np.allclose(positions.min(axis=0), -1) and np.allclose(positions.max(axis=0), 1))

    def test_compound_instances_place_shapes_in_order(self):
        data, metadata = packfile([CUBE, CUBE * 0.5], compound=[(0, 5, 0), (10, 0, 0)])
        positions = gw2_physics.collision_sections(data, metadata)[0]['positions']
        self.assertAlmostEqual(float(positions[:, 1].max()), 6.0, places=4)
        self.assertAlmostEqual(float(positions[:, 0].max()), 10.5, places=4)

    def test_parked_pieces_are_left_out(self):
        data, metadata = packfile([CUBE, CUBE], compound=[(0, 0, 0), (0, -1000, 0)])
        positions = gw2_physics.collision_sections(data, metadata)[0]['positions']
        self.assertGreater(float(positions[:, 1].min()), -2.0)

    def test_compound_with_unmatched_instances_is_not_guessed(self):
        data, metadata = packfile([CUBE], compound=[(0, 0, 0), (1, 0, 0)])
        self.assertIsNone(gw2_physics.collision_sections(data, metadata))



def gw1_packfile(shapes):
    """A version 9 packfile from (class name, object bytes), with the container's root count set to 1."""
    names = bytearray()
    body = bytearray(0x50 + 0x30)
    struct.pack_into('<I', body, 0x50 + 12, 1)
    listed = [(0, 'hkRootLevelContainer'), (0x50, 'HavokPhysicsContainer')]
    for name, raw in shapes:
        listed.append((len(body), name))
        body += raw + bytes(-len(raw) % 16)
    virtual = len(body)
    for at, name in listed:
        body += struct.pack('<3I', at, 0, len(names))
        names += name.encode() + b'\0'
    body += b'\xff' * 12
    header = bytearray(256)
    header[0:16] = struct.pack('<4I', 0x57e0e057, 0x10c0c010, 0, 9)
    header[64:78] = b'__classnames__'
    header[160:168] = b'__data__'
    struct.pack_into('<I', header, 84, 256)
    start = 256 + len(names)
    struct.pack_into('<I', header, 180, start)
    struct.pack_into('<4I', header, 192, virtual, len(body), len(body), len(body))
    return bytes(header) + bytes(names) + bytes(body), [0] * 16


def gw1_box(half):
    raw = bytearray(0x30)
    struct.pack_into('<3f', raw, 32, *half)
    return 'hkpBoxShape', bytes(raw)


def gw1_compound(instances, low, high):
    raw = bytearray(128 + len(instances) * 64)
    struct.pack_into('<I', raw, 36, len(instances))
    struct.pack_into('<3f', raw, 0x50, *low)
    struct.pack_into('<3f', raw, 0x60, *high)
    for i, (position, quaternion) in enumerate(instances):
        row = 128 + i * 64
        struct.pack_into('<3f', raw, row, *position)
        struct.pack_into('<4f', raw, row + 16, *quaternion)
        struct.pack_into('<3f', raw, row + 32, 1, 1, 1)
    return 'hkpStaticCompoundShape', bytes(raw)


class Gw1PhysicsChecks(unittest.TestCase):
    TURN = (0.7071068, 0, 0, 0.7071068)  # 90 degrees about x
    STILL = (0, 0, 0, 1)

    def read(self, shapes):
        data, metadata = gw1_packfile(shapes)
        return gw2_physics.collision_sections(data, metadata)

    def test_lone_box(self):
        positions = self.read([gw1_box((1, 2, 3))])[0]['positions']
        self.assertTrue(np.allclose(positions.max(axis=0), (1, 2, 3)))

    def test_compound_places_its_shapes_in_order(self):
        shapes = [gw1_compound([((0, 5, 0), self.TURN), ((10, 0, 0), self.STILL)], (-1, -1, -1), (11, 8, 1)),
                  gw1_box((1, 1, 3)), gw1_box((1, 1, 1))]
        positions = self.read(shapes)[0]['positions']
        self.assertAlmostEqual(float(positions[:, 1].max()), 8.0, places=4)  # the first box's z is now its height
        self.assertAlmostEqual(float(positions[:, 0].max()), 11.0, places=4)

    def test_compound_whose_shapes_miss_its_bounds_is_not_guessed(self):
        shapes = [gw1_compound([((0, 5, 0), self.TURN), ((10, 0, 0), self.STILL)], (-1, -1, -1), (11, 8, 1)),
                  gw1_box((1, 1, 1)), gw1_box((1, 1, 3))]
        self.assertIsNone(self.read(shapes))

    def test_compound_sharing_one_shape_uses_it_for_every_instance(self):
        shapes = [gw1_compound([((0, 0, 0), self.STILL), ((10, 0, 0), self.STILL)], (-1, -1, -1), (11, 1, 1)), gw1_box((1, 1, 1))]
        self.assertAlmostEqual(float(self.read(shapes)[0]['positions'][:, 0].max()), 11.0, places=4)

    def test_compound_sharing_several_shapes_is_not_guessed(self):
        shapes = [gw1_compound([((0, 0, 0), self.STILL)] * 3, (-1, -1, -1), (1, 1, 1)), gw1_box((1, 1, 1)), gw1_box((1, 1, 1))]
        self.assertIsNone(self.read(shapes))

    def test_unknown_shape_is_not_guessed(self):
        self.assertIsNone(self.read([('hkpMysteryShape', bytes(64))]))


if __name__ == '__main__':
    unittest.main()
