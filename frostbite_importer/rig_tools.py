"""Control rig for imported Frostbite characters, built on top of the game skeleton.

The game's bones are never moved, renamed or re-parented, so the games' animation clips keep importing onto
them. Every game bone gets a control copy (CTRL_<name>) with the same rest pose, a shape and a side colour;
the game bone follows its control through a Copy Transforms constraint whose influence is the armature's
"Controls" value: 0 plays game animation as imported, 1 lets the controls drive the body.

Chains named like Spine1..8, LeftArm1..7 or FrontLeftLeg_leaf1..7 get an IK target with an FK/IK slider each.
Eyes get one aim target. "Controls from animation" copies an imported clip onto the controls (for editing),
"Bake to game skeleton" bakes the controls back into a plain game-skeleton action.
"""
import math
import re

import bpy
from mathutils import Matrix, Vector

PREFIX, IK_PREFIX = 'CTRL_', 'IK_'
CONTROL_PROP = 'rig_controls'
HELPER = re.compile(r'(_Offset$|^Offset_|_Prop\d*$|^Wep_|^Camera|Trajectory$|^Connect$|^Helmet$)', re.I)
FACE = re.compile(r'eye|brow|lid|mouth|cheek|jaw|lip|ear|nose|hair|tongue', re.I)
IK_CHAIN = re.compile(r'(spine|neck|arm|leg|tail|tentacle|vine)', re.I)
SKIP_IK = re.compile(r'(hair|eyebrow|pod|mouth|finger|toe|cheek|neck_?leaf|petal|whisker)', re.I)
WIDGETS = 'Frostbite rig shapes'


def is_control(bone):
    return bone.name.startswith((PREFIX, IK_PREFIX)) or bool(bone.get('frostbite_control'))


def game_bones(armature):
    return [b for b in armature.data.bones if not is_control(b)]


# ---------------------------------------------------------------- shapes
def _widget(name, verts, edges):
    ob = bpy.data.objects.get(name)
    if ob:
        return ob
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, edges, [])
    ob = bpy.data.objects.new(name, me)
    col = bpy.data.collections.get(WIDGETS)
    if col is None:
        col = bpy.data.collections.new(WIDGETS)
        bpy.context.scene.collection.children.link(col)
        col.hide_viewport = col.hide_render = True
    col.objects.link(ob)
    return ob


def _circle(n=24, radius=1.0, axis='Y', y=0.0):
    verts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        c, s = math.cos(a) * radius, math.sin(a) * radius
        verts.append({'Y': (c, y, s), 'Z': (c, s, y), 'X': (y, c, s)}[axis])
    return verts, [(i, (i + 1) % n) for i in range(n)]


