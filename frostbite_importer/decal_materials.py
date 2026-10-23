"""Alpha-blended decal surfaces ('surface': 'decal'): Battle for Neighborville decal volumes,
projected onto the map by the converter (tools/bfn_decals.py)."""
import bpy

from .game_materials import _Nodes


def build(mat, doc, image):
    mat.use_nodes = True
    mat.node_tree.nodes.clear()
    mat.use_backface_culling = False
    mat['frostbite_game_shader'] = 'decal'
    mat['frostbite_source_shader'] = doc.get('source_decal_template', '')
    mat['frostbite_surface_approximation'] = doc.get('surface_approximation', '')
    if hasattr(mat, 'surface_render_method'):
        mat.surface_render_method = 'BLENDED'
    if hasattr(mat, 'use_transparent_shadow'):
        mat.use_transparent_shadow = True
    tint = tuple(doc.get('tint', [1, 1, 1, 1]))
    mat.diffuse_color = tint
    t = _Nodes(mat)
    uv = t.new('ShaderNodeUVMap', -900)
    tex = t.new('ShaderNodeTexImage', -650, label='Decal colour', extension='CLIP')
    tex.image = image(doc.get('textures', {}).get('base'), False)
    t.link(uv.outputs['UV'], tex.inputs['Vector'])
    color = t.mix(tex.outputs['Color'], tint, 1, 'MULTIPLY', x=-350)
    bsdf = t.new('ShaderNodeBsdfPrincipled', -100, label='Decal')
    t.link(color, bsdf.inputs['Base Color'])
    t.link(tex.outputs['Alpha'], bsdf.inputs['Alpha'])
    bsdf.inputs['Roughness'].default_value = .6
    out = t.new('ShaderNodeOutputMaterial', 200)
    t.link(bsdf.outputs['BSDF'], out.inputs['Surface'])
    for node in t.nodes:
        node.select = False
    tex.select = True
    t.nodes.active = tex
