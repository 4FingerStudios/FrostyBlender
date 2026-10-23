"""Character cosmetics (customizations) of Garden Warfare 1 and 2 and Battle for Neighborville.

Every cosmetic ships in its own unlock bundle, so the character/slot/item list comes straight
from bundle names in the superbundle TOCs, and an import reads only the chosen bundles. Meshes
are skinned; they are converted in their bind pose and placed at the origin, which is where
the character's skeleton expects them, so a body, head and hat chosen together line up. The
export also takes each character's SkeletonAsset, and the conversion writes it as a skeleton
JSON with a .skin file of bone weights beside every skinned mesh, so they can be rigged.

    python cosmetics.py discover --game GAME --out items.json
    python cosmetics.py export --game GAME --items items.json --out EXPORT [--names BFN_TYPES]
    python cosmetics.py convert --export EXPORT --textures TEXTURES --assets ASSETS --out scene.json

BFN also needs AURA_BFN_KEY, as for maps. Game files are only read.
"""
import argparse
import json
import os
import re
import struct
import sys
from pathlib import Path

import gw2

GW1_EXE, GW2_EXE, BFN_EXE = 'PVZ.Main_Win64_Retail.exe', 'GW2.Main_Win64_Retail.exe', 'PVZBattleforNeighborville.exe'
# Bundle names: team, character, slot, file. Slots that only hold textures or UI are skipped.
PATTERNS = {
    'gw1': re.compile(r'^(?:win32|xenon)/_pvz/animations/characters/(plants|zombies)/([^/]+)/(?:attachments/)?([^/]+)/([^/]+?)_unlockasset_bpb$'),
    'gw2': re.compile(r'^win32/characters/(plants|zombie)/playable/([^/]+)/([^/]+)/([^/]+?)_(?:att)?unlockasset_bpb$'),
    'bfn': re.compile(r'^win32/characters(?:_cu\d+)?/(plant|zombie)/playable/([^/]+)/([^/]+)/(?:[^/]+/)*([^/]+?)_(?:visualasset|unlockasset)_bpb$'),
}
# Tattoo and emote slots hold no meshes of their own: tattoos come from their visual unlocks (TATTOOS) and
# emotes from the animation clips (emotes()) when discover() is asked for extras.
SKIPPED_SLOTS = {'tattoo', 'tattoos', 'textures', 'emote', 'emotes', 'ui', 'animation', 'animations',
                 'spheretattoo', 'capetattoo', 'tattoohead'}
MESH_TYPES = ('SkinnedMeshAsset', 'RigidMeshAsset', 'CompositeMeshAsset')
# Tattoos are material variations of a head or body mesh, one visual unlock per design and character
# variant: GW2 <char>_tattoo_<design>[silver|gold]_<variant>, GW1 <char>_head_<head>_<design>. GW2 also
# tattoos Citron's sphere form, Super Brainz's cape and Kernel Corn's head apart from the body.
TATTOOS = {
    'gw1': re.compile(r'^(?:win32|xenon)/_pvz/animations/characters/(plants|zombies)/([^/]+)/(?:attachments/)?(tattoos?)/([^/]+?)_visunlockasset_bpb$'),
    'gw2': re.compile(r'^win32/characters/(plants|zombie)/playable/([^/]+)/(tattoos?|spheretattoo|capetattoo|tattoohead)/([^/]+?)_visunlockasset_bpb$'),
}
TATTOO_SLOTS = {'spheretattoo': 'sphere_tattoos', 'capetattoo': 'cape_tattoos', 'tattoohead': 'head_tattoos'}
# Emotes (the games' gestures and taunts) are animation clips: items name them as clip:<key>:<banks>.
EMOTE = re.compile(r'gesture|taunt|(?<!r)emote|heroic_pose', re.I)
EMOTE_WORD = re.compile(r'gesture|taunt|emote|heroic', re.I)  # the word of a clip name that starts the emote's own name
NPC_CLIP = re.compile(r'(?:^|_)npc(?:_|$)', re.I)
CLIP = 'clip:'


def game_kind(game):
    game = Path(game)
    for kind, exe in (('gw1', GW1_EXE), ('gw2', GW2_EXE), ('bfn', BFN_EXE)):
        if (game / exe).is_file():
            return kind
    import gw1_x360
    if gw1_x360.is_view(game):  # an Xbox 360 GW1 disc and its packs, seen through their view
        return 'gw1'
    raise ValueError('Not a Garden Warfare 1, Garden Warfare 2 or Battle for Neighborville folder: %s' % game)


def title(text):
    return ' '.join(w.capitalize() for w in text.replace('-', '_').split('_') if w)


def character_of(kind, bundle):
    match = PATTERNS[kind].match(bundle.lower())
    return match.group(2) if match else None


def item_character(kind, item):
    """The character whose skeleton an item binds to: its own (NPCs name it) or its bundle's."""
    if kind == 'gw1' and '/browncoatzombie/costumes/browncoatzombie_costumedisco' in (item.get('bundle') or '').lower():
        return 'discozombie'  # Shares Browncoat's folder, but has its own authored bind pose.
    return item.get('character') or character_of(kind, item.get('bundle') or '')


