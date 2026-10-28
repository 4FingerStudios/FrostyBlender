"""Build a Blender scene from an aura-frostbite-scene JSON file.

Aura is right-handed and Y-up with DirectX-style UVs (v points down).
Blender is right-handed and Z-up with v pointing up, so positions are
rotated (x, y, z) -> (x, -z, y), V is flipped and normal-map green is negated.

Skinned meshes whose tags name a skin and a skeleton (character cosmetics) can be rigged: each
skeleton becomes one armature in its bind pose, and the meshes are parented to it with an
Armature modifier and a vertex group per bone.
"""
import json
import math
import os
import time
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import amsh
from .materials import build_material

AXIS = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], np.float32)
AXIS4 = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))
# Gameplay markers (tools/markers.py): collection names, and the empty's shape and size per kind.
MARKER_GROUPS = {'player_spawn': 'Player spawns', 'ai_spawn': 'AI spawns', 'vehicle_spawn': 'Vehicle spawns',
                 'teleporter': 'Teleporters', 'purchase': 'Purchase spots', 'objective': 'Objectives',
                 'collectible': 'Collectibles'}
MARKER_SHAPES = {'player_spawn': ('SINGLE_ARROW', 2.0), 'ai_spawn': ('SPHERE', 0.6), 'vehicle_spawn': ('CONE', 1.5),
                 'teleporter': ('CIRCLE', 1.5), 'purchase': ('CUBE', 0.6), 'objective': ('CIRCLE', 6.0),
                 'collectible': ('SPHERE', 0.4)}
# Frostbite bones run along their local X; Blender bones along local Y. Columns: the Blender
# bone's X, Y and Z axes in Frostbite bone space.
BONE_AXES = Matrix(((0, 1, 0, 0), (1, 0, 0, 0), (0, 0, -1, 0), (0, 0, 0, 1)))
# Strength of Blender's sky texture standing in for Battle for Neighborville's physical sky, beside the
# scene's 2 W/m2 sun (tools/environment.py scales the game's lights the same way).
PHYSICAL_SKY_STRENGTH = 0.25
MAX_SHADOW_LIGHTS = 32  # lights that keep their shadows on a map, beside the sun
MAP_SHADOW_POOL = 1024  # MB of EEVEE shadow pool a map import asks for


