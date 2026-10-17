"""Frostbite particle effects as Geometry Nodes, from the effect JSON written by tools/effects.py.

Every emitter becomes a point cloud, one point per particle, holding the particle's spawn time, life,
start position, velocity, size, spin, flipbook frame and tint. A Geometry Nodes modifier works out
from the scene time which particles are alive, where they are (velocity, gravity and drag solved
exactly) and how big, then instances a camera-facing quad (or the emitter's mesh) on each. The
material reads the particle's age to apply the colour and transparency curves and to pick the
flipbook frame. Nothing is cached, so the timeline scrubs both ways.
"""
import json
import math

import bpy
import numpy as np
from mathutils import Matrix

AXIS = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], np.float64)
AXIS4 = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))


# ---------------------------------------------------------------- helpers
class Tree:
    """Small helper for building node trees in code."""

    def __init__(self, tree):
        self.tree, self.nodes, self.links = tree, tree.nodes, tree.links
        self.x = 0

    def node(self, kind, **settings):
        n = self.nodes.new(kind)
        for key, value in settings.items():
            setattr(n, key, value)
        n.location = (self.x, 0)
        self.x += 160
        return n

    def link(self, a, b):
        self.links.new(a, b)

    def set(self, socket, value):
        if isinstance(value, bpy.types.NodeSocket):
            self.link(value, socket)
        elif value is not None:
            socket.default_value = value

    def math(self, op, a=None, b=None, c=None):
        n = self.node('ShaderNodeMath', operation=op)
        for i, v in enumerate((a, b, c)):
            self.set(n.inputs[i], v)
        return n.outputs[0]

    def vmath(self, op, a=None, b=None, scale=None):
        n = self.node('ShaderNodeVectorMath', operation=op)
        self.set(n.inputs[0], a)
        self.set(n.inputs[1], b)
        if scale is not None:
            self.set(n.inputs['Scale'], scale)
        return n.outputs['Vector'] if op != 'LENGTH' else n.outputs['Value']

    def curve(self, value, samples):
        """Float Curve through `samples` (spread evenly over 0..1), straight between them."""
        n = self.node('ShaderNodeFloatCurve')
        self.set(n.inputs['Value'], value)
        lo, hi = min(samples + [0.0]), max(samples + [1.0])
        mapping = n.mapping
        mapping.use_clip = False
        mapping.clip_min_y, mapping.clip_max_y = lo, hi
        points = mapping.curves[0].points
        count = len(samples)
        while len(points) < count:
            points.new(0.5, 0.5)
        for i, (point, y) in enumerate(zip(points, samples)):
            point.location = (i / (count - 1), y)
            point.handle_type = 'VECTOR'
        mapping.update()
        return n.outputs['Value']

    def attribute(self, name, kind='FLOAT'):
        n = self.node('GeometryNodeInputNamedAttribute', data_type=kind)
        n.inputs['Name'].default_value = name
        return n.outputs['Attribute']


def _quad(name):
    me = bpy.data.meshes.new(name)
    me.from_pydata([(-.5, -.5, 0), (.5, -.5, 0), (.5, .5, 0), (-.5, .5, 0)], [], [(0, 1, 2, 3)])
    uv = me.uv_layers.new(name='UVMap')
    for loop, co in zip(uv.data, ((0, 0), (1, 0), (1, 1), (0, 1))):
        loop.uv = co
    return me


def _to_blender(points, transform):
    """Frostbite emitter-local points -> Blender world (Z up)."""
    points = np.asarray(points, np.float64).reshape(-1, 3)
    t = np.asarray(transform, np.float64)
    world = points @ t[:3, :3].T + t[:3, 3]
    return world @ AXIS.T


def _dirs_to_blender(vectors, transform):
    vectors = np.asarray(vectors, np.float64).reshape(-1, 3)
    t = np.asarray(transform, np.float64)
    return (vectors @ t[:3, :3].T) @ AXIS.T


# ---------------------------------------------------------------- material
def _colors(samples):
    """HDR colour samples -> (normalised colours, emission strengths)."""
    colors, strengths = [], []
    for c in samples:
        peak = max(max(c), 1e-6)
        if peak > 1.0:
            colors.append([v / peak for v in c])
            strengths.append(0.8 * math.sqrt(peak))
        else:
            colors.append(list(c))
            strengths.append(1.0)
    return colors, strengths


