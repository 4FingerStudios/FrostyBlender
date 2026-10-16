"""NPCs tab: browse a game's AI characters (horde, bosses, deployables, vehicles) with their
costumes, props, weapons and abilities, and spawn the chosen ones into the scene.

Items import like customizations, in bind pose and rigged to the NPC's own skeleton; an NPC
imported again reuses its armature, so pieces added later move with it. With "Spawn at 3D
cursor" the new pieces are moved to the cursor.
"""
import json
import os
import subprocess
import time
from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty

from . import BRIDGE, GAMES, _check_bfn, _check_paths, _redraw, _wrap, draw_credits, prefs, scene_import

SOURCES = {'GW1': 'gw1_path', 'GW2': 'gw2_path', 'BFN': 'bfn_path'}
_data = {}   # game folder -> discover result
_job = {}    # the running NPC import
_slots = []  # EnumProperty items must stay referenced while Blender shows them


def _game(context):
    state = context.scene.frostbite_npcs
    return getattr(prefs(context), SOURCES[state.source], '') or ''


def _title(text):
    return ' '.join(w.capitalize() for w in text.split('_'))


class FrostbiteNpc(bpy.types.PropertyGroup):
    name: StringProperty()
    id: StringProperty()
    team: StringProperty()
    group: StringProperty()


class FrostbiteNpcItem(bpy.types.PropertyGroup):
    name: StringProperty()
    slot: StringProperty()
    bundle: StringProperty()
    sb: StringProperty()
    selected: BoolProperty(name='', description='Include this item in the import')


def _fill_items(state, context):
    """Show the active NPC's items."""
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


class FrostbiteNpcsState(bpy.types.PropertyGroup):
    source: EnumProperty(name='Game', default='GW2', update=_reset, items=[
        ('GW1', 'GW1', 'Garden Warfare 1'), ('GW2', 'GW2', 'Garden Warfare 2'), ('BFN', 'BFN', 'Battle for Neighborville')])
    characters: CollectionProperty(type=FrostbiteNpc)
    char_index: IntProperty(update=_fill_items)
    items: CollectionProperty(type=FrostbiteNpcItem)
    item_index: IntProperty()
    slot: EnumProperty(name='Slot', items=_slot_items)
    search: StringProperty(name='', description='Filter items by name', options={'TEXTEDIT_UPDATE'})
    rigged: BoolProperty(name='Rig to skeleton', default=True,
                         description="Bind the items to an armature of the character's skeleton, ready to pose")
    spawn_at_cursor: BoolProperty(name='Spawn at 3D cursor', default=True,
                                  description='Move newly imported NPCs to the 3D cursor; pieces added to an NPC already in the scene stay on its rig')
    status: StringProperty()