# NPCs whose skeleton sits in another character's folder: tried in order after their own.
SKELETON_FALLBACKS = {'browncoat': ['browncoatzombie'], 'berzerkerzombie': ['browncoatzombie'],
                      'gravedigger': ['browncoatzombie'],
                      'goop_shroom': ['fume_shroom'], 'peashooter_turret_fire': ['peashooter_turret_cannon'],
                      'peashooter_turret_ice': ['peashooter_turret_cannon'],
                      'peashooter_turret_repeater': ['peashooter_turret_gatling'],
                      'allstarimp': ['gargantuanimp'], 'yeti_imp': ['gargantuanimp'], 'yetiimp': ['simpleimp'],
                      'pirate_imp': ['gargantuanimp', 'allstarimp'], 'mech_zombie_imp': ['gargantuanimp'],
                      'gigatorchwood': ['torchwood'], 'squash': ['spookysquash', 'gigasquash']}


# Skeletons exported per character; a mesh is bound to the first one its skin fits.
MAX_SKELETONS = 3


def skeleton_candidates(names, character, root=None):
    """pick_skeleton for a character, then for the characters whose skeleton it shares."""
    fallbacks = SKELETON_FALLBACKS.get(character, [])
    if 'gnome' in character and character != 'gnome':
        fallbacks = fallbacks + ['gnome']
    found = [n for name in [character] + fallbacks for n in pick_skeleton(names, name, root)]
    if root:  # a skeleton from the item's own folder family first, whichever name finds it
        found.sort(key=lambda n: not n.lower().startswith(root + '/'))
    return found


def pick_skeleton(names, character, root=None):
    """The character's own SkeletonAsset among EBX names: in its folder, not a master skeleton,
    preferring one under the item's own top folder (BFN keeps GW2's characters beside its own),
    then <character>_skeleton or <character>_default_skeleton, then the shortest path."""
    preferred = ('%s_skeleton' % character, '%s_default_skeleton' % character)
    found = [n for n in names if n.lower().endswith('skeleton')
             and ('/%s/' % character in n.lower() or n.lower().rsplit('/', 1)[-1] in preferred)
             and 'master' not in n.lower().rsplit('/', 1)[-1]]
    if not found:  # only a master skeleton ships for some (the Tall-nut, BFN's Potato Mine)
        found = [n for n in names if n.lower().endswith('skeleton') and '/%s/' % character in n.lower()]
    found.sort(key=lambda n: (bool(root) and not n.lower().startswith(root + '/'),
                              n.lower().rsplit('/', 1)[-1] not in preferred, len(n), n))
    return found


def gw_skeletons(game, output, inventory, characters):
    """Add each character's SkeletonAsset (from the shared character bundles) to a GW1/GW2 export."""
    if not characters:
        return {}
    indexes = gw2.cached_dependency_index(game)
    by_name = {}
    for key, location in indexes['ebx'].items():
        name = location[3]['name']
        if name.lower().endswith('skeleton'):
            by_name.setdefault(name, location)
    chosen, options = {}, inventory.setdefault('skeleton_options', {})
    for character in sorted(characters):
        for name in skeleton_candidates(by_name, character):
            if len(options.get(character, ())) >= MAX_SKELETONS:
                break
            path, offset, size, source = by_name[name]
            with path.open('rb') as stream:
                stream.seek(offset)
                content = gw2.decompress(gw2.Reader(stream.read(size)), source['original_size'])
            document = gw2.Ebx(content).document()
            if document['root_type'] != 'SkeletonAsset':
                continue
            asset = dict(source, path='ebx/%s.bin' % source['sha1'], document='ebx/%s.json' % source['sha1'],
                         bytes=len(content), root_type=document['root_type'], guid=document['guid'])
            (output / asset['path']).parent.mkdir(parents=True, exist_ok=True)
            (output / asset['path']).write_bytes(content)
            (output / asset['document']).write_text(json.dumps(document), encoding='utf-8')
            inventory['assets'].append(asset)
            chosen.setdefault(character, name)
            options.setdefault(character, []).append(name)
            print('Skeleton:', name, flush=True)
        if character not in chosen:
            inventory['errors'].append({'stage': 'skeleton', 'name': character, 'error': 'No SkeletonAsset found'})
    return chosen


def bundle_root(bundle):
    """Win32/GW2_Characters/... -> gw2_characters; Win32/characters_cu3/... -> characters. An item given by
    mesh name ('asset:gw2_characters/...') takes the mesh's top folder."""
    if bundle.lower().startswith('asset:'):
        return re.sub(r'_cu\d+$', '', bundle[6:].lower().split('/')[0])
    parts = bundle.lower().split('/')
    return re.sub(r'_cu\d+$', '', parts[1]) if len(parts) > 2 else None


def variation_index(game, kind):
    """Mesh GUID -> the MeshVariationDatabases that list it, cached per install. Meshes exported by name
    (abilities and other skeleton-only characters) get their textures from these, not from a bundle."""
    import hashlib
    key = hashlib.sha256(('%s|%s|variations2' % (Path(game).resolve(), kind)).encode()).hexdigest()[:16]
    base = Path(os.environ.get('AURA_IMPORT_CACHE') or Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'aura-frostbite')
    path = base / ('variations_%s_%s.json' % (kind, key))
    if path.is_file():
        return json.loads(path.read_text(encoding='utf-8'))
    index = gw2.cached_dependency_index(Path(game))
    out = {}
    for location in index['ebx'].values():
        name = location[3]['name']
        if 'meshvariationdb' not in name.lower():
            continue
        file, offset, size, source = location
        try:
            with file.open('rb') as stream:
                stream.seek(offset)
                content = gw2.decompress(gw2.Reader(stream.read(size)), source['original_size'])
            imports = {i['file'] for i in gw2.Ebx(content).document()['imports']}
        except (ValueError, OSError, struct.error, IndexError):
            continue
        for guid in imports:
            out.setdefault(guid, []).append(name)
    for guid, names in out.items():  # the ability's own weapon table first, then the smallest
        names.sort(key=lambda n: ('/weapons/' not in n.lower(), len(n)))
        del names[3:]
    base.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out), encoding='utf-8')
    return out


