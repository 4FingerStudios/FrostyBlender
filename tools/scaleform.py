"""Scaleform (GFx) and SWF menu movies: list the images inside one and save them as PNG.

    python scaleform.py list MOVIE.gfx
    python scaleform.py extract MOVIE.gfx FOLDER

Reads plain and compressed movies (GFX/CFX, FWS/CWS; ZWS needs LZMA). Embedded bitmaps decode here:
DefineBitsLossless/Lossless2 directly, DefineBits/JPEG2/JPEG3/JPEG4 through OpenImageIO (Blender's Python has it),
with the JPEG3/4 alpha plane applied. Scaleform's external images (DefineExternalImage/2) and sub-images
(DefineSubImage) are listed with the file name they point at, so the caller can find the game texture.
Vector shapes, fonts, sprites and text are counted, not drawn.
"""
import json
import lzma
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

TAG_NAMES = {
    2: 'DefineShape', 22: 'DefineShape2', 32: 'DefineShape3', 83: 'DefineShape4', 46: 'DefineMorphShape',
    84: 'DefineMorphShape2', 10: 'DefineFont', 48: 'DefineFont2', 75: 'DefineFont3', 91: 'DefineFont4',
    11: 'DefineText', 33: 'DefineText2', 37: 'DefineEditText', 39: 'DefineSprite', 7: 'DefineButton',
    34: 'DefineButton2', 14: 'DefineSound', 60: 'DefineVideoStream', 6: 'DefineBits', 21: 'DefineBitsJPEG2',
    35: 'DefineBitsJPEG3', 90: 'DefineBitsJPEG4', 20: 'DefineBitsLossless', 36: 'DefineBitsLossless2',
    56: 'ExportAssets', 57: 'ImportAssets', 71: 'ImportAssets2', 82: 'DoABC', 72: 'DoABC', 12: 'DoAction',
    59: 'DoInitAction', 1000: 'ExporterInfo', 1001: 'DefineExternalImage', 1002: 'FontTextureInfo',
    1003: 'DefineExternalGradient', 1004: 'DefineGradientMap', 1005: 'DefineCompactedFont',
    1006: 'DefineExternalSound', 1007: 'DefineExternalStreamSound', 1008: 'DefineSubImage',
    1009: 'DefineExternalImage2',
}
SHAPES = {2, 22, 32, 83, 46, 84}
FONTS = {10, 48, 75, 91, 1005}
JPEGS = {6, 21, 35, 90}
LOSSLESS = {20, 36}
EXTERNAL = {1001, 1009}
SUB_IMAGE = 1008


