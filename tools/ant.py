"""ANT animation banks (AntStateAsset data) of Garden Warfare 1 and 2 and Battle for Neighborville.

A bank is a sequence of "GD." sections: GD.REFL describes classes (GenericData v1), each
GD.DATA holds one object of such a class; newer banks use GD.REF2/GD.DAT2 instead. Objects are
read generically into dicts, then animation assets are decoded into per-channel keyframes.

The layout follows the AssetBankPlugin of the Frosty Tool Suite fork maintained for Battle for
Neighborville (GPL-3.0), cross-checked against the games' own banks.
"""
import struct

GD = b'GD.'


class Reader:
    def __init__(self, data, at=0):
        self.data, self.at = data, at

    def take(self, n):
        if n < 0 or self.at + n > len(self.data):
            raise ValueError('ANT read out of range')
        v = self.data[self.at:self.at + n]
        self.at += n
        return v

    def num(self, fmt):
        size = struct.calcsize(fmt)
        v = struct.unpack_from(fmt, self.data, self.at)[0]
        self.at += size
        return v

    def cstring(self):
        end = self.data.index(b'\0', self.at)
        v = self.data[self.at:end].decode('ascii', 'replace')
        self.at = end + 1
        return v


def guid(raw, big):
    """A GUID as Frostbite prints it (the .NET byte order), from big- or little-endian storage."""
    if big:
        raw = raw[3::-1] + raw[5:3:-1] + raw[7:5:-1] + raw[8:]
    return raw.hex()


# ---------------- GenericData v1 ----------------
PRIMITIVE = {'Bool': '?', 'Int8': 'b', 'UInt8': 'B', 'Int16': 'h', 'UInt16': 'H', 'Int32': 'i', 'UInt32': 'I',
             'Int64': 'q', 'UInt64': 'Q', 'Float': 'f', 'Double': 'd', 'DataRef': 'q'}


def _layout(r, e):
    """A REFL layout entry: (name, hash, size, alignment, fields[(name, offset, flags, layout offset)])."""
    start = r.at
    lo, hi, size, align, strings, _, _, native = (r.num(e + 'i'), r.num(e + 'i'), r.num(e + 'I'), r.num(e + 'I'),
                                                   r.num(e + 'I'), r.num(e + 'I'), r.num('?'), r.num('?'))
    r.take(2)
    hash_ = r.num(e + 'I')
    count = hi - lo + 1
    entries = []
    while len(entries) < count:
        (lhash, esize, off, name, n, flags, ealign, rle), layout = struct.unpack_from(e + 'IIIIHHHh', r.data, r.at), None
        r.at += 24
        layout = r.num(e + 'q')
        if esize and n:
            entries.append((name, off, flags, layout))
        else:
            count -= 1  # empty slots do not count
    r.take(1)
    name = r.cstring()
    fields = []
    for fname, off, flags, layout in entries:
        end = r.data.index(b'\0', start + strings + fname)
        fields.append((r.data[start + strings + fname:end].decode('ascii', 'replace'), off, flags, layout))
    return {'name': name, 'hash': hash_, 'size': size, 'align': max(align, 1), 'native': native, 'fields': fields}