def _asset_items(items):
    """Items that name a mesh EBX instead of an unlock bundle (abilities, skeleton-only characters)."""
    return [i for i in items if i.get('asset') and i.get('bundle', '').lower().startswith('asset:')]


def bfn_skeletons(game, characters):
    """Names of the Battle for Neighborville characters' SkeletonAssets, from the bundle manifests."""
    import bfn
    if not characters:
        return {}
    archive = bfn.Archive(game)
    names = set()
    for sb in archive.sbs:
        if 'character' not in sb.lower():
            continue
        for entry in archive.toc(sb)['bundles'].values():
            try:
                listed = archive.bundle(entry)
            except (ValueError, struct.error, IndexError):
                continue
            names.update(a['name'] for a in listed if a['kind'] == 'ebx' and a['name'].lower().endswith('skeleton'))
    return {c: found[:MAX_SKELETONS] for c, root in characters.items() for found in [skeleton_candidates(names, c, root)] if found}


def bundle_names(game, kind):
    """(bundle name, superbundle) for every bundle the cosmetics could live in."""
    game = Path(game)
    if kind == 'bfn':
        import bfn
        archive = bfn.Archive(game)
        return [(entry[0], sb) for sb in archive.sbs if 'character' in sb.lower()
                for entry in archive.toc(sb)['bundles'].values()]
    out = {}
    for root in gw2.data_roots(game):
        for toc in sorted((root / 'Win32').glob('*.toc')):
            if 'chunk' in toc.stem.lower():
                continue
            if root != game / 'Data' and not gw2.patch_applies(game, toc):
                continue  # a PC patch over Xenon superbundles
            sb = toc.relative_to(root).with_suffix('').as_posix()
            for entry in gw2.read_db(toc).get('bundles', []):
                out.setdefault(entry['id'].lower(), (entry['id'], sb))
    return list(out.values())


def tattoo_name(kind, character, stem):
    """chomper_tattoo_clawmarkssilver_chomper -> Clawmarks Silver (Chomper); cactus_head_bandit_camoblue ->
    Camoblue (Bandit)."""
    stem = re.sub(r'^%s_(?:[a-z]*tattoo[a-z]*_|head_)?' % re.escape(character), '', stem)
    if kind == 'gw1':
        variant, _, design = stem.partition('_')
    else:
        design, _, variant = stem.rpartition('_')
    design = re.sub(r'(?<=[a-z])(silver|gold)$', r'_\1', design or stem)
    return '%s (%s)' % (title(design), title(variant)) if design and variant else title(stem)


def _gw_document(location):
    path, offset, size, source = location
    with path.open('rb') as stream:
        stream.seek(offset)
        return gw2.Ebx(gw2.decompress(gw2.Reader(stream.read(size)), source['original_size'])).document()


def gesture_names(game, kind):
    """In-game gesture names, per clip prefix (Chomper, AssaultCorn...): GW2 by gesture wheel index (its
    GestureUnlockAssets matched to the UI items), GW1 by the number in the UI picture name."""
    if kind == 'bfn':
        return {}
    by_id, indexes, out = {}, {}, {}
    for location in gw2.cached_dependency_index(Path(game))['ebx'].values():
        name = location[3]['name'].lower()
        try:
            if re.search(r'_pvz/ui/customization/[^/]+/[^/]+_gestures?$', name):
                for o in _gw_document(location)['objects']:
                    if o.get('$type') == 'UIKitItemDescription':
                        for i in o.get('ItemIds') or []:
                            by_id[i] = (o.get('TexturePath') or '').split('/')[-1]
            elif kind == 'gw2' and '/gestures/' in name and 'gestureunlock' in name:
                o = _gw_document(location)['objects'][0]
                indexes[o.get('Identifier')] = o.get('GestureIndex')
        except (ValueError, OSError, struct.error, IndexError, KeyError):
            continue
    for ident, picture in by_id.items():
        if kind == 'gw2':
            prefix, _, label = picture.partition('_Gesture_')
            number = indexes.get(ident)
        else:  # Chomper15_No
            m = re.match(r'([A-Za-z]+?)(\d+)_?(.*)$', picture)
            prefix, number, label = (m.group(1), int(m.group(2)), m.group(3)) if m else ('', None, '')
        label = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', label.replace('_', ' ')).replace(' ,', ',').strip()  # HotSteppin
        if prefix and number is not None and label:
            out.setdefault(prefix.lower(), {})[number] = label
    return out


def _own_clip(clip, character):
    """Whether a clip's name starts with its rig's character (Eng_ for Engineer, S_Dragon_ for SnapDragon), not one
    borrowed from another character (Chomper's rig plays Peashooter_Gesture_Celebrate)."""
    words = re.split(r'_', clip)
    at = next((i for i, w in enumerate(words) if EMOTE_WORD.match(w)), len(words))
    prefix, char = ''.join(words[:at]).lower(), character.replace(' ', '').lower()
    return not prefix or prefix in char or char in prefix or (char[:1] == prefix[:1] and char.endswith(words[at - 1].lower()))


