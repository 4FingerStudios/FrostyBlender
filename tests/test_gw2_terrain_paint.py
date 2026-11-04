"""Synthetic GW2 painted-terrain fixtures (mask tree, mask pages, layer textures); no game data."""
import struct
import sys
import unittest
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from gw2_terrain import (mask_tree, mask_pages, tile_masks, layer_textures, read_compositing, scatter_meshes,
                         scatter_weights, HEIGHT_PAGE, MASK_PAGE)


def quadrants(x, z, X, Z):
    mx, mz = (x + X) / 2, (z + Z) / 2
    return [(x, z, mx, mz), (mx, z, X, mz), (mx, mz, X, Z), (x, mz, mx, Z)]


def group(node, pages):
    """One node's records: its four children, then (layer, quadrant) pages. The byte closing each record is the
    layer of the next one."""
    records = [(0, r) for r in quadrants(*node)] + list(pages)
    out = bytearray(struct.pack('<2H', len(records), len(pages)) + bytes(3))
    for i, (_, rect) in enumerate(records):
        following = records[i + 1][0] if i + 1 < len(records) else 0
        child = int(i < 4)
        out += struct.pack('<4f', *rect) + bytes((1, child)) + b'\xff' * 10 + b'\x1f' + bytes([following])
    return bytes(out)


def raster(groups, bounds=(-512, -512, 512, 512)):
    count = sum(len(pages) + 4 for _, pages in groups)
    out = bytearray(struct.pack('<6I4f2I', 66, 2, 1, 2048, 2048, 1, *bounds, len(groups), count))
    for i, (node, pages) in enumerate(groups):
        # Before each node: two bytes, then a zero 7-byte entry for each sibling without pages before it.
        out += bytes(2) + bytes(7 * (i % 2)) + group(node, pages)
    return bytes(out)


WORLD = (-512, -512, 512, 512)
NODE = (0, 0, 512, 512)          # a tile
DEEP = (256, 256, 512, 512)      # below that tile, so its pages live in the tile's extra chunk


def tree(extra=True):
    groups = [(WORLD, []), (NODE, [(3, (256, 256, 512, 512)), (5, (0, 0, 256, 256))]),
              (DEEP, [(3, (384, 384, 512, 512))])]
    tile = {'rect': NODE, 'size': 1, 'guid': 'main', 'extra': (1, 'extra') if extra else None,
            'metadata': 0, 'children': []}
    root = {'rect': WORLD, 'size': 0, 'guid': '0' * 32, 'extra': None, 'metadata': 0, 'children': [tile]}
    return {'rasters': {1: raster(groups)}, 'tiles': [root, tile]}


def page(value):
    return np.full((66, 66), value, np.uint8)


class MaskTreeChecks(unittest.TestCase):
    def test_groups_list_each_node_and_its_layer_pages(self):
        groups = mask_tree(tree()['rasters'][1])
        self.assertEqual([node for node, _ in groups], [WORLD, NODE, DEEP])
        self.assertEqual(groups[1][1], [(3, (256, 256, 512, 512)), (5, (0, 0, 256, 256))])
        self.assertEqual(groups[2][1], [(3, (384, 384, 512, 512))])

    def test_wrong_record_count_is_not_guessed(self):
        data = bytearray(tree()['rasters'][1])
        struct.pack_into('<I', data, 44, 99)
        self.assertIsNone(mask_tree(bytes(data)))


class MaskPageChecks(unittest.TestCase):
    def chunks(self):
        main = bytes(HEIGHT_PAGE) + page(10).tobytes() + page(20).tobytes()
        return {'main': main, 'extra': page(30).tobytes()}

    def test_tile_pages_follow_the_height_page_and_deep_pages_are_in_the_extra_chunk(self):
        data = self.chunks()
        pages = mask_pages(tree(), data.get)
        found = {(layer, rect): int(p[0, 0]) for layer, rect, p in pages}
        self.assertEqual(found, {(3, (256, 256, 512, 512)): 10, (5, (0, 0, 256, 256)): 20,
                                 (3, (384, 384, 512, 512)): 30})

    def test_missing_extra_chunk_leaves_out_only_its_pages(self):
        data = self.chunks()
        del data['extra']
        self.assertEqual(len(mask_pages(tree(), data.get)), 2)

    def test_chunk_too_small_for_its_pages_gives_up(self):
        data = self.chunks()
        data['main'] = page(10).tobytes()
        self.assertIsNone(mask_pages(tree(), data.get))


class TileMaskChecks(unittest.TestCase):
    def test_finer_page_replaces_coarser_one(self):
        pages = [(3, (0, 0, 512, 512), page(255)), (3, (256, 256, 512, 512), page(0))]
        mask = tile_masks(pages, (0, 0, 512, 512), {3})[3]
        side = mask.shape[0]
        self.assertEqual(side, 128)  # the finest page: 4 m texels
        self.assertAlmostEqual(float(mask[0, 0]), 1.0)
        self.assertAlmostEqual(float(mask[-1, -1]), 0.0)

    def test_page_texels_are_centred_inside_its_border(self):
        ramp = np.tile(np.arange(66, dtype=np.uint8) * 3, (66, 1))  # value 3 * column
        mask = tile_masks([(4, (0, 0, 64, 64), ramp)], (0, 0, 64, 64), {4})[4]
        # Output texel 0 sits at x = 0.5 m, the centre of page column 1 (column 0 is the border).
        self.assertAlmostEqual(float(mask[0, 0]) * 255, 3.0, places=3)
        self.assertAlmostEqual(float(mask[0, -1]) * 255, 192.0, places=3)

    def test_rows_follow_z(self):
        ramp = np.tile((np.arange(66, dtype=np.uint8) * 3)[:, None], (1, 66))
        mask = tile_masks([(4, (0, 0, 64, 64), ramp)], (0, 0, 64, 64), {4})[4]
        self.assertLess(float(mask[0, 0]), float(mask[-1, 0]))