class FROSTBITE_OT_npcs_scan(bpy.types.Operator):
    bl_idname = 'frostbite.npcs_scan'
    bl_label = 'Scan NPCs'
    bl_description = "List the chosen game's NPCs and their costumes, props, weapons and abilities"

    def execute(self, context):
        p, state = prefs(context), context.scene.frostbite_npcs
        if not _check_paths(self, p, False):
            return {'CANCELLED'}
        game = _game(context)
        if not game or not (Path(bpy.path.abspath(game)) / GAMES[SOURCES[state.source]]).is_file():
            self.report({'ERROR'}, 'Set the %s game folder in the add-on preferences' % state.source)
            return {'CANCELLED'}
        out = Path(bpy.app.tempdir or os.environ.get('TEMP', '.')) / 'frostbite_npcs.json'
        proc = subprocess.run([p.python, str(BRIDGE), 'npcs-discover', '--tools', bpy.path.abspath(p.tools),
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
            entry.name, entry.id, entry.team, entry.group = character['name'], character['id'], character['team'], character.get('group', '')
        state.char_index = 0
        _fill_items(state, context)
        count = sum(len(v) for c in result['characters'] for v in c['slots'].values())
        self.report({'INFO'}, 'Found %d NPCs, %d items' % (len(state.characters), count))
        return {'FINISHED'}


class FROSTBITE_OT_npcs_clear(bpy.types.Operator):
    bl_idname = 'frostbite.npcs_clear'
    bl_label = 'Clear selection'
    bl_description = "Untick every item of this character"

    def execute(self, context):
        for item in context.scene.frostbite_npcs.items:
            item.selected = False
        return {'FINISHED'}


def _spawn(root, before, cursor):
    """Move the new top-level objects of an import to the cursor. Meshes parented to an armature
    that was already in the scene stay where that rig puts them."""
    def objects(col):
        yield from col.objects
        for child in col.children:
            yield from objects(child)
    for ob in set(objects(root)):
        if ob.parent is None and ob not in before:
            ob.location = ob.location + cursor


def _poll():
    proc = _job.get('proc')
    if proc is None:
        return None
    state = bpy.context.scene.frostbite_npcs if bpy.context.scene else None
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
    label, rig, cursor = _job['label'], _job.get('rig', True), _job.get('cursor')
    _job.clear()
    try:
        result = json.loads((job / 'result.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        result = {'ok': False, 'error': 'Import process exited without a result; see logs in ' + str(job)}
    try:
        if not result.get('ok'):
            raise RuntimeError(result['error'])
        importer = scene_import.Importer(result['scene'], Path(result['assets']), False, True,
                                         report=lambda level, text: print('Frostbite NPCs:', text),
                                         rig=rig)
        before = {o for o in bpy.context.scene.objects if o.type == 'ARMATURE'}
        root = importer.run(bpy.context)
        if root is not None:
            root.name = label
            if cursor:
                _spawn(root, before, cursor)
        note = ' (%d converter errors)' % result['errors'] if result.get('errors') else ''
        if state:
            rigged = ', rigged to %s' % ', '.join(o.name for o, _ in importer.armatures.values()) if importer.armatures else ''
            state.status = 'Done: %d meshes%s%s' % (len(importer.meshes), rigged, note)
    except Exception as error:
        if state:
            state.status = 'Failed: %s' % error
        print('Frostbite NPC import failed:', error)
    _redraw()
    return None


class FROSTBITE_OT_npcs_import(bpy.types.Operator):
    bl_idname = 'frostbite.npcs_import'
    bl_label = 'Import selected'
    bl_description = 'Convert the ticked NPC items (or the highlighted one) in the background and spawn them in this scene'

    @classmethod
    def poll(cls, context):
        state = context.scene.frostbite_npcs
        return not _job and len(state.items) > 0

    def execute(self, context):
        p, state = prefs(context), context.scene.frostbite_npcs
        if not _check_paths(self, p, True):
            return {'CANCELLED'}
        bfn = state.source == 'BFN'
        if bfn and not _check_bfn(self, p):
            return {'CANCELLED'}
        chosen = [i for i in state.items if i.selected]
        if not chosen and 0 <= state.item_index < len(state.items):
            chosen = [state.items[state.item_index]]
        if not chosen:
            self.report({'ERROR'}, 'Tick the items to import')
            return {'CANCELLED'}
        character = state.characters[state.char_index]
        stamp = time.strftime('%Y%m%d_%H%M%S')
        job = Path(bpy.path.abspath(p.work)) / 'npcs' / ('%s_%s_%s' % (state.source.lower(), character.id, stamp))
        job.mkdir(parents=True, exist_ok=True)
        items = [{'name': i.name, 'slot': i.slot, 'bundle': i.bundle, 'sb': i.sb, 'character': character.id} for i in chosen]
        (job / 'items.json').write_text(json.dumps(items, indent=1), encoding='utf-8')
        command = [p.python, str(BRIDGE), 'npcs-import', '--tools', bpy.path.abspath(p.tools),
                   '--reader', bpy.path.abspath(p.reader), '--game', bpy.path.abspath(_game(context)),
                   '--items', str(job / 'items.json'), '--job', str(job),
                   '--texture-size', context.scene.frostbite.texture_size]
        env = None
        if bfn:
            command += ['--bfn-types', bpy.path.abspath(p.bfn_types)]
            env = dict(os.environ, AURA_BFN_KEY=''.join(p.bfn_key.split()))
        _job.update(proc=subprocess.Popen(command, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)),
                    dir=job, start=time.time(), label='%s %s' % (state.source, character.name),
                    rig=state.rigged, cursor=state.spawn_at_cursor and context.scene.cursor.location.copy())
        state.status = 'Starting...'
        bpy.app.timers.register(_poll, first_interval=1.0)
        self.report({'INFO'}, 'Converting %d NPC items into %s' % (len(items), job))
        return {'FINISHED'}


class FROSTBITE_UL_npcs(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0):
        row = layout.row()
        row.label(text=item.name, icon={'Zombies': 'OUTLINER_OB_ARMATURE', 'Gnomes': 'GHOST_ENABLED'}.get(item.team, 'OUTLINER_OB_FORCE_FIELD'))
        row.label(text=item.group)


class FROSTBITE_UL_npc_items(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0):
        row = layout.row(align=True)
        row.prop(item, 'selected')
        row.label(text=item.name)
        row.label(text=_title(item.slot))

    def filter_items(self, context, data, propname):
        state = context.scene.frostbite_npcs
        items = getattr(data, propname)
        search = state.search.lower().replace(' ', '')
        flags = [self.bitflag_filter_item if (state.slot in ('ALL', '', i.slot) and
                                              (not search or search in i.name.lower().replace(' ', ''))) else 0
                 for i in items]
        return flags, []


class FROSTBITE_PT_npcs(bpy.types.Panel):
    bl_label = 'Frostbite NPCs'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'NPCs'

    def draw(self, context):
        layout, state = self.layout, context.scene.frostbite_npcs
        layout.row(align=True).prop(state, 'source', expand=True)
        game = _game(context)
        if not game:
            layout.label(text='Set this game folder in the add-on preferences', icon='ERROR')
        layout.operator('frostbite.npcs_scan', icon='VIEWZOOM')
        layout.label(text='NPC')
        layout.template_list('FROSTBITE_UL_npcs', '', state, 'characters', state, 'char_index', rows=5)
        if state.items:
            row = layout.row(align=True)
            row.prop(state, 'slot', text='')
            row.prop(state, 'search', icon='VIEWZOOM')
            layout.template_list('FROSTBITE_UL_npc_items', '', state, 'items', state, 'item_index', rows=8)
            ticked = sum(1 for i in state.items if i.selected)
            row = layout.row(align=True)
            row.label(text='%d ticked' % ticked if ticked else 'Tick items, or import the highlighted one')
            row.operator('frostbite.npcs_clear', text='', icon='X')
        layout.prop(context.scene.frostbite, 'texture_size')
        layout.prop(state, 'rigged')
        layout.prop(state, 'spawn_at_cursor')
        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator('frostbite.npcs_import', icon='IMPORT')
        if state.status:
            box = layout.box()
            for line in _wrap(state.status, 40):
                box.label(text=line)
        layout.label(text='Add a body first; props, weapons and abilities attach to its rig', icon='INFO')
        if state.rigged:
            layout.label(text='Pose them through the character armature', icon='ARMATURE_DATA')
        draw_credits(layout)


classes = (FrostbiteNpc, FrostbiteNpcItem, FrostbiteNpcsState, FROSTBITE_OT_npcs_scan,
           FROSTBITE_OT_npcs_clear, FROSTBITE_OT_npcs_import, FROSTBITE_UL_npcs,
           FROSTBITE_UL_npc_items, FROSTBITE_PT_npcs)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.frostbite_npcs = bpy.props.PointerProperty(type=FrostbiteNpcsState)


def unregister():
    del bpy.types.Scene.frostbite_npcs
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