class BankV1:
    def __init__(self, data, at, e):
        self.data, self.e, self.big = data, e, e == '>'
        self.classes = {}
        self.objects = []

    def read_refl(self, at):
        e, r = self.e, Reader(self.data, at)
        r.take(8)
        size = r.num(e + 'I')
        r.num(e + 'I')
        base = r.at
        count = r.num(e + 'q')
        offsets = [r.num(e + 'q') for _ in range(count)]
        layouts = {}
        for off in offsets:
            r.at = base + off
            layouts[off] = _layout(r, e)
        for off, layout in layouts.items():
            fields = []
            for name, field_off, flags, type_off in layout['fields']:
                r.at = base + type_off
                t = _layout(r, e)
                fields.append({'name': name, 'offset': field_off, 'array': flags == 1, 'type': t['name'],
                               'hash': t['hash'], 'size': t['size'], 'align': t['align']})
            self.classes[layout['hash']] = {'name': layout['name'], 'size': layout['size'], 'align': layout['align'],
                                            'fields': fields}
        return at + size

    def read_data(self, at):
        e = self.e
        size, indices = struct.unpack_from(e + 'II', self.data, at + 8)
        base = at + 16
        # Object header: hash (8), pad (8), type (8), pad (4), offset (2), pad (2).
        h, t = struct.unpack_from(e + 'Q', self.data, base)[0], struct.unpack_from(e + 'Q', self.data, base + 16)[0]
        offset = struct.unpack_from(e + 'H', self.data, base + 28)[0]
        self.objects.append((base, int(t & 0xffffffff), offset))
        return at + size

    def values(self, base, type_hash, at, depth=0):
        """The fields of an object of class type_hash stored at `at` (absolute)."""
        if depth > 32:
            raise ValueError('ANT object nesting too deep')
        cls = self.classes.get(type_hash)
        if cls is None:
            return {}
        e, d = self.e, self.data
        out = {}
        for f in cls['fields']:
            pos = at + f['offset']
            t = f['type']
            if f['array'] or t == 'String':
                count = struct.unpack_from(e + 'I', d, pos + 4)[0]
                start = base + struct.unpack_from(e + 'q', d, pos + 8)[0]
                if t == 'String':
                    out[f['name']] = d[start:start + count].split(b'\0')[0].decode('ascii', 'replace')
                elif t in PRIMITIVE:
                    fmt = PRIMITIVE[t]
                    out[f['name']] = list(struct.unpack_from(e + fmt * count, d, start))
                elif t == 'Guid':
                    out[f['name']] = [guid(d[start + 16 * i:start + 16 * i + 16], self.big) for i in range(count)]
                else:
                    stride = (f['size'] + f['align'] - 1) // f['align'] * f['align']
                    out[f['name']] = [self.values(base, f['hash'], start + stride * i, depth + 1) for i in range(count)]
            elif t in PRIMITIVE:
                out[f['name']] = struct.unpack_from(e + PRIMITIVE[t], d, pos)[0]
            elif t == 'Guid':
                out[f['name']] = guid(d[pos:pos + 16], self.big)
            else:
                out[f['name']] = self.values(base, f['hash'], pos, depth + 1)
        return out

    def header(self, at):
        """An object header: (type hash, data offset)."""
        e = self.e
        return struct.unpack_from(e + 'Q', self.data, at + 16)[0] & 0xffffffff, struct.unpack_from(e + 'H', self.data, at + 28)[0]

    def objects_of(self):
        """Every object, its base class's fields merged in (the object's own fields win)."""
        for base, type_hash, offset in self.objects:
            cls = self.classes.get(type_hash)
            if cls is None:
                continue
            values = self.values(base, type_hash, base + offset)
            link = values.get('__base')
            if isinstance(link, int) and link:
                base_type, base_offset = self.header(base + link)
                if base_type in self.classes:
                    for k, v in self.values(base, base_type, base + base_offset + link).items():
                        values.setdefault(k, v)
            values['__type'] = cls['name']
            yield values


def read_bank(data):
    """The objects of a bank, as dicts with '__type' and the class's fields."""
    at = 4  # packaging type (big-endian)
    if data[at:at + 3] != GD:
        at += struct.unpack_from('>I', data, at)[0]
    v2, v1 = data.find(b'GD.REF2', at, at + 4096), data.find(b'GD.REFL', at, at + 4096)
    if v2 >= 0 and (v1 < 0 or v2 < v1):
        return read_bank_v2(data, at)
    bank = None
    while True:
        at = data.find(GD, at)
        if at < 0 or at + 12 > len(data):
            break
        tag, endian = data[at:at + 7], data[at + 7:at + 8]
        e = '>' if endian == b'b' else '<'
        size = struct.unpack_from(e + 'I', data, at + 8)[0]
        if tag in (b'GD.REFL', b'GD.DATA', b'GD.STRM'):
            if bank is None:
                bank = BankV1(data, at, e)
            if tag == b'GD.REFL':
                bank.read_refl(at)
            elif tag == b'GD.DATA':
                bank.read_data(at)
        # GD.STRM wraps the sections after it; only its 16-byte header is its own.
        at += 16 if tag == b'GD.STRM' else max(size, 12)
    if bank is None:
        raise ValueError('No ANT sections found')
    return bank


# ---------------- VBR clips ----------------
DCT = [
    [0.12500000, 0.24519631, 0.23096988, 0.20786740, 0.17677669, 0.13889255, 0.09567086, 0.04877256],
    [0.12500000, 0.20786740, 0.09567086, -0.04877258, -0.17677669, -0.24519633, -0.23096988, -0.13889250],
    [0.12500000, 0.13889255, -0.09567088, -0.24519633, -0.17677666, 0.04877260, 0.23096989, 0.20786734],
    [0.12500000, 0.04877256, -0.23096991, -0.13889250, 0.17677675, 0.20786734, -0.09567098, -0.24519630],
    [0.12500000, -0.04877258, -0.23096988, 0.13889261, 0.17677669, -0.20786744, -0.09567074, 0.24519636],
    [0.12500000, -0.13889259, -0.09567078, 0.24519631, -0.17677681, -0.04877255, 0.23096983, -0.20786740],
    [0.12500000, -0.20786741, 0.09567090, 0.04877252, -0.17677663, 0.24519633, -0.23096994, 0.13889278],
    [0.12500000, -0.24519633, 0.23096989, -0.20786744, 0.17677671, -0.13889271, 0.09567098, -0.04877289],
]
LOGS = [0.693147, 1.098612, 1.386294, 1.609438, 1.791759, 1.945910, 2.079442, 2.197225]


