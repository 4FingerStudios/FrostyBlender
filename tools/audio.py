"""Game audio of Garden Warfare 1 and 2: SoundWave assets as playable WAV files.

A SoundWaveAsset (EBX) lists its sound data chunks and its segments: each segment starts at SamplesOffset in
one chunk with an EA "SPS" stream. That stream is a run of blocks, each a one-byte id and a three-byte
big-endian size that counts the four header bytes: 0x48 holds the header, 0x44 holds samples, 0x45 ends it.
The header packs version (4 bits), codec (4), channels - 1 (6) and sample rate (18), then the stream type (2),
a loop flag (1) and the sample count (29); a looped stream adds its loop start.

EA-XAS v1 (codec 4, what Garden Warfare uses) and big-endian PCM (codec 2) are decoded here, with numpy.
Other codecs (EALayer3, EA-Opus, ...) go through vgmstream when one is found: AURA_VGMSTREAM, vgmstream-cli
on PATH, or vgmstream-cli.exe in Documents/Frosty Blender/vgmstream. It is never downloaded for you.

    python audio.py list --game GAME --out sounds.json
    python audio.py export --game GAME --items names.json --dest FOLDER --out result.json
"""
import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

import gw2

CODECS = {0: 'None', 2: 'PCM', 3: 'EA-XMA', 4: 'EA-XAS', 5: 'EALayer3', 6: 'EALayer3 v2 PCM', 7: 'EALayer3 v2 Spike',
          8: 'GameCube ADPCM', 9: 'EA-Speex', 10: 'EA-TRAX', 11: 'EA-MP3', 12: 'EA-Opus', 13: 'EA-ATRAC9',
          14: 'EA-Opus multichannel', 15: 'EA-Opus multichannel'}
DECODED_HERE = {2, 4}
# EA-XA predictor pairs: coef1 is the first row, coef2 the second.
XA_COEFS = np.array([[0, 240, 460, 392], [0, 0, -208, -220]], dtype=np.int64)


# ---------------- finding the data ----------------
class Chunks:
    """Every chunk of an install: the chunk superbundles (patch first) and the bundles' own chunks."""

    def __init__(self, game):
        self.game = Path(game)
        self.loose = {}
        for root in gw2.data_roots(self.game):  # base first, then the patch overrides it
            for toc in sorted((root / 'Win32').rglob('*.toc')):
                try:
                    listed = gw2.read_db(toc).get('chunks') or []
                except (ValueError, struct.error, OSError):
                    continue
                for chunk in listed:
                    self.loose[chunk['id']] = (toc.with_suffix('.sb'), chunk['offset'], chunk['size'])
        self.bundled = gw2.cached_dependency_index(self.game)['chunk']

    def read(self, guid, head=False):
        """The chunk's bytes, or only its first compressed block with head=True (enough for the header)."""
        if guid in self.loose:
            path, offset, size = self.loose[guid]
        elif guid in self.bundled:
            path, offset, size, _ = self.bundled[guid]
        else:
            return None
        with path.open('rb') as stream:
            stream.seek(offset)
            data = stream.read(size if not head else min(size, 0x20000))
        out, at = bytearray(), 0
        while at + 8 <= len(data):
            decoded = struct.unpack_from('>I', data, at)[0] & 0xFFFFFF
            kind = struct.unpack_from('<H', data, at + 4)[0]
            packed = struct.unpack_from('>H', data, at + 6)[0] | ((kind >> 8 & 15) << 16)
            stored = decoded if kind & 127 == 0 else packed
            if not decoded or at + 8 + stored > len(data):
                break
            out += gw2.decompress(gw2.Reader(data[at:at + 8 + stored]), decoded)
            at += 8 + stored
            if head:
                break
        return bytes(out)


# ---------------- the SPS stream ----------------
def header(data, at=0):
    """{codec, channels, rate, samples, loop, start} of the SPS stream at `at`; start is its first sample block."""
    if at + 12 > len(data) or data[at] != 0x48:
        raise ValueError('No EA sound header at offset %d' % at)
    size = int.from_bytes(data[at + 1:at + 4], 'big')
    first, second = struct.unpack_from('>II', data, at + 4)
    info = {'version': first >> 28, 'codec': first >> 24 & 15, 'channels': (first >> 18 & 63) + 1,
            'rate': first & 0x3FFFF, 'type': second >> 30, 'loop': bool(second >> 29 & 1),
            'samples': second & 0x1FFFFFFF, 'start': at + size}
    if info['loop'] and size >= 16:
        info['loop_start'] = struct.unpack_from('>I', data, at + 12)[0]
    return info


def blocks(data, at):
    """(sample count, payload) of each 0x44 block from `at` up to the 0x45 end block."""
    while at + 4 <= len(data):
        kind, size = data[at], int.from_bytes(data[at + 1:at + 4], 'big')
        if kind == 0x45 or size < 4:
            return
        if kind == 0x44 and at + size <= len(data):
            yield struct.unpack_from('>I', data, at + 4)[0], data[at + 8:at + size]
        at += size