def body(data):
    """(version, uncompressed movie bytes after the 8-byte header, is_scaleform)."""
    sig, version = data[:3], data[3]
    scaleform = sig[1:3] == b'FX'
    kind = sig[:1]
    if kind in (b'F', b'G'):
        rest = data[8:]
    elif kind == b'C':
        rest = zlib.decompress(data[8:])
    elif kind == b'Z':  # LZMA SWF: u32 compressed size, 5 bytes of properties, then the raw stream
        props = data[12:17]
        filters = [{'id': lzma.FILTER_LZMA1, 'dict_size': struct.unpack_from('<I', props, 1)[0],
                    'lc': props[0] % 9, 'lp': (props[0] // 9) % 5, 'pb': props[0] // 45}]
        rest = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=filters).decompress(data[17:])
    else:
        raise ValueError('not a Flash/Scaleform movie')
    return version, rest, scaleform


def _tags(data, pos, end):
    while pos + 2 <= end:
        head = struct.unpack_from('<H', data, pos)[0]
        pos += 2
        code, size = head >> 6, head & 0x3F
        if size == 0x3F:
            size = struct.unpack_from('<I', data, pos)[0]
            pos += 4
        yield code, pos, size
        if code == 0:
            return
        pos += size


def _string(data, pos, length):
    return data[pos:pos + length].split(b'\0')[0].decode('utf-8', 'replace')


class Movie:
    """The tags of one movie, with every image-like definition and a count of everything else."""

    def __init__(self, data):
        self.version, self.data, self.scaleform = body(data)
        nbits = self.data[0] >> 3
        start = (5 + 4 * nbits + 7) // 8
        rect = self._rect(nbits)
        self.width, self.height = (rect[1] - rect[0]) / 20.0, (rect[3] - rect[2]) / 20.0
        self.frames = struct.unpack_from('<H', self.data, start + 2)[0]
        self.counts, self.images, self.exports = {}, [], {}
        self.jpeg_tables = b''
        self.exporter = {}
        self._walk(start + 4, len(self.data))

    def _rect(self, nbits):
        value = int.from_bytes(self.data[:(5 + 4 * nbits + 7) // 8], 'big')
        total = (5 + 4 * nbits + 7) // 8 * 8
        out = []
        for i in range(4):
            shift = total - 5 - nbits * (i + 1)
            v = (value >> shift) & ((1 << nbits) - 1)
            if nbits and v >> (nbits - 1):
                v -= 1 << nbits
            out.append(v)
        return out

    def _walk(self, pos, end):
        d = self.data
        for code, at, size in _tags(d, pos, end):
            name = TAG_NAMES.get(code, 'Tag%d' % code)
            self.counts[name] = self.counts.get(name, 0) + 1
            if code == 39:  # sprites hold only control tags, but walk them for completeness
                self._walk(at + 4, at + size)
            elif code == 8:
                self.jpeg_tables = d[at:at + size]
            elif code in JPEGS or code in LOSSLESS:
                self.images.append({'id': struct.unpack_from('<H', d, at)[0], 'kind': name, 'tag': code,
                                    'at': at, 'size': size})
            elif code in EXTERNAL:
                self.images.append(self._external(code, at, size))
            elif code == SUB_IMAGE:
                cid, image, x1, y1, x2, y2 = struct.unpack_from('<6H', d, at)
                self.images.append({'id': cid, 'kind': 'DefineSubImage', 'tag': code, 'image': image,
                                    'rect': [x1, y1, x2, y2]})
            elif code == 56:
                count = struct.unpack_from('<H', d, at)[0]
                p = at + 2
                for _ in range(count):
                    cid = struct.unpack_from('<H', d, p)[0]
                    zero = d.index(b'\0', p + 2)
                    self.exports[cid] = d[p + 2:zero].decode('utf-8', 'replace')
                    p = zero + 1
            elif code == 1000:
                self.exporter = self._exporter(at, size)

    def _external(self, code, at, size):
        d = self.data
        if code == 1001:  # u16 id, u16 format, u16 width, u16 height, u8 length, file name
            cid, fmt, w, h = struct.unpack_from('<4H', d, at)
            p = at + 8
            export, p = '', p
            fname = _string(d, p + 1, d[p])
        else:  # 1009: u32 id, u16 format, u16 width, u16 height, u8 + export name, u8 + file name
            cid, fmt, w, h = struct.unpack_from('<I3H', d, at)
            p = at + 10
            export = _string(d, p + 1, d[p])
            p += 1 + d[p]
            fname = _string(d, p + 1, d[p]) if p < at + size else ''
        return {'id': cid, 'kind': TAG_NAMES[code], 'tag': code, 'format': fmt, 'width': w, 'height': h,
                'export': export, 'file': fname}

    def _exporter(self, at, size):
        d = self.data
        version = struct.unpack_from('<H', d, at)[0]
        p = at + 2
        flags = 0
        if version >= 0x10A:
            flags = struct.unpack_from('<I', d, p)[0]
            p += 4
        fmt = struct.unpack_from('<H', d, p)[0]
        p += 2
        prefix = _string(d, p + 1, d[p])
        p += 1 + d[p]
        swf = _string(d, p + 1, d[p]) if p < at + size else ''
        return {'version': version, 'flags': flags, 'format': fmt, 'prefix': prefix, 'swf': swf}

    # ---------------- summary ----------------
    def shapes(self):
        return sum(n for k, n in self.counts.items() if k in {TAG_NAMES[c] for c in SHAPES})

    def describe(self):
        """What the movie holds, as plain data for JSON."""
        images = []
        for image in self.images:
            entry = {k: v for k, v in image.items() if k not in ('at', 'tag')}
            if image['id'] in self.exports:
                entry['name'] = self.exports[image['id']]
            images.append(entry)
        return {'scaleform': self.scaleform, 'version': self.version, 'stage': [self.width, self.height],
                'frames': self.frames, 'images': images, 'shapes': self.shapes(),
                'fonts': sum(n for k, n in self.counts.items() if k in {TAG_NAMES[c] for c in FONTS}),
                'tags': dict(sorted(self.counts.items())), 'exporter': self.exporter}

    # ---------------- decoding ----------------
    def pixels(self, image):
        """(width, height, RGBA bytes) for an embedded bitmap, or None for references."""
        if image['tag'] in LOSSLESS:
            return self._lossless(image)
        if image['tag'] in JPEGS:
            return self._jpeg(image)
        return None

    def _lossless(self, image):
        d, at, size = self.data, image['at'], image['size']
        cid, fmt, w, h = struct.unpack_from('<HBHH', d, at)
        alpha = image['tag'] == 36
        p = at + 7
        colours = 0
        if fmt == 3:
            colours = d[p] + 1
            p += 1
        raw = zlib.decompress(d[p:at + size])
        out = bytearray(w * h * 4)
        if fmt == 3:
            entry = 4 if alpha else 3
            table = raw[:colours * entry]
            stride = (w + 3) & ~3
            idx = raw[colours * entry:]
            palette = []
            for i in range(colours):
                c = table[i * entry:(i + 1) * entry]
                palette.append(_unpremultiply(c[0], c[1], c[2], c[3]) if alpha else (c[0], c[1], c[2], 255))
            for y in range(h):
                row = idx[y * stride:y * stride + w]
                for x, v in enumerate(row):
                    out[(y * w + x) * 4:(y * w + x) * 4 + 4] = bytes(palette[v] if v < colours else (0, 0, 0, 0))
        elif fmt == 4:  # 15-bit, rows padded to 4 bytes
            stride = (w * 2 + 3) & ~3
            for y in range(h):
                for x in range(w):
                    v = struct.unpack_from('>H', raw, y * stride + x * 2)[0]
                    r, g, b = (v >> 10) & 31, (v >> 5) & 31, v & 31
                    out[(y * w + x) * 4:(y * w + x) * 4 + 4] = bytes((r << 3 | r >> 2, g << 3 | g >> 2, b << 3 | b >> 2, 255))
        elif fmt == 5:  # ARGB (premultiplied for Lossless2), XRGB otherwise
            import numpy as np
            a = np.frombuffer(raw[:w * h * 4], np.uint8).reshape(h, w, 4).astype(np.float32)
            rgba = np.empty_like(a)
            rgba[..., :3] = a[..., 1:4]
            rgba[..., 3] = a[..., 0] if alpha else 255
            if alpha:
                scale = np.where(rgba[..., 3:] > 0, 255.0 / np.maximum(rgba[..., 3:], 1), 0)
                rgba[..., :3] = np.clip(rgba[..., :3] * scale + 0.5, 0, 255)
            return w, h, rgba.astype(np.uint8).tobytes()
        else:
            raise ValueError('lossless format %d' % fmt)
        return w, h, bytes(out)

    def _jpeg(self, image):
        d, at, size, tag = self.data, image['at'], image['size'], image['tag']
        p, end = at + 2, at + size
        alpha = None
        if tag == 6:
            jpeg = _join_jpeg(self.jpeg_tables, d[p:end])
        elif tag == 21:
            jpeg = d[p:end]
        else:
            length = struct.unpack_from('<I', d, p)[0]
            p += 4
            if tag == 90:
                p += 2  # deblocking filter
            jpeg = d[p:p + length]
            alpha = d[p + length:end]
        jpeg = _clean_jpeg(bytes(jpeg))
        if jpeg[:8] == b'\x89PNG\r\n\x1a\n' or jpeg[:6] in (b'GIF89a', b'GIF87a'):
            alpha = None  # PNG/GIF payloads carry their own alpha
        w, h, rgba = _decode_file(jpeg)
        if alpha:
            import numpy as np
            plane = np.frombuffer(zlib.decompress(alpha), np.uint8)[:w * h]
            if plane.size == w * h:
                px = np.frombuffer(rgba, np.uint8).reshape(h, w, 4).astype(np.float32)
                a = plane.reshape(h, w).astype(np.float32)
                scale = np.where(a > 0, 255.0 / np.maximum(a, 1), 0)[..., None]
                px[..., :3] = np.clip(px[..., :3] * scale + 0.5, 0, 255)  # JPEG3 colour is premultiplied
                px[..., 3] = a
                rgba = px.astype(np.uint8).tobytes()
        return w, h, rgba


def _unpremultiply(r, g, b, a):
    if a == 0:
        return (0, 0, 0, 0)
    return (min(255, r * 255 // a), min(255, g * 255 // a), min(255, b * 255 // a), a)


def _clean_jpeg(data):
    """Drop the stray EOI/SOI pair Flash files often carry at the front, which most decoders reject."""
    if data[:4] == b'\xff\xd9\xff\xd8':
        return data[4:]
    seam = data.find(b'\xff\xd9\xff\xd8')
    if data[:2] == b'\xff\xd8' and 0 < seam < 1024:  # tables and image joined with an EOI/SOI between them
        return data[:seam] + data[seam + 4:]
    return data


def _join_jpeg(tables, data):
    tables = _clean_jpeg(tables)
    data = _clean_jpeg(data)
    if tables[-2:] == b'\xff\xd9':
        tables = tables[:-2]
    if data[:2] == b'\xff\xd8':
        data = data[2:]
    return tables + data


def _decode_file(blob):
    """(width, height, RGBA) of a JPEG/PNG/GIF blob via OpenImageIO."""
    import numpy as np
    import OpenImageIO as oiio
    ext = '.png' if blob[:4] == b'\x89PNG' else '.gif' if blob[:3] == b'GIF' else '.jpg'
    fd, path = tempfile.mkstemp(suffix=ext, prefix='frosty_swf_')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(blob)
        buf = oiio.ImageBuf(path)
        spec = buf.spec()
        px = buf.get_pixels(oiio.UINT8)
        if px is None or not spec.width:
            raise ValueError('image could not be decoded: ' + (oiio.geterror() or buf.geterror()))
        px = np.asarray(px, np.uint8).reshape(spec.height, spec.width, spec.nchannels)
        if spec.nchannels == 1:
            px = np.repeat(px, 3, axis=2)
        if px.shape[2] == 3:
            px = np.concatenate([px, np.full(px.shape[:2] + (1,), 255, np.uint8)], axis=2)
        return spec.width, spec.height, px[..., :4].tobytes()
    finally:
        os.unlink(path)


def png_bytes(width, height, rgba):
    rows = b''.join(b'\0' + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))

    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload) & 0xFFFFFFFF)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 6)) + chunk(b'IEND', b''))