class Bits:
    """MSB-first bits over a byte string."""
    def __init__(self, data):
        self.data, self.bit = data, 0

    def read(self, n):
        v = 0
        for _ in range(n):
            byte = self.data[self.bit >> 3] if (self.bit >> 3) < len(self.data) else 0
            v = (v << 1) | ((byte >> (7 - (self.bit & 7))) & 1)
            self.bit += 1
        return v


def _reverse(value, n):
    out = 0
    for _ in range(n):
        out = (out << 1) | (value & 1)
        value >>= 1
    return out


_BYTE_REVERSED = bytes(_reverse(b, 8) for b in range(256))


def vbr_decode(a):
    """A VbrAnimationAsset's per-frame values: (animated rows, palette indices, constant channel map).
    Rows are frames x (quaternions + vec3s + floats) x 4 floats."""
    q, v, fl = a.get('QuaternionCount', 0), a.get('Vector3Count', 0), a.get('FloatCount', a.get('NumFloat', 0))
    cq, cv, cf = a.get('ConstQuaternionCount', 0), a.get('ConstVector3Count', 0), a.get('ConstFloatCount', 0)
    keys = a.get('NumKeys', 0)
    channels = q * 4 + v * 3 + fl
    const_dofs = cq * 4 + cv * 3 + cf
    data = bytearray(a['Data'])
    sizes = a.get('FrameBlockSizes') or []
    total = sum(sizes)
    header = len(data) - total
    key_time, chan_map = a.get('KeyTimeSize', 0), a.get('ConstChanMapSize', 0)
    vec_off, float_off = a.get('VectorOffsetSize', 0), a.get('FloatOffsetSize', 0)
    # Some Battle for Neighborville clips were compiled with a wrong header size; their blocks
    # were byte-swapped from that offset. Undo it and swap from the real one, as Frosty does.
    buggy = key_time + const_dofs + chan_map + channels * 4 + vec_off + float_off
    if buggy != header:
        for start in (buggy, header):
            at = start
            for size in sizes:
                lo, hi = at + 3, at + size
                if size > 3 and hi <= len(data):
                    data[lo:hi] = data[lo:hi][::-1]
                at += size
    meta, frames = bytes(data[:header]), bytes(data[header:])
    pos = key_time
    if a.get('ConstPaletteSize', 0) <= 256:
        palette_index = list(meta[pos:pos + const_dofs])
        pos += const_dofs
    else:
        palette_index = list(struct.unpack_from('<%dH' % const_dofs, meta, pos))
        pos += const_dofs * 2
    const_map = list(meta[pos:pos + chan_map])
    pos += chan_map
    nibbles = []
    for byte in meta[pos:pos + channels * 4]:
        nibbles += [byte >> 4, byte & 15]
    pos += channels * 4
    curves = meta[pos:pos + vec_off + float_off]

    blocks, quant = [], []
    at = 0
    dct = a.get('Dct') or 4.0
    if dct <= 0:
        dct = 4.0
    for size in sizes:
        block = frames[at:at + size]
        at += size
        scales = []
        for table in block[:3]:  # rotation, trajectory, translation quantisation
            s = (table + 1.0) * 0.2 * dct
            scales.append([(s * LOGS[c] + 1.0) * dct / 32768.0 for c in range(8)])
        quant.append(scales)
        r = Bits(bytes(_BYTE_REVERSED[b] for b in block[3:][::-1]))
        coefficients = []
        for chan in range(channels):
            n = chan * 8
            bits = [nibbles[n + 1], nibbles[n], nibbles[n + 3], nibbles[n + 2],
                    nibbles[n + 5], nibbles[n + 4], nibbles[n + 7], nibbles[n + 6]]
            present = [r.read(1) if b else 0 for b in bits]
            sign = [r.read(1) if (p and b) else 0 for p, b in zip(present, bits)]
            values = [0] * 8
            for j in range(8):
                if present[j]:
                    value = _reverse(r.read(bits[j]), bits[j])
                    values[j] = value if sign[j] else -value
            coefficients.append(values)
        blocks.append(coefficients)

    ranges = []
    for chan in range(channels):
        if chan < q * 4:
            ranges.append((0, a['QuatMax'] - a['QuatMin'], a['QuatMin']))
        elif chan < q * 4 + 3:
            ranges.append((1, a['TrajMax'] - a['TrajMin'], a['TrajMin']))
        elif chan < q * 4 + v * 3:
            ranges.append((2, a['Vec3Max'] - a['Vec3Min'], a['Vec3Min']))
        else:
            ranges.append((2, a.get('FloatMax', 0) - a.get('FloatMin', 0), a.get('FloatMin', 0)))
    result = [[0.0] * channels for _ in range(keys)]
    last_start = keys - keys % 8
    for frame in range(keys):
        b = frame // 8
        if b >= len(blocks):
            break
        coeff = frame - (keys - 8) if (frame >= last_start and frame > 7) else frame % 8
        row = DCT[coeff]
        for chan in range(channels):
            table, delta, low = ranges[chan]
            scales = quant[b][table]
            values = blocks[b][chan]
            total_ = 0.5
            for i in range(8):
                if values[i]:
                    total_ += values[i] * row[i] * scales[i]
            result[frame][chan] = total_ * delta + low

    def curve_set(start, raw_scale, first):
        if start >= len(curves) or not raw_scale:
            return
        scale = raw_scale / 127.0
        at = start
        count = curves[at]
        at += 1
        chan = first
        for _ in range(count):
            if at + 1 >= len(curves):
                break
            chan += curves[at]
            points = curves[at + 1]
            at += 2
            if not points:
                chan += 1
                continue
            target = -1
            vec_start, float_start = q * 4, q * 4 + v * 4
            if chan >= float_start:
                target = q * 4 + v * 3 + (chan - float_start)
            elif chan >= vec_start and (chan - vec_start) % 4 < 3:
                target = q * 4 + (chan - vec_start) // 4 * 3 + (chan - vec_start) % 4
            y = struct.unpack('b', curves[at:at + 1])[0] * scale
            at += 1
            frame = 0
            for _ in range(points - 1):
                if at + 1 >= len(curves):
                    break
                span = curves[at] * 8
                y2 = struct.unpack('b', curves[at + 1:at + 2])[0] * scale
                at += 2
                if target >= 0:
                    for f in range(span):
                        if frame + f >= keys:
                            break
                        result[frame + f][target] += y + (y2 - y) * f / span
                frame += span
                y = y2
            if target >= 0:
                for f in range(frame, keys):
                    result[f][target] += y
    curve_set(0, a.get('VectorOffsetScale', 0), q * 4)
    curve_set(vec_off, a.get('FloatOffsetScale', 0), q * 4 + v * 4)

    rows = []
    for frame in range(keys):
        r = result[frame]
        dof = [tuple(r[i * 4:i * 4 + 4]) for i in range(q)]
        dof += [tuple(r[q * 4 + i * 3:q * 4 + i * 3 + 3]) + (0.0,) for i in range(v)]
        dof += [(r[q * 4 + v * 3 + i], 0.0, 0.0, 0.0) for i in range(fl)]
        rows.append(dof)
    return rows, palette_index, const_map


