"""Battle for Neighborville v256 terrain streaming trees.

The layout follows Garden Warfare 2's (gw2_terrain.py) with a longer header and an extra
float per bounds node, and every node below the top levels carries its own visual heightfield
inline: 137x137 u16 samples whose inner 129x129 span the node's rectangle, followed by layer
data that is not translated.
"""
import struct
import numpy as np
from gw2 import Reader
from gw2_terrain import height_section

SIDE, BORDER = 137, 4
NODE_PAYLOAD = 39919  # heights plus per-node layer trailer


def terrain_tree(data):
    """Bounds nodes, as {'rect', 'box', 'heights' (offset or None), 'children'}."""
    if len(data) < 127 or struct.unpack_from('<I', data)[0] != 256:
        raise ValueError('Unsupported BFN terrain streaming tree version')
    scale = struct.unpack_from('<f', data, 47)[0]
    expected, with_heights = struct.unpack_from('<II', data, 107)
    if not np.isfinite(scale) or not 0 < scale <= 1e7 or not 0 < expected <= 1000000 or with_heights > expected:
        raise ValueError('Invalid BFN terrain header')
    r = Reader(data)
    r.at = 127
    count = heights = 0

    def bit():
        value = r.number('B')
        if value not in (0, 1):
            raise ValueError('Invalid BFN terrain node flag')
        return value

    def node(depth=0):
        nonlocal count, heights
        count += 1
        if count > expected or depth > 16:
            raise ValueError('BFN terrain bounds tree exceeds limits')
        box = struct.unpack('<6f', r.take(24))
        r.take(4)
        if not all(np.isfinite(box)) or any(box[i] > box[i + 3] for i in range(3)):
            raise ValueError('Invalid BFN terrain bounds')
        out = {'rect': (box[0], box[2], box[3], box[5]), 'box': box, 'heights': None, 'children': []}
        if bit():
            return out
        # The fourth flag marks an inline heightfield, as on Garden Warfare 1's root node.
        has_ranges, _, _, has_heights = (bit() for _ in range(4))
        if has_ranges:
            # 8x8, 4x4, 2x2, 1x1 cells, each with u16 min/max.
            r.take(85 * 4)
        if has_heights:
            out['heights'] = r.at
            r.take(NODE_PAYLOAD)
            heights += 1
        if bit():
            out['children'] = [node(depth + 1) for _ in range(4)]
        return out

    root = node()
    if count != expected or heights != with_heights:
        raise ValueError('BFN terrain node count mismatch')
    tree = {'root': root, 'scale': scale, 'bounds': root['box'], 'data': data}
    if not with_heights and r.at < len(data):
        # Streamed terrain (e.g. dsub_tvm_zrefinery): Garden Warfare 2's raster list and tile tree
        # follow, and each tile's chunk holds the same 137x137 height page an inline node does.
        tree['root'] = streamed_tree(r, root['rect'])
        if r.at != len(data):
            raise ValueError('BFN terrain tile tree has trailing data')
    return tree


def streamed_tree(r, rect):
    while True:
        kind = r.number('B')
        if kind == 255:
            break
        if kind not in (1, 2, 3, 4):
            raise ValueError('Unsupported BFN terrain raster type')
        r.take(r.number('<I'))
    count = 0

    def bit():
        value = r.number('B')
        if value not in (0, 1):
            raise ValueError('Invalid BFN terrain tile flag')
        return value

    def descriptor():
        size, guid = r.number('<I'), r.take(16).hex()
        if bool(size) != (guid != '0' * 32):
            raise ValueError('Invalid BFN terrain chunk descriptor')
        return size, guid

    def tile(rect, depth=0):
        nonlocal count
        count += 1
        if count > 1000000 or depth > 16:
            raise ValueError('BFN terrain tile tree exceeds limits')
        size, guid = descriptor()
        if bit():
            descriptor()
        bit()  # metadata
        # A tile's chunk starts with the 39919-byte height page only when it has one; the other rasters
        # (and chunks without heights, as Xbox One data gives coarse tiles) are even-sized.
        heights = size >= NODE_PAYLOAD and (size - NODE_PAYLOAD) % 2 == 0
        out = {'rect': rect, 'heights': None, 'guid': guid if heights else None,
               'size': size, 'children': []}
        if bit():
            x, z, X, Z = rect
            mx, mz = (x + X) * .5, (z + Z) * .5
            out['children'] = [tile(c, depth + 1) for c in ((x, z, mx, mz), (mx, z, X, mz), (mx, mz, X, Z), (x, mz, mx, Z))]
        return out
    return tile(rect)


def usable(n):
    return n['heights'] is not None or n.get('guid') is not None


def tiles(tree, spacing=2.0):
    """The coarsest heightfield nodes whose sample spacing is at most `spacing` metres,
    or the finest available where the tree stops earlier."""
    out = []

    def walk(n):
        x, z, X, Z = n['rect']
        fine = (X - x) / (SIDE - 2 * BORDER - 1) <= spacing
        complete = len(n['children']) == 4 and all(usable(c) or c['children'] for c in n['children'])
        if usable(n) and (fine or not complete):
            out.append(n)
            return
        for c in n['children']:
            walk(c)
    walk(tree['root'])
    return out


def tile_section(tree, n, chunk=None):
    """A tile's mesh section; streamed tiles need their chunk's bytes."""
    if n['heights'] is None:
        if chunk is None or len(chunk) < SIDE * SIDE * 2:
            raise ValueError('BFN terrain tile chunk missing or truncated')
        raw = np.frombuffer(chunk, '<u2', count=SIDE * SIDE).reshape(SIDE, SIDE)
    else:
        raw = np.frombuffer(tree['data'], '<u2', count=SIDE * SIDE, offset=n['heights']).reshape(SIDE, SIDE)
    heights = raw[BORDER:-BORDER, BORDER:-BORDER].astype(np.float32)
    section = height_section(None, n, tree, heights=heights)
    if section is not None:
        section['name'] = 'BFN heightfield'
    return section