def widgets():
    ring_v, ring_e = _circle(axis='Y', y=0.5)
    cube_v = [(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
    cube_e = [(a, b) for a in range(8) for b in range(a + 1, 8) if bin(a ^ b).count('1') == 1]
    sv, se = [], []
    for axis in 'XYZ':
        v, e = _circle(16, 1.0, axis)
        se += [(a + len(sv), b + len(sv)) for a, b in e]
        sv += v
    root_v, root_e = _circle(40, 1.0, 'Z')
    arrow = [(0, 1.25, 0), (-0.15, 1.05, 0), (0.15, 1.05, 0)]
    root_e += [(len(root_v), len(root_v) + 1), (len(root_v), len(root_v) + 2)]
    root_v += arrow
    return {'ring': _widget('WGT_ring', ring_v, ring_e), 'cube': _widget('WGT_cube', cube_v, cube_e),
            'sphere': _widget('WGT_sphere', sv, se), 'root': _widget('WGT_root', root_v, root_e)}


# ---------------------------------------------------------------- chains
def chains(bones):
    """Linear runs of numbered bones (Spine1, Spine2, ...) where each is the previous one's child."""
    groups = {}
    for b in bones:
        m = re.match(r'^(.*?)(\d+)$', b.name)
        if m:
            groups.setdefault(m.group(1), []).append((int(m.group(2)), b))
    by_name = {b.name: b for b in bones}
    out = {}
    for base, members in groups.items():
        members.sort(key=lambda x: x[0])
        run = [members[0][1]]
        plain = by_name.get(base)  # Spine, Spine1, Spine2: the first one has no number
        if plain is not None and run[0].parent == plain:
            run.insert(0, plain)
        for _, b in members[1:]:
            if b.parent == run[-1]:
                run.append(b)
            else:
                break
        if len(run) >= 3:
            out[base.rstrip('_')] = [b.name for b in run]
    # Humanoid limbs: upper and lower bone by name (LeftArm > LeftForeArm, LeftUpLeg > LeftLeg).
    for side in ('Left', 'Right', 'L_', 'R_'):
        for upper, lower, key in (('Arm', 'ForeArm', 'Arm'), ('UpperArm', 'LowerArm', 'Arm'), ('UpperArm', 'ForeArm', 'Arm'),
                                  ('Shoulder', 'Elbow', 'Arm'),
                                  ('UpLeg', 'Leg', 'Leg'), ('Thigh', 'Calf', 'Leg'), ('Thigh', 'Shin', 'Leg')):
            a, b = by_name.get(side + upper), by_name.get(side + lower)
            if a is not None and b is not None and b.parent == a and (side + key) not in out:
                out[side + key] = [a.name, b.name]
    return out


def _side(name):
    low = name.lower()
    if low.startswith('left') or re.search(r'(^|_)l(eft)?_', low) or low.endswith('_l'):
        return 'left'
    if low.startswith('right') or re.search(r'(^|_)r(ight)?_', low) or low.endswith('_r'):
        return 'right'
    return 'center'


def _driver(target, path, armature, prop, index=-1):
    fcurve = target.driver_add(path, index) if index >= 0 else target.driver_add(path)
    driver = fcurve.driver
    driver.type = 'AVERAGE'
    for v in list(driver.variables):
        driver.variables.remove(v)
    var = driver.variables.new()
    var.name = 'v'
    var.type = 'SINGLE_PROP'
    var.targets[0].id_type = 'OBJECT'
    var.targets[0].id = armature
    var.targets[0].data_path = '["%s"]' % prop
    return fcurve


def _prop(armature, name, value, description):
    armature[name] = value
    ui = armature.id_properties_ui(name)
    ui.update(min=0.0, max=1.0, soft_min=0.0, soft_max=1.0, description=description)


COLOURS = {'left': 'THEME04', 'right': 'THEME01', 'center': 'THEME09'}


def _create_controls(eb, names, master):
    """Edit mode: a CTRL_ copy of each game bone, parented like the game bone (to CTRL_Master at the top)."""
    for name in names:
        src = eb[name]
        ctrl = eb.new(PREFIX + name)
        ctrl.head, ctrl.tail, ctrl.roll = src.head.copy(), src.tail.copy(), src.roll
        ctrl.use_deform = False
    for name in names:
        src, ctrl = eb[name], eb[PREFIX + name]
        parent = eb.get(PREFIX + src.parent.name) if src.parent else None
        ctrl.parent = parent or master
        ctrl.use_connect = False


def _collections(data):
    def collection(name, visible=True, parent=None):
        col = data.collections_all.get(name) or data.collections.new(name, parent=parent)  # nested ones too
        col.is_visible = visible
        return col
    controls = collection('Controls')
    groups = {g: collection(g, parent=controls) for g in ('Body', 'Arms', 'Legs', 'Face', 'IK')}
    return groups, collection('Helpers', False), collection('Game skeleton', False)


def _setup_controls(armature, names, height, shapes):
    """Pose mode: shape, colour and group for each new control, and the game bone following it."""
    data = armature.data
    groups, helpers, game = _collections(data)
    for name in names:
        game.assign(data.bones[name])
        ctrl = data.bones[PREFIX + name]
        ctrl['frostbite_control'] = 1
        pb = armature.pose.bones[PREFIX + name]
        pb.rotation_mode = 'QUATERNION'
        low = name.lower()
        if HELPER.search(name) or name in ('Reference',):
            helpers.assign(ctrl)
        elif FACE.search(name):
            groups['Face'].assign(ctrl)
        elif re.search(r'arm|hand|finger|pod|clav|shoulder|thumb', low):
            groups['Arms'].assign(ctrl)
        elif re.search(r'leg|foot|toe|thigh|calf|knee|root', low):
            groups['Legs'].assign(ctrl)
        else:
            groups['Body'].assign(ctrl)
        length = max(data.bones[name].length, height * 0.02)
        pb.custom_shape = shapes['ring']
        pb.use_custom_shape_bone_size = False
        # Ring sizes: small on the face and along chains, bigger for the body's main bones.
        if FACE.search(name):
            scale = height * 0.02
        elif low in ('hips', 'pelvis', 'cog', 'root'):
            scale = height * 0.16
        elif low in ('head', 'neck', 'chest'):
            scale = height * 0.1
        else:
            scale = min(max(length * 0.45, height * 0.025), height * 0.07)
        pb.custom_shape_scale_xyz = (scale, length, scale)
        pb.color.palette = COLOURS[_side(name)]
        ctrl.color.palette = COLOURS[_side(name)]
        constraint = armature.pose.bones[name].constraints.new('COPY_TRANSFORMS')
        constraint.name = 'Frostbite control'
        constraint.target, constraint.subtarget = armature, PREFIX + name
        _driver(constraint, 'influence', armature, CONTROL_PROP)


def extend(context, armature):
    """Controls for game bones added after the rig was built (a hat, prop or weapon imported onto the character
    brings its own bones). Returns how many were added."""
    have = {b.name for b in armature.data.bones}
    missing = [b.name for b in game_bones(armature) if PREFIX + b.name not in have]
    if not missing:
        return 0
    order = []
    def visit(name):
        bone = armature.data.bones[name]
        if bone.parent and bone.parent.name in missing and bone.parent.name not in order:
            visit(bone.parent.name)
        if name not in order:
            order.append(name)
    for name in missing:
        visit(name)
    height = armature.get('frostbite_rig_height') or max((b.head_local.z for b in game_bones(armature)), default=1.0) or 1.0
    shapes = widgets()
    mode = armature.mode
    context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode='EDIT')
    eb = armature.data.edit_bones
    _create_controls(eb, order, eb.get('CTRL_Master'))
    bpy.ops.object.mode_set(mode='POSE')
    _setup_controls(armature, order, height, shapes)
    bpy.ops.object.mode_set(mode='OBJECT' if mode not in ('POSE',) else 'POSE')
    return len(order)


# ---------------------------------------------------------------- build
def build(context, armature):
    """Add the control rig to `armature` (once). Returns a short report."""
    if any(is_control(b) for b in armature.data.bones):
        raise ValueError('%s already has a control rig' % armature.name)
    deform_names = {vg for ob in armature.children if ob.type == 'MESH' for vg in ob.vertex_groups.keys()}
    names = [b.name for b in game_bones(armature)]
    found = {k: v for k, v in chains([armature.data.bones[n] for n in names]).items()}
    def deforms(chain):  # the chain's own bones, or the twist bones they carry (BFN arms), move the mesh
        return any(n in deform_names or any(c.name in deform_names for c in armature.data.bones[n].children)
                   for n in chain)
    ik = {base: chain for base, chain in found.items() if IK_CHAIN.search(base) and not SKIP_IK.search(base)
          and deforms(chain)}
    height = max((armature.data.bones[n].head_local.z for n in names), default=1.0) or 1.0
    shapes = widgets()

    context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode='EDIT')
    eb = armature.data.edit_bones
    master = eb.new('CTRL_Master')
    master.head, master.tail, master.roll = Vector((0, 0, 0)), Vector((0, height * 0.25, 0)), 0
    _create_controls(eb, names, master)
    for base, chain in ik.items():
        last = eb[chain[-1]]
        target = eb.new(IK_PREFIX + base)
        direction = (last.tail - last.head)
        size = max(direction.length, height * 0.04)
        target.head = last.tail.copy()
        target.tail = last.tail + direction.normalized() * size
        target.roll = last.roll
        target.parent = master
        target.use_deform = False
    eyes = [n for n in names if re.search(r'eye$', n, re.I) and not re.search(r'lid|brow|offset', n, re.I)]
    eye_target = None
    if eyes:
        heads = [eb[n].head for n in eyes]
        mid = sum(heads, Vector()) / len(heads)
        reach = max(height * 0.6, 0.3)
        eye_target = eb.new('CTRL_EyeTarget')
        eye_target.head = mid + Vector((0, -reach, 0))  # Frostbite characters face -Y in Blender
        eye_target.tail = eye_target.head + Vector((0, 0, max(height * 0.05, 0.03)))
        eye_target.parent = eb.get(PREFIX + 'Head') or master
        eye_target.use_deform = False
        for n in eyes:
            t = eb.new('CTRL_%s_Aim' % n)
            t.head = eye_target.head + Vector((eb[n].head.x - mid.x, 0, eb[n].head.z - mid.z))
            t.tail = t.head + Vector((0, 0, max(height * 0.03, 0.02)))
            t.parent = eye_target
            t.use_deform = False
    eye_axes = {}
    for n in eyes:  # which local axis of the eye looks forward (-Y)
        m = eb[n].matrix.to_3x3()
        best = max(((abs(m.col[i].dot(Vector((0, -1, 0)))), i, m.col[i].dot(Vector((0, -1, 0)))) for i in range(3)))
        eye_axes[n] = ('TRACK_' if best[2] > 0 else 'TRACK_NEGATIVE_') + 'XYZ'[best[1]]
    bpy.ops.object.mode_set(mode='POSE')

    data = armature.data
    groups, helpers, game = _collections(data)
    colours = COLOURS
    _prop(armature, CONTROL_PROP, 1.0, 'Off (0): game animation plays as imported. On (1): the controls drive the body')
    armature['frostbite_rig_height'] = height
    _setup_controls(armature, names, height, shapes)
    master_pb = armature.pose.bones['CTRL_Master']
    master_pb.custom_shape = shapes['root']
    master_pb.use_custom_shape_bone_size = False
    master_pb.custom_shape_scale_xyz = (height * 0.45,) * 3
    master_pb.color.palette = 'THEME09'
    data.bones['CTRL_Master']['frostbite_control'] = 1
    groups['Body'].assign(data.bones['CTRL_Master'])

    for base, chain in ik.items():
        prop = 'ik_' + re.sub(r'\W+', '_', base).lower()
        _prop(armature, prop, 0.0, 'FK (0) or IK (1) for %s' % base)
        target = armature.pose.bones[IK_PREFIX + base]
        data.bones[IK_PREFIX + base]['frostbite_control'] = 1
        groups['IK'].assign(data.bones[IK_PREFIX + base])
        target.custom_shape = shapes['cube']
        target.use_custom_shape_bone_size = False
        target.custom_shape_scale_xyz = (height * 0.04,) * 3
        target.color.palette = COLOURS[_side(base)]
        target.rotation_mode = 'QUATERNION'
        constraint = armature.pose.bones[PREFIX + chain[-1]].constraints.new('IK')
        constraint.name = 'Frostbite IK'
        constraint.target, constraint.subtarget = armature, IK_PREFIX + base
        constraint.chain_count = len(chain)
        constraint.use_stretch = False
        _driver(constraint, 'influence', armature, prop)
        # The bone after the chain (a hand, a head) turns with the IK target while in IK.
        after = [c.name for c in data.bones[chain[-1]].children if not HELPER.search(c.name)]
        if after:
            rot = armature.pose.bones[PREFIX + after[0]].constraints.new('COPY_ROTATION')
            rot.name = 'Frostbite IK rotation'
            rot.target, rot.subtarget = armature, IK_PREFIX + base
            _driver(rot, 'influence', armature, prop)
    if eye_target is not None:
        _prop(armature, 'eye_aim', 0.0, 'How much the eyes follow CTRL_EyeTarget')
        for bone_name in ['CTRL_EyeTarget'] + ['CTRL_%s_Aim' % n for n in eyes]:
            data.bones[bone_name]['frostbite_control'] = 1
            groups['Face'].assign(data.bones[bone_name])
            pb = armature.pose.bones[bone_name]
            pb.custom_shape = shapes['sphere']
            pb.use_custom_shape_bone_size = False
            pb.custom_shape_scale_xyz = (height * (0.05 if bone_name == 'CTRL_EyeTarget' else 0.025),) * 3
            pb.color.palette = 'THEME09' if bone_name == 'CTRL_EyeTarget' else COLOURS[_side(bone_name[5:])]
        for n in eyes:
            track = armature.pose.bones[PREFIX + n].constraints.new('DAMPED_TRACK')
            track.name = 'Frostbite eye aim'
            track.target, track.subtarget = armature, 'CTRL_%s_Aim' % n
            track.track_axis = eye_axes[n]
            _driver(track, 'influence', armature, 'eye_aim')
    data.display_type = 'OCTAHEDRAL'
    armature.show_in_front = True
    armature['frostbite_control_rig'] = 1
    bpy.ops.object.mode_set(mode='OBJECT')
    return '%d controls, IK on %s%s' % (len(names), ', '.join(sorted(ik)) or 'no chains',
                                        ', eye aim' if eyes else '')