def _to_sun(values):
    """Blender-space direction toward the game's sun: Frostbite gives its azimuth and elevation, Y up."""
    azimuth, elevation = (math.radians(a) for a in values.get('sun_rotation', (0.0, 45.0)))
    to_sun = Vector((math.cos(elevation) * math.sin(azimuth), math.sin(elevation),
                     math.cos(elevation) * math.cos(azimuth)))
    return Vector((to_sun.x, -to_sun.z, to_sun.y))


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
                 add_lighting=True, report=print, rig=True, include_markers=True):
        self.scene_path = Path(scene_path)
        self.assets = Path(assets)
        self.include_collision = include_collision
        self.include_markers = include_markers
        self.marker_root, self.marker_groups = None, {}
        self.group_by_level = group_by_level
        self.add_lighting = add_lighting
        self.report = report
        self.rig = rig
        self.meshes, self.materials, self.images, self.collections = {}, {}, {}, {}
        self.armatures = {}
        # Skinned map objects: armature data per skeleton, shared clip Actions, and how many were rigged/animated.
        self.rig_data, self.clip_actions = {}, {}
        self.rigged = self.animated = self.clip_length = 0
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
        self.weight(ob, rig, names, tags['skin'])

    def place_rig(self, context, ob, tags, collection):
        """A skinned map object (flag, windmill, machine): its own armature object at the object's map transform,
        sharing one armature datablock per skeleton (poses are per object), and its looping clip if it has one."""
        rel = tags['skeleton']
        if rel not in self.rig_data:
            doc = json.loads((self.assets / rel).read_text(encoding='utf-8'))
            arm = bpy.data.armatures.new(Path(rel).stem[:60])
            arm.display_type = 'STICK'
            builder = bpy.data.objects.new(arm.name, arm)
            collection.objects.link(builder)
            try:
                names = self.build_bones(context, builder, doc['bones'])
            finally:
                bpy.data.objects.remove(builder)
            self.rig_data[rel] = (arm, names, doc.get('name', ''))
        arm, names, skeleton = self.rig_data[rel]
        rig = bpy.data.objects.new((ob.name + ' rig')[:60], arm)
        rig.matrix_world = ob.matrix_world
        rig['frostbite_skeleton'] = skeleton
        rig['frostbite_map_rig'] = rel
        collection.objects.link(rig)
        self.weight(ob, rig, names, tags['skin'])
        ob.matrix_parent_inverse.identity()
        ob.matrix_basis.identity()
        self.rigged += 1
        if tags.get('clip'):
            try:
                self.loop_clip(context, rig, rel, tags['clip'])
                self.animated += 1
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.warnings.append('%s: clip not applied: %s' % (ob.name, error))

    def loop_clip(self, context, rig, skeleton, rel):
        """Put a decoded map clip on a rig, looping. Rigs of one skeleton playing one clip share its Action."""
        from . import animation_import
        from .rig_tools import _fcurves
        key = (skeleton, rel)
        action = self.clip_actions.get(key)
        if action is None:
            scene = context.scene
            frames = scene.frame_start, scene.frame_end
            context.view_layer.update()  # a new object gets its pose bones on the next update
            clip = animation_import.load_clip(self.assets / rel)
            action, _ = animation_import.apply_clip(context, rig, clip, set_fps=False)
            scene.frame_start, scene.frame_end = frames  # a map keeps its own range; the clips cycle
            # One key per clip frame: stretch them to the scene's frame rate so the clip plays at game speed.
            stretch = scene.render.fps / scene.render.fps_base / float(clip.get('fps') or 30.0)
            for fc in _fcurves(action):
                if abs(stretch - 1.0) > 1e-3:
                    for point in fc.keyframe_points:
                        point.co.x = 1.0 + (point.co.x - 1.0) * stretch
                        point.handle_left.x = 1.0 + (point.handle_left.x - 1.0) * stretch
                        point.handle_right.x = 1.0 + (point.handle_right.x - 1.0) * stretch
                    fc.update()
                if not any(m.type == 'CYCLES' for m in fc.modifiers):
                    fc.modifiers.new('CYCLES')
            action.use_fake_user = False
            self.clip_actions[key] = action
            self.clip_length = max(self.clip_length, int(action.frame_range[1]))
            return
        rig.animation_data_create()
        rig.animation_data.action = action
        if hasattr(rig.animation_data, 'action_slot') and rig.animation_data.action_slot is None and action.slots:
            rig.animation_data.action_slot = action.slots[0]

    def weight(self, ob, rig, names, skin):
        bones, weights = amsh.read_skin(self.assets / skin, len(ob.data.vertices))
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
                    if self.rig and tags.get('skin') and tags.get('skeleton') and kind == 'frostbite_mesh':
                        try:
                            if tags.get('rig_instance'):
                                self.place_rig(context, ob, tags, self.collection(root, tags.get('level_instance')))
                            else:
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
                elif kind == 'frostbite_marker' and self.include_markers:
                    self.marker(place, root)
                elif kind == 'frostbite_effect':
                    from . import effects_build
                    try:
                        effects_build.build(self, context, place, root)
                        count += 1
                    except (OSError, ValueError, KeyError) as error:
                        self.warnings.append('%s: %s' % (place.get('name'), error))
                elif kind == 'frostbite_environment' and self.add_lighting:
                    self.environment(context, root, place.get('values', {}))
                elif kind == 'frostbite_light' and self.add_lighting:
                    self.light(place, root)
                    count += 1
        finally:
            wm.progress_end()
        self.limit_shadows(root)
        for space in (a.spaces.active for w in wm.windows for a in w.screen.areas if a.type == 'VIEW_3D'):
            space.clip_end = max(space.clip_end, 10000.0)
        if self.clip_length and context.scene.frame_end < self.clip_length:
            context.scene.frame_end = self.clip_length  # show at least one full loop of the map's clips
        self.report({'INFO'}, 'Imported %d objects, %d meshes, %d materials in %.1fs%s%s' % (
            count, len(self.meshes), len(self.materials), time.time() - start,
            '; %d skinned objects rigged, %d animated' % (self.rigged, self.animated) if self.rigged else '',
            '; %d warnings (see console)' % len(self.warnings) if self.warnings else ''))
        for warning in self.warnings[:200]:
            print('Frostbite import:', warning)
        return root

    def marker(self, place, root):
        """A gameplay marker (tools/markers.py) as a labelled empty, one collection per kind, not rendered."""
        tags = place.get('tags', {})
        category = tags.get('category', 'marker')
        if self.marker_root is None:
            self.marker_root = bpy.data.collections.new(root.name + ' gameplay')
            root.children.link(self.marker_root)
            self.marker_root.hide_render = True
        group = self.marker_groups.get(category)
        if group is None:
            group = bpy.data.collections.new(MARKER_GROUPS.get(category, category.replace('_', ' ').title()))
            self.marker_root.children.link(group)
            self.marker_groups[category] = group
        ob = bpy.data.objects.new(place.get('name', 'marker')[:60], None)
        ob.empty_display_type, ob.empty_display_size = MARKER_SHAPES.get(category, ('PLAIN_AXES', 1.0))
        ob.matrix_world = AXIS4 @ Matrix(place['matrix']) @ AXIS4.inverted()
        ob.show_name = category != 'ai_spawn'  # AI spawns come in the hundreds
        for key in ('team', 'layer', 'blueprint'):
            if tags.get(key):
                ob['frostbite_' + key] = tags[key]
        group.objects.link(ob)

    def environment(self, context, root, values):
        """The level's sun, sky and fog. GW1/GW2 maps carry the game's own values (see gw2_convert.environment);
        others get a neutral daylight sun and sky."""
        sun = bpy.data.lights.new('Frostbite sun', 'SUN')
        sun.angle = 0.02
        ob = bpy.data.objects.new('Frostbite sun', sun)
        color = values.get('sun_color')
        if color and max(color) > 0:
            peak = max(color)
            sun.color = [c / peak for c in color]
            sun.energy = min(max(peak * 2.0, 0.05), 10.0)
            ob.rotation_euler = (-_to_sun(values)).to_track_quat('-Z', 'Y').to_euler()
        else:
            sun.energy = float(values.get('sun_intensity', 2.4)) * 1.5
            ob.rotation_euler = (0.85, 0.0, 0.6)
        if values.get('environment'):
            ob['frostbite_environment'] = values['environment']
        root.objects.link(ob)
        # A whole map's sun shadow overflows EEVEE's default 512 MB shadow pool (missing shadows); raise it.
        eevee = getattr(context.scene, 'eevee', None)
        if eevee is not None and hasattr(eevee, 'shadow_pool_size') and int(eevee.shadow_pool_size) < MAP_SHADOW_POOL:
            eevee.shadow_pool_size = str(MAP_SHADOW_POOL)
        world = context.scene.world or bpy.data.worlds.new('World')
        context.scene.world = world
        world.use_nodes = True
        if not values.get('sky_color'):
            bg = world.node_tree.nodes.get('Background')
            if bg:
                bg.inputs['Color'].default_value = (0.55, 0.7, 0.9, 1.0)
                bg.inputs['Strength'].default_value = 0.6
            return
        self.game_sky(context, world, values)

    def game_sky(self, context, world, values):
        """World nodes: the game's ambient sky/ground colours light the scene, its panoramic sky texture is what
        the camera sees, and its fog fades distant geometry into the fog colour."""
        nt = world.node_tree
        nt.nodes.clear()
        n, link = nt.nodes, nt.links.new

        def node(kind, x, y, **settings):
            made = n.new(kind)
            made.location = (x, y)
            for key, value in settings.items():
                setattr(made, key, value)
            return made

        def math_node(op, a, b=None, x=0, y=0):
            made = node('ShaderNodeMath', x, y, operation=op)
            for i, value in enumerate((a, b)):
                if value is None:
                    continue
                if isinstance(value, bpy.types.NodeSocket):
                    link(value, made.inputs[i])
                else:
                    made.inputs[i].default_value = value
            return made.outputs[0]

        coords = node('ShaderNodeTexCoord', -1400, 0)
        direction = node('ShaderNodeSeparateXYZ', -1200, 0)
        link(coords.outputs['Generated'], direction.inputs[0])
        # Ambient: ground colour below the horizon blending into the sky colour above it.
        up = math_node('MULTIPLY_ADD', direction.outputs['Z'], 0.5, x=-1000, y=200)
        up.node.inputs[2].default_value = 0.5
        ambient = node('ShaderNodeMix', -800, 200, data_type='RGBA')
        link(up, ambient.inputs['Factor'])
        ambient.inputs['A'].default_value = (*values.get('ground_color', (0.05, 0.05, 0.05)), 1.0)
        ambient.inputs['B'].default_value = (*values['sky_color'], 1.0)
        light_bg = node('ShaderNodeBackground', -600, 200)
        link(ambient.outputs['Result'], light_bg.inputs['Color'])
        light_bg.inputs['Strength'].default_value = 3.0  # the game's ambient values are small and scene-referred
        seen = light_bg
        camera_bg = None
        path = values.get('sky_texture')
        if path and (self.assets / path).is_file():
            # The panorama wraps around by azimuth. Its UV range says which band of the sky dome it covers, with
            # dome V running 0 at the zenith, 0.5 at the horizon: [0, 0.5] is the whole upper half.
            u_min, u_max, v_min, v_max = values.get('sky_uv', (0.0, 1.0, 0.0, 0.5))
            azimuth = math_node('ARCTAN2', direction.outputs['Y'], direction.outputs['X'], x=-1000, y=-150)
            u = math_node('MULTIPLY_ADD', azimuth, 1.0 / (2 * math.pi), x=-850, y=-150)
            u.node.inputs[2].default_value = 0.5 + float(values.get('sky_rotation', 0.0))
            u = math_node('MULTIPLY', u, float(values.get('sky_tile', 1.0)) * (u_max - u_min), x=-700, y=-150)
            u = math_node('ADD', u, u_min, x=-560, y=-150)
            height = math_node('ARCSINE', math_node('MAXIMUM', direction.outputs['Z'], 0.0, x=-1000, y=-320), x=-850, y=-320)
            dome = math_node('MULTIPLY_ADD', height, -0.5 / (math.pi / 2), x=-700, y=-320)  # 0 zenith .. 0.5 horizon
            dome.node.inputs[2].default_value = 0.5
            v = math_node('MULTIPLY_ADD', dome, 1.0 / max(v_max - v_min, 1e-4), x=-560, y=-320)
            v.node.inputs[2].default_value = -v_min / max(v_max - v_min, 1e-4)
            v = math_node('SUBTRACT', 1.0, v, x=-420, y=-320)  # Blender's image V runs upwards
            v.node.use_clamp = True
            uv = node('ShaderNodeCombineXYZ', -150, -200)
            link(u, uv.inputs['X'])
            link(v, uv.inputs['Y'])
            image = node('ShaderNodeTexImage', 0, -200, extension='REPEAT', interpolation='Linear')
            image.image = bpy.data.images.load(str(self.assets / path), check_existing=True)
            link(uv.outputs['Vector'], image.inputs['Vector'])
            camera_bg = node('ShaderNodeBackground', 300, -200)
            link(image.outputs['Color'], camera_bg.inputs['Color'])
            camera_bg.inputs['Strength'].default_value = float(values.get('sky_brightness', 1.0))
        elif values.get('sun_lux') and values.get('sun_color'):
            # Battle for Neighborville draws a physically based sky, not a panorama: Blender's own sky texture
            # with the game's sun stands in for it where the camera looks.
            to_sun = _to_sun(values)
            sky = node('ShaderNodeTexSky', 0, -200)
            sky.sky_type = next(i.identifier for i in sky.bl_rna.properties['sky_type'].enum_items)
            if hasattr(sky, 'sun_elevation'):
                sky.sun_elevation = math.asin(max(min(to_sun.z, 1.0), -1.0))
                sky.sun_rotation = math.atan2(-to_sun.x, to_sun.y)  # Cycles: rotation 0 puts the sun along +Y
            if hasattr(sky, 'sun_disc'):
                sky.sun_disc = False  # the sun lamp already lights the scene
            camera_bg = node('ShaderNodeBackground', 300, -200)
            link(sky.outputs['Color'], camera_bg.inputs['Color'])
            camera_bg.inputs['Strength'].default_value = PHYSICAL_SKY_STRENGTH
        if camera_bg is not None:
            ray = node('ShaderNodeLightPath', 300, 300)
            pick = node('ShaderNodeMixShader', 550, 0)
            link(ray.outputs['Is Camera Ray'], pick.inputs['Fac'])
            link(light_bg.outputs['Background'], pick.inputs[1])
            link(camera_bg.outputs['Background'], pick.inputs[2])
            seen = pick
        out = node('ShaderNodeOutputWorld', 800, 0)
        link(seen.outputs[0], out.inputs['Surface'])
        start, end = float(values.get('fog_start', 0.0)), float(values.get('fog_end', 0.0))
        if values.get('fog_color') and end > start >= 0:
            curve = values.get('fog_curve')
            peak = min(max(sum(curve) if curve else 0.8, 0.05), 0.95)
            if values.get('sun_lux'):
                # Battle for Neighborville's fog is physical scattering that tints rather than whites out (its
                # colour is plain white); at full strength it would bury the sky.
                peak = min(peak, 0.3)
            try:
                self.fog(context, world, values['fog_color'], start, end, peak)
            except (AttributeError, RuntimeError, KeyError, TypeError) as error:
                self.warnings.append('fog not set up: %s' % error)
        world['frostbite_environment'] = values.get('environment', '')

    def fog(self, context, world, color, start, end, peak):
        """The game's distance fog as a compositor step on the mist pass: pixels fade toward the fog colour from
        the fog start, reaching `peak` (the game's curve tops out near 80%) at the fog end. A world volume would
        also dim the sun in EEVEE, so the fog lives in the compositor and shows in renders, not the viewport."""
        scene = context.scene
        world.mist_settings.start = start
        world.mist_settings.depth = max(end - start, 1.0)
        world.mist_settings.falloff = 'LINEAR'
        for layer in scene.view_layers:
            layer.use_pass_mist = True
        if hasattr(scene, 'compositing_node_group'):  # Blender 5: the compositor is a node group
            tree = bpy.data.node_groups.new('Frostbite fog', 'CompositorNodeTree')
            tree.interface.new_socket('Image', in_out='OUTPUT', socket_type='NodeSocketColor')
            output = tree.nodes.new('NodeGroupOutput')
            target = output.inputs[0]
            scene.compositing_node_group = tree
        else:  # Blender 4.x
            scene.use_nodes = True
            tree = scene.node_tree
            tree.nodes.clear()
            output = tree.nodes.new('CompositorNodeComposite')
            target = output.inputs['Image']
        layers = tree.nodes.new('CompositorNodeRLayers')
        amount = tree.nodes.new('ShaderNodeMath')
        amount.operation = 'MULTIPLY'
        amount.inputs[1].default_value = peak
        tree.links.new(layers.outputs['Mist'], amount.inputs[0])
        mix = tree.nodes.new('ShaderNodeMix')
        mix.data_type = 'RGBA'
        tree.links.new(amount.outputs[0], mix.inputs['Factor'])
        tree.links.new(layers.outputs['Image'], mix.inputs['A'])
        mix.inputs['B'].default_value = (*color, 1.0)
        tree.links.new(mix.outputs['Result'], target)
        layers.location, amount.location, mix.location, output.location = (-400, 0), (-150, 150), (50, 0), (300, 0)
        scene['frostbite_fog'] = 'Compositor: %s, fog %.0f-%.0f m' % (tree.name, start, end)

    def limit_shadows(self, root):
        """Hundreds of shadowed lamps (map lights and the lights of placed effects) overflow EEVEE's shadow pool:
        missing shadows and a slow viewport. Only the MAX_SHADOW_LIGHTS that light the most (power times reach
        squared) keep their shadows; the sun always does."""
        def weight(light):
            reach = light.cutoff_distance if getattr(light, 'use_custom_distance', False) else 10.0
            return light.energy * reach * reach
        lights = {ob.data for ob in root.all_objects if ob.type == 'LIGHT' and ob.data.type != 'SUN'}
        shadowed = sorted((light for light in lights if light.use_shadow), key=weight, reverse=True)
        for light in shadowed[MAX_SHADOW_LIGHTS:]:
            light.use_shadow = False

    def light(self, place, root):
        """A game point, spot or rectangle light. GW1/GW2 strength is the game's intensity in watts-ish units;
        Battle for Neighborville lights come in watts already, scaled with the sun (tools/environment.py).
        Reach is the light's radius."""
        values = place.get('values', {})
        kind = values.get('type') if values.get('type') in ('SPOT', 'AREA') else 'POINT'
        light = bpy.data.lights.new(place.get('name', 'Frostbite light'), kind)
        light.color = values.get('color', (1.0, 1.0, 1.0))
        if 'watts' in values:
            light.energy = min(float(values['watts']), 1000000.0)
            light.use_shadow = light.energy >= 1.0  # faint lamps never cast shadows (see limit_shadows)
        else:
            light.energy = min(float(values.get('intensity', 10.0)) * 10.0, 100000.0)
        if kind == 'AREA':
            light.shape = 'RECTANGLE'
            light.size, light.size_y = max(float(values.get('width', 1.0)), 0.01), max(float(values.get('height', 1.0)), 0.01)
        else:
            light.shadow_soft_size = max(float(values.get('size', 0.0)) * 0.5, 0.05)
        if hasattr(light, 'use_custom_distance'):
            light.use_custom_distance = True
            light.cutoff_distance = max(float(values.get('radius', 10.0)), 0.1)
        if kind == 'SPOT':
            outer = min(max(float(values.get('cone_outer', 45.0)), 1.0), 180.0)
            light.spot_size = math.radians(outer)
            light.spot_blend = min(max(1.0 - float(values.get('cone_inner', 0.0)) / outer, 0.0), 1.0)
        ob = bpy.data.objects.new(place.get('name', 'Frostbite light'), light)
        # Frostbite spots shine along -forward, which is Blender's local +Y here; Blender spots shine along -Z.
        ob.matrix_world = AXIS4 @ Matrix(place['matrix']) @ AXIS4.inverted() @ Matrix.Rotation(math.radians(90), 4, 'X')
        ob.scale = (1.0, 1.0, 1.0)
        lights = bpy.data.collections.get(root.name + ' lights')
        if lights is None:
            lights = bpy.data.collections.new(root.name + ' lights')
            root.children.link(lights)
        lights.objects.link(ob)