# ---------------- clips onto bones ----------------
ROTATION, POSITION, SCALE = 14, 2049856663, 2049856454
NO_CHANNEL_MAP = 8
EMPTY = '0' * 32


def _slot(item):
    if isinstance(item, dict):
        name = item.get('chars') if 'chars' in item else item.get('Name', 'unknown')
        if isinstance(name, list):
            name = bytes(name).split(b'\0')[0].decode('ascii', 'replace')
        return str(name), item.get('Type', 0)
    return 'unknown', 0


class Database:
    """The objects of one or more banks by GUID, with clip lookups."""
    def __init__(self, banks):
        self.objects = {}
        for bank in banks:
            for o in bank.objects_of():
                if o.get('__guid'):
                    self.objects[o['__guid']] = o
        self.clips_of = {}
        for o in self.objects.values():
            if o['__type'] in ('ClipControllerAsset', 'ClipControllerData'):
                for anim in [o.get('Anim')] + list(o.get('Anims') or []):
                    if isinstance(anim, str) and anim.strip('0'):
                        self.clips_of.setdefault(anim, o)
        self.rigs = [o for o in self.objects.values() if o['__type'] == 'RigAsset']

    def get(self, guid):
        return self.objects.get(guid) if isinstance(guid, str) and guid.strip('0') else None

    def clip_name(self, anim):
        """Battle for Neighborville names its clips by hash; their clip controllers carry the real names."""
        name = anim.get('__name') or ''
        clip = self.clips_of.get(anim['__guid'])
        if clip and clip.get('__name') and (len(name) == 16 and all(c in '0123456789ABCDEF' for c in name) or not name):
            return clip['__name']
        return name or anim['__guid']

    def animations(self):
        return [o for o in self.objects.values() if o['__type'] in ('VbrAnimationAsset', 'DctAnimationAsset', 'RawAnimationAsset')]

    # -- channel names of a target hierarchy, in slot order
    def channel_names(self, target):
        names, dof_sets = {}, set()

        def dof_set(guid):
            dof_sets.add(guid)
            o = self.get(guid)
            if o and o['__type'] == 'DofSetAsset':
                for item in o.get('Slots') or []:
                    n, t = _slot(item)
                    names.setdefault(n, t)
                for child in o.get('DofAssets') or []:
                    dof_set(child)
        o = self.get(target)
        if o is None:
            return [], dof_sets, None
        if o['__type'] == 'LayoutHierarchyAsset':
            for layout in o.get('LayoutAssets') or []:
                la = self.get(layout)
                if la is None:
                    continue
                if la['__type'] == 'LayoutAsset':
                    for item in la.get('Slots') or []:
                        n, t = _slot(item)
                        names.setdefault(n, t)
                elif la['__type'] == 'DofSetAsset':
                    dof_set(layout)
                elif la['__type'] == 'DeltaTrajLayoutAsset':
                    for x in range(8):
                        names.setdefault(str(x), ROTATION)
        elif o['__type'] == 'DofSetListAsset':
            for d in o.get('DofSetAssets') or []:
                dof_set(d)
        elif o['__type'] == 'DofSetAsset':
            dof_set(target)
        return list(names.items()), dof_sets, o

    def _contains(self, hierarchy, target, depth=0):
        if hierarchy is None or depth > 32:
            return False
        if hierarchy['__guid'] == target:
            return True
        return any(self._contains(self.get(c), target, depth + 1) for c in hierarchy.get('Children') or []
                   if (self.get(c) or {}).get('__type') == 'LayoutHierarchyAsset')

    def _dof_sets_of_list(self, guid, out, depth=0):
        o = self.get(guid)
        if o is None or o['__type'] != 'DofSetListAsset' or depth > 32:
            return
        out.update(o.get('DofSetAssets') or [])
        for child in o.get('DofSetListAssets') or []:
            self._dof_sets_of_list(child, out, depth + 1)

    def rig_of(self, mapping, dof_sets, target):
        rig = self.get(mapping.get('Rig'))
        if rig:
            return rig
        if dof_sets:
            for r in self.rigs:
                have = set(r.get('RigDofSets') or [])
                if not have and r.get('DofSetLists'):
                    for dsl in r['DofSetLists']:
                        self._dof_sets_of_list(dsl, have)
                if r.get('DofIds') and have >= dof_sets:
                    return r
        if target is not None and target['__type'] == 'LayoutHierarchyAsset':
            for r in self.rigs:
                for dsl in r.get('DofSetLists') or []:
                    root = self.get(dsl)
                    if root is not None and root['__type'] == 'LayoutHierarchyAsset' and self._contains(root, target['__guid']):
                        return r
        return None

    def rig_slots(self, rig):
        """Battle for Neighborville: DofId -> (name, type) through the rig's DofSets."""
        out = {}
        dof_ids = rig.get('DofIds') or []
        sets = rig.get('RigDofSets') or []
        starts = rig.get('DofSetIdIndices') or []
        sequential = 0
        for i, guid in enumerate(sets):
            o = self.get(guid)
            slots = [_slot(item) for item in (o.get('Slots') or [])] if o and o['__type'] == 'DofSetAsset' else []
            start = starts[i] if len(starts) == len(sets) else sequential
            for j, (n, t) in enumerate(slots):
                if 0 <= start + j < len(dof_ids):
                    out[dof_ids[start + j]] = (n, t)
            sequential += len(slots)
        return out

    def channels(self, anim, game):
        """Ordered (name, type, dof id) channels of an animation, the rig, and the clip's FPS."""
        mapping = self.get(anim.get('ChannelToDofAsset')) or {}
        if 'ChannelToDofIds' in mapping:
            index = list(mapping['ChannelToDofIds'])
        elif 'DofIds' in mapping:
            index = list(mapping['DofIds'])
        else:
            raw = list(mapping.get('IndexData') or [])
            index = [(raw[i] << 8) | raw[i + 1] for i in range(0, len(raw) - 1, 2)] if game == 'gw1' and len(raw) > 256 else raw
        clip = self.clips_of.get(anim['__guid']) or {}
        target = clip.get('Target') if self.get(clip.get('Target')) else anim.get('DofSetList')
        names, dof_sets, target_obj = self.channel_names(target)
        rig = self.rig_of(mapping, dof_sets, target_obj)
        ordered = []
        if game == 'bfn':
            slots = self.rig_slots(rig) if rig else {}
            for dof in index:
                n, t = slots.get(dof, ('Unknown_%d' % dof, 0))
                ordered.append((n, t, dof))
        elif game == 'gw2':
            positions = {d: i for i, d in enumerate((rig or {}).get('DofIds') or [])}
            for dof in index:
                i = positions.get(dof, -1)
                if 0 <= i < len(names):
                    ordered.append((names[i][0], names[i][1], dof))
                else:
                    ordered.append(('Unknown_%d' % dof, 0, dof))
        else:
            for i in index:
                if 0 <= i < len(names):
                    ordered.append((names[i][0], names[i][1], 0))
        return ordered, rig, clip.get('FPS') or 30.0

    def defaults(self, rig):
        out = {}
        for key, size in (('DefaultVector4Values', 4), ('DefaultVector3Values', 3)):
            for item in (rig or {}).get(key) or []:
                v = item.get('Value') or {}
                if isinstance(v, dict):
                    out[item.get('DofId')] = tuple(v.get(c, 0.0) for c in 'xyzw'[:size])
                else:  # GenericData v2 stores vectors as plain lists
                    out[item.get('DofId')] = tuple(list(v)[:size])
        return out


