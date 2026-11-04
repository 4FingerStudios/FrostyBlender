"""Synthetic GW terrain decal resources (roads laid over the terrain); no game data."""
import struct
import sys
import unittest
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from terrain_decals import parse, bounds_key


def vertex(position, uv, gw1=False):
    out = struct.pack('<%df' % len(position), *position)
    if gw1:
        return out + struct.pack('<2f', *uv) + np.array([1, 1, 1, 1], '<f2').tobytes()
    halves = [0, 1, 0, 0, 1, 0, 0, 0, 0, uv[0], 0, uv[1], 1, 1, 1, 1]
    return out + np.array(halves, '<f2').tobytes()


def group(records, gw1=False):
    """records: (shader, [positions], [uvs]); each is one quad (two triangles)."""
    head, vertices, indices = struct.pack('<I', len(records)), b'', []
    for shader, positions, uvs in records:
        data = b''.join(vertex(p, uv, gw1) for p, uv in zip(positions, uvs))
        xs = [p[0] for p in positions]
        zs = [p[-1] for p in positions]
        box = (min(xs), 64, min(zs), max(xs), 64, max(zs))
        head += shader.encode() + b'\0' + bytes(2 if gw1 else 3)
        head += struct.pack('<II', len(indices), 2) + struct.pack('<13f', *box, 0, 64, 0, 128, 64, 128, .01)
        head += struct.pack('<II', len(vertices), len(data)) + b'\1'
        vertices += data
        indices += [0, 1, 2, 0, 2, 3]
    index_data = struct.pack('<%dI' % len(indices), *indices)
    return head + struct.pack('<II', len(vertices), len(index_data)) + vertices + index_data


HEADER = struct.pack('<fIIB', 120, 0, 1, 0)
QUAD_XZ = [(0, 0), (4, 0), (4, 10), (0, 10)]
UVS = [(0, 0), (1, 0), (1, -2.5), (0, -2.5)]


class DecalChecks(unittest.TestCase):
    def test_flat_road_and_its_raised_twin(self):
        data = HEADER + group([('art/Shaders/Roads/RoadA_2D', QUAD_XZ, UVS)]) + group(
            [('art/Shaders/Roads/RoadA_3D', [(x, 64, z) for x, z in QUAD_XZ], UVS)])
        flat, raised = parse(data)
        self.assertTrue(flat['flat'] and np.isnan(flat['positions'][:, 1]).all())
        self.assertFalse(raised['flat'])
        np.testing.assert_allclose(raised['positions'][2], (4, 64, 10))
        np.testing.assert_allclose(raised['uv'][2], (1, -2.5))
        self.assertEqual(raised['indices'].tolist(), [[0, 1, 2], [0, 2, 3]])
        self.assertEqual(bounds_key(flat), bounds_key(raised))

    def test_empty_groups_are_only_their_count(self):
        data = HEADER + struct.pack('<2I', 0, 0) + group([('art/Shaders/Water/Water_Pond_01', [(x, 5, z) for x, z in QUAD_XZ], UVS)])
        piece, = parse(data)
        self.assertEqual(piece['shader'], 'art/Shaders/Water/Water_Pond_01')

    def test_garden_warfare_1_layout(self):
        data = HEADER + group([('Systems/Terrain/Decal/WhiteDecal3d', [(x, 38, z) for x, z in QUAD_XZ], UVS)], gw1=True)
        piece, = parse(data, name_padding=2)
        np.testing.assert_allclose(piece['positions'][1], (4, 38, 0))
        np.testing.assert_allclose(piece['uv'][3], (0, -2.5))
        np.testing.assert_allclose(piece['colour'][0], (1, 1, 1))


if __name__ == '__main__':
    unittest.main()
