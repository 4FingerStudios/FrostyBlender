"""GW2 v256 terrain streaming trees and their visual heightfield tiles."""
import struct
import numpy as np
from gw2 import Reader, LIMIT, guid_big


def terrain_tree(data):
    endian = '>' if data[:4] == b'\0\0\1\0' else '<'
    if len(data) < 108 or struct.unpack_from(endian + 'I', data)[0] != 256:
        raise ValueError('Unsupported GW2 terrain streaming tree version')
    scale = struct.unpack_from(endian + 'f', data, 47)[0]
    expected = struct.unpack_from(endian + 'I', data, 72 if endian == '>' else 88)[0]
    if not np.isfinite(scale) or not 0 < scale <= 1e7 or not 0 < expected <= 1000000:
        raise ValueError('Invalid GW2 terrain header')
    r = Reader(data, endian)
    r.at = 92 if endian == '>' else 108
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
        box = struct.unpack(endian + '6f', r.take(24))
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
            r.take(36534 if endian == '>' else HEIGHT_PAGE)
        if bit():
            for _ in range(4):
                bounds_node(depth+1)
        return box
    box = bounds_node()
    if count != expected:
        raise ValueError(f'Terrain bounds node count mismatch: {count} != {expected} at {r.at}')
    samples = None
    rasters = {}
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
        rasters[kind] = raster
        if kind == 1:
            if len(raster) < 8:
                raise ValueError('Truncated GW2 terrain height raster')
            samples, pixel_size = struct.unpack_from(endian + 'II', raster)
            if pixel_size != 2 or samples < 4 or samples > 258 or (samples-2) & (samples-3):
                raise ValueError('Unsupported GW2 height raster layout')
    if samples is None:
        raise ValueError('GW2 terrain has no height raster')
    tiles = []
    def descriptor():
        size = r.number('<I')
        raw = r.take(16)
        guid = guid_big(raw) if endian == '>' else raw.hex()
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
    return {'root': root, 'tiles': tiles, 'scale': scale, 'samples': samples, 'bounds': box,
            'endian': endian, 'rasters': rasters}


# A tile chunk concatenates the layers that tile stores: an optional height
# page (133x133 u16 plus a trailer), optional 130x130 two-byte pages and
# 66x66 one-byte mask pages. Tiles store heights only where they add detail;
# the rest, and samples written as 0, come from the nearest ancestor.
HEIGHT_PAGE, WIDE_PAGE, MASK_PAGE = 37759, 33800, 4356