def controls_from_animation(armature):
    """Copy the game bones' keys in the current action onto their controls (and turn the controls on, in FK)."""
    action = armature.animation_data.action if armature.animation_data else None
    if action is None:
        raise ValueError('Import a game clip onto this armature first')
    # Read everything first as plain data: adding F-curves can move Blender's storage and leave references to
    # the old ones dangling (a crash on bigger characters).
    sources = []
    for fc in _fcurves(action):
        m = re.match(r'pose\.bones\["(.+?)"\]\.(.+)$', fc.data_path)
        if not m or m.group(1).startswith((PREFIX, IK_PREFIX)) or PREFIX + m.group(1) not in armature.pose.bones:
            continue
        keys = [(tuple(k.co), k.interpolation, tuple(k.handle_left), tuple(k.handle_right)) for k in fc.keyframe_points]
        sources.append((m.group(1), m.group(2), fc.array_index, keys))
    targets = {('pose.bones["%s%s"].%s' % (PREFIX, bone, prop), index) for bone, prop, index, _ in sources}
    while True:  # drop earlier copies, looking each one up afresh
        old = next(((f.data_path, f.array_index) for f in _fcurves(action) if (f.data_path, f.array_index) in targets), None)
        if old is None:
            break
        _remove(action, _find(action, *old))
    for bone, prop, index, keys in sources:
        new = _new_fcurve(action, 'pose.bones["%s%s"].%s' % (PREFIX, bone, prop), index, PREFIX + bone)
        new.keyframe_points.add(len(keys))
        for point, (co, interpolation, left, right) in zip(new.keyframe_points, keys):
            point.co, point.interpolation, point.handle_left, point.handle_right = co, interpolation, left, right
        new.update()
    copied = len(sources)
    for key in list(armature.keys()):
        if key.startswith('ik_'):
            armature[key] = 0.0
    armature[CONTROL_PROP] = 1.0
    return copied