def _walk(const_map, counts):
    """Ordered channel indices for each run of counts, following a constant channel map."""
    out, ordered, offset, count = [], const_map[0] if const_map else 0, 1, 0

    def advance():
        nonlocal ordered, offset, count
        for _ in range(2):
            if offset < len(const_map) and count >= const_map[offset]:
                if offset + 1 < len(const_map):
                    ordered += const_map[offset + 1]
                offset += 2
                count = 0
    for n in counts:
        part = []
        for _ in range(n):
            advance()
            part.append(ordered)
            ordered += 1
            count += 1
        out.append(part)
    return out


def _walk_animated(const_map, counts):
    out, ordered, offset, count = [], 0, 0, 0

    def advance():
        nonlocal ordered, offset, count
        for _ in range(2):
            if offset < len(const_map) and count >= const_map[offset]:
                if offset + 1 < len(const_map):
                    ordered += const_map[offset + 1]
                offset += 2
                count = 0
    for n in counts:
        part = []
        for _ in range(n):
            advance()
            part.append(ordered)
            ordered += 1
            count += 1
        out.append(part)
    return out


def clip_frames(db, anim, game):
    """A decoded clip: {'name', 'fps', 'rig', 'frames': [{bone: {'r': xyzw, 't': xyz, 's': xyz}}], 'additive'}."""
    if anim['__type'] != 'VbrAnimationAsset':
        raise NotImplementedError(anim['__type'] + ' clips are not decoded yet')
    ordered, rig, fps = db.channels(anim, game)
    if not ordered:
        raise ValueError('No channel mapping for ' + anim.get('__name', '?'))
    rows, palette_index, const_map = vbr_decode(anim)
    q, v, fl = anim.get('QuaternionCount', 0), anim.get('Vector3Count', 0), anim.get('FloatCount', 0)
    cq, cv, cf = anim.get('ConstQuaternionCount', 0), anim.get('ConstVector3Count', 0), anim.get('ConstFloatCount', 0)
    palette = anim.get('ConstantPalette') or []
    additive = bool(anim.get('Additive'))
    defaults = {} if additive else db.defaults(rig)
    total = len(ordered)
    if anim.get('Flags', 0) & NO_CHANNEL_MAP:
        quats = [i for i, c in enumerate(ordered) if c[1] == ROTATION]
        vecs = [i for i, c in enumerate(ordered) if c[1] in (POSITION, SCALE)]
        aq, av, af, kq, kv, kf = quats[:q], vecs[:v], [], [], [], []
    elif const_map:
        kq, kv, kf = _walk(const_map, (cq, cv, cf))
        aq, av, af = _walk_animated(const_map, (q, v, fl))
    else:
        aq, av, af, kq, kv, kf = [], [], [], [], [], []
        for i, (_, t, _) in enumerate(ordered):
            if t == ROTATION:
                (aq if len(aq) < q else kq).append(i)
            elif t in (POSITION, SCALE):
                (av if len(av) < v else kv).append(i)
            else:
                (af if len(af) < fl else kf).append(i)
    qmin, qd = anim.get('QuatMin', -1.0), anim.get('QuatMax', 1.0) - anim.get('QuatMin', -1.0)
    vmin, vd = anim.get('Vec3Min', 0.0), anim.get('Vec3Max', 0.0) - anim.get('Vec3Min', 0.0)
    # Constant channels: the same palette values every frame.
    constant, p = {}, 0
    for i in kq:
        if p + 3 >= len(palette_index) or any(palette_index[p + k] >= len(palette) for k in range(4)):
            break
        constant[i] = tuple(palette[palette_index[p + k]] * qd + qmin for k in range(4))
        p += 4
    for i in kv:
        if p + 2 >= len(palette_index) or any(palette_index[p + k] >= len(palette) for k in range(3)):
            break
        constant[i] = tuple(palette[palette_index[p + k]] * vd + vmin for k in range(3))
        p += 3
    base = {}
    for i, (n, t, dof) in enumerate(ordered):
        if t == ROTATION:
            base[i] = defaults.get(dof, (0.0, 0.0, 0.0, 1.0))
        elif t == POSITION:
            base[i] = defaults.get(dof, (0.0, 0.0, 0.0))
        elif t == SCALE:
            base[i] = defaults.get(dof, (0.0, 0.0, 0.0) if additive else (1.0, 1.0, 1.0))
    frames = []
    for row in rows:
        values = dict(base)
        values.update(constant)
        for k, i in enumerate(aq):
            if i < total and k < len(row):
                values[i] = row[k]
        for k, i in enumerate(av):
            if i < total and q + k < len(row):
                values[i] = row[q + k][:3]
        pose = {}
        for i, (n, t, _) in enumerate(ordered):
            if i not in values or t not in (ROTATION, POSITION, SCALE):
                continue
            bone = n.rsplit('.', 1)[0] if n[-2:] in ('.q', '.t', '.s') else n
            key = {ROTATION: 'r', POSITION: 't', SCALE: 's'}[t]
            value = values[i]
            if t == ROTATION:
                length = sum(x * x for x in value) ** 0.5 or 1.0
                value = tuple(x / length for x in value)
            pose.setdefault(bone, {})[key] = [float(x) for x in value]
        frames.append(pose)
    return {'name': db.clip_name(anim), 'fps': fps, 'additive': additive,
            'rig': (rig or {}).get('__name', ''), 'frames': frames}


