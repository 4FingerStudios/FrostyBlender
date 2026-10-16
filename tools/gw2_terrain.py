"""GW2 v256 terrain streaming trees and their visual heightfield tiles."""
import struct
import numpy as np
from gw2 import Reader, LIMIT


def terrain_tree(data):
    if len(data) < 108 or struct.unpack_from('<I', data)[0] != 256:
        raise ValueError('Unsupported GW2 terrain streaming tree version')
    scale = struct.unpack_from('<f', data, 47)[0]
    expected = struct.unpack_from('<I', data, 88)[0]
    if not np.isfinite(scale) or not 0 < scale <= 1e7 or not 0 < expected <= 1000000:
        raise ValueError('Invalid GW2 terrain header')
    r = Reader(data)
    r.at = 108
    count = 0
    def bit():
        value = r.number('B')
        if value not in (0, 1):
            raise ValueError('Invalid GW2 terrain node flag')
        return value
    def bounds_node(depth=0):
        nonlocal count
        count += 1
        if count > expected or depth > 16:
            raise ValueError('GW2 terrain bounds tree exceeds limits')
        box = struct.unpack('<6f', r.take(24))
        if not all(np.isfinite(box)) or any(box[i] > box[i+3] for i in range(3)):
            raise ValueError('Invalid GW2 terrain bounds')
        if bit():
            return box
        has_ranges, _, _, inline = (bit() for _ in range(4))
        if has_ranges:
            # 8x8, 4x4, 2x2, 1x1 cells, each with u16 min/max.
            r.take(85 * 4)
        if inline:
            # Garden Warfare 1 stores its root heightfield page inline; tiles cover it.
            r.take(HEIGHT_PAGE)
        if bit():
            for _ in range(4):
                bounds_node(depth+1)
        return box
    box = bounds_node()
    if count != expected:
        raise ValueError('GW2 terrain bounds node count mismatch')
    samples = None
    seen = set()
    while True:
        kind = r.number('B')
        # The terminator has no size field. The following bytes belong to
        # the tile tree, including descriptors for empty root tiles.
        if kind == 255:
            break
        if kind in seen or kind not in (1, 2, 3, 4):
            raise ValueError('Unsupported GW2 terrain raster type')
        seen.add(kind)
        size = r.number('<I')
        if size > LIMIT:
            raise ValueError('GW2 terrain raster exceeds limit')
        raster = r.take(size)
        if kind == 1:
            if len(raster) < 8:
                raise ValueError('Truncated GW2 terrain height raster')
            samples, pixel_size = struct.unpack_from('<II', raster)
            if pixel_size != 2 or samples < 4 or samples > 258 or (samples-2) & (samples-3):
                raise ValueError('Unsupported GW2 height raster layout')
    if samples is None:
        raise ValueError('GW2 terrain has no height raster')
    tiles = []
    def descriptor():
        size = r.number('<I')
        guid = r.take(16).hex()
        if size > LIMIT or bool(size) != (guid != '0'*32):
            raise ValueError('Invalid GW2 terrain chunk descriptor')
        return size, guid
    def tile_node(rect, depth=0):
        if len(tiles) >= 1000000 or depth > 16:
            raise ValueError('GW2 terrain tile tree exceeds limits')
        size, guid = descriptor()
        extra = descriptor() if bit() else None
        metadata = bit()
        children = bit()
        tile = {'rect': rect, 'size': size, 'guid': guid, 'extra': extra,
                'metadata': metadata, 'children': []}
        tiles.append(tile)
        if children:
            x, z, X, Z = rect
            mx, mz = (x+X)*.5, (z+Z)*.5
            for child in ((x,z,mx,mz), (mx,z,X,mz), (mx,mz,X,Z), (x,mz,mx,Z)):
                tile['children'].append(tile_node(child, depth+1))
        return tile
    root = tile_node((box[0], box[2], box[3], box[5]))
    if r.at != len(data):
        raise ValueError('GW2 terrain tile tree has trailing data')
    return {'root': root, 'tiles': tiles, 'scale': scale, 'samples': samples, 'bounds': box}