def _fcurves(action):
    """F-curves of an action on Blender 4.4+ layered actions and on older flat ones."""
    if getattr(action, 'layers', None):
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    yield from bag.fcurves
    else:
        yield from action.fcurves


def _find(action, path, index):
    return next((f for f in _fcurves(action) if f.data_path == path and f.array_index == index), None)


def _remove(action, fcurve):
    if getattr(action, 'layers', None):
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    if fcurve in bag.fcurves[:]:
                        bag.fcurves.remove(fcurve)
                        return
    else:
        action.fcurves.remove(fcurve)


def _new_fcurve(action, path, index, group):
    if hasattr(action, 'fcurve_ensure_for_datablock'):
        return action.fcurve_ensure_for_datablock(bpy.context.object, path, index=index, group_name=group)
    return action.fcurves.new(path, index=index, action_group=group)


def bake_to_game(context, armature):
    """Bake what the controls do into a new action on the game bones only, then play it with controls off."""
    action = armature.animation_data.action if armature.animation_data else None
    start, end = (int(action.frame_range[0]), int(action.frame_range[1])) if action else \
        (context.scene.frame_start, context.scene.frame_end)
    context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode='POSE')
    for pb in armature.pose.bones:
        chosen = not is_control(pb.bone)
        if hasattr(pb, 'select'):  # Blender 5: selection lives on the pose bone
            pb.select = chosen
        else:
            pb.bone.select = chosen
    bpy.ops.nla.bake(frame_start=start, frame_end=end, only_selected=True, visual_keying=True,
                     clear_constraints=False, use_current_action=False, bake_types={'POSE'})
    baked = armature.animation_data.action
    baked.name = ((action.name if action else 'Frostbite') + ' (baked)')[:60]
    # Keep only the game bones' channels: that is a plain clip of the game skeleton.
    for fc in list(_fcurves(baked)):
        if fc.data_path.startswith(('pose.bones["%s' % PREFIX, 'pose.bones["%s' % IK_PREFIX)):
            _remove(baked, fc)
    armature[CONTROL_PROP] = 0.0
    bpy.ops.object.mode_set(mode='OBJECT')
    return baked