def decode_xas(payloads, channels):
    """EA-XAS v1: per channel, frames of 0x4C bytes for 128 samples, the channels' frames interleaved.
    A frame has four 32-bit little-endian group headers (two start samples, predictor and shift each), then 15
    rows of four bytes: byte `g` of a row holds two 4-bit samples of group g, high nibble first."""
    frames = np.frombuffer(b''.join(payloads), dtype=np.uint8)
    frames = frames[:len(frames) // (0x4C * channels) * 0x4C * channels].reshape(-1, channels, 0x4C)
    count = frames.shape[0]
    heads = frames[:, :, :16].copy().view('<u4').astype(np.int64)        # (frames, channels, 4 groups)
    nibbles = frames[:, :, 16:].reshape(count, channels, 15, 4)          # rows x groups
    nibbles = np.stack((nibbles >> 4, nibbles & 15), axis=-1)            # high nibble first
    nibbles = nibbles.transpose(0, 1, 3, 2, 4).reshape(count, channels, 4, 30).astype(np.int64)
    nibbles = np.where(nibbles >= 8, nibbles - 16, nibbles)
    index = heads & 15
    coef1, coef2 = XA_COEFS[0][index & 3], XA_COEFS[1][index & 3]
    hist2 = (heads & 0xFFF0).astype(np.int16).astype(np.int64)
    hist1 = (heads >> 16 & 0xFFF0).astype(np.int16).astype(np.int64)
    shift = 20 - (heads >> 16 & 15)
    out = np.empty((count, channels, 4, 32), dtype=np.int64)
    out[..., 0], out[..., 1] = hist2, hist1
    for i in range(30):
        sample = ((nibbles[..., i] << shift) + hist1 * coef1 + hist2 * coef2 + 128) >> 8
        sample = np.clip(sample, -32768, 32767)
        out[..., i + 2] = sample
        hist2, hist1 = hist1, sample
    return out.reshape(count, channels, 128).transpose(0, 2, 1).reshape(-1, channels).astype(np.int16)


def decode_stream(data, at):
    """(rate, int16 samples x channels) of the SPS stream at `at`, or raises NotImplementedError for codecs
    only vgmstream reads."""
    info = header(data, at)
    listed = list(blocks(data, info['start']))
    channels = info['channels']
    if info['codec'] == 4:
        pcm = decode_xas([p for _, p in listed], channels)
    elif info['codec'] == 2:
        raw = b''.join(p[:n * 2 * channels] for n, p in listed)
        pcm = np.frombuffer(raw[:len(raw) // (2 * channels) * 2 * channels], dtype='>i2').reshape(-1, channels)
    else:
        raise NotImplementedError(CODECS.get(info['codec'], 'codec %d' % info['codec']))
    return info['rate'], pcm[:info['samples']] if info['samples'] else pcm


def write_wav(path, rate, pcm):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(pcm.shape[1])
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(np.ascontiguousarray(pcm, dtype='<i2').tobytes())


# ---------------- vgmstream, for the codecs decoded elsewhere ----------------
def vgmstream():
    """Path of a vgmstream command line decoder the user installed, or None."""
    candidates = [os.environ.get('AURA_VGMSTREAM'), shutil.which('vgmstream-cli'), shutil.which('vgmstream')]
    documents = Path(os.environ.get('USERPROFILE') or Path.home()) / 'Documents' / 'Frosty Blender' / 'vgmstream'
    candidates += [str(documents / 'vgmstream-cli.exe'), str(documents / 'test.exe')]
    return next((c for c in candidates if c and Path(c).is_file()), None)


VGMSTREAM_MISSING = ('%s audio needs vgmstream: put vgmstream-cli.exe (from vgmstream.org) in Documents/Frosty '
                     'Blender/vgmstream, on PATH, or name it in AURA_VGMSTREAM')


def via_vgmstream(data, at, target):
    tool = vgmstream()
    if not tool:
        raise RuntimeError(VGMSTREAM_MISSING % CODECS.get(header(data, at)['codec'], 'This'))
    end = header(data, at)['start']
    for _, payload in blocks(data, end):
        end += len(payload) + 8
    with tempfile.TemporaryDirectory(prefix='frosty_audio_') as tmp:
        sps = Path(tmp) / 'sound.sps'
        sps.write_bytes(data[at:end + 4])
        target.parent.mkdir(parents=True, exist_ok=True)
        done = subprocess.run([tool, '-o', str(target), str(sps)], capture_output=True, text=True,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if done.returncode or not target.is_file():
            raise RuntimeError('vgmstream could not decode it: ' + (done.stderr or done.stdout).strip()[-200:])


# ---------------- assets ----------------
def kind_of(name):
    low = name.lower()
    return 'Voice' if '/vo/' in low or low.startswith('sound/vo/') else 'Music' if '/music/' in low else 'Effect'


def variations(root):
    """[(chunk index, [segment, ...]), ...] for each variation of a SoundWaveAsset root object."""
    segments = root.get('Segments') or []
    out = []
    for v in root.get('RuntimeVariations') or []:
        first, count = v.get('FirstSegmentIndex', 0), max(1, v.get('SegmentCount', 1))
        out.append((v.get('ChunkIndex', 0), segments[first:first + count]))
    if not out and segments:
        out = [(0, segments)]
    return out


def describe(name, root, chunks):
    """Listing row for one SoundWaveAsset: name, kind, duration, channels, codec, rate, variation count."""
    found = variations(root)
    length = sum(s.get('SegmentLength', 0) for s in found[0][1]) if found else 0
    row = {'name': name, 'kind': kind_of(name), 'duration': round(length, 3), 'channels': root.get('ChannelCount', 0),
           'variations': len(found), 'codec': '', 'rate': 0}
    listed = root.get('Chunks') or []
    if found and listed:
        index, segs = found[0]
        guid = listed[min(index, len(listed) - 1)].get('ChunkId')
        head = chunks.read(guid, head=True) if guid else None
        try:
            if head is None:
                row['codec'] = 'missing data'
            else:
                info = header(head, segs[0].get('SamplesOffset', 0) if segs else 0)
                row['codec'], row['rate'] = CODECS.get(info['codec'], 'codec %d' % info['codec']), info['rate']
        except (ValueError, struct.error):
            row['codec'] = 'unknown'
    return row


def _sound_names(source):
    """EBX under sound/ (GW2) or _pvz/sound/ (GW1): no SoundWaveAsset lives anywhere else."""
    return sorted(n for n in source.ebx if 'sound' in n.split('/')[:2])


def listing(game):
    """{'sounds': [row, ...], 'decoder': vgmstream path or ''} for every SoundWaveAsset, cached per install."""
    import explorer
    source = explorer.Source(game)
    if source.kind == 'bfn':
        raise ValueError('Battle for Neighborville sounds are not readable yet; Garden Warfare 1 and 2 are.')
    stamp = sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in (Path(game) / 'Data/Win32').rglob('*.toc'))
    key = hashlib.sha256(repr(['sounds-v2', str(Path(game).resolve()), stamp]).encode()).hexdigest()[:16]
    path = explorer.cache_root() / ('sounds_%s_%s.json' % (source.kind, key))
    if path.is_file():
        rows = json.loads(path.read_text(encoding='utf-8'))
    else:
        chunks, rows = Chunks(game), []
        for name in _sound_names(source):
            data = source.ebx_bytes(name)
            if not data or b'SoundWaveAsset' not in data:
                continue
            try:
                document = gw2.Ebx(data).document()
            except (ValueError, struct.error, IndexError):
                continue
            if document['root_type'] == 'SoundWaveAsset':
                rows.append(describe(source.ebx[name][3]['name'], document['objects'][0], chunks))
        path.write_text(json.dumps(rows), encoding='utf-8')
    return {'game': source.kind, 'sounds': rows, 'decoder': vgmstream() or ''}


def export_sound(source, chunks, name, root, dest):
    """WAV files for one SoundWaveAsset under dest (mirroring its path); one per variation, numbered when there
    are several. Returns the written paths."""
    listed = [c.get('ChunkId') for c in root.get('Chunks') or []]
    cache, written = {}, []
    found = variations(root)
    base = Path(dest) / name.replace('\\', '/')
    for number, (index, segments) in enumerate(found, 1):
        guid = listed[min(index, len(listed) - 1)] if listed else None
        if guid not in cache:
            cache[guid] = chunks.read(guid) if guid else None
        data = cache[guid]
        if data is None:
            raise ValueError('The sound data chunk is not in the game archives')
        target = base.with_name(base.name + ('_%02d' % number if len(found) > 1 else '') + '.wav')
        parts, rate = [], 0
        try:
            for segment in segments:
                rate, pcm = decode_stream(data, segment.get('SamplesOffset', 0))
                parts.append(pcm)
            write_wav(target, rate, np.concatenate(parts) if len(parts) > 1 else parts[0])
        except NotImplementedError:
            if len(segments) != 1:
                raise RuntimeError('This codec needs vgmstream, and only one segment per variation is supported')
            via_vgmstream(data, segments[0].get('SamplesOffset', 0), target)
        written.append(str(target))
    return written


def export(game, names, dest):
    import explorer
    source, chunks = explorer.Source(game), Chunks(game)
    written, errors = [], []
    for name in names:
        try:
            data = source.ebx_bytes(name)
            document = gw2.Ebx(data).document() if data else None
            if not document or document['root_type'] != 'SoundWaveAsset':
                raise ValueError('Not a SoundWaveAsset')
            written += export_sound(source, chunks, name, document['objects'][0], dest)
        except Exception as error:  # one sound must not stop the others
            errors.append({'name': name, 'error': str(error)})
    return {'written': written, 'errors': errors, 'dest': str(dest)}


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('action', choices=('list', 'export'))
    p.add_argument('--game', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--items')
    p.add_argument('--dest')
    a = p.parse_args()
    if a.action == 'list':
        result = listing(a.game)
    else:
        result = export(a.game, json.loads(Path(a.items).read_text(encoding='utf-8')), a.dest)
    Path(a.out).write_text(json.dumps(result), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