def _emote_label(clip):
    """Chomper_Gesture_Dance_01 -> Dance 01; Rose_Petal_Confetti_Gesture -> Petal Confetti; Chomper_Taunt_03 -> Taunt 03."""
    words = [w for w in re.split(r'[_\s]+', clip) if w]
    at = next((i for i, w in enumerate(words) if EMOTE_WORD.match(w)), None)
    if at is None:
        return title('_'.join(words[1:]) or clip)
    rest = words[at + 1:] if at + 1 < len(words) else words[1:at]
    if words[at].lower().startswith(('taunt', 'heroic')):
        rest = [re.sub(r's$', '', words[at].capitalize())] + rest  # Cactus_Taunts_03 -> Taunt 03
    return ' '.join(w if w.isupper() else w.capitalize() for w in rest) or title(clip)


def _gesture_slot(kind, clips):
    """Clip key -> gesture wheel index (GW2) or UI number (GW1) for a character's gesture clips."""
    out = {}
    if kind == 'gw2':
        def number(name):
            m = re.search(r'(\d+)$', name)
            return int(m.group(1)) if m else 1
        for role, first in (('personality', 3), ('dance', 5)):
            found = sorted((c for c in clips if role in c['name'].lower() and 'loop' not in c['name'].lower()),
                           key=lambda c: number(c['name']))
            for offset, c in enumerate(found[:2]):
                out[c['key']] = first + offset
        for c in clips:
            low = c['name'].lower()
            if 'celebrat' in low:
                out.setdefault(c['key'], 0)
            elif re.search(r'_(?:yes|positive)$', low):
                out.setdefault(c['key'], 1)
            elif re.search(r'_(?:no|negative)$', low):
                out.setdefault(c['key'], 2)
    elif kind == 'gw1':
        basics = {'celebrate': 11, 'follow': 12, 'point': 13, 'wave': 14, 'no': 15, 'yes': 16}
        for c in clips:
            low = c['name'].lower().replace(' ', '')
            m = re.search(r'_taunts?_(\d+)$', low)
            word = re.search(r'_gesture_([a-z]+)$', low)
            if m and int(m.group(1)) <= 10:
                out[c['key']] = int(m.group(1))
            elif word and word.group(1) in basics:
                out[c['key']] = basics[word.group(1)]
    return out


def emotes(game, kind):
    """Character id -> its emote items: gesture and taunt clips of the character's animation rig, named as in
    the game where its gesture tables say so. Built from the animation clip index (cached per install)."""
    import animations
    try:
        index = animations.discover(game)
    except (ValueError, OSError, struct.error) as error:
        print('No emotes: %s' % error, flush=True)
        return {}
    names = gesture_names(game, kind)
    out = {}
    for rig in index['characters']:
        model = rig.get('model') or {}
        if model.get('section') != 'Characters':
            continue
        clips = [c for c in rig['clips'] if EMOTE.search(c['name']) and not NPC_CLIP.search(c['name'])]
        character = animations.character_of(rig['rig'])
        own = [c for c in clips if _own_clip(c['name'], character)]
        slots = _gesture_slot(kind, own)
        labels = names.get(character.replace(' ', '').lower(), {})
        items = out.setdefault(model['id'], [])
        for c in clips:
            label = _emote_label(c['name'])
            number = slots.get(c['key'])
            if c not in own:
                label = '%s (%s)' % (label, title(c['name'].split('_')[0]))
            elif labels.get(number) and labels[number].lower() != label.lower():
                label = '%s (%s)' % (labels[number], label)
            items.append({'name': label, 'bundle': '%s%s:%s' % (CLIP, c['key'], c.get('banks') or ''), 'sb': '',
                          'clip': c['name'], 'order': number if number is not None else 99})
    return out


def clip_item(item):
    """An emote item as animations.py export takes it, or None for a mesh item."""
    bundle = item.get('bundle') or ''
    if not bundle.startswith(CLIP):
        return None
    key, _, banks = bundle[len(CLIP):].partition(':')
    return {'name': item.get('clip') or item.get('name'), 'key': key, 'banks': banks or None}


def discover(game, extras=False):
    """Characters and their cosmetic slots. With `extras`, also tattoos and emotes (emotes read the animation
    clip index, slow the first time on an install)."""
    kind = game_kind(game)
    pattern = PATTERNS[kind]
    characters = {}
    for name, sb in bundle_names(game, kind):
        match = pattern.match(name.lower())
        tattoo = TATTOOS.get(kind) and TATTOOS[kind].match(name.lower())
        if extras and tattoo and not match:
            team, character, folder, stem = tattoo.groups()
            entry = characters.setdefault(character, {'name': title(character), 'id': character,
                                                      'team': 'Plants' if team.startswith('plant') else 'Zombies', 'slots': {}})
            entry['slots'].setdefault(TATTOO_SLOTS.get(folder, 'tattoos'), []).append(
                {'name': tattoo_name(kind, character, stem), 'bundle': name, 'sb': sb})
            continue
        if not match:
            continue
        team, character, slot, stem = match.groups()
        if slot in SKIPPED_SLOTS:
            continue
        # Item names drop the character and slot the bundle path already gives.
        item = re.sub(r'^%s_' % re.escape(character), '', stem)
        item = re.sub(r'^(?:endlessgiant_)?(?:%s|%s)_' % (re.escape(slot), re.escape(slot.rstrip('s'))), '', item)
        if item in ('bounds', 'none'):
            continue
        if slot == 'rigandmodel':
            slot = 'base'
        team = 'Plants' if team.startswith('plant') else 'Zombies'
        entry = characters.setdefault(character, {'name': title(character), 'id': character, 'team': team, 'slots': {}})
        entry['slots'].setdefault(slot, []).append({'name': title(item) or title(stem), 'bundle': name, 'sb': sb})
    for character, items in (emotes(game, kind) if extras else {}).items():
        if character in characters and items:
            items.sort(key=lambda i: (i['order'], i['name']))
            characters[character]['slots']['emotes'] = [{k: v for k, v in i.items() if k != 'order'} for i in items]
    for entry in characters.values():
        for slot, items in entry['slots'].items():
            if slot != 'emotes':
                items.sort(key=lambda i: i['name'])
    return {'game': kind, 'characters': sorted(characters.values(), key=lambda c: (c['team'], c['name']))}


