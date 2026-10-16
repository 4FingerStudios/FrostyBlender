"""Bounded decoding of GW2 cooked static compound instance transforms."""
import struct
import numpy as np


def group_instances(data, metadata):
    if len(data) < 80 or len(metadata) != 16:
        raise ValueError('Truncated GW2 grouped physics resource')
    count = struct.unpack_from('<I', data, 52)[0]
    indices = struct.unpack_from('<Q', data, 56)[0]
    pack = struct.unpack('<4I', bytes(metadata))[0] & 0xffffff
    if not 0 < count <= 1000000 or indices + count * 2 > len(data) or pack + 272 > len(data):
        raise ValueError('Invalid GW2 grouped instance table')
    # The first packfile is the 32-bit cooked hknpStaticCompoundShape.
    # Its instance array contains one transform per physical model, even
    # when that model has several collision parts. The second packfile
    # mirrors it for the 64-bit runtime and must not be counted again.
    if struct.unpack_from('<4I', data, pack) != (0x57e0e057, 0x10c0c010, 0, 11):
        raise ValueError('Unsupported GW2 grouped Havok packfile')
    if data[pack + 208:pack + 216] != b'__data__':
        raise ValueError('Missing GW2 grouped Havok data section')
    section = pack + struct.unpack_from('<I', data, pack + 228)[0]
    start = section + 304
    if start + count * 128 > len(data):
        raise ValueError('Truncated GW2 grouped instance transforms')
    rows = np.ndarray((count, 5, 4), '<f4', buffer=data, offset=start, strides=(128, 16, 4)).astype(np.float64)
    rotation, position, scale = rows[:, :3, :3], rows[:, 3, :3], rows[:, 4, :3]
    # Fourth components contain packed shape indices, not matrix elements.
    if not np.isfinite(rows[:, :, :3]).all() or np.max(abs(rotation @ rotation.transpose(0, 2, 1) - np.eye(3))) > .001:
        raise ValueError('Invalid GW2 grouped instance rotation')
    if np.any(abs(np.linalg.det(rotation) - 1) > .001) or np.any(abs(scale) < 1e-8):
        raise ValueError('Invalid GW2 grouped instance scale')
    poses = np.tile(np.eye(4), (count, 1, 1))
    poses[:, :3, :3] = rotation.transpose(0, 2, 1) * scale[:, None, :]
    poses[:, :3, 3] = position
    bodies = np.frombuffer(data, '<u2', count=count, offset=indices).copy()
    return poses, bodies


def gw1_part_poses(data, metadata):
    """Garden Warfare 1 (Havok 2012, packfile version 9): the 32-bit hkpStaticCompoundShape's
    64-byte instances hold an hkQsTransform (translation, quaternion, scale) and, at byte 60,
    the physics part index the group's members address through PhysicsPartRange."""
    if len(metadata) != 16:
        raise ValueError('Truncated GW1 grouped physics resource')
    pack = struct.unpack('<4I', bytes(metadata))[0] & 0xffffff
    if pack + 64 > len(data) or struct.unpack_from('<4I', data, pack) != (0x57e0e057, 0x10c0c010, 0, 9):
        raise ValueError('Unsupported GW1 grouped Havok packfile')
    floats = np.frombuffer(data[:len(data) // 4 * 4], '<f4')

    def valid(at):
        with np.errstate(over='ignore', invalid='ignore'):
            return _valid(at)

    def _valid(at):
        t, q, s = floats[at // 4:at // 4 + 3], floats[at // 4 + 4:at // 4 + 8], floats[at // 4 + 8:at // 4 + 11]
        return (np.isfinite(floats[at // 4:at // 4 + 11]).all() and abs(float(q @ q) - 1) < 1e-3
                and np.all(abs(s) > 1e-6) and np.all(abs(t) < 1e6))
    # The longest run of such records is the instance array.
    best, at = (0, 0), pack
    while at + 64 <= len(data):
        if not valid(at):
            at += 16
            continue
        start = at
        while at + 64 <= len(data) and valid(at):
            at += 64
        best = max(best, ((at - start) // 64, start))
    count, start = best
    if count < 1:
        raise ValueError('No GW1 grouped instance transforms')
    poses = {}
    for i in range(count):
        at = start + i * 64
        x, y, z, w = (float(v) for v in floats[at // 4 + 4:at // 4 + 8])
        rotation = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                             [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                             [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
        pose = np.eye(4)
        pose[:3, :3] = rotation * floats[at // 4 + 8:at // 4 + 11].astype(np.float64)[None, :]
        pose[:3, 3] = floats[at // 4:at // 4 + 3]
        poses[struct.unpack_from('<H', data, at + 60)[0]] = pose
    return poses
