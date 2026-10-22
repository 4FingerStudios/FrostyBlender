"""Customizations tab: browse a game's characters and their cosmetics, and import the chosen ones.

Items are converted in their bind pose at the origin, so a body, head and props chosen
together line up on the character. With "Rig to skeleton" on they are also bound to one
armature of the character's skeleton, ready to pose.

Tattoos are material variations of the head or body: their textures are layered over the
material's colour, and on a character already in the scene they replace that piece's
materials instead of adding a second copy. Emotes are animation clips: each becomes an
Action on the character's armature (and its controls, with the pro rig).
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty

from . import BRIDGE, GAMES, _check_bfn, _check_paths, _redraw, _wrap, draw_credits, prefs, scene_import

SOURCES = {'GW1': 'gw1_path', 'GW2': 'gw2_path', 'BFN': 'bfn_path'}
_data = {}   # game folder -> discover result
_job = {}    # the running cosmetics import
_slots = []  # EnumProperty items must stay referenced while Blender shows them


def _game(context):
    state = context.scene.frostbite_cosmetics
    return getattr(prefs(context), SOURCES[state.source], '') or ''


def _title(text):
    return ' '.join(w.capitalize() for w in text.split('_'))


class FrostbiteCharacter(bpy.types.PropertyGroup):
    name: StringProperty()
    id: StringProperty()
    team: StringProperty()


class FrostbiteCosmetic(bpy.types.PropertyGroup):
    name: StringProperty()
    slot: StringProperty()
    bundle: StringProperty()
    sb: StringProperty()
    selected: BoolProperty(name='', description='Include this item in the import')


def _fill_items(state, context):
    """Show the active character's cosmetics."""
    state.items.clear()
    state.item_index = 0
    data = _data.get(_game(context))
    if not data or not 0 <= state.char_index < len(state.characters):
        return
    wanted = state.characters[state.char_index].id
    character = next((c for c in data['characters'] if c['id'] == wanted), None)
    if not character:
        return
    for slot in sorted(character['slots']):
        for item in character['slots'][slot]:
            entry = state.items.add()
            entry.name, entry.slot, entry.bundle, entry.sb = item['name'], slot, item['bundle'], item.get('sb', '')
    state.slot = 'ALL'


def _slot_items(state, context):
    _slots[:] = [('ALL', 'All slots', '')] + [(s, _title(s), '') for s in sorted({i.slot for i in state.items})]
    return _slots


def _reset(state, context):
    state.characters.clear()
    state.items.clear()
    state.status = ''


class FrostbiteCosmeticsState(bpy.types.PropertyGroup):
    source: EnumProperty(name='Game', default='GW2', update=_reset, items=[
        ('GW1', 'GW1', 'Garden Warfare 1'), ('GW2', 'GW2', 'Garden Warfare 2'), ('BFN', 'BFN', 'Battle for Neighborville')])
    characters: CollectionProperty(type=FrostbiteCharacter)
    char_index: IntProperty(update=_fill_items)
    items: CollectionProperty(type=FrostbiteCosmetic)
    item_index: IntProperty()
    slot: EnumProperty(name='Slot', items=_slot_items)
    search: StringProperty(name='', description='Filter items by name', options={'TEXTEDIT_UPDATE'})
    rigged: BoolProperty(name='Rig to skeleton', default=True,
                         description="Bind the items to an armature of the character's skeleton, ready to pose")
    pro_rig: BoolProperty(name='Pro rig (animator controls)', default=True,
                          description='After import, add the control rig: FK/IK controls and eye aim on top of the game '
                                      'skeleton. Game animations still import onto it')
    status: StringProperty()


