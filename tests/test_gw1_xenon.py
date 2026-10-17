"""Synthetic format fixtures; no game data is included in these tests."""
import struct
import sys
import unittest
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import gw2
import gw1_xenon
import cosmetics
from convert import read_attr
from gw2_terrain import terrain_tree, tile_heights
from gw2_groups import gw1_part_poses


def hash_name(value):
    result = 5381
    for byte in value.encode():
        result = ((result * 33) ^ byte) & 0xffffffff
    return result


def ebx_fixture(endian):
    """One reflected exported object with string, resource, float, GUID and pointer fields."""
    names = b'Asset\0Name\0Resource\0Scale\0Guid\0Link\0'
    fields = [('Name', 7, 8), ('Resource', 23, 16), ('Scale', 19, 24), ('Guid', 21, 28), ('Link', 3, 44)]
    data = bytearray(608)
    struct.pack_into(endian + '4I6H3I', data, 0, 0x0fb2d1ce, 512, 96, 0,
                     1, 1, 1, 1, len(fields), len(names), 16, 0, 80)
    canonical = bytes.fromhex('1234567890abcdef1234567890abcdef')
    encoded_guid = bytes.fromhex(gw2.guid_big(canonical)) if endian == '>' else canonical
    data[40:56] = encoded_guid
    data[64:64 + len(names)] = names
    at = 64 + len(names)
    for name, kind, offset in fields:
        struct.pack_into(endian + 'IHHII', data, at, hash_name(name), kind << 4, 0, offset, 0)
        at += 16
    struct.pack_into(endian + 'IIBBHHH', data, at, hash_name('Asset'), 0, 5, 8, 0, 48, 0)
    struct.pack_into(endian + 'HH', data, at + 16, 0, 1)
    data[512:520] = b'fixture\0'
    data[528:544] = encoded_guid
    struct.pack_into(endian + 'I', data, 552, 0)
    struct.pack_into(endian + 'Q', data, 560, 0x123456789abcdef0)
    struct.pack_into(endian + 'f', data, 568, 1.25)
    data[572:588] = encoded_guid
    struct.pack_into(endian + 'I', data, 588, 1)
    return bytes(data)


