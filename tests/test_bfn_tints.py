"""Battle for Neighborville prop tints and the level colour grade, from synthetic data; no game data."""
import struct
import sys
import unittest
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from convert_legacy import tint_vectors, bfn_tint, bfn_prop_decal
import environment


def vector(name, rgb):
    return {'ParameterName': name, 'Value': {'x': rgb[0], 'y': rgb[1], 'z': rgb[2], 'w': 1.0}, 'ParameterType': 6}


class PropTintChecks(unittest.TestCase):
    def test_quadrant_colours_and_the_unmasked_colour(self):
        shader = {'VectorParameters': [vector('TintColorQ1', (0, .5, .8)), vector('TintColorQ2', (.9, .9, .9)),
                                       vector('TintColorQ3', (1, .7, 0)), vector('TintColorQ4', (.9, 0, 0)),
                                       vector('Tint_Color2', (.5, .1, .1)), vector('ParallaxHeight', (.05, 0, 0))]}
        tint = bfn_tint(tint_vectors(shader), {'__normaltint': None})
        self.assertEqual(tint['quadrants'][2], [1.0, .7, 0.0, 1.0])
        self.assertTrue(tint['masked'])
        self.assertEqual(tint['unmasked'], [.5, .1, .1, 1.0])

    def test_a_single_tint_colours_every_quadrant(self):
        tint = bfn_tint(tint_vectors({'VectorParameters': [vector('Tint_Color', (0, .2, 1.5))]}), {})
        self.assertEqual(tint['quadrants'], [[0.0, .2, 1.5, 1.0]] * 4)
        self.assertFalse(tint['masked'])

    def test_white_and_missing_tints_are_left_out(self):
        self.assertIsNone(bfn_tint(tint_vectors({'VectorParameters': [vector('Tint_Color', (1, 1, 1))]}), {}))
        self.assertIsNone(bfn_tint({}, {}))

    def test_later_sources_override(self):
        vectors = {}
        for source in ({'VectorParameters': [vector('Tint_Color', (1, 0, 0))]},
                       {'VectorParameters': [vector('Tint_Color', (0, 0, 1))]}):
            vectors.update(tint_vectors(source))
        self.assertEqual(vectors['Tint_Color'][:3], [0.0, 0.0, 1.0])


class PropDecalChecks(unittest.TestCase):
    def test_lerp_everything_takes_quadrant_and_stitch_colours(self):
        shader = {'VectorParameters': [vector('DecalColor', (.5, .5, .5)), vector('DecalColorQ4', (.9, .2, .6)),
                                       vector('DecalColor2', (1, .7, 0)),
                                       {'ParameterName': 'DecalSmoothness', 'Value': {'x': .7, 'y': 0, 'z': 0, 'w': 0}}]}
        decal = bfn_prop_decal(tint_vectors(shader), 'art/Shaders/implementations/Props/GW3_Prop_Decal_LerpEverything')
        self.assertEqual(decal['mode'], 'everything')
        self.assertEqual(decal['quadrants'][0], [.5, .5, .5, 1.0])
        self.assertEqual(decal['quadrants'][3], [.9, .2, .6, 1.0])
        self.assertEqual(decal['stitches'], [1.0, .7, 0.0, 1.0])
        self.assertAlmostEqual(decal['smoothness'], .7)

    def test_lerp_normal_has_no_colour_of_its_own(self):
        decal = bfn_prop_decal({}, 'art/Shaders/implementations/Props/GW3_Prop_Decal_LerpNormal')
        self.assertEqual(decal['mode'], 'normal')
        self.assertEqual(decal['quadrants'], [[1.0, 1.0, 1.0, 1.0]] * 4)
        self.assertNotIn('stitches', decal)


def lut_bytes(n, function):
    """A grading LUT resource header and chunk: function(r, g, b) -> (r, g, b), stored as the game does."""
    g = np.arange(n) / (n - 1)
    blue, green, red = np.meshgrid(g, g, g, indexing='ij')
    out = np.stack(function(red, green, blue), -1)[:, ::-1]     # green rows top down
    q = np.round(out * 1023).astype(np.uint32)
    words = q[..., 0] | (q[..., 1] << 10) | (q[..., 2] << 20) | np.uint32(3 << 30)
    header = bytearray(144)
    struct.pack_into('<I', header, 8, 2)
    struct.pack_into('<3H', header, 22, n, n, n)
    return bytes(header), words.astype('<u4').tobytes()


