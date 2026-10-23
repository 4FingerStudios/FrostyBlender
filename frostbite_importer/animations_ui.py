"""Animations tab: browse a game's characters and their clips, and put the chosen clips on a
rigged character (an armature from the Customizations tab with "Rig to skeleton" on).

Every clip becomes its own Action; the first one is assigned, the rest are kept (with a fake
user) for the Action Editor or NLA.
"""
import json
import os
import subprocess
import time
from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, PointerProperty, StringProperty

from . import BRIDGE, GAMES, game_ok, _check_paths, _redraw, _wrap, animation_import, draw_credits, prefs

SOURCES = {'GW1': 'gw1_path', 'GW2': 'gw2_path', 'BFN': 'bfn_path'}
_data = {}  # game folder -> discover result
_job = {}   # the running clip export


def _game(context):
    return getattr(prefs(context), SOURCES[context.scene.frostbite_animations.source], '') or ''


class FrostbiteAnimCharacter(bpy.types.PropertyGroup):
    name: StringProperty()
    rig: StringProperty()
    count: IntProperty()
    model: StringProperty(description='The model these clips animate')
    model_tab: StringProperty(description='The sidebar tab that imports it')


class FrostbiteClip(bpy.types.PropertyGroup):
    name: StringProperty()
    key: StringProperty()
    frames: IntProperty()
    fps: bpy.props.FloatProperty()
    additive: BoolProperty()
    face: BoolProperty()
    face_only: BoolProperty()
    banks: StringProperty()
    selected: BoolProperty(name='', description='Import this clip')


def _fill_clips(state, context):
    state.clips.clear()
    state.clip_index = 0
    data = _data.get(_game(context))
    if not data or not 0 <= state.char_index < len(state.characters):
        return
    rig = state.characters[state.char_index].rig
    character = next((c for c in data['characters'] if c['rig'] == rig), None)
    for clip in (character or {}).get('clips', []):
        entry = state.clips.add()
        entry.name, entry.key, entry.frames = clip['name'], clip['key'], clip.get('frames', 0)
        entry.fps, entry.additive = clip.get('fps', 30.0), clip.get('additive', False)
        entry.face, entry.face_only = clip.get('face', False), clip.get('face_only', False)
        entry.banks = clip.get('banks') or ''


def _reset(state, context):
    state.characters.clear()
    state.clips.clear()
    state.status = ''


def _is_armature(self, obj):
    return obj.type == 'ARMATURE'


class FrostbiteAnimationsState(bpy.types.PropertyGroup):
    source: EnumProperty(name='Game', default='GW2', update=_reset, items=[
        ('GW1', 'GW1', 'Garden Warfare 1'), ('GW2', 'GW2', 'Garden Warfare 2'), ('BFN', 'BFN', 'Battle for Neighborville')])
    characters: CollectionProperty(type=FrostbiteAnimCharacter)
    char_index: IntProperty(update=_fill_clips)
    clips: CollectionProperty(type=FrostbiteClip)
    clip_index: IntProperty()
    search: StringProperty(name='', description='Filter clips by name', options={'TEXTEDIT_UPDATE'})
    kind: EnumProperty(name='Clips', default='ALL', items=[
        ('ALL', 'All', 'Body and facial clips'), ('BODY', 'Body', 'Body clips only'),
        ('FACE', 'Face', 'Facial clips: expressions and talking. One that moves only the face plays over the body clip')])
    hide_additive: BoolProperty(name='Hide additive', default=True,
                                description='Hide additive layers (aim offsets, leans), which only look right blended onto a base clip')
    target: PointerProperty(name='Armature', type=bpy.types.Object, poll=_is_armature,
                            description='Rigged character to animate; defaults to the active armature')
    set_fps: BoolProperty(name='Set scene FPS', default=True, description="Match the scene frame rate to the clip's")
    status: StringProperty()


def _target(context):
    state = context.scene.frostbite_animations
    if state.target and state.target.type == 'ARMATURE':
        return state.target
    obj = context.active_object
    return obj if obj and obj.type == 'ARMATURE' else None


