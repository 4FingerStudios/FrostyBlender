"""Battle for Neighborville textures cooked for Xbox One ('gen4b' bundles) laid out as PC textures.

Xbox One texture chunks are 1D-tiled: each mip is cut into micro tiles of 8 x 8 elements (BC blocks, or
pixels for uncompressed formats), the elements of a tile in Morton order and the tiles row by row. A mip
narrower or shorter than a tile is padded to whole tiles, so the small mips of the header's size table are
bigger than on PC. A texture's header flags it with 0x08 in byte 21.

detile() turns such a chunk into the PC layout and the header's mip sizes into the PC sizes, so the reader
and the rest of the BFN path see what a PC texture would give.
"""
import struct

import numpy as np

TILED_FLAG = 0x08  # header byte 21
TILE = 8
# BFN format ordinal -> (element width in pixels, bytes per element); the ordinals are bfn.FORMATS's.
ELEMENTS = {18: (1, 4), 20: (1, 4), 23: (1, 4), 24: (1, 4)}
ELEMENTS.update({n: (4, 8) for n in (54, 55, 56, 57, 62)})
ELEMENTS.update({n: (4, 16) for n in (58, 59, 60, 61, 63, 64, 65, 66, 67)})
_orders = {}


def is_tiled(header):
    return len(header) >= 132 and header[21] & TILED_FLAG != 0


def _morton():
    """(x, y) of each element of a micro tile, in its stored order."""
    if 'm' not in _orders:
        i = np.arange(TILE * TILE)
        x = (i & 1) | (i >> 1 & 2) | (i >> 2 & 4)
        y = (i >> 1 & 1) | (i >> 2 & 2) | (i >> 3 & 4)
        _orders['m'] = x, y
    return _orders['m']


def _layout(header):
    """(element width, bytes per element, [(elements across, down)] per mip) or None for an unknown format."""
    fmt = struct.unpack_from('<I', header, 12)[0]
    if fmt not in ELEMENTS:
        return None
    block, size = ELEMENTS[fmt]
    width, height = struct.unpack_from('<HH', header, 22)
    mips = header[30]
    dims = [(-(-max(1, width >> i) // block), -(-max(1, height >> i) // block)) for i in range(mips)]
    return block, size, dims


def _detile_mip(data, ew, eh, size, slices):
    pw, ph = -(-ew // TILE) * TILE, -(-eh // TILE) * TILE
    mx, my = _morton()
    tiles = np.arange((pw // TILE) * (ph // TILE))
    # Element (x, y) of every stored element, in stored order.
    x = ((tiles % (pw // TILE))[:, None] * TILE + mx[None, :]).ravel()
    y = ((tiles // (pw // TILE))[:, None] * TILE + my[None, :]).ravel()
    keep = (x < ew) & (y < eh)
    order = np.argsort((y * ew + x)[keep], kind='stable')
    stored = np.flatnonzero(keep)[order]
    out = []
    per = pw * ph * size
    for s in range(slices):
        elements = np.frombuffer(data, np.uint8, per, s * per).reshape(-1, size)
        out.append(elements[stored].tobytes())
    return b''.join(out)


def detile(header, data):
    """(PC header, PC chunk) for a tiled Xbox One texture; unchanged when it is not tiled.
    Raises ValueError when the chunk does not match the tiled sizes the header describes."""
    if not is_tiled(header):
        return header, data
    layout = _layout(header)
    if layout is None:
        raise ValueError('Xbox One texture format %d is not detiled' % struct.unpack_from('<I', header, 12)[0])
    _, size, dims = layout
    mips, first = header[30], header[31]
    sizes = list(struct.unpack_from('<15I', header, 48))
    slices = max(1, struct.unpack_from('<H', header, 28)[0])
    tiled = [-(-ew // TILE) * TILE * -(-eh // TILE) * TILE * size * slices for ew, eh in dims]
    if tiled != sizes[:mips]:
        raise ValueError('Xbox One texture mip sizes are not 1D-tiled')
    start = 0 if len(data) == sum(tiled) else first if first and len(data) == sum(tiled[first:]) else None
    if start is None:
        raise ValueError('Xbox One texture chunk size matches neither all mips nor the streamed tail')
    out, at = [], 0
    for i in range(start, mips):
        ew, eh = dims[i]
        out.append(_detile_mip(data[at:at + tiled[i]], ew, eh, size, slices))
        at += tiled[i]
    linear = [ew * eh * size * slices for ew, eh in dims]
    h = bytearray(header)
    h[21] &= ~TILED_FLAG & 0xff
    struct.pack_into('<%dI' % mips, h, 48, *linear)
    struct.pack_into('<I', h, 108, sum(linear))
    return bytes(h), b''.join(out)