class GradingLutChecks(unittest.TestCase):
    def test_slices_side_by_side_with_green_up(self):
        n = 5
        header, data = lut_bytes(n, lambda r, g, b: (r, g, b))
        width, height, pixels = environment.grading_lut(header, data)
        self.assertEqual((width, height), (25, 5))
        image = np.frombuffer(pixels, np.uint8).reshape(height, width, 4)
        # Row 0 (the top) is green 1; column = blue slice * n + red.
        self.assertEqual(image[0, 0, :3].tolist(), [0, 255, 0])
        self.assertEqual(image[-1, 4, :3].tolist(), [255, 0, 0])
        self.assertEqual(image[-1, 4 * n, :3].tolist(), [0, 0, 255])

    def test_garden_warfare_tables_are_8_bit_with_green_rows_bottom_up(self):
        n = 4
        g = np.arange(n) / (n - 1)
        blue, green, red = np.meshgrid(g, g, g, indexing='ij')
        texels = np.round(np.stack([red, green, blue, np.ones_like(red)], -1) * 255).astype(np.uint8)
        header = bytearray(144)
        struct.pack_into('<II', header, 8, 2, environment.GW_RGBA8)
        struct.pack_into('<3H', header, 18, n, n, n)
        header[32:48] = bytes(range(16))
        self.assertEqual(environment.lut_chunk(bytes(header)), bytes(range(16)).hex())
        width, height, pixels = environment.grading_lut(bytes(header), texels.tobytes())
        image = np.frombuffer(pixels, np.uint8).reshape(height, width, 4)
        self.assertEqual((width, height), (16, 4))
        self.assertEqual(image[0, 0, :3].tolist(), [0, 255, 0])        # top row: green 1
        self.assertEqual(image[-1, 3, :3].tolist(), [255, 0, 0])
        self.assertEqual(image[-1, 3 * n, :3].tolist(), [0, 0, 255])

    def test_other_layouts_are_refused(self):
        header, data = lut_bytes(5, lambda r, g, b: (r, g, b))
        self.assertIsNone(environment.grading_lut(header, data[:-4]))
        flat = bytearray(header)
        struct.pack_into('<I', flat, 8, 0)
        self.assertIsNone(environment.grading_lut(bytes(flat), data))


class GradeChoiceChecks(unittest.TestCase):
    def test_highest_priority_grade_wins_and_underwater_is_skipped(self):
        objects = {}

        def env(name, cc):
            entity = {'Components': [{'id': name + '.cc'}]}
            objects[name + '.entity'] = entity
            objects[name + '.cc'] = dict({'$type': 'ColorCorrectionComponentData', 'Enable': True}, **cc)
            return (None, {'Name': name, 'Object': {'id': name + '.entity'}})

        def resolve(doc, ref):
            return (None, objects[ref['id']]) if ref and ref.get('id') in objects else None

        environments = [
            (8000, '', env('VE_Neutral_Grade', {'ColorGradingEnable': True, 'HdrColorGradingLut': {'id': 'neutral'}})),
            (8500, '', env('VE_Grade_TownCenter', {'ColorGradingEnable': True, 'HdrColorGradingLut': {'id': 'town'}})),
            (14750, '', env('VisEnv_UnderWater', {'ColorGradingEnable': True, 'HdrColorGradingLut': {'id': 'sea'}})),
            (0, '', env('VE_Overtime', {'Saturation': {'x': 1, 'y': .8, 'z': .8}}))]
        out = environment.grade(environments, resolve, lambda doc, ref: 'grade_%s.png' % ref['id'])
        self.assertEqual(out['grade_lut'], 'grade_town.png')
        self.assertEqual(out['grade_environment'], 'VE_Grade_TownCenter')
        self.assertNotIn('grade_saturation', out)

    def test_garden_warfare_grade_without_a_table_keeps_its_colour_correction(self):
        objects = {'e': {'Components': [{'id': 'cc'}]},
                   'cc': {'$type': 'ColorCorrectionComponentData', 'Enable': True, 'ColorGradingEnable': False,
                          'ColorGradingTexture': {'$pointer': 0}, 'Brightness': {'x': 1.1, 'y': 1.1, 'z': 1.1},
                          'Contrast': {'x': 1, 'y': 1, 'z': 1}, 'Saturation': {'x': 1.15, 'y': 1.15, 'z': 1.15}}}

        def resolve(doc, ref):
            return (None, objects[ref['id']]) if ref and ref.get('id') in objects else None
        out = environment.grade([(10, '', (None, {'Name': 'VisEnv_Suburbia', 'Object': {'id': 'e'}}))], resolve,
                                lambda doc, ref: self.fail('no table to read'))
        self.assertNotIn('grade_lut', out)
        self.assertEqual(out['grade_brightness'], [1.1, 1.1, 1.1])
        self.assertNotIn('grade_contrast', out)
        self.assertEqual(out['grade_saturation'], [1.15, 1.15, 1.15])

    def test_no_colour_correction_means_no_grade(self):
        self.assertEqual(environment.grade([], lambda d, r: None, lambda d, r: None), {})


if __name__ == '__main__':
    unittest.main()