class FROSTBITE_OT_animations_scan(bpy.types.Operator):
    bl_idname = 'frostbite.animations_scan'
    bl_label = 'Scan characters'
    bl_description = "List the chosen game's characters and their animation clips (the first scan of a game takes about a minute)"

    def execute(self, context):
        p, state = prefs(context), context.scene.frostbite_animations
        if not _check_paths(self, p, False):
            return {'CANCELLED'}
        game = _game(context)
        if not game_ok(game, GAMES[SOURCES[state.source]]):
            self.report({'ERROR'}, 'Set the %s game folder in the add-on preferences' % state.source)
            return {'CANCELLED'}
        out = Path(bpy.app.tempdir or os.environ.get('TEMP', '.')) / 'frostbite_animations.json'
        proc = subprocess.run([p.python, str(BRIDGE), 'animations-discover', '--tools', bpy.path.abspath(p.tools),
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
        for c in result['characters']:
            entry = state.characters.add()
            entry.name, entry.rig, entry.count = c['name'], c['rig'], len(c['clips'])
            if c.get('model'):
                entry.model = c['model']['name']
                entry.model_tab = 'Customizations' if c['model']['section'] == 'Characters' else 'NPCs'
        state.char_index = 0
        _fill_clips(state, context)
        self.report({'INFO'}, 'Found %d characters, %d clips' % (len(state.characters), sum(c.count for c in state.characters)))
        return {'FINISHED'}


class FROSTBITE_OT_animations_clear(bpy.types.Operator):
    bl_idname = 'frostbite.animations_clear'
    bl_label = 'Clear selection'

    def execute(self, context):
        for clip in context.scene.frostbite_animations.clips:
            clip.selected = False
        return {'FINISHED'}


def _poll():
    proc = _job.get('proc')
    if proc is None:
        return None
    context = bpy.context
    state = context.scene.frostbite_animations if context.scene else None
    job = _job['dir']
    if proc.poll() is None:
        if state:
            state.status = 'Decoding clips (%ds)' % (time.time() - _job['start'])
        _redraw()
        return 1.0
    armature_name, set_fps = _job['armature'], _job['set_fps']
    _job.clear()
    try:
        result = json.loads((job / 'result.json').read_text(encoding='utf-8'))
        if not result.get('ok'):
            raise RuntimeError(result['error'])
        armature = bpy.data.objects.get(armature_name)
        if armature is None:
            raise RuntimeError('The armature %s is gone' % armature_name)
        first, done, problems, clips = None, 0, [], []
        for path in result['clips']:
            try:
                clips.append(animation_import.load_clip(path))
            except Exception as error:
                problems.append(str(error))
        # Body clips first: face clips then play over the first of them.
        clips.sort(key=lambda c: bool(c.get('face_only')))
        for clip in clips:
            try:
                if clip.get('face_only') and first:
                    armature.animation_data.action = first
                action, matched = animation_import.apply_clip(context, armature, clip, set_fps)
                action.use_fake_user = True
                first = first or action
                done += 1
            except Exception as error:
                problems.append(str(error))
        if first:
            # Show the first clip; apply_clip set the frame range for the last one.
            armature.animation_data.action = first
            start, end = first.frame_range
            context.scene.frame_start, context.scene.frame_end = int(start), max(int(end), int(start) + 1)
        failed = result.get('errors', 0) + len(problems)
        if state:
            state.status = 'Done: %d clips on %s%s' % (done, armature.name,
                                                     ' (%d failed: %s)' % (failed, (problems or [d.get('error', '') for d in result.get('error_details', [])] or [''])[0]) if failed else '')
    except Exception as error:
        if state:
            state.status = 'Failed: %s' % error
        print('Frostbite animations import failed:', error)
    _redraw()
    return None


class FROSTBITE_OT_animations_import(bpy.types.Operator):
    bl_idname = 'frostbite.animations_import'
    bl_label = 'Import selected'
    bl_description = 'Decode the ticked clips (or the highlighted one) and add each as an Action on the armature'

    @classmethod
    def poll(cls, context):
        return not _job and len(context.scene.frostbite_animations.clips) > 0

    def execute(self, context):
        p, state = prefs(context), context.scene.frostbite_animations
        if not _check_paths(self, p, False):
            return {'CANCELLED'}
        armature = _target(context)
        if armature is None:
            self.report({'ERROR'}, 'Select a rigged character (import it from the Customizations tab with "Rig to skeleton")')
            return {'CANCELLED'}
        chosen = [c for c in state.clips if c.selected]
        if not chosen and 0 <= state.clip_index < len(state.clips):
            chosen = [state.clips[state.clip_index]]
        if not chosen:
            self.report({'ERROR'}, 'Tick the clips to import')
            return {'CANCELLED'}
        character = state.characters[state.char_index]
        job = Path(bpy.path.abspath(p.work)) / 'animations' / ('%s_%s_%s' % (
            state.source.lower(), character.rig.lower(), time.strftime('%Y%m%d_%H%M%S')))
        job.mkdir(parents=True, exist_ok=True)
        (job / 'items.json').write_text(json.dumps([{'name': c.name, 'key': c.key, 'banks': c.banks or None}
                                                    for c in chosen]), encoding='utf-8')
        command = [p.python, str(BRIDGE), 'animations-import', '--tools', bpy.path.abspath(p.tools),
                   '--game', bpy.path.abspath(_game(context)), '--items', str(job / 'items.json'), '--job', str(job)]
        _job.update(proc=subprocess.Popen(command, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)),
                    dir=job, start=time.time(), armature=armature.name, set_fps=state.set_fps)
        state.status = 'Starting...'
        bpy.app.timers.register(_poll, first_interval=1.0)
        self.report({'INFO'}, 'Decoding %d clips for %s' % (len(chosen), armature.name))
        return {'FINISHED'}


class FROSTBITE_UL_anim_characters(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0):
        row = layout.row()
        row.label(text=item.name, icon='ARMATURE_DATA')
        row.label(text=str(item.count))


class FROSTBITE_UL_clips(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0):
        row = layout.row(align=True)
        row.prop(item, 'selected')
        row.label(text=item.name + (' (additive)' if item.additive else ''), icon='MONKEY' if item.face else 'NONE')
        row.label(text='%d f' % item.frames)

    def filter_items(self, context, data, propname):
        state = context.scene.frostbite_animations
        search = state.search.lower().replace(' ', '')
        flags = [self.bitflag_filter_item if ((not search or search in c.name.lower().replace(' ', '').replace('_', '')
                                                or search in c.name.lower()) and not (state.hide_additive and c.additive)
                                               and state.kind in ('ALL', 'FACE' if c.face else 'BODY')) else 0
                 for c in getattr(data, propname)]
        return flags, []


class FROSTBITE_PT_animations(bpy.types.Panel):
    bl_label = 'Frostbite Animations'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Animations'

    def draw(self, context):
        layout, state = self.layout, context.scene.frostbite_animations
        layout.row(align=True).prop(state, 'source', expand=True)
        if not _game(context):
            layout.label(text='Set this game folder in the add-on preferences', icon='ERROR')
        layout.operator('frostbite.animations_scan', icon='VIEWZOOM')
        layout.label(text='Character')
        layout.template_list('FROSTBITE_UL_anim_characters', '', state, 'characters', state, 'char_index', rows=5)
        if 0 <= state.char_index < len(state.characters) and state.characters[state.char_index].model:
            character = state.characters[state.char_index]
            box = layout.box()
            box.label(text='Model: %s' % character.model, icon='ARMATURE_DATA')
            box.label(text='Import it rigged from the %s tab first' % character.model_tab)
        if state.clips:
            row = layout.row(align=True)
            row.prop(state, 'search', icon='VIEWZOOM')
            row.prop(state, 'hide_additive', text='', icon='HIDE_ON' if state.hide_additive else 'HIDE_OFF')
            if any(c.face for c in state.clips):
                layout.row(align=True).prop(state, 'kind', expand=True)
            layout.template_list('FROSTBITE_UL_clips', '', state, 'clips', state, 'clip_index', rows=10)
            ticked = sum(1 for c in state.clips if c.selected)
            row = layout.row(align=True)
            row.label(text='%d ticked' % ticked if ticked else 'Tick clips, or import the highlighted one')
            row.operator('frostbite.animations_clear', text='', icon='X')
        layout.prop(state, 'target')
        target = _target(context)
        layout.label(text='On: %s' % target.name if target else 'Select a rigged character', icon='ARMATURE_DATA' if target else 'ERROR')
        layout.prop(state, 'set_fps')
        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator('frostbite.animations_import', icon='ACTION')
        if state.status:
            box = layout.box()
            for line in _wrap(state.status, 40):
                box.label(text=line)
        from . import rig_tools
        rig_tools.draw(layout, context)
        draw_credits(layout)


classes = (FrostbiteAnimCharacter, FrostbiteClip, FrostbiteAnimationsState, FROSTBITE_OT_animations_scan,
           FROSTBITE_OT_animations_clear, FROSTBITE_OT_animations_import, FROSTBITE_UL_anim_characters,
           FROSTBITE_UL_clips, FROSTBITE_PT_animations)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.frostbite_animations = bpy.props.PointerProperty(type=FrostbiteAnimationsState)


def unregister():
    del bpy.types.Scene.frostbite_animations
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