def particle_material(name, emitter, image, source='INSTANCER'):
    mat = bpy.data.materials.new(name[:60])
    mat.use_nodes = True
    mat.use_backface_culling = False
    for attr, value in (('surface_render_method', 'BLENDED'), ('blend_method', 'BLEND'), ('use_transparency_overlap', True)):
        if hasattr(mat, attr):
            try:
                setattr(mat, attr, value)
            except (TypeError, AttributeError):
                pass
    t = Tree(mat.node_tree)
    t.nodes.clear()
    out = t.node('ShaderNodeOutputMaterial')

    def instancer(attribute, kind='Fac'):
        n = t.node('ShaderNodeAttribute', attribute_type=source, attribute_name=attribute)
        return n.outputs[kind]

    age = instancer('fx_age')
    colors, strengths = _colors(emitter['curves']['color'])
    ramp = t.node('ShaderNodeValToRGB')
    elements = ramp.color_ramp.elements
    step = max(1, math.ceil(len(colors) / 30))
    picked = list(range(0, len(colors), step))
    if picked[-1] != len(colors) - 1:
        picked.append(len(colors) - 1)
    while len(elements) < len(picked):
        elements.new(0.5)
    for element, i in zip(elements, picked):
        element.position = i / (len(colors) - 1)
        element.color = (*colors[i], 1.0)
    t.link(age, ramp.inputs['Fac'])
    tint = instancer('fx_tint', 'Color')
    color = t.node('ShaderNodeMix', data_type='RGBA', blend_type='MULTIPLY')
    color.inputs['Factor'].default_value = 1
    t.link(ramp.outputs['Color'], color.inputs['A'])
    t.link(tint, color.inputs['B'])
    color = color.outputs['Result']
    alpha = t.curve(age, emitter['curves']['alpha']) if emitter['curves'].get('alpha') else 1.0

    if image is not None:
        tex = t.node('ShaderNodeTexImage', image=image, extension='EXTEND')
        frames, columns = emitter['frames'], emitter['columns']
        flips = any(any(f) for f in emitter['particles'].get('flip', []))
        if source != 'INSTANCER':
            frames = 1  # ribbons run the texture along their length
        if frames > 1 or flips:
            uv = t.node('ShaderNodeTexCoord').outputs['UV']
            sep = t.node('ShaderNodeSeparateXYZ')
            t.link(uv, sep.inputs[0])
            u, v = sep.outputs['X'], sep.outputs['Y']
            if flips:  # u' = u + flip * (1 - 2u)
                flip = t.node('ShaderNodeSeparateXYZ')
                t.link(instancer('fx_flip', 'Vector'), flip.inputs[0])
                u = t.math('ADD', u, t.math('MULTIPLY', flip.outputs['X'], t.math('MULTIPLY_ADD', u, -2.0, 1.0)))
                v = t.math('ADD', v, t.math('MULTIPLY', flip.outputs['Y'], t.math('MULTIPLY_ADD', v, -2.0, 1.0)))
            if frames > 1:
                rows = math.ceil(frames / columns)
                cell = instancer('fx_cell')
                col = t.math('FLOORED_MODULO', cell, columns)
                row = t.math('FLOOR', t.math('DIVIDE', cell, columns))
                u = t.math('DIVIDE', t.math('ADD', u, col), columns)
                v = t.math('DIVIDE', t.math('ADD', v, t.math('SUBTRACT', rows - 1, row)), rows)
            join = t.node('ShaderNodeCombineXYZ')
            t.link(u, join.inputs['X'])
            t.link(v, join.inputs['Y'])
            t.link(join.outputs['Vector'], tex.inputs['Vector'])
        mix = t.node('ShaderNodeMix', data_type='RGBA', blend_type='MULTIPLY')
        mix.inputs['Factor'].default_value = 1
        t.link(color, mix.inputs['A'])
        t.link(tex.outputs['Color'], mix.inputs['B'])
        color = mix.outputs['Result']
        alpha = t.math('MULTIPLY', alpha, tex.outputs['Alpha'])
    if emitter.get('emissive'):
        shader = t.node('ShaderNodeEmission')
        t.link(color, shader.inputs['Color'])
        t.set(shader.inputs['Strength'], t.curve(age, strengths))
    else:
        shader = t.node('ShaderNodeBsdfPrincipled')
        t.link(color, shader.inputs['Base Color'])
        shader.inputs['Roughness'].default_value = 1.0
    clear = t.node('ShaderNodeBsdfTransparent')
    mix = t.node('ShaderNodeMixShader')
    t.set(mix.inputs['Fac'], t.math('MINIMUM', t.math('MAXIMUM', alpha, 0.0), 1.0))
    t.link(clear.outputs[0], mix.inputs[1])
    t.link(shader.outputs[0], mix.inputs[2])
    t.link(mix.outputs[0], out.inputs['Surface'])
    return mat


