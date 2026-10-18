"""Build a Blender scene from an aura-frostbite-scene JSON file.

Aura is right-handed and Y-up with DirectX-style UVs (v points down).
Blender is right-handed and Z-up with v pointing up, so positions are
rotated (x, y, z) -> (x, -z, y), V is flipped and normal-map green is negated.

Skinned meshes whose tags name a skin and a skeleton (character cosmetics) can be rigged: each
skeleton becomes one armature in its bind pose, and the meshes are parented to it with an
Armature modifier and a vertex group per bone.
"""
import json
import os
import time
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix

from . import amsh
from .materials import build_material

AXIS = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], np.float32)
AXIS4 = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))
# Frostbite bones run along their local X; Blender bones along local Y. Columns: the Blender
# bone's X, Y and Z axes in Frostbite bone space.
BONE_AXES = Matrix(((0, 1, 0, 0), (1, 0, 0, 0), (0, 0, -1, 0), (0, 0, 0, 1)))


def find_assets(scene_path):
    """Find the asset root that the scene's relative mesh paths point into."""
    scene_path = Path(scene_path).resolve()
    doc_hint = scene_path.parent / 'assets'
    if (doc_hint / 'meshes').is_dir():
        return doc_hint
    for parent in scene_path.parents:
        # Aura jobs live in <project>/.aura/frostbite-import/jobs/<id>.
        if (parent / 'assets' / 'meshes' / 'frostbite').is_dir():
            return parent / 'assets'
    return None


def bone_names(bones):
    """Blender names of skeleton bones; a repeated name gets Blender's .001 suffix."""
    seen, out = {}, []
    for i, bone in enumerate(bones):
        name = bone['name'][:59] or 'bone_%d' % i
        count = seen.get(name, 0)
        seen[name] = count + 1
        out.append(name if not count else '%s.%03d' % (name, count))
    return out


