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


def particle_material(name, emitter, image):
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
        n = t.node('ShaderNodeAttribute', attribute_type='INSTANCER', attribute_name=attribute)
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
        if frames > 1:
            rows = math.ceil(frames / columns)
            uv = t.node('ShaderNodeTexCoord').outputs['UV']
            sep = t.node('ShaderNodeSeparateXYZ')
            t.link(uv, sep.inputs[0])
            cell = instancer('fx_cell')
            col = t.math('FLOORED_MODULO', cell, columns)
            row = t.math('FLOOR', t.math('DIVIDE', cell, columns))
            u = t.math('DIVIDE', t.math('ADD', sep.outputs['X'], col), columns)
            v = t.math('DIVIDE', t.math('ADD', sep.outputs['Y'], t.math('SUBTRACT', rows - 1, row)), rows)
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


# ---------------------------------------------------------------- objects
def emitter_object(importer, emitter, name, collection, holder, camera):
    p = emitter['particles']
    count = len(p['spawn'])
    if not count:
        return None
    transform = emitter['transform']
    me = bpy.data.meshes.new(name[:60])
    me.vertices.add(count)
    me.vertices.foreach_set('co', _to_blender(p['position'], transform).astype(np.float32).ravel())

    def add(attr, values, kind='FLOAT'):
        a = me.attributes.new(attr, kind, 'POINT')
        a.data.foreach_set('value' if kind == 'FLOAT' else 'vector', np.asarray(values, np.float32).ravel())
    add('fx_spawn', p['spawn'])
    add('fx_life', p['life'])
    add('fx_size', p['size'])
    add('fx_rotation', p['rotation'])
    add('fx_spin', p['spin'])
    add('fx_frame', p['frame'])
    add('fx_velocity', _dirs_to_blender(p['velocity'], transform), 'FLOAT_VECTOR')
    add('fx_tint', np.asarray(p['tint'], np.float64).reshape(-1, 3), 'FLOAT_VECTOR')
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
    if emitter['kind'] != 'mesh' or not emitter.get('mesh'):
        quad = _quad(name[:54] + ' quad')
        quad.materials.append(particle_material(name, emitter, importer.image(emitter.get('texture'))))
        source = bpy.data.objects.new(name[:54] + ' quad', quad)
    collection.objects.link(source)
    source.parent = holder
    source.hide_viewport = source.hide_render = True
    source.hide_set(True)

    mod = ob.modifiers.new('Frostbite particles', 'NODES')
    mod.node_group = particle_nodes(name, emitter, source, camera)
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
