"""Terrains where no compositing shader draws every layer: a synthetic shader database linking each layer
combination's graph to its shader and texture table (tools/gw2_terrain.py combination_layers); no game data."""
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import gw2_terrain
from gw2_terrain import combination_layers, _shader_database


def djb(name):
    h = 5381
    for c in name.encode():
        h = ((h * 33) ^ c) & 0xffffffff
    return h


def dxbc(stage):
    """A minimal DXBC container: one SHEX chunk whose version token names the stage (0 pixel, 1 vertex)."""
    chunk = b'SHEX' + struct.pack('<2I', 8, (stage << 16) | 0x50) + bytes(4)
    head = b'DXBC' + bytes(16) + struct.pack('<3I', 1, 0, 1) + struct.pack('<I', 36)
    blob = bytearray(head + chunk)
    struct.pack_into('<I', blob, 24, len(blob))
    return bytes(blob)


def binding_object(textures):
    """A binding object: its u64 size, then 152-byte texture entries (u32 slot, u32 0, path)."""
    body = bytearray(40)
    for slot, path in textures.items():
        entry = bytearray(152)
        struct.pack_into('<2I', entry, 0, slot, 0)
        entry[8:8 + len(path)] = path.encode()
        body += entry
    return struct.pack('<Q', 8 + len(body)) + bytes(body)


def database(graphs):
    """graphs: [(graph name, texture table)]. Graph n uses pixel shader n (after one vertex shader, which the
    pixel-shader count skips) and binding object n."""
    out = bytearray(0x18) + struct.pack('<I', len(graphs))
    for _, textures in graphs:
        out += binding_object(textures)
    for stage in [1] + [0] * len(graphs):
        out += bytes(20) + struct.pack('<I', 0) + dxbc(stage)
    out += bytes(20) + struct.pack('<I', len(graphs))
    for n in range(len(graphs)):
        solution = bytearray(144)
        struct.pack_into('<Q', solution, 32, n)
        struct.pack_into('<Q', solution, 72, n)
        out += solution
    out += struct.pack('<I', len(graphs))
    for name, _ in graphs:
        out += struct.pack('<I', djb(name)) + bytes(28)
    return bytes(out)


SNOW = {4: 'Worlds/Snow/Textures/Snow_CarpetA_VarA_Col', 5: 'Worlds/Snow/Textures/Snow_CarpetA_Normal'}
MUD = {6: 'Worlds/Terrain/_Textures/TRN_MudA_Color'}
PATH = {6: 'Worlds/Terrain/_Textures/TRN_CobblePathA_Color', 7: 'Worlds/Terrain/_Textures/TRN_CobblePathA_Normal'}


class ShaderDatabaseLinks(unittest.TestCase):
    def test_a_graph_finds_its_pixel_shader_and_texture_table(self):
        data = database([('t__0mv_3mv__2d__0', {**SNOW, **MUD}), ('t__1mv_3mv__2d__0', {**SNOW, **PATH})])
        ps, records, solution, table = _shader_database(data)
        self.assertEqual(len(ps), 2)
        record, = records[djb('t__1mv_3mv__2d__0')]
        shader, binding = solution(record)
        self.assertEqual((shader, binding), (1, 1))
        self.assertEqual(table(binding), {**SNOW, **PATH})

    def test_other_files_are_not_read_as_a_database(self):
        self.assertIsNone(_shader_database(bytes(64)))


class CombinationLayers(unittest.TestCase):
    """Each combination's shader weights its first layer by the rest, and mask n the n-th layer after the base
    layer (0), or the n-th layer when the combination leaves the base out."""
    def weights(self, shader):
        # read_compositing's (scales, heights, {position: registers}); position 0 is the rest, n + 1 is mask n.
        # Shader 0 draws layers 0 and 3; shader 1 draws 1, 3 and 5, and its mask 0 (layer 1's) is unused.
        return [({}, {}, {0: {4, 5}, 1: {6}}),
                ({6: 0.15}, {1: (7, 2, 10.0)}, {0: {4, 5}, 2: {6, 7}, 3: {4}})][shader]

    def test_layers_are_put_together_from_every_combination(self):
        data = database([('t__0mv_3mv__2d__0', {**SNOW, **MUD}), ('t__1mv_3mv_5mv__2d__0', {**SNOW, **PATH})])
        ps = _shader_database(data)[0]
        with mock.patch.object(gw2_terrain, '_bindings', lambda d, at: {'texture_maskMap': 3}), \
                mock.patch.object(gw2_terrain, '_disassemble', lambda d, at: str(ps.index(at))), \
                mock.patch.object(gw2_terrain, 'read_compositing', lambda text, mask: self.weights(int(text))):
            found = combination_layers(data, {'t__0mv_3mv__2d__0': [0, 3], 't__1mv_3mv_5mv__2d__0': [1, 3, 5]})
        self.assertEqual(found[0]['paths'], list(SNOW.values()))
        self.assertEqual(found[1]['paths'], list(SNOW.values()))
        # layer 3 is mud where it is first drawn; the second combination adds the path textures to it
        self.assertEqual(found[3]['paths'][0], MUD[6])
        self.assertEqual(found[3]['scale'], 0.15)
        self.assertEqual(found[3]['height'], {'path': PATH[7], 'channel': 2, 'contrast': 10.0})
        self.assertEqual(found[5]['paths'], [SNOW[4]])

    def test_no_combinations_give_none(self):
        self.assertIsNone(combination_layers(database([]), {}))


if __name__ == '__main__':
    unittest.main()