def shaderdb(tables):
    out = bytearray(64)
    for table in tables:
        for slot, path in enumerate(table):
            entry = bytearray(152)
            struct.pack_into('<2I', entry, 0, slot + 4, 0)
            entry[8:8 + len(path)] = path.encode()
            out += entry
        out += bytes(40)
    return bytes(out)


class LayerTextureChecks(unittest.TestCase):
    FULL = ['Worlds/Terrain/_Textures/TRN_MudA_Color', 'Worlds/Terrain/_Textures/TRN_MudA_Normal',
            'Worlds/Grass/Textures/TRN_GrassB_Normal', 'Worlds/Grass/Textures/TRN_GrassB_VarB',
            'Worlds/Snow/Textures/Snow_CarpetA_VarA_Col']

    def test_the_shader_combining_every_layer_names_them_in_order(self):
        data = shaderdb([self.FULL[2:], self.FULL])
        self.assertEqual(layer_textures(data, 3), [self.FULL[:2], self.FULL[2:4], self.FULL[4:]])

    def test_no_shader_with_every_layer_gives_none(self):
        self.assertIsNone(layer_textures(shaderdb([self.FULL[2:]]), 3))


# A cut-down terrain compositing shader as the D3D disassembler prints it: three layers, the third sharpened
# by the blue channel of its second texture, then the base, second and third layers added up by their weights.
COMPOSITING = """
//   float4 external_clipToWorldSpaceXzTransform;// Offset:    0 Size:    16
//   float4 external_worldToMask0UvTransform;// Offset:   16 Size:    16
//   float4 external_worldToMask1UvTransform;// Offset:   32 Size:    16
ps_5_0
mad r0.xyzw, v1.xyxy, cb1[0].xyxy, cb1[0].zwzw
mul r1.xyzw, r0.zwzw, l(0.150000, 0.150000, 0.500000, 0.500000)
sample_indexable(texture2d)(float,float,float,float) r2.xyz, r1.xyxx, t4.xyzw, s3
sample_indexable(texture2d)(float,float,float,float) r3.xyz, r1.zwzz, t5.xyzw, s3
sample_indexable(texture2d)(float,float,float,float) r4.xyz, r1.xyxx, t6.xyzw, s3
sample_indexable(texture2d)(float,float,float,float) r4.w, r1.xyxx, t7.wxyz, s3
mad r5.xy, r0.zwzz, cb1[1].xyxx, cb1[1].zwzz
sample_indexable(texture2d)(float,float,float,float) r5.z, r5.xyxx, t3.yzxw, s2
mad r5.xy, r0.zwzz, cb1[2].xyxx, cb1[2].zwzz
sample_indexable(texture2d)(float,float,float,float) r6.x, r5.xyxx, t3.xyzw, s2
lt r6.y, r6.x, l(0.500000)
dp2 r6.z, r4.wwww, r6.xxxx
add r6.w, -r6.x, l(1.000000)
movc r6.x, r6.y, r6.z, r6.w
add r6.x, r6.x, l(-0.500000)
mul_sat r6.x, r6.x, l(9.999998)
add r7.x, -r6.x, l(1.000000)
mul r7.y, r5.z, r7.x
mad r7.z, -r5.z, r7.x, r7.x
lt r7.w, l(0.010000), r7.z
if_nz r7.w
  mul r8.xyz, r2.xyzx, r7.zzzz
else
  mov r8.xyz, l(0,0,0,0)
endif
mad r8.xyz, r3.xyzx, r7.yyyy, r8.xyzx
mad r8.xyz, r4.xyzx, r6.xxxx, r8.xyzx
ret
"""


class CompositingChecks(unittest.TestCase):
    def test_layers_get_their_tiling_textures_and_height_blend(self):
        scales, heights, weights = read_compositing(COMPOSITING, 3)
        self.assertEqual(scales, {4: .15, 5: .5, 6: .15, 7: .15})
        self.assertEqual(weights, {0: {4}, 1: {5}, 2: {6}})
        self.assertEqual(heights, {1: (7, 2, 10.0)})  # .w of the swizzle wxyz is the texture's blue


class ScatterChecks(unittest.TestCase):
    def test_scatter_meshes_by_identifier(self):
        def entry(name, identifier):
            return name.encode() + b'\0' + bytes(8) + bytes(6) + struct.pack('<I', identifier) + bytes(16)
        data = bytes(40) + entry('art/terrain/leaves_Mesh', 0) + entry('worlds/grass/clump_Mesh', 3)
        self.assertEqual(scatter_meshes(data), {0: 'art/terrain/leaves_Mesh', 3: 'worlds/grass/clump_Mesh'})

    def test_painted_layers_above_hide_scatter_below(self):
        masks = {1: np.full((2, 2), .5, np.float32), 2: np.array([[1, 0], [0, 0]], np.float32),
                 9: np.full((2, 2), .25, np.float32)}
        weights = scatter_weights(masks, [0, 1, 9], [0, 1, 2])
        np.testing.assert_allclose(weights[1], [[0, .5], [.5, .5]])
        np.testing.assert_allclose(weights[0], [[0, .5], [.5, .5]])  # the base shows where nothing is painted
        np.testing.assert_allclose(weights[9], masks[9])  # a scatter-only layer above every painted one


if __name__ == '__main__':
    unittest.main()