def mask_tree(raster):
    """The painted-layer layout in raster 1: [(node rect, [(layer, page rect), ...])] in file order, or None.

    Header: u32 66 (page side), 2, 1, 2048, 2048, 1, bounds (4 f32), then the record count at 44. Each node that
    stores pages is a group: a short preamble of padding, u16 records, u16 pages and three more bytes, then the
    records: rect (4 f32), two flags, an 85-bit patch mask (11 bytes; GW1 puts its length, u16 11, before it)
    and a byte that is the layer of the next record. Records with the second flag set are the node's children
    (GW2 lists all four, GW1 only some or none); the others, in page order, are each one 66x66 page of one layer
    over one child quadrant. Layer 0 (the base) has no pages."""
    if len(raster) < 48 or struct.unpack_from('<I', raster)[0] != 66:
        return None
    lo_x, lo_z, hi_x, hi_z = struct.unpack_from('<4f', raster, 24)
    total = struct.unpack_from('<I', raster, 44)[0]

    def rect(at, size):
        if at + size > len(raster):
            return None
        x, z, X, Z = struct.unpack_from('<4f', raster, at)
        w = X - x
        if not (w >= .25 and abs(Z - z - w) <= 1e-3 * w and lo_x <= x and X <= hi_x and lo_z <= z and Z <= hi_z):
            return None
        return x, z, X, Z

    def group(at, count, page_count, size):
        """(node, pages) for the group whose records start at `at`, or None."""
        records = [(rect(at + size * i, size), raster[at + size * i + 17] if at + size * i + 18 <= len(raster) else 2)
                   for i in range(count)]
        if any(found is None or child not in (0, 1) for found, child in records):
            return None
        if sum(child for _, child in records) != count - page_count:
            return None
        # The node is the 2x2 block of the grid that its children and pages sit in.
        first = records[0][0]
        w = first[2] - first[0]
        x = lo_x + (first[0] - lo_x) // (2 * w) * (2 * w)
        z = lo_z + (first[1] - lo_z) // (2 * w) * (2 * w)
        if any(q[2] - q[0] != w or not x <= q[0] < x + 2 * w or not z <= q[1] < z + 2 * w for q, _ in records):
            return None
        pages = [(raster[at + size * i - 1], found) for i, (found, child) in enumerate(records) if not child]
        return (x, z, x + 2 * w, z + 2 * w), pages

    def parse(size):
        groups, at, seen = [], 48, 0
        while seen < total:
            # The padding between groups depends on the nodes in between; find the next group header.
            for at in range(at, min(at + 4096, len(raster) - 7)):
                count, pages = struct.unpack_from('<2H', raster, at)
                if 0 < count <= pages + 4 and pages <= count and seen + count <= total:
                    found = group(at + 7, count, pages, size)
                    if found:
                        break
            else:
                return None
            groups.append(found)
            at += 7 + size * count
            seen += count
        return groups

    if total == 0:
        return []
    gw1 = raster[48:4096].find(b'\x0b\x00\xff\xff') >= 0  # GW1: the mask length, then the full masks of the top nodes
    return parse(32 if gw1 else 30) or parse(30 if gw1 else 32)


def mask_pages(tree, chunk):
    """Every painted-layer page as (layer, rect, 66x66 uint8), or None when the layout can't be read.

    A node that is a tile keeps its pages at the end of the tile's chunk. Nodes below a leaf tile keep theirs in
    that tile's extra chunk, last node first. chunk(guid) returns a chunk's bytes, or None; pages in a missing
    chunk are left out, and coarser pages cover for them."""
    groups = mask_tree(tree['rasters'].get(1, b''))
    if groups is None:
        return None
    by_node = dict(groups)
    order = [node for node, _ in groups]
    out = []

    def read(data, offset, node):
        for i, (layer, rect) in enumerate(by_node[node]):
            page = np.frombuffer(data, np.uint8, MASK_PAGE, offset + i * MASK_PAGE).reshape(66, 66)
            out.append((layer, rect, page))

    def inside(node, rect):
        return node != rect and rect[0] <= node[0] and rect[1] <= node[1] and node[2] <= rect[2] and node[3] <= rect[3]

    for tile in tree['tiles']:
        if not tile['size']:
            continue
        rect = tuple(tile['rect'])
        if rect in by_node:
            data = chunk(tile['guid'])
            size = len(by_node[rect]) * MASK_PAGE
            if data is not None and len(data) < size:
                return None
            if data is not None:
                read(data, len(data) - size, rect)
        if tile['children'] or not tile['extra'] or not tile['extra'][0]:
            continue
        deep = [node for node in reversed(order) if inside(node, rect)]
        if not deep:
            continue
        data = chunk(tile['extra'][1])
        if data is None:
            continue
        size = sum(len(by_node[node]) for node in deep) * MASK_PAGE
        if len(data) < size or (len(data) - size) % HEIGHT_PAGE:
            return None
        offset = len(data) - size
        for node in deep:
            read(data, offset, node)
            offset += len(by_node[node]) * MASK_PAGE
    return out


