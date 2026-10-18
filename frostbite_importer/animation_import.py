"""Frostbite ANT clips onto an armature built by the rigged cosmetics import.

A clip gives, per frame, parent-local bone transforms in Frostbite space: rotation (x, y, z, w),
translation and scale. Bones it leaves out keep their rest pose. Each frame is composed down the
hierarchy in Frostbite space, moved to armature space with the same axis conversions the rig
was built with, and stored as pose keys of one Action.
"""
import json
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector

from .scene_import import AXIS4, BONE_AXES

AXIS4_INV = AXIS4.inverted()
BONE_AXES_INV = BONE_AXES.inverted()


def _order(bones):
    """Bones with every parent before its children."""
    done, out = set(), []

    def visit(b):
        if b.name in done:
            return
        if b.parent:
            visit(b.parent)
        done.add(b.name)
        out.append(b)
    for b in bones:
        visit(b)
    return out


def apply_clip(context, armature, clip, set_fps=True):
    """Create an Action on `armature` from a decoded clip dict; returns (action, matched bone count)."""
    bones = _order(armature.data.bones)
    by_lower = {b.name.lower(): b.name for b in bones}
    rest = {b.name: b.matrix_local.copy() for b in bones}
    # Rest pose in Frostbite space, and each bone's rest transform relative to its parent.
    fb_rest = {n: AXIS4_INV @ m @ BONE_AXES_INV for n, m in rest.items()}
    rest_local = {}
    for b in bones:
        local = fb_rest[b.parent.name].inverted() @ fb_rest[b.name] if b.parent else fb_rest[b.name]
        t, q, _ = local.decompose()
        rest_local[b.name] = (t, q)
    frames = clip['frames']
    matched = {by_lower[k.lower()] for f in frames[:1] for k in f if k.lower() in by_lower}
    if not matched:
        raise ValueError('None of the clip\'s bones are on %s; import the matching character first' % armature.name)
    additive = clip.get('additive')

    action = bpy.data.actions.new(clip.get('name') or 'Frostbite clip')
    armature.animation_data_create()
    armature.animation_data.action = action
    for pb in armature.pose.bones:
        pb.rotation_mode = 'QUATERNION'
    scene = context.scene
    fps = float(clip.get('fps') or 30.0)
    if set_fps:
        scene.render.fps, scene.render.fps_base = max(1, round(fps)), 1.0
    for index, frame in enumerate(frames):
        pose = {by_lower[k.lower()]: v for k, v in frame.items() if k.lower() in by_lower}
        world = {}
        for b in bones:
            t_rest, q_rest = rest_local[b.name]
            channels = pose.get(b.name, {})
            t, q, s = t_rest, q_rest, Vector((1.0, 1.0, 1.0))
            if 'r' in channels:
                x, y, z, w = channels['r']
                q = q_rest @ Quaternion((w, x, y, z)) if additive else Quaternion((w, x, y, z))
            if 't' in channels:
                # Translation is an authored animation channel, including root motion
                # and moving attachment bones. Magnitude is not a retargeting rule.
                t = t_rest + Vector(channels['t']) if additive else Vector(channels['t'])
            if 's' in channels and not additive:
                s = Vector(channels['s'])
            local = Matrix.Translation(t) @ q.to_matrix().to_4x4() @ Matrix.Diagonal(s.to_4d())
            world[b.name] = world[b.parent.name] @ local if b.parent else local
        for b in bones:
            target = AXIS4 @ world[b.name] @ BONE_AXES
            if b.parent:
                parent_target = AXIS4 @ world[b.parent.name] @ BONE_AXES
                basis = (rest[b.parent.name].inverted() @ rest[b.name]).inverted() @ (parent_target.inverted() @ target)
            else:
                basis = rest[b.name].inverted() @ target
            pb = armature.pose.bones[b.name]
            loc, rot, scl = basis.decompose()
            pb.location, pb.rotation_quaternion, pb.scale = loc, rot, scl
            for path in ('location', 'rotation_quaternion', 'scale'):
                pb.keyframe_insert(path, frame=index + 1)
    scene.frame_start, scene.frame_end = 1, max(1, len(frames))
    action['frostbite_clip'] = clip.get('name', '')
    action['frostbite_fps'] = fps
    return action, len(matched)


def load_clip(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))