# ---------------- Garden Warfare 1 and 2 ----------------
def gw_bundle(game, sb, bundle):
    """One bundle of a GW1/GW2 superbundle, with the installed patch applied."""
    game = Path(game)
    base_toc, patch_toc = game / 'Data' / (sb + '.toc'), game / 'Update/Patch/Data' / (sb + '.toc')
    base = {e['id'].lower(): e for e in (gw2.read_db(base_toc).get('bundles', []) if base_toc.is_file() else [])}
    patch = {e['id'].lower(): e for e in (gw2.read_db(patch_toc).get('bundles', [])
                                          if patch_toc.is_file() and gw2.patch_applies(game, base_toc) else [])}

    def read(toc, entry):
        if entry['offset'] < 0 or not 0 < entry['size'] <= gw2.LIMIT:
            raise ValueError('Invalid bundle file range')
        with toc.with_suffix('.sb').open('rb') as stream:
            stream.seek(entry['offset'])
            data = stream.read(entry['size'])
        if len(data) != entry['size']:
            raise ValueError('Truncated bundle file range')
        return data
    key = bundle.lower()
    entry, old = patch.get(key), base.get(key)
    if entry is None or entry.get('base'):
        if old is None:
            raise ValueError('Bundle not found: ' + bundle)
        return read(base_toc, old), False
    if entry.get('delta'):
        return gw2.patch_bundle(read(base_toc, old) if old else b'', read(patch_toc, entry)), True
    return read(patch_toc, entry), False


def gw_export(game, kind, items, output):
    game, output = Path(game), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    inventory = {'profile': kind, 'game': str(game), 'package': 'cosmetics', 'assets': [], 'errors': [],
                 'archive_policy': 'base-and-installed-patch'}
    seen = set()
    by_name = None
    asset_meshes = set()
    for item in _asset_items(items):
        if by_name is None:
            by_name = {loc[3]['name'].lower(): loc for loc in gw2.cached_dependency_index(game)['ebx'].values()}
        location = by_name.get(item['asset'].lower())
        if location is None:
            inventory['errors'].append({'name': item['asset'], 'stage': 'ebx', 'error': 'Not in the shared index'})
            continue
        path, offset, size, source = location
        try:
            with path.open('rb') as stream:
                stream.seek(offset)
                content = gw2.decompress(gw2.Reader(stream.read(size)), source['original_size'])
            document = gw2.Ebx(content).document()
        except (ValueError, OSError, struct.error, IndexError) as error:
            inventory['errors'].append({'name': item['asset'], 'stage': 'ebx', 'error': str(error)})
            continue
        entry = dict(source, path='ebx/%s.bin' % source['sha1'], document='ebx/%s.json' % source['sha1'],
                     bytes=len(content), root_type=document['root_type'], guid=document['guid'], bundle=item['bundle'])
        (output / entry['path']).parent.mkdir(parents=True, exist_ok=True)
        (output / entry['path']).write_bytes(content)
        (output / entry['document']).write_text(json.dumps(document), encoding='utf-8')
        inventory['assets'].append(entry)
        seen.add(('ebx', source.get('guid', source['name'])))
        asset_meshes.add(document['guid'])
        print('Mesh:', item['asset'], flush=True)
    if asset_meshes:
        from props import _trim_variations
        tables = variation_index(game, kind)
        # Each mesh takes the first of its tables that is readable and lists it; the rest are fallbacks.
        covered = set()
        for name in [n for g in sorted(asset_meshes) for n in tables.get(g, [])]:
            location = by_name.get(name.lower())
            if location is None or not (set(g for g in asset_meshes if name in tables.get(g, [])) - covered):
                continue
            path, offset, size, source = location
            with path.open('rb') as stream:
                stream.seek(offset)
                content = gw2.decompress(gw2.Reader(stream.read(size)), source['original_size'])
            document = gw2.Ebx(content).document()
            if not _trim_variations(document, asset_meshes - covered):
                continue
            covered |= {g for g in asset_meshes if name in tables.get(g, [])}
            entry = dict(source, path='ebx/%s.bin' % source['sha1'], document='ebx/%s.json' % source['sha1'],
                         bytes=len(content), root_type=document['root_type'], guid=document['guid'])
            (output / entry['path']).write_bytes(content)
            (output / entry['document']).write_text(json.dumps(document), encoding='utf-8')
            inventory['assets'].append(entry)
            print('Variations:', name, flush=True)
    for item in items:
        if item in _asset_items(items):
            continue
        try:
            raw, decoded = gw_bundle(game, item['sb'], item['bundle'])
            assets, r = gw2.bundle(raw)
        except (ValueError, OSError, struct.error) as error:
            inventory['errors'].append({'bundle': item['bundle'], 'stage': 'bundle', 'error': str(error)})
            continue
        for asset in assets:
            content = r.take(asset['original_size']) if decoded else gw2.decompress(r, asset['original_size'])
            key = gw2.asset_key(asset)
            if key in seen:
                continue
            seen.add(key)
            entry = dict(asset)
            if kind == 'gw2' and asset['kind'] == 'resource' and asset.get('resource_type') == 0x6bde20ba and len(content) == 132:
                # As gw2.export: the reader's v11 header shape, and GW2's BC5 ordinal.
                content = content[:28] + b'\0' * 4 + content[28:] + b'\0' * 8
                if struct.unpack_from('<I', content, 12)[0] == 63:
                    entry['source_format'] = 63
                    content = content[:12] + struct.pack('<I', 64) + content[16:]
            entry['path'] = '%s/%s.bin' % (asset['kind'], asset['sha1'])
            (output / entry['path']).parent.mkdir(parents=True, exist_ok=True)
            (output / entry['path']).write_bytes(content)
            entry['bytes'] = len(content)
            entry['bundle'] = item['bundle']
            if asset['kind'] == 'ebx':
                try:
                    document = gw2.Ebx(content).document()
                    entry['document'] = 'ebx/%s.json' % asset['sha1']
                    (output / entry['document']).write_text(json.dumps(document), encoding='utf-8')
                    entry['root_type'], entry['guid'] = document['root_type'], document['guid']
                except (ValueError, IndexError, struct.error, UnicodeError) as error:
                    inventory['errors'].append({'name': asset['name'], 'stage': 'ebx', 'error': str(error)})
            inventory['assets'].append(entry)
        print('Cosmetic bundle:', item['bundle'], flush=True)
    gw2.complete_dependencies(game, output, inventory)
    if kind == 'gw1':
        import gw1
        gw1.finish(game, output, inventory)
    else:
        gw2.complete_texture_chunks(game, output, inventory)
    inventory['skeletons'] = gw_skeletons(game, output, inventory,
                                          {c for c in (item_character(kind, i) for i in items) if c})
    inventory['items'] = items
    (output / 'inventory.json').write_text(json.dumps(inventory, indent=1), encoding='utf-8')
    return inventory