def image_name(movie, image):
    """A file-safe name for one image: its export name when it has one, else its file name or character id."""
    name = movie.exports.get(image['id']) or Path(image.get('file') or '').stem or image.get('export') or ''
    name = ''.join(c if c.isalnum() or c in '-_.' else '_' for c in name).strip('._')
    return ('%d_%s' % (image['id'], name)) if name else str(image['id'])


def extract(movie, dest):
    """Write every embedded bitmap of `movie` as PNG under `dest`. Returns (written paths, errors)."""
    dest = Path(dest)
    written, errors = [], []
    for image in movie.images:
        if image['tag'] not in JPEGS and image['tag'] not in LOSSLESS:
            continue
        try:
            w, h, rgba = movie.pixels(image)
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / (image_name(movie, image) + '.png')
            target.write_bytes(png_bytes(w, h, rgba))
            written.append(str(target))
        except Exception as error:
            errors.append({'name': image_name(movie, image), 'error': str(error)})
    return written, errors


def char_id(image):
    """DefineExternalImage2 keeps flags in the high half of its id; sub-images point at the low half."""
    return image['id'] & 0xFFFF


def texture_name(movie_name, image):
    """The game texture an external image stands for: in Frostbite it sits beside the movie, named after the file."""
    stem = Path(image.get('file') or image.get('export') or '').stem.lower()
    return '%s/%s' % (movie_name.lower().replace('\\', '/'), stem) if stem else None


