"""Synthetic GW2 terrain colour map pages and holes; no game data."""
import struct
import sys
import unittest
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from gw2_terrain import (bc1, colour_pages, tile_colour, mask_pages, tile_heights, height_section, HEIGHT_PAGE,
                         WIDE_PAGE, COLOUR_SIDE)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_gw2_terrain_paint import tree, page


def rgb565(r, g, b):
    return (r >> 3) << 11 | (g >> 2) << 5 | b >> 3


def solid(colour, blocks=65 * 65):
    """A BC1 page of one colour: both endpoints the colour, every index 0."""
    value = rgb565(*colour)
    return struct.pack('<4H', value, value, 0, 0) * blocks


class Bc1Checks(unittest.TestCase):
    def test_endpoints_and_interpolated_colours(self):
        white, black = rgb565(255, 255, 255), 0
        # Indices per pixel, row-major: 0, 1, 2, 3 across each row.
        indices = sum(i << (2 * (row * 4 + i)) for row in range(4) for i in range(4))
        block = struct.pack('<2HI', white, black, indices)
        pixels = bc1(block, 4, 4)
        self.assertEqual(pixels[0, 0].tolist(), [255, 255, 255])
        self.assertEqual(pixels[0, 1].tolist(), [0, 0, 0])
        self.assertEqual(pixels[2, 2].tolist(), [170, 170, 170])  # 2/3 white
        self.assertEqual(pixels[3, 3].tolist(), [85, 85, 85])

    def test_three_colour_blocks(self):
        grey, white = rgb565(128, 128, 128), rgb565(255, 255, 255)
        block = struct.pack('<2HI', grey, white, 0b11_10)  # pixel 0: half way, pixel 1: black
        pixels = bc1(block, 4, 4)
        self.assertEqual(pixels[0, 1].tolist(), [0, 0, 0])
        self.assertAlmostEqual(int(pixels[0, 0, 0]), (132 + 255) // 2, delta=1)


class ColourPageChecks(unittest.TestCase):
    def terrain(self):
        t = tree()
        t['rasters'][2] = b'\x04\x01'
        t['samples'] = 66
        green = solid((64, 160, 64))
        chunks = {'main': bytes(HEIGHT_PAGE) + page(10).tobytes() + page(20).tobytes() + green,
                  'extra': page(30).tobytes()}
        return t, chunks

    def test_colour_page_follows_the_mask_pages(self):
        t, chunks = self.terrain()
        (rect, colour), = colour_pages(t, chunks.get)
        self.assertEqual(rect, (0, 0, 512, 512))
        self.assertEqual(colour.shape, (COLOUR_SIDE, COLOUR_SIDE, 3))
        self.assertEqual(colour[100, 100].tolist(), [66, 162, 66])
        # The mask pages are still found in front of it.
        found = {(layer, rect): int(p[0, 0]) for layer, rect, p in mask_pages(t, chunks.get)}
        self.assertEqual(found[(3, (256, 256, 512, 512))], 10)
        self.assertEqual(found[(5, (0, 0, 256, 256))], 20)

    def test_terrain_without_raster_2_has_no_colour(self):
        t, chunks = self.terrain()
        del t['rasters'][2]
        self.assertEqual(colour_pages(t, chunks.get), [])

    def test_finer_page_wins_and_uncovered_is_neutral(self):
        coarse = np.full((COLOUR_SIDE, COLOUR_SIDE, 3), 255, np.uint8)
        fine = np.zeros((COLOUR_SIDE, COLOUR_SIDE, 3), np.uint8)
        pages = [((0, 0, 1024, 1024), coarse), ((0, 0, 256, 256), fine)]
        colour = tile_colour(pages, (0, 0, 512, 512))
        self.assertEqual(colour.shape[0], 256)  # the finest page: 1 m texels, at most 256
        self.assertAlmostEqual(float(colour[0, 0, 0]), 0.0)
        self.assertAlmostEqual(float(colour[-1, -1, 0]), 1.0)
        self.assertIsNone(tile_colour(pages, (2048, 2048, 2560, 2560)))


class HoleChecks(unittest.TestCase):
    def test_a_tiles_own_zero_samples_are_holes_not_its_parents_floor(self):
        t = {'samples': 66, 'scale': 1024.0, 'bounds': (0, 1, 0, 64, 100, 64), 'endian': '<'}
        side = 133
        heights = np.full((side, side), 2000, np.uint16)
        heights[40:80, 40:80] = 0
        raw = heights.tobytes() + bytes(HEIGHT_PAGE - side * side * 2)
        tile = {'rect': (0, 0, 64, 64), 'size': len(raw)}
        floor = (np.full((129, 129), 830, np.float32), (0, 0, 64, 64))
        own = tile_heights(raw, tile, t, floor)
        self.assertEqual(int((own == 0).sum()), 40 * 40)
        section = height_section(None, tile, t, heights=own)
        full = 2 * 128 * 128
        self.assertLess(len(section['indices']) // 3, full - 2 * 39 * 39)

    def test_tiles_without_heights_take_their_parents(self):
        t = {'samples': 66, 'scale': 1024.0, 'bounds': (0, 1, 0, 64, 100, 64), 'endian': '<'}
        floor = (np.full((129, 129), 830, np.float32), (0, 0, 64, 64))
        inherited = tile_heights(None, {'rect': (0, 0, 32, 32), 'size': 0}, t, floor)
        self.assertTrue((inherited == 830).all())


if __name__ == '__main__':
    unittest.main()
