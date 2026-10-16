"""Reader for the AMSH meshes written by aura-plugin-frostbite's write_mesh()."""
import struct
import numpy as np

MAGIC = 0x48534d41
HEADER = struct.Struct('<16I6f')
PART = struct.Struct('<4I6f')


def _align(offset):
    return offset + (-offset) % 16


def read(path):
    data = open(path, 'rb').read()
    if len(data) < HEADER.size:
        raise ValueError('Truncated AMSH: ' + str(path))
    header = HEADER.unpack_from(data)
    if header[0] != MAGIC or header[1] != 1:
        raise ValueError('Unsupported AMSH: ' + str(path))
    stride, vertex_count, index_count, part_count, string_size = header[3], header[4], header[5], header[6], header[13]
    if stride != 48:
        raise ValueError('Unsupported AMSH vertex stride %d: %s' % (stride, path))
    at = _align(HEADER.size)
    vertices = np.frombuffer(data, '<f4', vertex_count * 12, at).reshape(-1, 12)
    at = _align(at + vertex_count * stride)
    indices = np.frombuffer(data, '<u4', index_count, at)
    at = _align(at + index_count * 4)
    parts = [PART.unpack_from(data, at + i * PART.size) for i in range(part_count)]
    at = _align(at + part_count * PART.size)
    strings = data[at:at + string_size]
    if len(strings) != string_size or indices.size and int(indices.max()) >= vertex_count:
        raise ValueError('Corrupt AMSH: ' + str(path))
    sections = []
    for first, count, slot, name, *_ in parts:
        end = strings.find(b'\0', name)
        sections.append({'first': first, 'count': count, 'slot': slot,
                         'name': strings[name:end].decode('utf-8', 'replace')})
    return {
        'positions': vertices[:, 0:3],
        'normals': vertices[:, 3:6],
        'colors': vertices[:, 6:9],
        'uv': vertices[:, 10:12],
        'indices': indices,
        'sections': sections,
    }


def read_skin(path, vertex_count):
    """Bone weights written beside an AMSH by aura-plugin-frostbite's write_skin(): per vertex,
    `influences` skeleton bone indices (u16) and weights (f32)."""
    data = open(path, 'rb').read()
    if len(data) < 16 or data[:4] != b'ASKN':
        raise ValueError('Not a skin file: ' + str(path))
    version, count, width = struct.unpack_from('<3I', data, 4)
    if version != 1 or count != vertex_count or not 0 < width <= 16 or len(data) != 16 + count * width * 6:
        raise ValueError('Skin does not match its mesh: ' + str(path))
    bones = np.frombuffer(data, '<u2', count * width, 16).reshape(count, width)
    weights = np.frombuffer(data, '<f4', count * width, 16 + count * width * 2).reshape(count, width)
    return bones, weights