def export(game, items, output, names=None):
    kind = game_kind(game)
    meshes = [i for i in items if not clip_item(i)]
    if len(meshes) < len(items):  # emotes are decoded from the animation banks at conversion
        inventory = export(game, meshes, output, names) if meshes else {
            'profile': kind, 'game': str(game), 'game_root': str(game), 'package': 'cosmetics', 'assets': [], 'errors': []}
        inventory.update(items=items, game_root=str(game))
        Path(output).mkdir(parents=True, exist_ok=True)
        (Path(output) / 'inventory.json').write_text(json.dumps(inventory, indent=1), encoding='utf-8')
        return inventory
    if kind == 'bfn':
        import bfn
        if not names:
            raise ValueError('Battle for Neighborville needs its type name table (--names)')
        skeletons = bfn_skeletons(game, {item_character(kind, i): bundle_root(i['bundle']) for i in items
                                         if item_character(kind, i)})
        by_asset = _asset_items(items)
        inventory = bfn.export(game, 'cosmetics', output, bfn.key_from(None), names,
                               int(os.environ.get('AURA_IMPORT_JOBS', '4')),
                               bundles=[i['bundle'] for i in items if i not in by_asset],
                               extra_ebx=sorted({n for found in skeletons.values() for n in found} | {i['asset'] for i in by_asset}))
        wanted = {i['asset'].lower(): i['bundle'] for i in by_asset}
        for a in inventory['assets']:
            if a.get('kind') == 'ebx' and a.get('name', '').lower() in wanted:
                a['bundle'] = wanted[a['name'].lower()]
        have = {a['name'].lower() for a in inventory['assets'] if a.get('root_type') == 'SkeletonAsset'}
        options = {c: [n for n in found if n.lower() in have] for c, found in skeletons.items()}
        inventory['skeleton_options'] = {c: found for c, found in options.items() if found}
        inventory['skeletons'] = {c: found[0] for c, found in inventory['skeleton_options'].items()}
        inventory['items'] = items
        (Path(output) / 'inventory.json').write_text(json.dumps(inventory, indent=1), encoding='utf-8')
        return inventory
    return gw_export(game, kind, items, output)


# ---------------- conversion ----------------
def linear_transform(value):
    """A LinearTransform as a column-vector 4x4 (columns: right, up, forward, translation)."""
    rows = [[0.0] * 4 for _ in range(4)]
    rows[3][3] = 1.0
    for column, key in enumerate(('right', 'up', 'forward', 'trans')):
        v = value.get(key) or {}
        for row, axis in enumerate('xyz'):
            rows[row][column] = float(v.get(axis, 0))
    return rows