# ---------------------------------------------------------------- operators and panel
def _armature(context):
    """The selected character's armature: the active armature, or the one a selected mesh is rigged to."""
    ob = context.active_object
    if ob is None:
        return None
    if ob.type == 'ARMATURE':
        return ob
    if ob.type == 'MESH':
        if ob.parent and ob.parent.type == 'ARMATURE':
            return ob.parent
        for m in ob.modifiers:
            if m.type == 'ARMATURE' and m.object:
                return m.object
    return None


class FROSTBITE_OT_make_control_rig(bpy.types.Operator):
    bl_idname = 'frostbite.make_control_rig'
    bl_label = 'Make control rig'
    bl_description = ('Add animator controls (FK, IK, eye aim) on top of the selected character, or controls for '
                      'bones added since (hats, props). The game bones are kept as they are, so game animations '
                      'still import onto it')
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _armature(context) is not None

    def execute(self, context):
        armature = _armature(context)
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        context.view_layer.objects.active = armature
        try:
            if armature.get('frostbite_control_rig'):
                added = extend(context, armature)
                report = ('%d new controls for added bones' % added) if added else 'every bone already has a control'
            else:
                report = build(context, armature)
        except (ValueError, RuntimeError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Control rig: ' + report)
        return {'FINISHED'}


class FROSTBITE_OT_controls_from_animation(bpy.types.Operator):
    bl_idname = 'frostbite.controls_from_animation'
    bl_label = 'Controls from animation'
    bl_description = 'Copy the imported game clip onto the controls so you can edit it with them'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ob = _armature(context)
        return ob is not None and ob.get('frostbite_control_rig')

    def execute(self, context):
        try:
            copied = controls_from_animation(_armature(context))
        except ValueError as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Copied %d channels onto the controls' % copied)
        return {'FINISHED'}


class FROSTBITE_OT_bake_to_game(bpy.types.Operator):
    bl_idname = 'frostbite.bake_to_game'
    bl_label = 'Bake to game skeleton'
    bl_description = 'Bake what the controls do into a new action on the game bones only (a plain game clip)'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ob = _armature(context)
        return ob is not None and ob.get('frostbite_control_rig')

    def execute(self, context):
        baked = bake_to_game(context, _armature(context))
        self.report({'INFO'}, 'Baked to %s' % baked.name)
        return {'FINISHED'}


def draw(layout, context):
    """The Control rig box (drawn in the Animations tab)."""
    box = layout.box()
    box.label(text='Control rig', icon='OUTLINER_OB_ARMATURE')
    ob = _armature(context)
    if ob is None:
        box.label(text='Select a rigged character')
        return
    if not ob.get('frostbite_control_rig'):
        box.operator('frostbite.make_control_rig', icon='BONE_DATA')
        return
    have = {b.name for b in ob.data.bones}
    missing = sum(1 for b in game_bones(ob) if PREFIX + b.name not in have)
    if missing:
        box.operator('frostbite.make_control_rig', text='Add controls for %d new bones' % missing, icon='BONE_DATA')
    box.prop(ob, '["%s"]' % CONTROL_PROP, text='Controls (0 = game animation)', slider=True)
    for key in sorted(k for k in ob.keys() if k.startswith('ik_')):
        box.prop(ob, '["%s"]' % key, text='IK ' + key[3:].replace('_', ' ').title(), slider=True)
    if 'eye_aim' in ob.keys():
        box.prop(ob, '["eye_aim"]', text='Eye aim', slider=True)
    row = box.row(align=True)
    row.operator('frostbite.controls_from_animation', icon='ACTION_TWEAK')
    row.operator('frostbite.bake_to_game', icon='REC')


classes = (FROSTBITE_OT_make_control_rig, FROSTBITE_OT_controls_from_animation, FROSTBITE_OT_bake_to_game)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