# ---------------- GenericData v2 (Battle for Neighborville) ----------------
PRIM2 = {0x01: '?', 0x02: 'b', 0x03: 'B', 0x04: 'h', 0x05: 'H', 0x06: 'i', 0x07: 'I', 0x08: 'q', 0x09: 'Q',
         0x0A: 'f', 0x13: 'd'}
PRIM2_SIZE = {0x01: 1, 0x02: 1, 0x03: 1, 0x04: 2, 0x05: 2, 0x06: 4, 0x07: 4, 0x08: 8, 0x09: 8, 0x0A: 4, 0x0B: 8,
              0x0C: 12, 0x0D: 16, 0x0E: 16, 0x0F: 64, 0x11: 8, 0x12: 8, 0x13: 8, 0x23: 8}
VECTORS = {0x0B: 2, 0x0C: 3, 0x0D: 4, 0x0E: 4, 0x0F: 16}


def _ref(data, pos, e):
    """A self-relative 60-bit reference stored at pos, as an absolute offset (0 for none)."""
    val = struct.unpack_from(e + 'q', data, pos)[0]
    if val in (0, -1):
        return 0
    val = ((val << 4) & 0xFFFFFFFFFFFFFFFF)
    val = val - (1 << 64) if val >= (1 << 63) else val
    return pos + (val >> 4)