class Rig:
    """A character skeleton as written for Blender: bone names, parents and bind (model) poses.
    Battle for Neighborville meshes may add procedural bones (ids with bit 15 set), which are
    appended per mesh under their parent bone."""
    def __init__(self, character, document):
        root = document['objects'][0]
        names, parents, poses = root.get('BoneNames', []), root.get('Hierarchy', []), root.get('ModelPose', [])
        if not names or len(parents) != len(names) or len(poses) != len(names):
            raise ValueError('SkeletonAsset has no bind pose')
        self.character, self.name = character, root.get('Name', character)
        self.bones = [{'name': n, 'parent': int(p), 'matrix': linear_transform(m)} for n, p, m in zip(names, parents, poses)]
        # A skin fitting the bone count alone does not make a usable rig.
        import numpy as np
        for index, bone in enumerate(self.bones):
            parent = bone['parent']
            if parent < -1 or parent >= len(names) or parent == index:
                raise ValueError('Invalid skeleton parent for ' + bone['name'])
            chain = {index}
            while parent != -1:
                if parent in chain:
                    raise ValueError('Skeleton hierarchy contains a cycle')
                chain.add(parent)
                parent = self.bones[parent]['parent']
                if parent < -1 or parent >= len(names):
                    raise ValueError('Invalid skeleton parent')
            matrix = np.asarray(bone['matrix'])
            if not np.isfinite(matrix).all() or abs(np.linalg.det(matrix[:3, :3])) < 1e-8:
                raise ValueError('Invalid skeleton bind transform for ' + bone['name'])
        self.core = len(self.bones)

    def bind(self, skin_path, mesh, label):
        """Point a mesh's .skin at this skeleton, appending its procedural bones."""
        import numpy as np
        raw = skin_path.read_bytes()
        if raw[:4] != b'ASKN':
            raise ValueError('Not a skin file')
        version, count, width = struct.unpack_from('<3I', raw, 4)
        bones = np.frombuffer(raw, '<u2', count * width, 16).astype(np.int64)
        weights = np.frombuffer(raw, '<f4', count * width, 16 + count * width * 2)
        used = weights > 0
        if not np.isfinite(weights).all() or (weights < 0).any():
            raise ValueError('Skin contains invalid bone weights')
        totals = weights.reshape(count, width).sum(axis=1)
        if not np.allclose(totals[totals > 0], 1, atol=1e-4):
            raise ValueError('Skin contains unnormalised vertices')
        procedural = bones >= 0x8000
        extra = (mesh.get('SkinnedProceduralAnimation') or {}).get('Bones') or []
        if (bones[used & procedural] - 0x8000 >= len(extra)).any():
            raise ValueError('Skin names procedural bones the mesh does not define')
        if (bones[used & ~procedural] >= self.core).any():
            raise ValueError('Skin bone index outside the %s skeleton' % self.character)
        if (used & procedural).any():
            base = len(self.bones)
            stem = re.sub(r'\W+', '_', label).strip('_')[:40]
            for n, bone in enumerate(extra):
                parent = int(bone.get('ParentIndex', -1))
                parent = parent - 0x8000 + base if parent >= 0x8000 else parent
                self.bones.append({'name': '%s_%d' % (stem, n), 'parent': parent if -1 <= parent < base + len(extra) else -1,
                                   'matrix': linear_transform(bone.get('LocalPose') or {})})
            bones = np.where(procedural, bones - 0x8000 + base, bones)
        skin_path.write_bytes(raw[:16] + bones.astype('<u2').tobytes() + weights.tobytes())

    def write(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'format': 'aura-frostbite-skeleton', 'version': 1, 'name': self.name,
                                    'character': self.character, 'bones': self.bones}), encoding='utf-8')


