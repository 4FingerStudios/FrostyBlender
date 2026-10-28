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


class Gw2PhysicsChecks(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
