"""Frostbite ANT clips onto an armature built by the rigged cosmetics import.

A clip gives, per frame, parent-local bone transforms in Frostbite space: rotation (x, y, z, w),
translation and scale. Bones it leaves out keep their rest pose. Each frame is composed down the
hierarchy in Frostbite space, moved to armature space with the same axis conversions the rig
was built with, and stored as pose keys of one Action.

A facial clip (face_only) keys just the face bones. On a character already playing a body clip, its
keys are also written into that body Action, looping, so face and body play together.
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
    # Only the game skeleton: a control rig's bones (CTRL_, IK_) are animated through the rig, not by clips.
    bones = _order([b for b in armature.data.bones
                    if not (b.name.startswith(('CTRL_', 'IK_')) or b.get('frostbite_control'))])
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
    # A facial clip keys only the face bones it drives, so it can play over a body clip.
    face_only = bool(clip.get('face_only'))
    keyed = [b for b in bones if b.name in matched] if face_only else bones

    armature.animation_data_create()
    body = armature.animation_data.action if face_only else None
    action = bpy.data.actions.new(clip.get('name') or 'Frostbite clip')
    armature.animation_data.action = action
    for b in bones:
        armature.pose.bones[b.name].rotation_mode = 'QUATERNION'
    scene = context.scene
    fps = float(clip.get('fps') or 30.0)
    if set_fps and body is None:
        scene.render.fps, scene.render.fps_base = max(1, round(fps)), 1.0
    poses = []
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
        pose = {}
        for b in keyed:
            target = AXIS4 @ world[b.name] @ BONE_AXES
            if b.parent:
                parent_target = AXIS4 @ world[b.parent.name] @ BONE_AXES
                basis = (rest[b.parent.name].inverted() @ rest[b.name]).inverted() @ (parent_target.inverted() @ target)
            else:
                basis = rest[b.name].inverted() @ target
            pb = armature.pose.bones[b.name]
            pose[b.name] = loc, rot, scl = basis.decompose()
            pb.location, pb.rotation_quaternion, pb.scale = loc, rot, scl
            for path in ('location', 'rotation_quaternion', 'scale'):
                pb.keyframe_insert(path, frame=index + 1)
        poses.append(pose)
    action['frostbite_clip'] = clip.get('name', '')
    action['frostbite_fps'] = fps
    action['frostbite_face'] = int(face_only)
    if body is not None:
        # The character already plays a body clip: the face keys go into it too, looping over its length.
        action.use_fake_user = True  # kept on its own as well, for the NLA
        armature.animation_data.action = body
        _merge_face(armature, body, poses)
        action = body
    else:
        scene.frame_start, scene.frame_end = 1, max(1, len(frames))
    if armature.get('frostbite_control_rig'):
        # A pro-rigged character: the clip goes onto the controls too, so they move with it and drive the body
        # (identical result), ready to adjust. The game bones keep their keys for Bake to game skeleton.
        from . import rig_tools
        rig_tools.controls_from_animation(armature)
    return action, len(matched)


def _merge_face(armature, action, poses):
    """Replace the face bones' keys in `action` with `poses` (one {bone: (loc, rot, scale)} per frame), cycling."""
    from . import rig_tools
    paths = {'pose.bones["%s"].%s' % (name, prop) for name in poses[0] for prop in ('location', 'rotation_quaternion', 'scale')}
    while True:  # look each one up afresh: removing F-curves can move the others
        old = next(((f.data_path, f.array_index) for f in rig_tools._fcurves(action) if f.data_path in paths), None)
        if old is None:
            break
        rig_tools._remove(action, rig_tools._find(action, *old))
    for name in poses[0]:
        for slot, (prop, size) in enumerate((('location', 3), ('rotation_quaternion', 4), ('scale', 3))):
            for index in range(size):
                path = 'pose.bones["%s"].%s' % (name, prop)
                if hasattr(action, 'fcurve_ensure_for_datablock'):
                    fc = action.fcurve_ensure_for_datablock(armature, path, index=index, group_name=name)
                else:
                    fc = action.fcurves.new(path, index=index, action_group=name)
                fc.keyframe_points.add(len(poses))
                for frame, (point, pose) in enumerate(zip(fc.keyframe_points, poses)):
                    point.co = (frame + 1, pose[name][slot][index])
                    point.interpolation = 'LINEAR'
                fc.modifiers.new('CYCLES')
                fc.update()


def load_clip(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))
