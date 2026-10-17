"""GW1 Xenon resources: 32-bit big-endian MeshSets and Xenos tiled textures.

Texture addressing follows the public hardware layout documented by Xenia:
https://github.com/xenia-project/xenia/blob/master/src/xenia/gpu/texture_address.h
Only exported copies are normalized; game archives are always read-only.
"""
import struct
import numpy as np
import gw2
from convert import read_attr, skin_attrs

TEXTURE = 0xACD91FE8


def mesh_sections(archive, resource, skinned=False):
    from gw2_convert import SkinnedMeshUnsupported
    data = archive.data(resource)
    lod = struct.unpack_from('>I', data, 32)[0]
    if not 0 < lod or lod + 84 > len(data):
        raise ValueError('Invalid Xenon mesh LOD')
    kind, _, count, table = struct.unpack_from('>4I', data, lod)
    if kind == 1 and not skinned:
        raise SkinnedMeshUnsupported('Xenon skinned mesh requires animation support')
    if kind not in (0, 1, 2) or not 0 < count <= 4096 or table + count * 176 > len(data):
        raise ValueError('Invalid Xenon mesh sections')
    index_size, vertex_size = struct.unpack_from('>II', data, lod + 56)
    guid = gw2.guid_big(data[lod + 68:lod + 84])
    if guid not in archive.chunks:
        raise ValueError('Xenon geometry chunk missing: ' + guid)
    geometry = archive.data(archive.chunks[guid])
    if vertex_size + index_size > len(geometry):
        raise ValueError('Truncated Xenon geometry')
    results = []
    for i in range(count):
        at = table + i * 176
        name_at, material, triangles, first, offset, vertices = struct.unpack_from('>6I', data, at + 4)
        if not vertices or not triangles:
            continue
        name = data[name_at:data.index(b'\0', name_at)].decode('utf-8')
        if not name:
            continue  # Unnamed sections are depth/shadow proxies, not material surfaces.
        if data[at + 29] != 3:
            raise ValueError('Unsupported Xenon primitive type')
        decl = at + 36
        elements, streams = data[decl + 72:decl + 74]
        if not 0 < elements <= 16 or not 0 < streams <= 4:
            raise ValueError('Invalid Xenon vertex declaration')
        bases, strides = [], []
        for stream in range(streams):
            stride = data[decl + 64 + stream * 2]
            if not stride:
                raise ValueError('Empty Xenon vertex stride')
            bases.append(offset)
            strides.append(stride)
            offset += stride * vertices
        if offset > vertex_size:
            raise ValueError('Xenon vertex stream overrun')
        attrs = {}
        for element in range(elements):
            usage, fmt, attribute_offset, stream = data[decl + element * 4:decl + element * 4 + 4]
            if stream >= streams:
                raise ValueError('Invalid Xenon vertex stream')
            if usage in (1, 6, 33) or skinned and usage in (2, 3, 4, 5):
                attrs[usage] = read_attr(geometry, bases[stream], vertices, strides[stream], fmt, attribute_offset, '>')
        indices = np.frombuffer(geometry, '>u2', triangles * 3, vertex_size + first * 2).astype(np.uint32)
        if indices.max() >= vertices or 1 not in attrs:
            raise ValueError('Invalid Xenon section vertices')
        positions = attrs[1][:, :3].copy()
        if not np.isfinite(positions).all():
            raise ValueError('Nonfinite Xenon positions')
        normals = attrs.get(6)
        if normals is None:
            normals = np.zeros((vertices, 3), np.float32)
            faces = indices.reshape(-1, 3)
            cross = np.cross(positions[faces[:, 1]] - positions[faces[:, 0]], positions[faces[:, 2]] - positions[faces[:, 0]])
            for corner in range(3):
                np.add.at(normals, faces[:, corner], cross)
        normals = normals[:, :3].copy()
        normals /= np.maximum(np.linalg.norm(normals, axis=1), 1e-12)[:, None]
        skin = None
        if skinned and kind == 1:
            count_bones = data[at + 31]
            palette_at = struct.unpack_from('>I', data, at + 32)[0]
            palette = struct.unpack_from('>%dH' % count_bones, data, palette_at)
            skin = skin_attrs(attrs, palette, vertices)
        results.append(dict(skin=skin, name=name, positions=positions, normals=normals,
                            uv=attrs.get(33, np.zeros((vertices, 2), np.float32))[:, :2].copy(),
                            indices=indices, color=np.ones((vertices, 3), np.float32),
                            palette=np.zeros(vertices, np.float32), material_id=material))
    if not results:
        raise ValueError('Xenon mesh has no visible sections')
    return results