class FormatChecks(unittest.TestCase):
    def test_disco_skeleton_in_shared_character_folder(self):
        names = ['characters/browncoatzombie/rigandmodel/browncoatzombie_skeleton',
                 'characters/browncoatzombie/rigandmodel/discozombie_skeleton']
        self.assertEqual(cosmetics.pick_skeleton(names, 'discozombie'), [names[1]])
        item = {'bundle': 'Xenon/_pvz/animations/characters/zombies/browncoatzombie/costumes/browncoatzombie_costumedisco_unlockasset_bpb'}
        self.assertEqual(cosmetics.item_character('gw1', item), 'discozombie')

    def test_gravedigger_body_can_use_browncoat_skeleton(self):
        names = ['characters/browncoatzombie/rigandmodel/browncoatzombie_skeleton']
        self.assertIn(names[0], cosmetics.skeleton_candidates(names, 'gravedigger'))

    def test_big_and_little_endian_ebx_match(self):
        little = gw2.Ebx(ebx_fixture('<')).document()
        big = gw2.Ebx(ebx_fixture('>')).document()
        self.assertEqual(little.pop('endian'), '<')
        self.assertEqual(big.pop('endian'), '>')
        self.assertEqual(little, big)
        self.assertEqual(big['objects'][0]['Name'], 'fixture')
        self.assertEqual(big['objects'][0]['Resource']['$resource'], str(0x123456789abcdef0))
        self.assertEqual(big['objects'][0]['Scale'], 1.25)
        self.assertEqual(big['objects'][0]['Link'], {'$pointer': 1, 'index': 0})

    def test_truncated_ebx_rejected(self):
        for endian in ('<', '>'):
            with self.subTest(endian=endian), self.assertRaises(ValueError):
                gw2.Ebx(ebx_fixture(endian)[:60])

    def test_vertex_byte_orders(self):
        expected = np.array([[1, -2, 3], [4, 5, -6]], np.float32)
        for endian in ('<', '>'):
            raw = expected.astype(endian + 'f4').tobytes()
            np.testing.assert_array_equal(read_attr(raw, 0, 2, 12, 3, 0, endian), expected)

    def test_packed_signed_normals(self):
        # Independently packed Comp3N_11_11_10 landmarks and fractional components.
        for endian in ('<', '>'):
            raw = struct.pack(endian + '3I', 1023 | (1025 << 11),
                              1024 | (1023 << 11) | (511 << 22),
                              256 | (1792 << 11) | (513 << 22))
            expected = [[1, -1, 0], [-1, 1, 1], [256/1023, -256/1023, -1]]
            np.testing.assert_allclose(read_attr(raw, 0, 3, 4, 43, 0, endian), expected, atol=1e-7)

    def test_vertex_bounds_checked(self):
        with self.assertRaises(ValueError):
            read_attr(bytes(3), 0, 1, 4, 43, 0, '>')

    def test_tiling_known_hardware_addresses(self):
        # Each physical 8-byte block stores its byte address. Check independent tile landmarks.
        raw = np.arange(16384 // 8, dtype='<u8').tobytes()
        result = np.frombuffer(gw1_xenon.untile(raw, 64, 32, 8), '<u8').reshape(32, 64)
        self.assertEqual(result[0, 0], 0)
        self.assertEqual(result[0, 1], 1)
        self.assertEqual(result[1, 0], 2)
        self.assertEqual(result[0, 8], 8)
        self.assertEqual(result[16, 0], 256)
        self.assertEqual(result[0, 32], 1024)
        self.assertEqual(len(np.unique(result)), 64 * 32)

    def test_truncated_tiled_surface_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Truncated'):
            gw1_xenon.untile(bytes(8), 32, 32, 8)

    def test_texture_normalized_to_pc_reader(self):
        header = bytearray(128)
        struct.pack_into('>II', header, 8, 0, 0)
        struct.pack_into('>4H', header, 18, 32, 32, 1, 1)
        header[26] = 6
        header[28:44] = bytes(range(16))
        out, chunk = gw1_xenon.texture(header, bytes(8192))
        self.assertEqual(len(out), 144)
        self.assertEqual(struct.unpack_from('<I', out, 12)[0], 54)
        self.assertEqual(out[26], 1)
        self.assertEqual(out[32:48].hex(), gw2.guid_big(header[28:44]))
        self.assertEqual(len(chunk), 512)
        self.assertEqual(struct.unpack_from('<I', out, 48)[0], 512)

    def test_mesh_reference_layouts(self):
        for endian, pointer_size, offset in (('<', 8, 88), ('<', 8, 96), ('>', 4, 68)):
            data = bytearray(256)
            struct.pack_into(endian + ('Q' if pointer_size == 8 else 'I'), data, 32, 64)
            data[64 + offset:64 + offset + 16] = bytes(range(16))
            expected = gw2.guid_big(bytes(range(16))) if endian == '>' else bytes(range(16)).hex()
            self.assertIn(expected, gw2.mesh_chunk_guids(data))

    def test_console_chunk_prefix_is_added(self):
        # A 32 KiB prefix and 32 KiB payload occupy a whole compression block.
        header = [80, 0xed1cedbb ^ 0x7065636e, 1, 0, 0, 1, 0, 0, 0]
        raw = struct.pack('>9I', *header) + bytes(20) + bytes(16) + struct.pack('>II', 0x28000, 0x8000) + bytes(4)
        assets, _ = gw2.bundle(raw)
        self.assertEqual(assets[0]['original_size'], 65536)

    def test_terrain_tree_byte_orders(self):
        for endian in ('<', '>'):
            start = 92 if endian == '>' else 108
            data = bytearray(start)
            struct.pack_into(endian + 'I', data, 0, 256)
            struct.pack_into(endian + 'f', data, 47, 100)
            struct.pack_into(endian + 'I', data, 72 if endian == '>' else 88, 1)
            data += struct.pack(endian + '6f', -1, 0, -1, 1, 100, 1) + b'\1'
            data += b'\1' + struct.pack(endian + 'III', 8, 66, 2) + b'\xff'
            data += bytes(4 + 16 + 3)
            tree = terrain_tree(data)
            self.assertEqual(tree['samples'], 66)
            self.assertEqual(tree['bounds'], (-1, 0, -1, 1, 100, 1))

    def test_xenon_height_pages(self):
        raw = np.full(133 * 133, 12345, dtype='>u2').tobytes() + bytes(36534 - 133 * 133 * 2)
        heights = tile_heights(raw, {'size': len(raw)}, {'samples': 66, 'endian': '>'})
        self.assertEqual(heights.shape, (129, 129))
        self.assertTrue(np.all(heights == 12345))

    def test_group_instances_exclude_nested_shape_transforms(self):
        data = bytearray(1280)
        struct.pack_into('>4I', data, 64, 0x57e0e057, 0x10c0c010, 0, 9)
        for start, count, flags in ((128, 3, 0x01000000), (512, 8, 0)):
            for index in range(count):
                at = start + index * 64
                struct.pack_into('>12f', data, at, index * 2, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 0)
                struct.pack_into('>I', data, at + 60, flags | (index if flags else 0))
        poses = gw1_part_poses(data, struct.pack('>4I', 64, 0, 0, 0), '>')
        self.assertEqual(sorted(poses), [0, 1, 2])
        self.assertEqual(poses[2][0, 3], 4)


if __name__ == '__main__':
    unittest.main()