def tile_masks(pages, rect, layers, largest=1024):
    """Per-layer coverage (0..1) over a tile rect, from the finest pages that cover each point: {layer: array}
    with rows along z and columns along x. Pages have one border texel each side; a finer page replaces a coarser
    one where it exists, as with the heightfield."""
    x0, z0, x1, z1 = rect
    width = x1 - x0
    touching = [(layer, r, page) for layer, r, page in pages
                if layer in layers and r[0] < x1 and r[2] > x0 and r[1] < z1 and r[3] > z0]
    if not touching:
        return {}
    texel = min(r[2] - r[0] for _, r, _ in touching) / 64
    side = int(min(largest, max(8, round(width / texel))))
    centres = (np.arange(side) + .5) * (width / side)
    xs, zs = x0 + centres, z0 + centres
    masks = {layer: np.zeros((side, side), np.float32) for layer in layers}
    for layer, (px, pz, pX, pZ), page in sorted(touching, key=lambda item: item[1][0] - item[1][2]):
        cols = np.flatnonzero((xs >= px) & (xs < pX))
        rows = np.flatnonzero((zs >= pz) & (zs < pZ))
        if not len(cols) or not len(rows):
            continue
        u = (xs[cols] - px) / (pX - px) * 64 + .5
        v = (zs[rows] - pz) / (pZ - pz) * 64 + .5
        u0 = np.clip(np.floor(u).astype(int), 0, 64)
        v0 = np.clip(np.floor(v).astype(int), 0, 64)
        fu, fv = (u - u0)[None, :], (v - v0)[:, None]
        p = page.astype(np.float32) / 255
        a, b = p[v0][:, u0], p[v0][:, u0 + 1]
        c, d = p[v0 + 1][:, u0], p[v0 + 1][:, u0 + 1]
        masks[layer][np.ix_(rows, cols)] = (a * (1 - fu) + b * fu) * (1 - fv) + (c * (1 - fu) + d * fu) * fv
    return masks


def layer_textures(shaderdb, layer_count):
    """Texture paths of each terrain layer in layer order, from the level's compiled shader database: [[path,
    ...], ...], or None. Each compiled terrain shader binds its layers' textures in ascending layer order, as
    152-byte entries (u32 slot, u32 0, 144-byte path); the shader that combines every layer lists them all."""
    import re
    from collections import Counter
    paths = [(m.start(), m.group().decode('ascii')) for m in re.finditer(rb'[A-Za-z_]+/[\x21-\x7e]{3,140}', shaderdb)]
    tables, current = [], []
    for at, path in paths:
        if current and at - current[-1][0] == 152:
            current.append((at, path))
        else:
            if len(current) > 1:
                tables.append(current)
            current = [(at, path)]
    if len(current) > 1:
        tables.append(current)
    found = Counter()
    for table in tables:
        if not any('/terrain/' in path.lower() or '/trn_' in path.lower() for _, path in table):
            continue
        layers = []
        for _, path in table:
            name = material_name(path)
            if layers and layers[-1][0] == name:
                layers[-1][1].append(path)
            else:
                layers.append((name, [path]))
        if len(layers) == layer_count and len({name for name, _ in layers}) == layer_count:
            found[tuple((name, tuple(group)) for name, group in layers)] += 1
    if not found:
        return None
    return [list(group) for _, group in found.most_common(1)[0][0]]


def material_name(path):
    """A texture path's surface name, without its role suffix (_Color, _Normal, _ASM, ...)."""
    import re
    return re.sub(r'(?i)(_vara)?_(asm|normal|color|col|vara|varb|tile|d|n|s|sso)$', '', path.rsplit('/', 1)[-1])


def has_height_page(size, side, endian='<'):
    if size == side * side * 2:
        return True
    page, wide = (36534, 8712) if endian == '>' else (HEIGHT_PAGE, WIDE_PAGE)
    return any(size - page - k * wide >= 0 and (size - page - k * wide) % MASK_PAGE == 0
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
        if len(raw) >= side * side * 2 and has_height_page(len(raw), side, tree.get('endian', '<')):
            own = np.frombuffer(raw, tree.get('endian', '<') + 'u2', count=side*side).reshape(side, side)[2:-2, 2:-2].astype(np.float32)
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