# ---------------------------------------------------------------- geometry nodes
def particle_nodes(name, emitter, instance, camera):
    group = bpy.data.node_groups.new(name[:60], 'GeometryNodeTree')
    face = group.interface
    face.new_socket('Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    face.new_socket('Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    eye = face.new_socket('Camera', in_out='INPUT', socket_type='NodeSocketObject')
    eye.description = 'Quads turn to face this object'
    if camera is not None and hasattr(eye, 'default_value'):
        eye.default_value = camera
    start = face.new_socket('Start (s)', in_out='INPUT', socket_type='NodeSocketFloat')
    start.description = 'Scene time in seconds at which the effect starts'
    loop = face.new_socket('Loop', in_out='INPUT', socket_type='NodeSocketBool')
    loop.default_value = bool(emitter['loop'])
    period = face.new_socket('Loop length (s)', in_out='INPUT', socket_type='NodeSocketFloat')
    period.default_value = max(emitter['duration'], 0.1)
    speed = face.new_socket('Speed', in_out='INPUT', socket_type='NodeSocketFloat')
    speed.default_value = 1.0
    t = Tree(group)
    gi, go = t.node('NodeGroupInput'), t.node('NodeGroupOutput')

    now = t.node('GeometryNodeInputSceneTime').outputs['Seconds']
    local = t.math('MULTIPLY', t.math('SUBTRACT', now, gi.outputs['Start (s)']), gi.outputs['Speed'])
    wrapped = t.math('FLOORED_MODULO', local, gi.outputs['Loop length (s)'])
    switch = t.node('GeometryNodeSwitch', input_type='FLOAT')
    t.link(gi.outputs['Loop'], switch.inputs['Switch'])
    t.link(local, switch.inputs['False'])
    t.link(wrapped, switch.inputs['True'])
    clock = switch.outputs['Output']

    age = t.math('SUBTRACT', clock, t.attribute('fx_spawn'))
    life = t.attribute('fx_life')
    fraction = t.math('DIVIDE', age, life)
    dead = t.node('FunctionNodeBooleanMath', operation='OR')
    t.link(t.math('LESS_THAN', age, 0.0), dead.inputs[0])
    t.link(t.math('GREATER_THAN', fraction, 1.0), dead.inputs[1])
    delete = t.node('GeometryNodeDeleteGeometry', domain='POINT')
    t.link(gi.outputs['Geometry'], delete.inputs['Geometry'])
    t.link(dead.outputs[0], delete.inputs['Selection'])
    geometry = delete.outputs['Geometry']

    # Position: velocity with gravity and linear drag, solved in closed form.
    velocity = t.attribute('fx_velocity', 'FLOAT_VECTOR')
    gravity = tuple((np.asarray(emitter['gravity'], float) @ AXIS.T).tolist())
    drag = float(emitter.get('drag') or 0.0)
    if drag > 1e-4:
        a = t.math('DIVIDE', t.math('SUBTRACT', 1.0, t.math('EXPONENT', t.math('MULTIPLY', age, -drag))), drag)
        b = t.math('DIVIDE', t.math('SUBTRACT', age, a), drag)
    else:
        a = age
        b = t.math('MULTIPLY', t.math('MULTIPLY', age, age), 0.5)
    offset = t.vmath('ADD', t.vmath('SCALE', velocity, scale=a), t.vmath('SCALE', gravity, scale=b))
    move = t.node('GeometryNodeSetPosition')
    t.link(geometry, move.inputs['Geometry'])
    t.link(offset, move.inputs['Offset'])
    geometry = move.outputs['Geometry']

    # Values the material reads from the instance: age (0..1) and flipbook cell.
    store = t.node('GeometryNodeStoreNamedAttribute', data_type='FLOAT', domain='POINT')
    store.inputs['Name'].default_value = 'fx_age'
    t.link(geometry, store.inputs['Geometry'])
    t.link(fraction, store.inputs['Value'])
    geometry = store.outputs['Geometry']
    frames = emitter['frames']
    if frames > 1:
        cell = t.attribute('fx_frame')
        rate = emitter.get('anim_speed') or 0.0
        if emitter.get('anim_on_life'):
            cell = t.math('ADD', cell, t.math('MULTIPLY', fraction, frames * (rate or 1.0)))
        elif rate:
            cell = t.math('ADD', cell, t.math('MULTIPLY', age, rate * frames))
        cell = t.math('FLOORED_MODULO', t.math('FLOOR', cell), frames)
        store = t.node('GeometryNodeStoreNamedAttribute', data_type='FLOAT', domain='POINT')
        store.inputs['Name'].default_value = 'fx_cell'
        t.link(geometry, store.inputs['Geometry'])
        t.link(cell, store.inputs['Value'])
        geometry = store.outputs['Geometry']

    # Size over life (X and Y may differ).
    size = t.attribute('fx_size')
    curves = emitter['curves']
    if curves.get('size'):
        size = t.math('MULTIPLY', size, t.curve(fraction, curves['size']))
    sx = t.math('MULTIPLY', size, t.curve(fraction, curves['size_x'])) if curves.get('size_x') else size
    sy = t.math('MULTIPLY', size, t.curve(fraction, curves['size_y'])) if curves.get('size_y') else size
    scale = t.node('ShaderNodeCombineXYZ')
    t.link(sx, scale.inputs['X'])
    t.link(sy, scale.inputs['Y'])
    t.link(sx if emitter['kind'] == 'mesh' else size, scale.inputs['Z'])

    # Facing: quads turn to the camera (or the emitter's world axis) and spin in that plane.
    spin = t.math('ADD', t.attribute('fx_rotation'), t.math('MULTIPLY', t.attribute('fx_spin'), age))
    if emitter['kind'] == 'mesh':
        euler = t.node('ShaderNodeCombineXYZ')
        t.link(t.attribute('fx_rotation'), euler.inputs['X'])
        t.link(spin, euler.inputs['Z'])
        rotation = euler.outputs['Vector']
    else:
        if emitter.get('alignment') == 2:
            facing = tuple((np.asarray(emitter.get('world_up') or [0, 1, 0], float) @ AXIS.T).tolist())
        else:
            info = t.node('GeometryNodeObjectInfo')
            t.link(gi.outputs['Camera'], info.inputs['Object'])
            position = t.node('GeometryNodeInputPosition').outputs['Position']
            facing = t.vmath('SUBTRACT', info.outputs['Location'], position)
        align = t.node('FunctionNodeAlignRotationToVector', axis='Z')
        t.set(align.inputs['Vector'], facing)
        turn = t.node('FunctionNodeAxisAngleToRotation')
        turn.inputs['Axis'].default_value = (0, 0, 1)
        t.link(spin, turn.inputs['Angle'])
        rotate = t.node('FunctionNodeRotateRotation', rotation_space='LOCAL')
        t.link(align.outputs['Rotation'], rotate.inputs['Rotation'])
        t.link(turn.outputs['Rotation'], rotate.inputs['Rotate By'])
        rotation = rotate.outputs['Rotation']

    source = t.node('GeometryNodeObjectInfo', transform_space='ORIGINAL')
    source.inputs['Object'].default_value = instance
    if 'As Instance' in source.inputs:
        source.inputs['As Instance'].default_value = False
    place = t.node('GeometryNodeInstanceOnPoints')
    t.link(geometry, place.inputs['Points'])
    t.link(source.outputs['Geometry'], place.inputs['Instance'])
    t.link(rotation, place.inputs['Rotation'])
    t.link(scale.outputs['Vector'], place.inputs['Scale'])
    t.link(place.outputs['Instances'], go.inputs['Geometry'])
    return group


def _clock(t, gi):
    now = t.node('GeometryNodeInputSceneTime').outputs['Seconds']
    local = t.math('MULTIPLY', t.math('SUBTRACT', now, gi.outputs['Start (s)']), gi.outputs['Speed'])
    wrapped = t.math('FLOORED_MODULO', local, gi.outputs['Loop length (s)'])
    switch = t.node('GeometryNodeSwitch', input_type='FLOAT')
    t.link(gi.outputs['Loop'], switch.inputs['Switch'])
    t.link(local, switch.inputs['False'])
    t.link(wrapped, switch.inputs['True'])
    return switch.outputs['Output']


def _interface(group, emitter, camera):
    face = group.interface
    face.new_socket('Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    face.new_socket('Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    eye = face.new_socket('Camera', in_out='INPUT', socket_type='NodeSocketObject')
    eye.description = 'Quads turn to face this object'
    if camera is not None and hasattr(eye, 'default_value'):
        eye.default_value = camera
    start = face.new_socket('Start (s)', in_out='INPUT', socket_type='NodeSocketFloat')
    start.description = 'Scene time in seconds at which the effect starts'
    loop = face.new_socket('Loop', in_out='INPUT', socket_type='NodeSocketBool')
    loop.default_value = bool(emitter['loop'])
    period = face.new_socket('Loop length (s)', in_out='INPUT', socket_type='NodeSocketFloat')
    period.default_value = max(emitter['duration'], 0.1)
    speed = face.new_socket('Speed', in_out='INPUT', socket_type='NodeSocketFloat')
    speed.default_value = 1.0


def _store(t, geometry, name, value, kind='FLOAT', domain='POINT'):
    n = t.node('GeometryNodeStoreNamedAttribute', data_type=kind, domain=domain)
    n.inputs['Name'].default_value = name
    t.link(geometry, n.inputs['Geometry'])
    t.set(n.inputs['Value'], value)
    return n.outputs['Geometry']


def tracked_nodes(name, emitter, instance, camera, ribbon_material=None):
    """Plays recorded particle paths. The point cloud holds the particles first, then KEYS path points
    per particle; each particle looks up the two path points around its age and blends them."""
    group = bpy.data.node_groups.new(name[:60], 'GeometryNodeTree')
    _interface(group, emitter, camera)
    t = Tree(group)
    gi, go = t.node('NodeGroupInput'), t.node('NodeGroupOutput')
    p = emitter['particles']
    count, keys = len(p['spawn']), len(p['track'][0])
    clock = _clock(t, gi)

    age = t.math('SUBTRACT', clock, t.attribute('fx_spawn'))
    life = t.attribute('fx_life')
    fraction = t.math('DIVIDE', age, life)
    index = t.node('GeometryNodeInputIndex').outputs['Index']
    k = t.math('MULTIPLY', t.math('MINIMUM', t.math('MAXIMUM', fraction, 0.0), 1.0), keys - 1)
    k0 = t.math('MINIMUM', t.math('FLOOR', k), keys - 2)
    weight = t.math('SUBTRACT', k, k0)
    first = t.math('ADD', t.math('MULTIPLY_ADD', index, float(keys), float(count)), k0)

    def sample(value, at, kind='FLOAT_VECTOR'):
        n = t.node('GeometryNodeSampleIndex', data_type=kind, domain='POINT')
        t.link(gi.outputs['Geometry'], n.inputs['Geometry'])
        t.set(n.inputs['Value'], value)
        t.set(n.inputs['Index'], at)
        return n.outputs['Value']
    here = t.node('GeometryNodeInputPosition').outputs['Position']
    p0, p1 = sample(here, first), sample(here, t.math('ADD', first, 1.0))
    position = t.vmath('ADD', p0, t.vmath('SCALE', t.vmath('SUBTRACT', p1, p0), scale=weight))
    velocity = t.vmath('SCALE', t.vmath('SUBTRACT', p1, p0), scale=t.math('DIVIDE', keys - 1, life))
    a0 = sample(t.attribute('fx_angle'), first, 'FLOAT')
    a1 = sample(t.attribute('fx_angle'), t.math('ADD', first, 1.0), 'FLOAT')
    angle = t.math('ADD', a0, t.math('MULTIPLY', t.math('SUBTRACT', a1, a0), weight))

    geometry = gi.outputs['Geometry']
    geometry = _store(t, geometry, 'fx_age', fraction)
    geometry = _store(t, geometry, 'fx_pos', position, 'FLOAT_VECTOR')
    geometry = _store(t, geometry, 'fx_vel', velocity, 'FLOAT_VECTOR')
    geometry = _store(t, geometry, 'fx_spin_now', angle)
    frames = emitter['frames']
    if frames > 1:
        cell = t.attribute('fx_frame')
        rate = emitter.get('anim_speed') or 0.0
        if emitter.get('anim_on_life'):
            cell = t.math('ADD', cell, t.math('MULTIPLY', fraction, frames * (rate or 1.0)))
        elif rate:
            cell = t.math('ADD', cell, t.math('MULTIPLY', age, rate * frames))
        geometry = _store(t, geometry, 'fx_cell', t.math('FLOORED_MODULO', t.math('FLOOR', cell), frames))

    dead = t.node('FunctionNodeBooleanMath', operation='OR')
    t.link(t.math('LESS_THAN', age, 0.0), dead.inputs[0])
    t.link(t.math('GREATER_THAN', fraction, 1.0), dead.inputs[1])
    gone = t.node('FunctionNodeBooleanMath', operation='OR')
    t.link(dead.outputs[0], gone.inputs[0])
    t.link(t.math('GREATER_THAN', index, count - 0.5), gone.inputs[1])  # the path points
    delete = t.node('GeometryNodeDeleteGeometry', domain='POINT')
    t.link(geometry, delete.inputs['Geometry'])
    t.link(gone.outputs[0], delete.inputs['Selection'])
    move = t.node('GeometryNodeSetPosition')
    t.link(delete.outputs['Geometry'], move.inputs['Geometry'])
    t.link(t.attribute('fx_pos', 'FLOAT_VECTOR'), move.inputs['Position'])
    geometry = move.outputs['Geometry']

    age = t.attribute('fx_age')
    size = t.attribute('fx_size')
    curves = emitter['curves']
    if curves.get('size'):
        size = t.math('MULTIPLY', size, t.curve(age, curves['size']))
    sx = t.math('MULTIPLY', size, t.curve(age, curves['size_x'])) if curves.get('size_x') else size
    sy = t.math('MULTIPLY', size, t.curve(age, curves['size_y'])) if curves.get('size_y') else size
    vel = t.attribute('fx_vel', 'FLOAT_VECTOR')
    align = emitter.get('align', 'camera')

    if emitter['kind'] == 'ribbon' and ribbon_material is not None:
        # The particles, oldest first, joined into one strip as wide as each particle.
        geometry = _store(t, geometry, 'fx_width', sx)
        points = t.node('GeometryNodeMeshToPoints')
        t.link(geometry, points.inputs['Mesh'])
        join = t.node('GeometryNodePointsToCurves')
        t.link(points.outputs['Points'], join.inputs['Points'])
        t.link(t.attribute('fx_spawn'), join.inputs['Weight'])
        curve = join.outputs['Curves']
        param = t.node('GeometryNodeSplineParameter')
        curve = _store(t, curve, 'fx_u', param.outputs['Factor'])
        # Face the camera: the strip spans the curve's binormal, so its normal points at the camera.
        info = t.node('GeometryNodeObjectInfo')
        t.link(gi.outputs['Camera'], info.inputs['Object'])
        toward = t.vmath('SUBTRACT', info.outputs['Location'], t.node('GeometryNodeInputPosition').outputs['Position'])
        normal = t.node('GeometryNodeSetCurveNormal')
        if 'Mode' in normal.inputs:  # Blender 5: a menu socket
            normal.inputs['Mode'].default_value = 'Free'
        elif hasattr(normal, 'mode'):
            normal.mode = 'FREE'
        t.link(curve, normal.inputs['Curve'])
        if 'Normal' in normal.inputs:
            t.link(toward, normal.inputs['Normal'])
        curve = normal.outputs['Curve']
        radius = t.node('GeometryNodeSetCurveRadius')
        t.link(curve, radius.inputs['Curve'])
        t.link(t.attribute('fx_width'), radius.inputs['Radius'])
        line = t.node('GeometryNodeCurvePrimitiveLine')
        line.inputs['Start'].default_value = (0, -0.5, 0)
        line.inputs['End'].default_value = (0, 0.5, 0)
        profile = _store(t, line.outputs['Curve'], 'fx_v', t.node('GeometryNodeSplineParameter').outputs['Factor'])
        mesh = t.node('GeometryNodeCurveToMesh')
        t.link(radius.outputs['Curve'], mesh.inputs['Curve'])
        t.link(profile, mesh.inputs['Profile Curve'])
        uv = t.node('ShaderNodeCombineXYZ')
        t.link(t.attribute('fx_u'), uv.inputs['X'])
        t.link(t.attribute('fx_v'), uv.inputs['Y'])
        out = _store(t, mesh.outputs['Mesh'], 'UVMap', uv.outputs['Vector'], 'FLOAT2', 'CORNER')
        paint = t.node('GeometryNodeSetMaterial')
        t.link(out, paint.inputs['Geometry'])
        paint.inputs['Material'].default_value = ribbon_material
        t.link(paint.outputs['Geometry'], go.inputs['Geometry'])
        return group

    if align == 'stretch':  # longer the faster it moves (as the VFX Editor draws it)
        length = t.math('MULTIPLY', t.vmath('LENGTH', vel), (emitter.get('stretch') or 1.0) / 30.0)
        stretched = t.math('MINIMUM', t.math('ADD', sy, length), emitter.get('stretch_clamp') or 100.0)
        stretched = t.math('MINIMUM', stretched, t.math('MULTIPLY', sy, emitter.get('stretch_relative_clamp') or 100.0))
        sy = t.math('MAXIMUM', stretched, sy)
    scale = t.node('ShaderNodeCombineXYZ')
    t.link(sx, scale.inputs['X'])
    t.link(sy, scale.inputs['Y'])
    t.link(sx if emitter['kind'] == 'mesh' else size, scale.inputs['Z'])

    spin = t.attribute('fx_spin_now')
    if emitter['kind'] == 'mesh':
        euler = t.node('ShaderNodeCombineXYZ')
        t.link(spin, euler.inputs['Z'])
        t.link(t.math('MULTIPLY', spin, 0.7), euler.inputs['X'])
        rotation = euler.outputs['Vector']
    else:
        if align == 'world':
            facing = tuple((np.asarray(emitter.get('world_up') or [0, 1, 0], float) @ AXIS.T).tolist())
        else:
            info = t.node('GeometryNodeObjectInfo')
            t.link(gi.outputs['Camera'], info.inputs['Object'])
            facing = t.vmath('SUBTRACT', info.outputs['Location'], t.node('GeometryNodeInputPosition').outputs['Position'])
        turned = t.node('FunctionNodeAlignRotationToVector', axis='Z')
        t.set(turned.inputs['Vector'], facing)
        rotation = turned.outputs['Rotation']
        if align in ('velocity', 'stretch'):
            along = t.node('FunctionNodeAlignRotationToVector', axis='Y', pivot_axis='Z')
            t.link(rotation, along.inputs['Rotation'])
            t.link(vel, along.inputs['Vector'])
            rotation = along.outputs['Rotation']
        else:
            turn = t.node('FunctionNodeAxisAngleToRotation')
            turn.inputs['Axis'].default_value = (0, 0, 1)
            t.link(spin, turn.inputs['Angle'])
            rotate = t.node('FunctionNodeRotateRotation', rotation_space='LOCAL')
            t.link(rotation, rotate.inputs['Rotation'])
            t.link(turn.outputs['Rotation'], rotate.inputs['Rotate By'])
            rotation = rotate.outputs['Rotation']

    source = t.node('GeometryNodeObjectInfo', transform_space='ORIGINAL')
    source.inputs['Object'].default_value = instance
    if 'As Instance' in source.inputs:
        source.inputs['As Instance'].default_value = False
    place = t.node('GeometryNodeInstanceOnPoints')
    t.link(geometry, place.inputs['Points'])
    t.link(source.outputs['Geometry'], place.inputs['Instance'])
    t.link(rotation, place.inputs['Rotation'])
    t.link(scale.outputs['Vector'], place.inputs['Scale'])
    t.link(place.outputs['Instances'], go.inputs['Geometry'])
    return group


# ---------------------------------------------------------------- objects
def emitter_object(importer, emitter, name, collection, holder, camera):
    p = emitter['particles']
    count = len(p['spawn'])
    if not count:
        return None
    transform = emitter['transform']
    tracked = 'track' in p  # version 2 files: recorded paths; version 1: start position and velocity
    me = bpy.data.meshes.new(name[:60])
    if tracked:
        track = np.asarray(p['track'], np.float64)
        keys = track.shape[1]
        points = np.concatenate([track[:, 0], track.reshape(-1, 3)])
    else:
        points = np.asarray(p['position'], np.float64)
    total = len(points)
    me.vertices.add(total)
    me.vertices.foreach_set('co', _to_blender(points, transform).astype(np.float32).ravel())

    def add(attr, values, kind='FLOAT'):
        values = np.asarray(values, np.float32)
        width = 1 if kind == 'FLOAT' else 3
        values = values.reshape(-1, width) if width > 1 else values.reshape(-1)
        if len(values) < total:  # path points carry only what they use
            pad = np.zeros((total - len(values),) + values.shape[1:], np.float32)
            values = np.concatenate([values, pad])
        a = me.attributes.new(attr, kind, 'POINT')
        a.data.foreach_set('value' if kind == 'FLOAT' else 'vector', values.ravel())
    add('fx_spawn', p['spawn'])
    add('fx_life', p['life'])
    add('fx_size', p['size'])
    add('fx_frame', p['frame'])
    add('fx_tint', np.asarray(p['tint'], np.float64).reshape(-1, 3), 'FLOAT_VECTOR')
    if tracked:
        angles = np.asarray(p['angle'], np.float64)
        add('fx_angle', np.concatenate([angles[:, 0], angles.reshape(-1)]))
        flip = np.zeros((count, 3))
        if p.get('flip'):
            flip[:, :2] = np.asarray(p['flip'], np.float64)
        add('fx_flip', flip, 'FLOAT_VECTOR')
    else:
        add('fx_rotation', p['rotation'])
        add('fx_spin', p['spin'])
        add('fx_velocity', _dirs_to_blender(p['velocity'], transform), 'FLOAT_VECTOR')
    ob = bpy.data.objects.new(name[:60], me)
    collection.objects.link(ob)
    ob.parent = holder
    ob['frostbite_emitter'] = emitter['name']
    if emitter.get('notes'):
        ob['frostbite_notes'] = '; '.join(emitter['notes'])

    # What each particle draws: the emitter's mesh, or a quad with the flipbook material.
    if emitter['kind'] == 'mesh' and emitter.get('mesh'):
        try:
            source = bpy.data.objects.new(name[:54] + ' mesh', importer.mesh(emitter['mesh'], emitter.get('materials', [])))
        except (OSError, ValueError, KeyError) as error:
            importer.warnings.append('%s: %s' % (name, error))
            emitter = dict(emitter, kind='quad')
    ribbon = None
    if tracked and emitter['kind'] == 'ribbon':
        ribbon = particle_material(name, emitter, importer.image(emitter.get('texture')), 'GEOMETRY')
    if emitter['kind'] != 'mesh' or not emitter.get('mesh'):
        quad = _quad(name[:54] + ' quad')
        quad.materials.append(ribbon or particle_material(name, emitter, importer.image(emitter.get('texture'))))
        source = bpy.data.objects.new(name[:54] + ' quad', quad)
    collection.objects.link(source)
    source.parent = holder
    source.hide_viewport = source.hide_render = True
    source.hide_set(True)

    mod = ob.modifiers.new('Frostbite particles', 'NODES')
    mod.node_group = tracked_nodes(name, emitter, source, camera, ribbon) if tracked else \
        particle_nodes(name, emitter, source, camera)
    if camera is not None:
        for socket in mod.node_group.interface.items_tree:
            if getattr(socket, 'in_out', '') == 'INPUT' and socket.name == 'Camera':
                try:
                    mod[socket.identifier] = camera
                except TypeError:
                    pass  # the socket's default already points at the camera
    return ob


def light_object(light, name, collection, holder, fps):
    color = light['color']
    peak = max(max(color), 1e-6)
    data = bpy.data.lights.new(name[:60], 'POINT')
    data.color = [c / peak for c in color] if peak > 1 else color
    data.shadow_soft_size = 0.25
    if hasattr(data, 'use_custom_distance'):
        data.use_custom_distance = True
        data.cutoff_distance = light['radius']
    energy = 25.0 * light['intensity'] * peak
    ob = bpy.data.objects.new(name[:60], data)
    collection.objects.link(ob)
    ob.parent = holder
    ob.location = _to_blender([[0, 0, 0]], light['transform'])[0]
    curve = light.get('curve') or [1.0]
    if light['lifetime'] > 0 and not light.get('looping'):
        start = 1 + light['delay'] * fps
        data.energy = 0
        data.keyframe_insert('energy', frame=start - 1)
        for i, v in enumerate(curve):
            data.energy = energy * v
            data.keyframe_insert('energy', frame=start + i / max(len(curve) - 1, 1) * light['lifetime'] * fps)
        data.energy = 0
        data.keyframe_insert('energy', frame=start + light['lifetime'] * fps + 1)
    else:
        data.energy = energy
    return ob


def build(importer, context, place, collection):
    """One effect place from a scene file -> an empty holding its emitters and lights."""
    tags = place.get('tags', {})
    path = importer.assets / tags['effect']
    effect = json.loads(path.read_text(encoding='utf-8'))
    holder = bpy.data.objects.new(place.get('name', 'Effect')[:60], None)
    holder.empty_display_type = 'SPHERE'
    holder.empty_display_size = 0.5
    holder.matrix_world = AXIS4 @ Matrix(place['matrix']) @ AXIS4.inverted()
    holder['frostbite_effect'] = tags.get('source', '')
    if effect.get('notes'):
        holder['frostbite_notes'] = '; '.join(effect['notes'])
    col = bpy.data.collections.new(place.get('name', 'Effect')[:60])
    collection.children.link(col)
    col.objects.link(holder)
    camera = context.scene.camera
    scene = context.scene
    fps = scene.render.fps / scene.render.fps_base
    for i, emitter in enumerate(effect['emitters']):
        name = emitter['name'].replace('em_', '', 1)
        try:
            emitter_object(importer, emitter, name, col, holder, camera)
        except Exception as error:  # an emitter that does not build must not lose the rest
            importer.warnings.append('%s: %s' % (name, error))
    for i, light in enumerate(effect['lights']):
        light_object(light, '%s light %d' % (place.get('name', 'Effect'), i + 1), col, holder, fps)
    end = int(math.ceil(1 + effect.get('duration', 1) * fps))
    if scene.frame_end < end:
        scene.frame_end = end
    return holder
