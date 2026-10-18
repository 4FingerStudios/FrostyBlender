"""Synthetic water shader/export fixtures; no game data."""
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from gw_water import water_constants, convert_water
from gw2_convert import Gw2Converter


def database():
    # One v5 table: texture record (140 bytes) and float4 record (60 bytes).
    raw = bytearray(28 + 256)
    struct.pack_into('>7I', raw, 0, 1, 5, len(raw)-12, 194, 5, 0, 1)
    struct.pack_into('>6I', raw, 28, 256, 48, 48, 188, 248, 248)
    raw[28+29:28+31] = bytes((1, 1))
    name = b'textures/Water/Water_01_N\0'
    raw[28+52:28+52+len(name)] = name
    raw[28+188:28+195] = b'Color4\0'
    struct.pack_into('>4f', raw, 28+188+44, .1, .3, .5, 0)
    return bytes(raw)


class WaterChecks(unittest.TestCase):
    def test_native_constants_and_malformed_ranges(self):
        raw = database()
        tables = water_constants(raw)
        self.assertEqual(tables[0]['textures'][0]['name'], 'textures/Water/Water_01_N')
        self.assertAlmostEqual(tables[0]['vectors']['Color4'][2], .5)
        self.assertEqual(water_constants(b'unknown'), [])
        with self.assertRaises(ValueError):
            water_constants(raw[:-40])
        corrupt = bytearray(raw)
        struct.pack_into('>I', corrupt, 28+12, 0xffffffff)
        with self.assertRaises(ValueError):
            water_constants(corrupt)

    def test_water_export_preserves_anonymous_databases_and_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            native = database()
            shaders = [{'kind': 'resource', 'resource_type': 0x36f3f2c0, 'resource_id': '0',
                        'name': name+'/shaderdb', 'bytes': len(native)} for name in ('level_a', 'level_b')]
            graph = {'$type': 'ShaderGraph', 'Name': 'Ocean'}
            doc = {'guid': 'source', 'objects': [graph], 'imports': []}
            arc = SimpleNamespace(assets=shaders, inventory={'profile': 'gw1'}, documents={},
                                  data=lambda a: native, resolve=lambda d, ref: (doc, graph))
            converter = SimpleNamespace(archive=arc, assets=assets, materials={}, counts={'vertices': 0, 'triangles': 0})
            water = {'Shader': {'$pointer': 1}, 'TileDimension': 64, 'WaveAmplitudeScale': 1.5}
            mesh, material = convert_water(converter, doc, water)
            exported = json.loads((assets/material).read_text())
            self.assertEqual(exported['source_water'], water)
            self.assertEqual(exported['source_shader'], graph)
            self.assertEqual(len(exported['native_shader_databases']), 2)
            for source in exported['native_shader_databases']:
                self.assertEqual((assets/source['path']).read_bytes(), native)
            self.assertTrue((assets/mesh).is_file())
            convert_water(converter, doc, water)
            self.assertEqual(converter.counts['triangles'], 2)

    def test_export_tint_colour_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            shader = {'VectorParameters': [{'ParameterName': 'Tint_Colour',
                       'Value': dict(zip('xyzw', [.2, .4, .7, 1]))}]}
            doc = {'guid': 'mesh', 'objects': [{'$type': 'MeshMaterial', 'Shader': shader}]}
            archive = SimpleNamespace(inventory={'profile': 'gw1'},
                resolve=lambda d, ref: (doc, doc['objects'][0]) if ref else None)
            converter = Gw2Converter.__new__(Gw2Converter)
            converter.archive, converter.assets = archive, Path(tmp)
            converter.materials, converter.variations, converter.errors = {}, {}, []
            path = converter.material_for((doc, {'Materials': [{'$pointer': 1}], 'Name': 'Mesh'}), 0, 0)
            result = json.loads((Path(tmp)/path).read_text())
            self.assertEqual(result['tint'], [.2, .4, .7, 1])
            self.assertEqual(result['parameters'][0], result['tint'])

    def test_material_variation_overrides_tint_without_losing_base_vectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            def parameter(name, values):
                return {'ParameterName': name, 'Value': dict(zip('xyzw', values))}
            base = {'Shader': {'VectorParameters': [parameter('Tint_Colour', [1, 0, 0, 1]),
                                                   parameter('Smoothness_Scale', [.6, 0, 0, 0])]}}
            override = {'Shader': {'VectorParameters': [parameter('Tint_Colour', [0, 0, 1, 1])]}}
            doc = {'guid': 'mesh', 'objects': [base, override]}
            archive = SimpleNamespace(inventory={'profile': 'gw1'},
                resolve=lambda d, ref: (d, d['objects'][ref['index']]) if ref else None)
            converter = Gw2Converter.__new__(Gw2Converter)
            converter.archive, converter.assets = archive, Path(tmp)
            converter.materials, converter.errors = {}, []
            converter.variations = {('mesh', 7): (doc, {'Materials': [
                {'MaterialVariation': {'$pointer': 1, 'index': 1}}]})}
            path = converter.material_for((doc, {'Materials': [{'$pointer': 1, 'index': 0}], 'Name': 'Mesh'}), 0, 7)
            result = json.loads((Path(tmp)/path).read_text())
            self.assertEqual(result['tint'], [0, 0, 1, 1])
            self.assertEqual(result['vectors']['Smoothness_Scale'][0], .6)


if __name__ == '__main__':
    unittest.main()