def count(movie):
    """Images an export writes: embedded bitmaps, external textures and the sub-images cut from them."""
    return len(movie.images)


def summary(movie, movie_name):
    info = movie.describe()
    info['image_count'] = count(movie)
    for image in info['images']:
        if image['kind'].startswith('DefineExternalImage'):
            image['texture'] = texture_name(movie_name, image)
    return info


def _decode_textures(game, kind, names, reader, size):
    """{texture name: (width, height, RGBA)} through the reader, uncropped (icons.decode trims loading screens)."""
    import subprocess
    import icons
    out = {}
    with tempfile.TemporaryDirectory(prefix='frosty_movie_') as tmp:
        export, textures = Path(tmp) / 'export', Path(tmp) / 'textures'
        export.mkdir()
        resource_of = icons._export_bfn(game, sorted(names), export) if kind == 'bfn' else \
            icons._export_gw(game, kind, sorted(names), export)
        if not resource_of:
            return out
        env = dict(os.environ, AURA_IMPORT_TEXTURE_SIZE=str(size))
        subprocess.run([str(reader), '--textures', str(export), str(textures)], env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            decoded = json.loads((textures / 'textures.json').read_text(encoding='utf-8'))['textures']
        except (OSError, ValueError, KeyError):
            return out
        for name, rid in resource_of.items():
            entry = decoded.get(str(rid))
            if entry:
                raw = (textures / entry['path']).read_bytes()
                w, h = struct.unpack_from('<II', raw)
                out[name] = (w, h, raw[8:8 + w * h * 4])
    return out


def _crop(w, h, rgba, rect, declared):
    """One sub-image out of its atlas; the rect is in the atlas's authored size, which the texture may not match."""
    sx, sy = w / float(declared[0] or w), h / float(declared[1] or h)
    x1, y1 = max(0, int(rect[0] * sx)), max(0, int(rect[1] * sy))
    x2, y2 = min(w, int(round(rect[2] * sx))), min(h, int(round(rect[3] * sy)))
    if x2 <= x1 or y2 <= y1:
        return None
    rows = b''.join(rgba[(y * w + x1) * 4:(y * w + x2) * 4] for y in range(y1, y2))
    return x2 - x1, y2 - y1, rows


def export_images(game, kind, movie_name, data, reader, folder, size=2048):
    """Every image of one menu movie as PNG in `folder`: embedded bitmaps, the textures its external images name
    (whole atlases), and each sub-image cut from them. Returns (written paths, errors)."""
    movie = Movie(data)
    folder = Path(folder)
    written, errors = extract(movie, folder)
    externals = [i for i in movie.images if i['tag'] in EXTERNAL]
    names = {texture_name(movie_name, i) for i in externals} - {None}
    if not names:
        return written, errors
    if not reader:
        return written, errors + [{'name': movie_name, 'error': 'external images need the texture reader'}]
    limit = min(8192, max([size] + [max(i['width'], i['height']) for i in externals]))
    pictures = _decode_textures(game, kind, names, reader, limit)
    atlases = {}
    for image in externals:
        name = texture_name(movie_name, image)
        if name not in pictures:
            errors.append({'name': name or image.get('file', ''), 'error': 'texture not found for ' + image.get('file', '')})
            continue
        w, h, rgba = pictures[name]
        atlases[char_id(image)] = (w, h, rgba, (image['width'], image['height']))
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (name.rsplit('/', 1)[-1] + '.png')
        target.write_bytes(png_bytes(w, h, rgba))
        written.append(str(target))
    pieces = folder / 'pieces'
    for image in movie.images:
        if image['tag'] != SUB_IMAGE:
            continue
        atlas = atlases.get(image['image'])
        cut = atlas and _crop(atlas[0], atlas[1], atlas[2], image['rect'], atlas[3])
        if not cut:
            errors.append({'name': '%s #%d' % (movie_name, image['id']), 'error': 'sub-image outside its atlas'})
            continue
        pieces.mkdir(parents=True, exist_ok=True)
        target = pieces / (image_name(movie, image) + '.png')
        target.write_bytes(png_bytes(*cut))
        written.append(str(target))
    return written, errors


def main():
    action, path = sys.argv[1], sys.argv[2]
    movie = Movie(Path(path).read_bytes())
    if action == 'list':
        print(json.dumps(movie.describe(), indent=1))
    else:
        written, errors = extract(movie, sys.argv[3])
        print(json.dumps({'written': written, 'errors': errors}, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