def untile(data, width, height, unit):
    """Return linear texels/BC blocks from a 32x32-block macro-tiled surface."""
    if not 0 < width <= 16384 or not 0 < height <= 16384 or unit not in (1, 2, 4, 8, 16):
        raise ValueError('Invalid Xenon tiled surface')
    x, y = np.meshgrid(np.arange(width, dtype=np.int64), np.arange(height, dtype=np.int64))
    pitch = (width + 31) & ~31
    block = (((y >> 5) * (pitch >> 5) + (x >> 5)) << 6) | (((y >> 1) & 7) << 3) | (x & 7)
    byte = block * unit
    pipe = ((x >> 3) & 3) ^ (((y >> 3) & 1) << 1)
    address = ((byte & 15) | (((byte >> 4) & 1) << 5) | (((byte >> 5) & 7) << 8) |
               ((byte >> 8) << 12) | ((y & 1) << 4) | (pipe << 6) | (((y >> 4) & 1) << 11))
    if int(address.max()) + unit > len(data):
        raise ValueError('Truncated Xenon tiled surface')
    raw = np.frombuffer(data, np.uint8)
    return raw[address.ravel()[:, None] + np.arange(unit)].reshape(-1).tobytes()


def texture(header, chunk):
    """Normalize the highest-resolution 2D mip to the existing PC texture decoder."""
    if len(header) != 128:
        raise ValueError('Invalid Xenon texture header')
    kind, fmt = struct.unpack_from('>II', header, 8)
    width, height, depth, slices = struct.unpack_from('>4H', header, 18)
    if kind != 0 or depth != 1 or slices != 1:
        raise ValueError('Only Xenon 2D textures are decoded')
    # GW1 ordinals: DXT1, DXT5, DXN mono/dual, and RGBA8.
    formats = {0: (54, 8, True), 3: (60, 16, True), 4: (62, 8, True),
               19: (64, 16, True), 20: (54, 8, True), 11: (18, 4, False),
               12: (18, 1, False), 13: (18, 2, False)}
    if fmt not in formats:
        raise ValueError('Unsupported Xenon texture format %d' % fmt)
    ordinal, unit, compressed = formats[fmt]
    bw, bh = (max(1, (width + 3) // 4), max(1, (height + 3) // 4)) if compressed else (width, height)
    linear = untile(chunk, bw, bh, unit)
    # Xenos BC payloads are endian-swapped in 16-bit words; RGBA texels in 32-bit words.
    if fmt in (12, 13):
        gray = (np.frombuffer(linear, '>u2') / 257).astype(np.uint8) if fmt == 13 else np.frombuffer(linear, np.uint8)
        linear = np.column_stack((gray, gray, gray, np.full(len(gray), 255, np.uint8))).tobytes()
    else:
        swap = 2 if compressed else 4
        linear = np.frombuffer(linear, np.uint8).reshape(-1, swap)[:, ::-1].copy().tobytes()
    out = bytearray(144)
    struct.pack_into('<II', out, 8, 0, ordinal)
    struct.pack_into('<4H', out, 18, width, height, depth, slices)
    out[26] = 1
    out[32:48] = bytes.fromhex(gw2.guid_big(header[28:44]))
    struct.pack_into('<I', out, 48, len(linear))
    struct.pack_into('<I', out, 108, len(linear))
    return bytes(out), linear