class Importer:
    def __init__(self, scene_path, assets, include_collision=False, group_by_level=True,
                 add_lighting=True, report=print, rig=True):
        self.scene_path = Path(scene_path)
        self.assets = Path(assets)
        self.include_collision = include_collision
        self.group_by_level = group_by_level
        self.add_lighting = add_lighting
        self.report = report
        self.rig = rig
        self.meshes, self.materials, self.images, self.collections = {}, {}, {}, {}
        self.armatures = {}
        self.warnings = []
        try:  # the optional game shaders (Garden Warfare 1 and 2 materials)
            self.game_shaders = bool(bpy.context.scene.frostbite.game_shaders)
        except AttributeError:
            self.game_shaders = False

    # ---------------------------------------------------------------- assets
    def image(self, rel, non_color=False):
        if not rel:
            return None
        key = (rel, non_color)
        if key in self.images:
            return self.images[key]
        path = self.assets / rel
        image = None
        if path.is_file():
            image = bpy.data.images.load(str(path), check_existing=True)
            colorspace = 'Non-Color' if non_color else 'sRGB'
            if image.colorspace_settings.name != colorspace:
                # Blender's check_existing ignores colour space. A single file
                # can be bound as both diffuse and data (GW1's Grave Digger eyes).
                # Never change the colour interpretation of an existing binding.
                referenced = image.users or any(i == image for i in self.images.values())
                if referenced:
                    image = image.copy()
                image.colorspace_settings.name = colorspace
            # Frostbite packs shader data into alpha, even on opaque diffuse
            # maps. Premultiplication discards RGB where that data channel is 0.
            image.alpha_mode = 'CHANNEL_PACKED'
        else:
            self.warnings.append('Missing texture ' + rel)
        self.images[key] = image
        return image

    def material(self, rel):
        if rel in self.materials:
            return self.materials[rel]
        path = self.assets / rel if rel else None
        doc = {}
        if path and path.is_file():
            doc = json.loads(path.read_text(encoding='utf-8'))
        elif rel:
            self.warnings.append('Missing material ' + rel)
        name = Path(doc.get('name') or rel or 'frostbite_default').name
        mat = bpy.data.materials.new(name[:60])
        mat['frostbite_material'] = rel or ''
        mat['frostbite_assets'] = str(self.assets.resolve())
        if doc.get('shader_name'):
            mat['frostbite_shader'] = doc['shader_name']
        build_material(mat, doc, self.image, self.game_shaders)
        self.materials[rel] = mat
        return mat

    def mesh(self, rel, material_paths):
        key = (rel, tuple(material_paths))
        if key in self.meshes:
            return self.meshes[key]
        data = amsh.read(self.assets / rel)
        me = bpy.data.meshes.new(Path(rel).stem)
        positions = data['positions'] @ AXIS.T
        normals = data['normals'] @ AXIS.T
        indices = data['indices']
        triangles = len(indices) // 3
        me.vertices.add(len(positions))
        me.vertices.foreach_set('co', positions.ravel())
        me.loops.add(len(indices))
        me.loops.foreach_set('vertex_index', indices.astype(np.int32))
        me.polygons.add(triangles)
        me.polygons.foreach_set('loop_start', np.arange(0, len(indices), 3, dtype=np.int32))
        slots = np.zeros(triangles, np.int32)
        for section in data['sections']:
            slots[section['first'] // 3:(section['first'] + section['count']) // 3] = section['slot']
        me.polygons.foreach_set('material_index', slots)
        uv = data['uv'][indices].copy()
        uv[:, 1] = 1.0 - uv[:, 1]
        me.uv_layers.new(name='UVMap').data.foreach_set('uv', uv.ravel())
        colors = data['colors']
        if colors.size and not np.allclose(colors, 1.0):
            attr = me.color_attributes.new('Col', 'FLOAT_COLOR', 'POINT')
            rgba = np.ones((len(colors), 4), np.float32)
            rgba[:, :3] = colors
            attr.data.foreach_set('color', rgba.ravel())
        for section in sorted(data['sections'], key=lambda s: s['slot']):
            while len(me.materials) <= section['slot']:
                me.materials.append(None)
            slot = section['slot']
            me.materials[slot] = self.material(material_paths[slot] if slot < len(material_paths) else None)
        me.update(calc_edges=True)
        me.validate(clean_customdata=False)
        me.shade_smooth()
        unit = normals / np.maximum(np.linalg.norm(normals, axis=1), 1e-12)[:, None]
        me.normals_split_custom_set_from_vertices(unit.tolist())
        self.meshes[key] = me
        return me

    # ------------------------------------------------------------ rigging
    def armature(self, context, rel, collection):
        """The armature of a skeleton JSON, built once in its bind pose."""
        if rel in self.armatures:
            return self.armatures[rel]
        doc = json.loads((self.assets / rel).read_text(encoding='utf-8'))
        bones = doc['bones']
        existing = self.existing_armature(context, rel, doc.get('name'))
        if existing is not None:
            self.armatures[rel] = (existing, self.extend_armature(context, existing, bones))
            return self.armatures[rel]
        name = (doc.get('character') or Path(rel).stem).replace('_', ' ').title()[:60]
        arm = bpy.data.armatures.new(name)
        arm.display_type = 'STICK'
        ob = bpy.data.objects.new(name, arm)
        ob.show_in_front = True
        ob['frostbite_skeleton'] = doc.get('name', '')
        ob['frostbite_rig'] = rel
        collection.objects.link(ob)
        self.armatures[rel] = (ob, self.build_bones(context, ob, bones))
        return self.armatures[rel]

    @staticmethod
    def existing_armature(context, rel, skeleton):
        """An armature an earlier import made of the same skeleton, so items imported one at a
        time end up on one rig: the active object if it is one, else the first in the scene."""
        found = [o for o in context.scene.objects if o.type == 'ARMATURE' and (o.get('frostbite_rig') == rel or
                 (skeleton and 'frostbite_rig' not in o and o.get('frostbite_skeleton') == skeleton))]
        active = context.view_layer.objects.active
        return active if active in found else (found[0] if found else None)

    def extend_armature(self, context, ob, bones):
        """Bone names of `bones` on an existing armature, adding the ones it lacks (procedural bones)."""
        have = {b.name for b in ob.data.bones}
        names = bone_names(bones)
        return names if all(n in have for n in names) else self.build_bones(context, ob, bones)

    def build_bones(self, context, ob, bones):
        worlds = []
        for bone in bones:
            m = Matrix(bone['matrix'])
            for i in range(3):  # bind poses are rigid; drop any scale
                axis = m.col[i].to_3d()
                m.col[i][:3] = axis.normalized() if axis.length > 1e-8 else Matrix.Identity(3).col[i]
            worlds.append(AXIS4 @ m)
        heads = [w.translation.copy() for w in worlds]
        children = {}
        for i, bone in enumerate(bones):
            children.setdefault(bone['parent'], []).append(i)
        # Bones are only made in edit mode. Timers (the background import) have no window in
        # their context, so borrow one.
        windows = context.window_manager.windows
        override = {} if context.window or not windows else {'window': windows[0]}
        with context.temp_override(**override):
            if bpy.context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            previous = bpy.context.view_layer.objects.active
            hidden = ob.hide_get()
            ob.hide_set(False)
            bpy.context.view_layer.objects.active = ob
            bpy.ops.object.mode_set(mode='EDIT')
            try:
                names = self.edit_bones(ob.data, bones, worlds, heads, children)
            finally:
                bpy.ops.object.mode_set(mode='OBJECT')
                bpy.context.view_layer.objects.active = previous
                ob.hide_set(hidden)
        return names

    @staticmethod
    def edit_bones(arm, bones, worlds, heads, children):
        names, lengths, made = [], [], set()
        for i, (bone, name) in enumerate(zip(bones, bone_names(bones))):
            if name in arm.edit_bones:  # already on a reused armature
                names.append(name)
                lengths.append(arm.edit_bones[name].length)
                continue
            made.add(i)
            spans = [(heads[c] - heads[i]).length for c in children.get(i, [])]
            spans = [d for d in spans if d > 1e-3]
            parent = bone['parent']
            length = min(spans) if spans else (lengths[parent] * 0.6 if 0 <= parent < i else 0.05)
            length = min(max(length, 0.01), 0.5)
            lengths.append(length)
            eb = arm.edit_bones.new(name)
            eb.head, eb.tail = (0, 0, 0), (0, length, 0)
            eb.matrix = worlds[i] @ BONE_AXES
            names.append(eb.name)
        for i, bone in enumerate(bones):
            if i in made and 0 <= bone['parent'] < len(bones) and bone['parent'] != i:
                arm.edit_bones[names[i]].parent = arm.edit_bones[names[bone['parent']]]
        return names

    def bind(self, context, ob, tags, collection):
        """Parent a skinned mesh object to its armature and weight it."""
        rig, names = self.armature(context, tags['skeleton'], collection)
        bones, weights = amsh.read_skin(self.assets / tags['skin'], len(ob.data.vertices))
        if bones.size and int(bones[weights > 0].max(initial=0)) >= len(names):
            raise ValueError('Skin names bones the skeleton does not have')
        vertex = np.repeat(np.arange(len(bones)), bones.shape[1])
        bone, weight = bones.ravel().astype(np.int64), weights.ravel()
        keep = weight > 0
        vertex, bone, weight = vertex[keep], bone[keep], weight[keep]
        for b in np.unique(bone):
            group = ob.vertex_groups.new(name=names[b])
            mine = bone == b
            for w in np.unique(weight[mine]):
                group.add(vertex[mine & (weight == w)].tolist(), float(w), 'ADD')
        modifier = ob.modifiers.new('Armature', 'ARMATURE')
        modifier.object = rig
        ob.parent = rig

    # ------------------------------------------------------------ hierarchy
    def collection(self, root, name):
        if not self.group_by_level or not name:
            return root
        if name not in self.collections:
            col = bpy.data.collections.new(name.split('/')[-1][:60])
            root.children.link(col)
            self.collections[name] = col
        return self.collections[name]

    def run(self, context):
        start = time.time()
        doc = json.loads(self.scene_path.read_text(encoding='utf-8'))
        if doc.get('format') != 'aura-frostbite-scene':
            raise ValueError('Not an Aura Frostbite scene file')
        root = bpy.data.collections.new(doc.get('name', self.scene_path.stem))
        context.scene.collection.children.link(root)
        collision_root = None
        places = doc['places']
        wm = context.window_manager
        wm.progress_begin(0, len(places))
        count = 0
        try:
            for i, place in enumerate(places):
                if i % 500 == 0:
                    wm.progress_update(i)
                kind = place.get('kind')
                tags = place.get('tags', {})
                if kind == 'frostbite_mesh' or (kind == 'frostbite_collision' and self.include_collision):
                    mats = []
                    while 'material_%d' % len(mats) in tags:
                        mats.append(tags['material_%d' % len(mats)])
                    try:
                        me = self.mesh(tags['mesh'], mats)
                    except (OSError, ValueError, KeyError) as error:
                        self.warnings.append('%s: %s' % (place.get('name'), error))
                        continue
                    ob = bpy.data.objects.new(place.get('name', 'mesh')[:60], me)
                    ob.matrix_world = AXIS4 @ Matrix(place['matrix']) @ AXIS4.inverted()
                    if tags.get('source'):
                        ob['frostbite_source'] = tags['source']
                    if self.rig and tags.get('skin') and tags.get('skeleton'):
                        try:
                            self.bind(context, ob, tags, root)
                        except (OSError, ValueError, KeyError, IndexError) as error:
                            self.warnings.append('%s: not rigged: %s' % (place.get('name'), error))
                    if kind == 'frostbite_collision':
                        if collision_root is None:
                            collision_root = bpy.data.collections.new(root.name + ' collision')
                            root.children.link(collision_root)
                            collision_root.hide_render = True
                        ob.display_type = 'WIRE'
                        collision_root.objects.link(ob)
                    else:
                        self.collection(root, tags.get('level_instance')).objects.link(ob)
                    count += 1
                elif kind == 'spawn':
                    ob = bpy.data.objects.new(place.get('name', 'spawn'), None)
                    ob.empty_display_type = 'SINGLE_ARROW'
                    ob.empty_display_size = 2
                    x, y, z = place['at']
                    ob.location = (x, -z, y)
                    root.objects.link(ob)
                elif kind == 'frostbite_effect':
                    from . import effects_build
                    try:
                        effects_build.build(self, context, place, root)
                        count += 1
                    except (OSError, ValueError, KeyError) as error:
                        self.warnings.append('%s: %s' % (place.get('name'), error))
                elif kind == 'frostbite_environment' and self.add_lighting:
                    self.environment(context, root, place.get('values', {}))
        finally:
            wm.progress_end()
        for space in (a.spaces.active for w in wm.windows for a in w.screen.areas if a.type == 'VIEW_3D'):
            space.clip_end = max(space.clip_end, 10000.0)
        self.report({'INFO'}, 'Imported %d objects, %d meshes, %d materials in %.1fs%s' % (
            count, len(self.meshes), len(self.materials), time.time() - start,
            '; %d warnings (see console)' % len(self.warnings) if self.warnings else ''))
        for warning in self.warnings[:200]:
            print('Frostbite import:', warning)
        return root

    def environment(self, context, root, values):
        sun = bpy.data.lights.new('Frostbite sun', 'SUN')
        sun.energy = float(values.get('sun_intensity', 2.4)) * 1.5
        sun.angle = 0.02
        ob = bpy.data.objects.new('Frostbite sun', sun)
        ob.rotation_euler = (0.85, 0.0, 0.6)
        root.objects.link(ob)
        world = context.scene.world or bpy.data.worlds.new('World')
        context.scene.world = world
        world.use_nodes = True
        bg = world.node_tree.nodes.get('Background')
        if bg:
            bg.inputs['Color'].default_value = (0.55, 0.7, 0.9, 1.0)
            bg.inputs['Strength'].default_value = 0.6