class BankV2:
    def __init__(self, data, e):
        self.data, self.e = data, e
        self.classes = {}
        self.roots = []
        self.cache = {}

    def read_ref2(self, at):
        d, e = self.data, self.e
        size = struct.unpack_from(e + 'I', d, at + 8)[0]
        count = struct.unpack_from(e + 'Q', d, at + 12)[0]
        offsets = [_ref(d, at + 20 + 8 * i, e) for i in range(count)]
        raw = {}
        for off in offsets:
            if not off:
                continue
            lo, hi, dsize, align, strings, _ = struct.unpack_from(e + 'iiIIII', d, off)
            reordered, pod = d[off + 24], d[off + 25]
            hash_ = struct.unpack_from(e + 'I', d, off + 28)[0]
            entries, p = [], off + 32
            for _ in range(hi - lo + 1):
                lh, esize, foff, name, n, flags, ealign, rle = struct.unpack_from(e + 'IIIiHHHh', d, p)
                lref = _ref(d, p + 24, e)
                entries.append([lh, esize, foff, name, n, flags, ealign, lref])
                p += 32
            table = d[p:p + strings]

            def text(i):
                if i < 0 or i >= len(table):
                    return 'unknown'
                end = table.find(b'\0', i)
                return table[i:end if end >= 0 else len(table)].decode('ascii', 'replace')
            raw[off] = {'name': text(1) if strings else 'Class_%08x' % hash_, 'size': dsize, 'align': max(align, 1),
                        'pod': bool(pod), 'hash': hash_, 'reordered': bool(reordered),
                        'entries': [(text(en[3]),) + tuple(en) for en in entries]}
        for off, r in raw.items():
            fields = []
            for name, lh, esize, foff, _, n, flags, ealign, lref in r['entries']:
                if lh in (0, 0xFFFFFFFF):
                    continue
                if lh in PRIM2_SIZE:
                    type_name, native = None, True
                elif lref in raw:
                    type_name, native = raw[lref]['name'], raw[lref]['pod']
                else:
                    type_name, native = 'Class_%08x' % lh, False
                fields.append({'name': name, 'hash': lh, 'offset': foff, 'size': esize, 'align': ealign,
                               'list': bool(flags & 1), 'count': n, 'native': native, 'type': type_name})
            if r['reordered']:
                fields.sort(key=lambda f: f['offset'])
            self.classes[r['hash']] = {'name': r['name'], 'size': r['size'], 'align': r['align'], 'fields': fields}
        for cls in self.classes.values():
            for f in cls['fields']:
                if not f['size'] and f['hash'] in self.classes:
                    f['size'], f['align'] = self.classes[f['hash']]['size'], self.classes[f['hash']]['align']
                if f['list'] and not f['size']:
                    f['size'], f['align'] = 16, 8
        return at + size

    def read_dat2(self, at):
        size = struct.unpack_from(self.e + 'I', self.data, at + 8)[0]
        self.roots.append(at + 28)
        return at + size

    # -- values
    def object_at(self, header, depth=0):
        if header in self.cache:
            return self.cache[header]
        if depth > 48 or header + 16 > len(self.data):
            return None
        type_hash = struct.unpack_from(self.e + 'Q', self.data, header)[0] & 0xffffffff
        cls = self.classes.get(type_hash)
        if cls is None:
            return None
        self.cache[header] = out = {'__type': cls['name']}
        out.update(self.values(header + 16, cls, depth + 1))
        return out

    def values(self, at, cls, depth):
        out = {}
        for f in cls['fields']:
            pos = at + f['offset']
            if f['list'] or f['hash'] == 0x11:
                out[f['name']] = self.heap(pos, f, depth)
            elif f['count'] > 1:
                out[f['name']] = self.array(pos, f, f['count'], depth)
            else:
                out[f['name']] = self.primitive(pos, f, depth)
        return out

    def heap(self, pos, f, depth):
        d, e = self.data, self.e
        count = struct.unpack_from(e + 'i', d, pos + 4)[0]
        target = _ref(d, pos + 8, e)
        if not target or count <= 0:
            return '' if f['hash'] == 0x11 else []
        if f['hash'] == 0x11:
            end = d.find(b'\0', target)
            return d[target:end].decode('utf-8', 'replace')
        element = dict(f)
        if f['hash'] in self.classes:
            element['size'], element['align'] = self.classes[f['hash']]['size'], self.classes[f['hash']]['align']
        elif f['hash'] in PRIM2_SIZE:
            element['size'] = PRIM2_SIZE[f['hash']]
            element['align'] = min(element['size'], 8)
        return self.array(target, element, count, depth)

    def array(self, pos, f, count, depth):
        if count <= 0 or count > 5000000:
            return []
        size, align = f['size'] or 1, f['align'] or 1
        stride = (size + align - 1) // align * align
        h = f['hash']
        if h in PRIM2 and stride == PRIM2_SIZE[h]:
            return list(struct.unpack_from(self.e + PRIM2[h] * count, self.data, pos))
        reference = size == 8 and align == 8 and h not in PRIM2_SIZE and h != 0
        out = []
        for i in range(count):
            p = pos + i * stride
            if reference:
                target = _ref(self.data, p, self.e)
                out.append(self.object_at(target + 16, depth + 1) if target else None)
            else:
                out.append(self.primitive(p, f, depth))
        return out

    def primitive(self, pos, f, depth):
        d, e, h = self.data, self.e, f['hash']
        if h in PRIM2:
            return struct.unpack_from(e + PRIM2[h], d, pos)[0]
        if h in VECTORS:
            return list(struct.unpack_from(e + 'f' * VECTORS[h], d, pos))
        if h == 0x23:
            return '%016X' % struct.unpack_from(e + 'Q', d, pos)[0]
        if h == 0x12:
            target = _ref(d, pos, e)
            return self.object_at(target + 16, depth + 1) if target else None
        if f['type'] == 'Guid':
            return guid(d[pos:pos + 16], e == '>')
        if f['size'] == 8 and f['align'] == 8 and not f['native']:
            target = _ref(d, pos, e)
            return self.object_at(target + 16, depth + 1) if target else None
        cls = self.classes.get(h)
        return self.values(pos, cls, depth + 1) if cls else None

    def objects_of(self):
        for header in self.roots:
            o = self.object_at(header)
            if o is None:
                continue
            o = dict(o)
            o.setdefault('__guid', o.get('__key'))
            yield o


def read_bank_v2(data, at):
    bank = None
    while True:
        at = data.find(GD, at)
        if at < 0 or at + 12 > len(data):
            break
        tag, endian = data[at:at + 7], data[at + 7:at + 8]
        e = '>' if endian == b'b' else '<'
        size = struct.unpack_from(e + 'I', data, at + 8)[0]
        if bank is None:
            bank = BankV2(data, e)
        if tag == b'GD.REF2':
            bank.read_ref2(at)
        elif tag == b'GD.DAT2':
            bank.read_dat2(at)
        at += 16 if tag == b'GD.STRM' else max(size, 12)
    return bank