# A tile chunk concatenates the layers that tile stores: an optional height
# page (133x133 u16 plus a trailer), optional 130x130 two-byte pages and
# 66x66 one-byte mask pages. Tiles store heights only where they add detail;
# the rest, and samples written as 0, come from the nearest ancestor.
HEIGHT_PAGE, WIDE_PAGE, MASK_PAGE = 37759, 33800, 4356


def has_height_page(size, side):
    if size == side * side * 2:
        return True
    return any(size - HEIGHT_PAGE - k * WIDE_PAGE >= 0 and (size - HEIGHT_PAGE - k * WIDE_PAGE) % MASK_PAGE == 0
               for k in range(4))


def tile_heights(raw, tile, tree, parent=None):
    """Raw u16 heights over the tile's 129x129 samples (0 = none), or None.

    parent is the (heights, rect) of the nearest ancestor, used to fill gaps.
    """
    side = tree['samples'] * 2 + 1
    own = None
    if raw is not None:
        if len(raw) != tile['size']:
            raise ValueError('Truncated GW2 visual terrain heightfield')
        if len(raw) >= side * side * 2 and has_height_page(len(raw), side):
            own = np.frombuffer(raw, '<u2', count=side*side).reshape(side, side)[2:-2, 2:-2].astype(np.float32)
    inherited = None
    if parent is not None:
        heights, (px, pz, pX, pZ) = parent
        rows, columns = heights.shape
        x, z, X, Z = tile['rect']
        # Children cover an exact sub-range of the parent's samples; interpolate
        # that range up to the full sample count.
        n = own.shape[0] if own is not None else side - 4
        fz = np.linspace((z-pz)/(pZ-pz), (Z-pz)/(pZ-pz), n) * (rows-1)
        fx = np.linspace((x-px)/(pX-px), (X-px)/(pX-px), n) * (columns-1)
        z0 = np.clip(np.floor(fz).astype(int), 0, rows-2); tz = (fz - z0)[:, None]
        x0 = np.clip(np.floor(fx).astype(int), 0, columns-2); tx = (fx - x0)[None, :]
        a, b = heights[z0][:, x0], heights[z0][:, x0+1]
        c, d = heights[z0+1][:, x0], heights[z0+1][:, x0+1]
        inherited = (a*(1-tx) + b*tx)*(1-tz) + (c*(1-tx) + d*tx)*tz
        # Interpolating towards a missing (0) sample is not a height.
        inherited[(np.minimum(np.minimum(a, b), np.minimum(c, d)) == 0)] = 0
    if own is None:
        return inherited
    if inherited is not None:
        own = np.where(own == 0, inherited, own)
    return own


def height_section(raw, tile, tree, parent=None, heights=None):
    if heights is None:
        heights = tile_heights(raw, tile, tree, parent)
    if heights is None:
        return None
    rows, columns = heights.shape
    holes = heights == 0 if tree['bounds'][1] > 0 else np.zeros_like(heights, bool)
    height = heights * (tree['scale']/65535)
    x, z, X, Z = tile['rect']
    xx, zz = np.meshgrid(np.linspace(x, X, columns), np.linspace(z, Z, rows))
    positions = np.stack((xx, height, zz), axis=-1).reshape(-1,3).astype(np.float32)
    dz, dx = np.gradient(height, (Z-z)/(rows-1), (X-x)/(columns-1))
    normals = np.stack((-dx, np.ones_like(height), -dz), axis=-1).reshape(-1,3)
    normals /= np.maximum(np.linalg.norm(normals, axis=1), 1e-12)[:,None]
    a = (np.arange(rows-1)[:,None]*columns + np.arange(columns-1)).reshape(-1)
    b, c, d = a+1, a+columns, a+columns+1
    hole = holes.reshape(-1)
    keep = ~(hole[a] | hole[b] | hole[c] | hole[d])
    a, b, c, d = a[keep], b[keep], c[keep], d[keep]
    if not len(a):
        return None
    indices = np.stack((a,c,b,b,c,d),axis=1).reshape(-1)
    if not keep.all():
        used, indices = np.unique(indices, return_inverse=True)
        positions, normals = positions[used], normals[used]
    indices = indices.astype(np.uint32)
    return {'name': 'GW2 heightfield', 'positions': positions, 'normals': normals,
            'uv': positions[:,[0,2]]*.25, 'indices': indices,
            'color': np.ones_like(positions), 'palette': np.zeros(len(positions),np.float32)}
