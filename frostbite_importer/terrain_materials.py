"""Painted Garden Warfare 2 terrain ('surface': 'terrain'), one material per terrain tile.

The converter (tools/gw2_terrain.py) reads the painted coverage of each layer and writes it as mask images over
the tile, four layers to an image. Here the layers' colour, normal and ASM maps are blended over the base layer
in layer order, each by its coverage, and the result is shaded by "PVZ GW2 Generic" like the rest of the map.
Each layer repeats at the game's own scale ('scale', repeats per metre), and layers the game blends by height
('height': which texture channel) get the game's sharpened edge.
"""
from .gw2_materials import _Builder, group
from .materials import placeholder

FLAT_NORMAL = (.5, .5, 1, 1)
PLAIN_ASM = (1, .3, 0, 1)  # no occlusion, smoothness 0.3, not metal


def build(mat, doc, image):
    mat.use_nodes = True
    mat.use_backface_culling = not doc.get('double_sided', False)
    mat['frostbite_game_shader'] = 'gw2_terrain'
    mat['frostbite_surface_approximation'] = doc.get('surface_approximation', '')
    mat.node_tree.nodes.clear()
    t = _Builder(mat.node_tree)
    layers = doc.get('layers') or [{'base': doc.get('textures', {}).get('base')}]

    uvmap = t.new('ShaderNodeUVMap', -2200)
    uv = uvmap.outputs['UV']
    # Mesh UVs are world (x, 1 - z) times uv_scale; the masks span the tile, with v growing with z.
    scale = float(doc.get('uv_scale', .25))
    masks = {}
    if doc.get('masks'):
        x0, z0, width = doc['mask_rect']
        to_tile = t.new('ShaderNodeVectorMath', -2000, label='Tile coordinates', operation='MULTIPLY_ADD')
        to_tile.inputs[1].default_value = (1 / (scale * width), -1 / (scale * width), 1)
        to_tile.inputs[2].default_value = (-x0 / width, (1 / scale - z0) / width, 0)
        t.set(to_tile.inputs[0], uv)
        for entry in doc['masks']:
            img = image(entry['path'], True)
            if img is None:
                continue
            node = t.new('ShaderNodeTexImage', -1800, label='Coverage ' + ', '.join(str(i) for i in entry['layers']),
                         image=img, extension='EXTEND')
            t.set(node.inputs['Vector'], to_tile.outputs['Vector'])
            channels = t.split(node, -1600)
            for channel, index in zip(('Red', 'Green', 'Blue', 'Alpha'), entry['layers']):
                masks[index] = node.outputs['Alpha'] if channel == 'Alpha' else channels[channel]

    def texture(path, non_color, label, at):
        img = image(path, non_color) if path else None
        if img is None:
            return None
        node = t.new('ShaderNodeTexImage', -1400, label=label, image=img)
        t.set(node.inputs['Vector'], at)
        return node

    def channel(node, index):
        if index == 3:
            return node.outputs['Alpha']
        return t.split(node, -1200)[('Red', 'Green', 'Blue')[index]]

    first = None
    colour = normal = normal_alpha = asm = None
    for layer in layers:
        index = layer.get('index', 0)
        # Each layer repeats at its own scale over world XZ; mesh UVs are (x, 1 - z) times uv_scale.
        at = uv
        if layer.get('scale'):
            k = float(layer['scale']) / scale
            remap = t.new('ShaderNodeVectorMath', -1600, label=f'Layer {index} tiling', operation='MULTIPLY_ADD')
            remap.inputs[1].default_value = (k, k, 1)
            remap.inputs[2].default_value = (0, 1 - k, 0)
            t.set(remap.inputs[0], uv)
            at = remap.outputs['Vector']
        base = texture(layer.get('base'), False, f'Layer {index} colour', at)
        nrm = texture(layer.get('normal'), True, f'Layer {index} normal', at)
        surf = texture(layer.get('asm'), True, f'Layer {index} ASM', at)
        layer_colour = base.outputs['Color'] if base else (1, 1, 1, 1)
        layer_normal = nrm.outputs['Color'] if nrm else FLAT_NORMAL
        layer_normal_alpha = nrm.outputs['Alpha'] if nrm else 1.0
        layer_asm = surf.outputs['Color'] if surf else PLAIN_ASM
        if colour is None:
            first = base
            colour, normal, normal_alpha, asm = layer_colour, layer_normal, layer_normal_alpha, layer_asm
            continue
        cover = masks.get(index)
        if cover is None:
            continue
        height = layer.get('height') or {}
        source = {'base': base, 'normal': nrm, 'asm': surf}.get(height.get('texture'))
        if source is not None:
            # The game sharpens this layer's edge by its height: saturate((overlay(mask, h) - .5) * contrast).
            contrast = float(height.get('contrast', 10))
            sharp = t.mix(cover, channel(source, int(height.get('channel', 2))), 1.0, blend='OVERLAY', x=-1100)
            cover = t.math('MULTIPLY_ADD', sharp, contrast, -.5 * contrast, x=-1050, clamp=True)
        colour = t.mix(colour, layer_colour, cover, x=-1000)
        normal = t.mix(normal, layer_normal, cover, x=-800)
        normal_alpha = t.math('MULTIPLY_ADD', t.math('SUBTRACT', layer_normal_alpha, normal_alpha, x=-900), cover,
                              normal_alpha, x=-800)
        asm = t.mix(asm, layer_asm, cover, x=-600)

    node = t.new('ShaderNodeGroup', -100)
    node.node_tree = group()
    node.width = 240
    gin = node.inputs
    t.set(gin['Base Colour'], colour)
    t.set(gin['Normal'], normal)
    t.set(gin['Normal Alpha'], normal_alpha)
    t.set(gin['ASM'], asm)
    out = t.new('ShaderNodeOutputMaterial', 250)
    t.set(out.inputs['Surface'], node.outputs['Shader'])
    # Solid/Texture view uses the active image: show the base layer.
    for n in t.nodes:
        n.select = False
    if first is None:
        first = t.new('ShaderNodeTexImage', -1400, image=placeholder('frostbite_white', (1, 1, 1, 1)))
    first.select = True
    t.nodes.active = first
