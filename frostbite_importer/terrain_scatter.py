"""The grass, leaves and twigs Garden Warfare 2 strews over its painted terrain, as Geometry Nodes on each tile.

The converter (tools/gw2_convert.py, terrain_scatter) writes the scattering types of the level (mesh, instances
per square metre, scale range) under 'terrain_scatter_types' in the scene, and per terrain tile ('scatter' tag)
mask images whose channels say how much each type's layer shows there. The game places the instances at run
time, and only near the camera, so here points are distributed at random by the same density and coverage, at
a quarter of it by default (a whole map at full density can run out of memory) and fewer in the viewport. The
modifier's inputs scale the density.
"""
import math
import zlib

import bpy

RENDER_DENSITY, VIEWPORT_DENSITY = .25, .05


def sources(importer, types, root):
    """One hidden object per scattering type, for the tiles to instance: {key: object}."""
    if importer.scatter_sources is not None:
        return importer.scatter_sources
    importer.scatter_sources = {}
    collection = bpy.data.collections.new((root.name + ' scatter meshes')[:60])
    root.children.link(collection)
    collection.hide_render = True
    collection.hide_viewport = True
    for key, kind in types.items():
        try:
            me = importer.mesh(kind['mesh'], kind.get('materials', []))
        except (OSError, ValueError, KeyError) as error:
            importer.warnings.append('Scatter mesh %s: %s' % (kind.get('name', key), error))
            continue
        # Blades are lit like the ground they grow from: their own normals face sideways, so half of them would
        # be dark under any sun.
        me = me.copy()
        me.normals_split_custom_set_from_vertices([(0.0, 0.0, 1.0)] * len(me.vertices))
        ob = bpy.data.objects.new(kind.get('name', key)[:60], me)
        # Nor do they cast shadows in the game; thousands of cards shading each other turn a field black.
        ob.visible_shadow = False
        collection.objects.link(ob)
        importer.scatter_sources[key] = ob
    return importer.scatter_sources


def add(importer, ob, spread, types, root):
    """Scatter over one terrain tile object; spread is its 'scatter' tag."""
    objects = sources(importer, types, root)
    tree = bpy.data.node_groups.new((ob.name + ' scatter')[:60], 'GeometryNodeTree')
    _build(tree, importer, spread, types, objects, seed=zlib.crc32(ob.name.encode()) & 0x7fff)
    if not tree.nodes:
        bpy.data.node_groups.remove(tree)
        return False
    modifier = ob.modifiers.new('Terrain scatter', 'NODES')
    modifier.node_group = tree
    return True


def _socket(sockets, name, kind=None):
    for socket in sockets:
        if socket.name == name and (kind is None or socket.type == kind) and socket.enabled:
            return socket
    for socket in sockets:
        if socket.name == name and (kind is None or socket.type == kind):
            return socket
    raise KeyError(name)