def convert(export_dir, textures, assets, out):
    export_dir, out = Path(export_dir), Path(out)
    inventory = json.loads((export_dir / 'inventory.json').read_text(encoding='utf-8'))
    kind = inventory.get('profile')
    item_of = {i['bundle'].lower(): i for i in inventory.get('items', [])}
    places, errors, rigs, bound, used = [], [], {}, {}, set()
    identity = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    if kind == 'bfn':
        from convert import Archive
        import convert_legacy
        archive = Archive(export_dir)
        converter = convert_legacy.LegacyConverter(archive, textures, assets, 'bfn')
        converter.cutouts = False
        converter.skinned = True

        def mesh_of(doc, obj, variation=0):
            rel, materials = converter.mesh(doc, obj)[:2]
            return rel, materials
    else:
        import gw2_convert
        archive = gw2_convert.Gw2Archive(export_dir)
        converter = gw2_convert.Gw2Converter(archive, textures, assets)
        converter.skinned = True

        def mesh_of(doc, obj, variation=0):
            return converter.convert_static((doc, obj), variation)
    def document(name):
        doc = archive.documents.get(name)
        doc = doc[1] if isinstance(doc, tuple) else doc
        return doc.load() if hasattr(doc, 'load') else doc

    # A tattoo's bundle pairs the head or body mesh with its ObjectVariation; that mesh converts with the
    # variation's materials (the tattoo textures and shader), whichever item brought the mesh in.
    variation_of = {}
    tattoo_bundles = {i['bundle'].lower() for i in inventory.get('items', [])
                      if kind in TATTOOS and TATTOOS[kind].match((i.get('bundle') or '').lower())}
    for asset in inventory['assets'] if tattoo_bundles else ():
        if asset.get('root_type') != 'BlueprintBundle' or (asset.get('bundle') or '').lower() not in tattoo_bundles:
            continue
        doc = document(asset['name'])  # a LazyDocument reads as empty until first indexed: no `doc or {}`
        for obj in (doc.get('objects') or [] if doc is not None else []):
            if obj.get('$type') == 'MeshAndVariationPair':
                mesh, variation = archive.resolve(doc, obj.get('MeshAsset')), archive.resolve(doc, obj.get('Variation'))
                if mesh and variation and variation[1].get('NameHash'):
                    variation_of[mesh[0]['guid']] = variation[1]['NameHash']

    def rigs_of(character, mesh_name):
        """The character's rigs, those from the mesh's own folder family first (BFN mixes in GW2's)."""
        primary = (inventory.get('skeletons') or {}).get(character)
        names = (inventory.get('skeleton_options') or {}).get(character) or ([primary] if primary else [])
        if not names:
            errors.append({'stage': 'skeleton', 'name': character, 'error': 'No skeleton exported'})
        root = mesh_name.lower().split('/')[0]
        out = []
        for index, name in sorted(enumerate(names), key=lambda e: not e[1].lower().startswith(root + '/')):
            key = (character, index)
            if key not in rigs:
                rigs[key] = None
                try:
                    doc = document(name)
                    if doc is None:
                        raise ValueError('Skeleton not exported: ' + name)
                    rigs[key] = Rig(character, doc)
                except (ValueError, KeyError, TypeError) as error:
                    errors.append({'stage': 'skeleton', 'name': name, 'error': str(error)})
            if rigs[key]:
                out.append((key, rigs[key]))
        return out

    def rig_file(key):
        character, index = key
        return 'skeletons/frostbite/%s_%s%s.json' % (kind, character, '_%d' % index if index else '')
    for asset in inventory['assets']:
        if asset.get('root_type') not in MESH_TYPES or 'document' not in asset:
            continue
        doc = document(asset['name'])
        if doc is None:
            continue
        obj = doc['objects'][0]
        item = item_of.get((asset.get('bundle') or '').lower())
        label = ('%s (%s)' % (item['name'], title(item['slot'])) if item.get('slot') else item['name']) if item             else asset['name'].split('/')[-1]
        try:
            rel, materials = mesh_of(doc, obj, variation_of.get(doc.get('guid'), 0))
        except Exception as error:  # one bad mesh must not lose the other items
            errors.append({'stage': 'mesh', 'name': asset['name'], 'error': str(error)})
            continue
        tags = {'mesh': rel, 'source': obj.get('Name', asset['name']), 'level_instance': label, 'profile': kind}
        skin = (Path(assets) / rel).with_suffix('.skin')
        character = item_character(kind, item) if item else character_of(kind, asset.get('bundle') or '')
        problems = []
        for key, rig in (rigs_of(character, asset['name']) if character and skin.is_file() else []):
            try:
                if skin not in bound:  # a mesh two items share is bound once
                    rig.bind(skin, obj, label)
                    bound[skin] = key
                elif bound[skin] != key:
                    continue
                tags.update(skin=Path(rel).with_suffix('.skin').as_posix(), skeleton=rig_file(key))
                used.add(key)
                break
            except (ValueError, struct.error) as error:
                problems.append(str(error))
        else:
            if problems:
                errors.append({'stage': 'skin', 'name': asset['name'], 'error': problems[0]})
        tags.update({'material_%d' % i: m for i, m in enumerate(materials)})
        places.append({'kind': 'frostbite_mesh', 'name': asset['name'].split('/')[-1], 'matrix': identity,
                       'tags': tags, 'values': {'collision': 0}})
    if hasattr(converter, 'finish_textures'):
        converter.finish_textures()
    for key in used:
        rigs[key].write(Path(assets) / rig_file(key))
    clips = [c for c in (clip_item(i) for i in inventory.get('items', [])) if c]
    decoded = {'clips': [], 'errors': []}
    if clips:  # emotes: one decoded clip each, for the add-on to put on the character's armature
        import animations
        decoded = animations.export(inventory.get('game_root') or inventory['game'], clips, Path(assets) / 'clips')
        errors += [{'stage': 'clip', 'name': e.get('clip'), 'error': e.get('error')} for e in decoded['errors']]
    meshes = len(places)
    if not places and not decoded['clips']:
        raise ValueError('No cosmetic meshes converted' + (': ' + errors[0]['error'] if errors else ''))
    if places:
        places.insert(0, {'kind': 'frostbite_environment', 'name': 'Cosmetics lighting', 'at': [0, 0, 0], 'tags': {},
                          'values': {'sun_intensity': 2.4}})
    scene = {'format': 'aura-frostbite-scene', 'version': 1, 'name': 'Cosmetics', 'places': places}
    report = {'profile': kind, 'placements': len(places), 'meshes': meshes, 'textures': 0, 'errors': errors,
              'skeletons': {rig_file(k).rsplit('/', 1)[-1][:-5]: rigs[k].name for k in used},
              'rigged_meshes': sum(1 for p in places if 'skin' in p['tags']), 'clips': decoded['clips'],
              'tattooed_meshes': len(variation_of)}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(scene, indent=1), encoding='utf-8')
    out.with_suffix('.report.json').write_text(json.dumps(report, indent=1), encoding='utf-8')
    return scene, report


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('action', choices=('discover', 'export', 'convert'))
    p.add_argument('--game')
    p.add_argument('--out', required=True)
    p.add_argument('--items', help='JSON list of {name, bundle, sb} from discover')
    p.add_argument('--names', help='BFN type name table')
    p.add_argument('--export')
    p.add_argument('--textures')
    p.add_argument('--assets')
    p.add_argument('--extras', action='store_true', help='discover: also tattoos and emotes')
    a = p.parse_args()
    if a.action == 'discover':
        Path(a.out).write_text(json.dumps(discover(a.game, a.extras), indent=1), encoding='utf-8')
    elif a.action == 'export':
        export(a.game, json.loads(Path(a.items).read_text(encoding='utf-8')), a.out, a.names)
    else:
        scene, report = convert(a.export, a.textures, a.assets, a.out)
        print('Cosmetic meshes:', report['meshes'], 'errors:', len(report['errors']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