class FROSTBITE_OT_cosmetics_scan(bpy.types.Operator):
    bl_idname = 'frostbite.cosmetics_scan'
    bl_label = 'Scan characters'
    bl_description = "List the chosen game's characters and their cosmetics"

    def execute(self, context):
        p, state = prefs(context), context.scene.frostbite_cosmetics
        if not _check_paths(self, p, False):
            return {'CANCELLED'}
        game = _game(context)
        if not game or not (Path(bpy.path.abspath(game)) / GAMES[SOURCES[state.source]]).is_file():
            self.report({'ERROR'}, 'Set the %s game folder in the add-on preferences' % state.source)
            return {'CANCELLED'}
        out = Path(bpy.app.tempdir or os.environ.get('TEMP', '.')) / 'frostbite_cosmetics.json'
        proc = subprocess.run([p.python, str(BRIDGE), 'cosmetics-discover', '--tools', bpy.path.abspath(p.tools),
                               '--game', bpy.path.abspath(game), '--out', str(out)], capture_output=True, text=True,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            result = json.loads(out.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.report({'ERROR'}, 'Scan failed: ' + ((proc.stderr or '').strip().splitlines() or ['no output'])[-1])
            return {'CANCELLED'}
        if not result.get('ok'):
            self.report({'ERROR'}, 'Scan failed: ' + result.get('error', ''))
            return {'CANCELLED'}
        _data[game] = result
        state.characters.clear()
        for character in result['characters']:
            entry = state.characters.add()
            entry.name, entry.id, entry.team = character['name'], character['id'], character['team']
        state.char_index = 0
        _fill_items(state, context)
        count = sum(len(v) for c in result['characters'] for v in c['slots'].values())
        self.report({'INFO'}, 'Found %d characters, %d cosmetics' % (len(state.characters), count))
        return {'FINISHED'}


class FROSTBITE_OT_cosmetics_clear(bpy.types.Operator):
    bl_idname = 'frostbite.cosmetics_clear'
    bl_label = 'Clear selection'
    bl_description = "Untick every item of this character"

    def execute(self, context):
        for item in context.scene.frostbite_cosmetics.items:
            item.selected = False
        return {'FINISHED'}


def _socket(sockets, identifier):
    return next(s for s in sockets if s.identifier == identifier)


def _mix(tree, a, b, factor, x, y, label):
    """A colour Mix node: a -> b by factor (sockets, or a colour / number for a constant)."""
    mix = tree.nodes.new('ShaderNodeMix')
    mix.data_type, mix.blend_type, mix.label, mix.location = 'RGBA', 'MIX', label, (x, y)
    for identifier, value in (('A_Color', a), ('B_Color', b), ('Factor_Float', factor)):
        socket = _socket(mix.inputs, identifier)
        if isinstance(value, bpy.types.NodeSocket):
            tree.links.new(value, socket)
        else:
            socket.default_value = value
    return _socket(mix.outputs, 'Result_Color')


def _layer_tattoo(mat, image):
    """Lay a material's tattoo over its colour, as the games' tattoo shaders do: the Tattoo map's colour by its
    alpha, then, for silver and gold designs, the Bling colour where Tattoo_BlingMask is white. A glowing design
    (Emissive_Color_Tattoo) also emits under the tattoo. Returns whether the material has a tattoo."""
    if mat.get('frostbite_tattoo') or not mat.get('frostbite_material') or not mat.use_nodes:
        return False
    assets = Path(mat.get('frostbite_assets', ''))
    try:
        doc = json.loads((assets / mat['frostbite_material']).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    named = doc.get('named_textures') or {}
    base_rel = (doc.get('textures') or {}).get('base') or named.get('Color') or named.get('Diffuse')
    if not named.get('Tattoo') or not base_rel:
        return False
    tree = mat.node_tree
    wanted = os.path.normcase(str((assets / base_rel).resolve()))
    base = next((n for n in tree.nodes if n.type == 'TEX_IMAGE' and n.image and n.image.filepath and
                 os.path.normcase(os.path.abspath(bpy.path.abspath(n.image.filepath))) == wanted), None)
    if base is None:
        return False
    coords = base.inputs['Vector'].links[0].from_socket if base.inputs['Vector'].is_linked else None
    x, y = base.location.x, base.location.y

    def texture(rel, non_color, label, dy):
        img = image(rel, non_color)
        if img is None:
            return None
        node = tree.nodes.new('ShaderNodeTexImage')
        node.image, node.label, node.location, node.extension = img, label, (x, y + dy), base.extension
        if coords:
            tree.links.new(coords, node.inputs['Vector'])
        return node
    tattoo = texture(named['Tattoo'], False, 'Tattoo (alpha = coverage)', 300)
    if tattoo is None:
        return False
    targets = [link.to_socket for link in base.outputs['Color'].links]
    colour = _mix(tree, base.outputs['Color'], tattoo.outputs['Color'], tattoo.outputs['Alpha'], x + 300, y + 200, 'Tattoo')
    mask = texture(named['Tattoo_BlingMask'], True, 'Tattoo_BlingMask', 600) if named.get('Tattoo_BlingMask') else None
    bling = texture(named['Bling'], False, 'Bling (silver / gold)', 900) if named.get('Bling') and mask else None
    if bling:
        colour = _mix(tree, colour, bling.outputs['Color'], mask.outputs['Color'], x + 500, y + 200, 'Tattoo bling')
        # Bling is polished metal: the GW2 shader's ASM (occlusion, smoothness, metal) goes metallic under the mask.
        asm = next((i for n in tree.nodes if n.type == 'GROUP' for i in n.inputs if i.name == 'ASM'), None)
        if asm is not None:
            current = asm.links[0].from_socket if asm.is_linked else tuple(asm.default_value)
            tree.links.new(_mix(tree, current, (1.0, 0.8, 1.0, 1.0), mask.outputs['Color'], x + 500, y - 100,
                                'Tattoo bling metal'), asm)
    for socket in targets:
        tree.links.new(colour, socket)
    glow = (doc.get('vectors') or {}).get('Emissive_Color_Tattoo')
    emission = next((i for n in tree.nodes for i in n.inputs
                     if i.name in ('Emission Colour', 'Emission Color') and n.type in ('GROUP', 'BSDF_PRINCIPLED')), None)
    if glow and any(glow[:3]) and emission is not None:
        current = emission.links[0].from_socket if emission.is_linked else tuple(emission.default_value)
        tree.links.new(_mix(tree, current, (*glow[:3], 1.0), tattoo.outputs['Alpha'], x + 300, y + 500, 'Tattoo glow'),
                       emission)
        if emission.node.type == 'BSDF_PRINCIPLED' and 'Emission Strength' in emission.node.inputs:
            emission.node.inputs['Emission Strength'].default_value = max(1.0, emission.node.inputs['Emission Strength'].default_value)
    mat['frostbite_tattoo'] = named['Tattoo']
    return True


def _objects(collection):
    out = list(collection.objects)
    for child in collection.children:
        out += _objects(child)
    return out


def _move_tattoos(context, root):
    """A tattooed head or body already on a character in the scene takes the new materials, and the copy just
    imported is removed. Returns how many pieces were retextured."""
    new = _objects(root)
    moved = 0
    for ob in [o for o in new if o.type == 'MESH' and o.get('frostbite_source')]:
        if not any(s.material and s.material.get('frostbite_tattoo') for s in ob.material_slots):
            continue
        twins = [o for o in context.scene.objects if o.type == 'MESH' and o not in new
                 and o.get('frostbite_source') == ob['frostbite_source']]
        for twin in twins:
            for index, slot in enumerate(ob.material_slots):
                if index < len(twin.material_slots):
                    twin.material_slots[index].material = slot.material
        if twins:
            moved += len(twins)
            bpy.data.objects.remove(ob)
    return moved


def _prune(collection):
    """Remove collections left empty under (and including) `collection`; returns whether it was removed."""
    for child in list(collection.children):
        _prune(child)
    if not collection.objects and not collection.children:
        bpy.data.collections.remove(collection)
        return True
    return False


def _character_armature(context, game, character):
    """The armature of a character imported earlier: its rig file names the game and character."""
    pattern = re.compile(r'/%s_%s(?:_\d+)?\.json$' % (re.escape(game.lower()), re.escape(character.lower())))
    found = [o for o in context.scene.objects if o.type == 'ARMATURE' and pattern.search(o.get('frostbite_rig', '').lower())]
    active = context.active_object
    if active in found:
        return active
    return found[0] if found else (active if active and active.type == 'ARMATURE' else None)


def _apply_clips(context, armature, paths):
    """Each decoded emote clip as an Action on the armature (the pro rig's controls follow, see apply_clip);
    the first one is left playing. Returns (done, problems)."""
    from . import animation_import
    first, done, problems = None, 0, []
    for path in paths:
        try:
            action, _ = animation_import.apply_clip(context, armature, animation_import.load_clip(path))
            action.use_fake_user = True
            first = first or action
            done += 1
        except Exception as error:  # one bad clip must not lose the others
            problems.append(str(error))
    if first:
        armature.animation_data.action = first  # each Action already holds its controls' keys
        start, end = first.frame_range
        context.scene.frame_start, context.scene.frame_end = int(start), max(int(end), int(start) + 1)
    return done, problems


def _poll():
    proc = _job.get('proc')
    if proc is None:
        return None
    state = bpy.context.scene.frostbite_cosmetics if bpy.context.scene else None
    job = _job['dir']
    if proc.poll() is None:
        try:
            progress = json.loads((job / 'progress.json').read_text(encoding='utf-8'))
            message = '%s (%d/3, %ds)' % (progress['message'], progress['step'], time.time() - _job['start'])
        except (OSError, ValueError, KeyError):
            message = 'Starting...'
        if state:
            state.status = message
        _redraw()
        return 1.0
    label, rig, pro = _job['label'], _job.get('rig', True), _job.get('pro', False)
    game, character = _job.get('game', ''), _job.get('character', '')
    _job.clear()
    try:
        result = json.loads((job / 'result.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        result = {'ok': False, 'error': 'Import process exited without a result; see logs in ' + str(job)}
    try:
        if not result.get('ok'):
            raise RuntimeError(result['error'])
        importer = scene_import.Importer(result['scene'], Path(result['assets']), False, True,
                                         report=lambda level, text: print('Frostbite cosmetics:', text),
                                         rig=rig)
        root, tattoo_note = None, ''
        if result.get('meshes'):  # an emotes-only import has no meshes
            root = importer.run(bpy.context)
            tattooed = sum(_layer_tattoo(m, importer.image) for m in importer.materials.values() if m)
            moved = _move_tattoos(bpy.context, root) if tattooed else 0
            if moved and not any(o.type in ('MESH', 'ARMATURE') for o in _objects(root)):
                for ob in _objects(root):  # only the lighting is left: the character already has its own
                    bpy.data.objects.remove(ob)
            if moved:
                tattoo_note = ', tattoo applied to %d piece%s in the scene' % (moved, 's' if moved > 1 else '')
            elif tattooed:
                tattoo_note = ', %d tattooed' % tattooed
            if not _prune(root):
                root.name = label
        pro_note = ''
        if importer.armatures:
            from . import rig_tools
            for armature, _ in importer.armatures.values():
                if armature.get('frostbite_control_rig'):  # pieces added to a character that has the pro rig
                    try:
                        added = rig_tools.extend(bpy.context, armature)
                        pro_note = ', %d new controls' % added if added else ''
                    except Exception as error:
                        pro_note = ', controls for the new bones failed: %s' % error
                    continue
                if not pro:
                    continue
                if not armature.get('frostbite_control_rig'):
                    try:
                        rig_tools.build(bpy.context, armature)
                        pro_note = ', pro rig added'
                    except Exception as error:  # the import itself is done; report the rig separately
                        pro_note = ', pro rig failed: %s' % error
                        print('Frostbite pro rig failed on %s: %s' % (armature.name, error))
        clip_note = ''
        if result.get('clips'):  # emotes: onto the character just imported, or the one already in the scene
            armature = next((o for o, _ in importer.armatures.values()), None) or _character_armature(bpy.context, game, character)
            if armature is None:
                clip_note = ', emotes not applied: import the character with "Rig to skeleton" first'
            else:
                done, problems = _apply_clips(bpy.context, armature, result['clips'])
                clip_note = ', %d emote%s on %s%s' % (done, '' if done == 1 else 's', armature.name,
                                                      ' (%d failed: %s)' % (len(problems), problems[0]) if problems else '')
        note = ' (%d converter errors)' % result['errors'] if result.get('errors') else ''
        if state:
            rigged = ', rigged to %s' % ', '.join(o.name for o, _ in importer.armatures.values()) if importer.armatures else ''
            meshes = '%d meshes' % len(importer.meshes) if result.get('meshes') else 'no meshes'
            state.status = 'Done: %s%s%s%s%s%s' % (meshes, rigged, pro_note, tattoo_note, clip_note, note)
    except Exception as error:
        if state:
            state.status = 'Failed: %s' % error
        print('Frostbite cosmetics import failed:', error)
    _redraw()
    return None


class FROSTBITE_OT_cosmetics_import(bpy.types.Operator):
    bl_idname = 'frostbite.cosmetics_import'
    bl_label = 'Import selected'
    bl_description = 'Convert the ticked cosmetics (or the highlighted one) in the background and add them to this scene'

    @classmethod
    def poll(cls, context):
        state = context.scene.frostbite_cosmetics
        return not _job and len(state.items) > 0

    def execute(self, context):
        p, state = prefs(context), context.scene.frostbite_cosmetics
        if not _check_paths(self, p, True):
            return {'CANCELLED'}
        bfn = state.source == 'BFN'
        if bfn and not _check_bfn(self, p):
            return {'CANCELLED'}
        chosen = [i for i in state.items if i.selected]
        if not chosen and 0 <= state.item_index < len(state.items):
            chosen = [state.items[state.item_index]]
        if not chosen:
            self.report({'ERROR'}, 'Tick the cosmetics to import')
            return {'CANCELLED'}
        character = state.characters[state.char_index]
        stamp = time.strftime('%Y%m%d_%H%M%S')
        job = Path(bpy.path.abspath(p.work)) / 'cosmetics' / ('%s_%s_%s' % (state.source.lower(), character.id, stamp))
        job.mkdir(parents=True, exist_ok=True)
        items = [{'name': i.name, 'slot': i.slot, 'bundle': i.bundle, 'sb': i.sb} for i in chosen]
        (job / 'items.json').write_text(json.dumps(items, indent=1), encoding='utf-8')
        command = [p.python, str(BRIDGE), 'cosmetics-import', '--tools', bpy.path.abspath(p.tools),
                   '--reader', bpy.path.abspath(p.reader), '--game', bpy.path.abspath(_game(context)),
                   '--items', str(job / 'items.json'), '--job', str(job),
                   '--texture-size', context.scene.frostbite.texture_size]
        env = None
        if bfn:
            command += ['--bfn-types', bpy.path.abspath(p.bfn_types)]
            env = dict(os.environ, AURA_BFN_KEY=''.join(p.bfn_key.split()))
        _job.update(proc=subprocess.Popen(command, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)),
                    dir=job, start=time.time(), label='%s %s cosmetics' % (state.source, character.name),
                    rig=state.rigged, pro=state.rigged and state.pro_rig, game=state.source, character=character.id)
        state.status = 'Starting...'
        bpy.app.timers.register(_poll, first_interval=1.0)
        self.report({'INFO'}, 'Converting %d cosmetics into %s' % (len(items), job))
        return {'FINISHED'}


class FROSTBITE_UL_characters(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0):
        layout.label(text=item.name, icon='OUTLINER_OB_ARMATURE' if item.team == 'Zombies' else 'OUTLINER_OB_FORCE_FIELD')


class FROSTBITE_UL_cosmetics(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0):
        row = layout.row(align=True)
        row.prop(item, 'selected')
        row.label(text=item.name)
        row.label(text=_title(item.slot))

    def filter_items(self, context, data, propname):
        state = context.scene.frostbite_cosmetics
        items = getattr(data, propname)
        search = state.search.lower().replace(' ', '')
        flags = [self.bitflag_filter_item if (state.slot in ('ALL', '', i.slot) and
                                              (not search or search in i.name.lower().replace(' ', ''))) else 0
                 for i in items]
        return flags, []


class FROSTBITE_PT_cosmetics(bpy.types.Panel):
    bl_label = 'Frostbite Customizations'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Customizations'

    def draw(self, context):
        layout, state = self.layout, context.scene.frostbite_cosmetics
        layout.row(align=True).prop(state, 'source', expand=True)
        game = _game(context)
        if not game:
            layout.label(text='Set this game folder in the add-on preferences', icon='ERROR')
        layout.operator('frostbite.cosmetics_scan', icon='VIEWZOOM')
        layout.label(text='Character')
        layout.template_list('FROSTBITE_UL_characters', '', state, 'characters', state, 'char_index', rows=5)
        if state.items:
            row = layout.row(align=True)
            row.prop(state, 'slot', text='')
            row.prop(state, 'search', icon='VIEWZOOM')
            layout.template_list('FROSTBITE_UL_cosmetics', '', state, 'items', state, 'item_index', rows=8)
            ticked = sum(1 for i in state.items if i.selected)
            row = layout.row(align=True)
            row.label(text='%d ticked' % ticked if ticked else 'Tick items, or import the highlighted one')
            row.operator('frostbite.cosmetics_clear', text='', icon='X')
        layout.prop(context.scene.frostbite, 'texture_size')
        layout.prop(context.scene.frostbite, 'game_shaders')
        layout.prop(state, 'rigged')
        if state.rigged:
            row = layout.row(align=True)
            row.prop(state, 'pro_rig')
            row.operator('frostbite.make_control_rig', text='Add to selected', icon='BONE_DATA')
        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator('frostbite.cosmetics_import', icon='IMPORT')
        if state.status:
            box = layout.box()
            for line in _wrap(state.status, 40):
                box.label(text=line)
        layout.label(text='Items import in bind pose at the origin', icon='INFO')
        if state.rigged:
            layout.label(text='Pose them through the character armature', icon='ARMATURE_DATA')
        if state.slot == 'emotes' or any(i.selected and i.slot == 'emotes' for i in state.items):
            layout.label(text="Emotes become Actions on the character's armature", icon='ACTION')
        elif 'tattoo' in state.slot or any(i.selected and 'tattoo' in i.slot for i in state.items):
            layout.label(text='Tattoos retexture the piece already in the scene', icon='BRUSH_DATA')
        draw_credits(layout)


classes = (FrostbiteCharacter, FrostbiteCosmetic, FrostbiteCosmeticsState, FROSTBITE_OT_cosmetics_scan,
           FROSTBITE_OT_cosmetics_clear, FROSTBITE_OT_cosmetics_import, FROSTBITE_UL_characters,
           FROSTBITE_UL_cosmetics, FROSTBITE_PT_cosmetics)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.frostbite_cosmetics = bpy.props.PointerProperty(type=FrostbiteCosmeticsState)


def unregister():
    del bpy.types.Scene.frostbite_cosmetics
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