def _build(tree, importer, spread, types, objects, seed):
    face = tree.interface
    face.new_socket('Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    face.new_socket('Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    density = face.new_socket('Density', in_out='INPUT', socket_type='NodeSocketFloat')
    density.default_value, density.min_value = RENDER_DENSITY, 0.0
    density.description = 'Instances relative to the game (1 = as dense as the game draws them)'
    viewport = face.new_socket('Viewport density', in_out='INPUT', socket_type='NodeSocketFloat')
    viewport.default_value, viewport.min_value = VIEWPORT_DENSITY, 0.0
    viewport.description = 'Instances in the viewport, relative to the game'
    nodes, links = tree.nodes, tree.links
    column = {}

    def new(kind, x, **settings):
        node = nodes.new(kind)
        for key, value in settings.items():
            setattr(node, key, value)
        row = column.get(x, 0)
        column[x] = row + 1
        node.location = (x, -row * 220)
        return node

    group_in = new('NodeGroupInput', -1600)
    group_out = new('NodeGroupOutput', 800)
    join = new('GeometryNodeJoinGeometry', 600)
    links.new(group_in.outputs['Geometry'], join.inputs['Geometry'])
    links.new(join.outputs['Geometry'], group_out.inputs['Geometry'])

    is_viewport = new('GeometryNodeIsViewport', -1400)
    switch = new('GeometryNodeSwitch', -1200, input_type='FLOAT')
    links.new(is_viewport.outputs[0], _socket(switch.inputs, 'Switch'))
    links.new(group_in.outputs['Density'], _socket(switch.inputs, 'False'))
    links.new(group_in.outputs['Viewport density'], _socket(switch.inputs, 'True'))
    scale_factor = switch.outputs[0]

    # Mesh UVs are world (x, 1 - z) times uv_scale; the masks span the tile with v growing with z.
    x0, z0, width = spread['mask_rect']
    s = float(spread.get('uv_scale', .25))
    uv = new('GeometryNodeInputNamedAttribute', -1400, data_type='FLOAT_VECTOR')
    _socket(uv.inputs, 'Name').default_value = 'UVMap'
    to_tile = new('ShaderNodeVectorMath', -1200, operation='MULTIPLY_ADD')
    to_tile.inputs[1].default_value = (1 / (s * width), -1 / (s * width), 1)
    to_tile.inputs[2].default_value = (-x0 / width, (1 / s - z0) / width, 0)
    links.new(_socket(uv.outputs, 'Attribute', 'VECTOR'), to_tile.inputs[0])

    # The terrain colour under each blade, for the scatter materials to blend towards ('terrain_colour' on the
    # instances; 'terrain_scatter' marks them, materials.py).
    surface = group_in.outputs['Geometry']
    colour = importer.image(spread['colour'], False) if spread.get('colour') else None
    if colour is not None:
        sample = new('GeometryNodeImageTexture', -1000, extension='EXTEND')
        _socket(sample.inputs, 'Image').default_value = colour
        links.new(to_tile.outputs['Vector'], _socket(sample.inputs, 'Vector'))
        store = new('GeometryNodeStoreNamedAttribute', -800, data_type='FLOAT_COLOR', domain='CORNER')
        _socket(store.inputs, 'Name').default_value = 'terrain_colour'
        links.new(surface, store.inputs['Geometry'])
        links.new(_socket(sample.outputs, 'Color'), _socket(store.inputs, 'Value', 'RGBA'))
        mark = new('GeometryNodeStoreNamedAttribute', -600, data_type='FLOAT', domain='POINT')
        _socket(mark.inputs, 'Name').default_value = 'terrain_scatter'
        _socket(mark.inputs, 'Value', 'VALUE').default_value = 1.0
        links.new(store.outputs['Geometry'], mark.inputs['Geometry'])
        surface = mark.outputs['Geometry']

    count = 0
    for entry in spread.get('masks', []):
        image = importer.image(entry['path'], True)
        if image is None:
            continue
        texture = new('GeometryNodeImageTexture', -1000, extension='EXTEND')
        _socket(texture.inputs, 'Image').default_value = image
        links.new(to_tile.outputs['Vector'], _socket(texture.inputs, 'Vector'))
        split = new('FunctionNodeSeparateColor', -800)
        links.new(_socket(texture.outputs, 'Color'), split.inputs[0])
        for channel, keys in enumerate(entry.get('channels', [])):
            cover = texture.outputs['Alpha'] if channel == 3 else split.outputs[channel]
            for key in keys:
                kind, source = types.get(key), objects.get(key)
                if not kind or source is None or kind.get('density', 0) <= 0:
                    continue
                count += 1
                points = new('GeometryNodeDistributePointsOnFaces', -400, distribute_method='RANDOM')
                links.new(surface, points.inputs['Mesh'])
                amount = new('ShaderNodeMath', -600, operation='MULTIPLY')
                amount.inputs[1].default_value = float(kind['density'])
                links.new(scale_factor, amount.inputs[0])
                links.new(amount.outputs[0], _socket(points.inputs, 'Density'))
                links.new(cover, _socket(points.inputs, 'Density Factor'))
                _socket(points.inputs, 'Seed').default_value = seed + count * 101
                info = new('GeometryNodeObjectInfo', -200, transform_space='ORIGINAL')
                _socket(info.inputs, 'Object').default_value = source
                _socket(info.inputs, 'As Instance').default_value = True
                place = new('GeometryNodeInstanceOnPoints', 200)
                links.new(points.outputs['Points'], place.inputs['Points'])
                links.new(_socket(info.outputs, 'Geometry'), place.inputs['Instance'])
                # A random turn about the up axis, and a size between the type's smallest and largest.
                turn = new('FunctionNodeRandomValue', -200, data_type='FLOAT')
                _socket(turn.inputs, 'Max', 'VALUE').default_value = 2 * math.pi
                _socket(turn.inputs, 'Seed').default_value = seed + count * 7
                euler = new('ShaderNodeCombineXYZ', 0)
                links.new(_socket(turn.outputs, 'Value', 'VALUE'), euler.inputs['Z'])
                links.new(euler.outputs[0], place.inputs['Rotation'])
                size = new('FunctionNodeRandomValue', -200, data_type='FLOAT')
                _socket(size.inputs, 'Seed').default_value = seed + count * 13
                lo, hi = kind.get('min_scale', [1, 1]), kind.get('max_scale', [1, 1])
                scale = new('ShaderNodeCombineXYZ', 0)
                for axis, index in (('X', 0), ('Y', 0), ('Z', 1)):
                    lerp = new('ShaderNodeMath', 0, operation='MULTIPLY_ADD')
                    links.new(_socket(size.outputs, 'Value', 'VALUE'), lerp.inputs[0])
                    lerp.inputs[1].default_value = float(hi[index]) - float(lo[index])
                    lerp.inputs[2].default_value = float(lo[index])
                    links.new(lerp.outputs[0], scale.inputs[axis])
                links.new(scale.outputs[0], place.inputs['Scale'])
                links.new(place.outputs['Instances'], join.inputs['Geometry'])
    if not count:
        nodes.clear()
