"""NPCs of Garden Warfare 1 and 2 and Battle for Neighborville: AI horde, bosses, turrets and vehicles.

NPCs ship like the playable characters' cosmetics, one unlock (or visual) bundle per body,
costume, prop, weapon or ability item, under the characters' AI folders. They are listed by
character with their slots, and imported through the cosmetics export and conversion, so
they come in bind pose at the origin and can be rigged to the NPC's own skeleton.

    python npcs.py discover --game GAME --out npcs.json
    python npcs.py export --game GAME --items items.json --out EXPORT [--names BFN_TYPES]
    python npcs.py convert --export EXPORT --textures TEXTURES --assets ASSETS --out scene.json

BFN also needs AURA_BFN_KEY, as for maps. Game files are only read.
"""
import argparse
import json
import re
import struct
import sys
from pathlib import Path

import cosmetics
from cosmetics import bundle_names, game_kind, title
from icons import norm

GW1_AI = re.compile(r'^(?:win32|xenon)/_pvz/animations/characters/ai/([^/]+)/(?:([^/]+)/)?([^/]+)_bpb$')
GW_NPC = re.compile(r'^win32/(?:gw2_)?characters(?:_cu\d+)?/(plants?|zombie|gnome)/(.+)_bpb$')
GROUPS = {'horde': 'Horde', 'boss': 'Boss', 'subboss': 'Boss', 'turret': 'Deployable', 'vehicle': 'Vehicle'}
# GW1 keeps every AI character in one folder; these are the plant side.
GW1_PLANTS = {'bambooshoot', 'bonk_choy', 'doom_shroom', 'fume_shroom', 'goop_shroom', 'iceshroom', 'laser_bean',
              'plant_health_station', 'scaredy_shroom', 'snap_dragon', 'sunflower_turret'}
SKIPPED = {'dismemberment', 'textures', 'ui', 'animation', 'animations', 'emotes'}
SLOTS = {'rigandmodel': 'body', 'costume': 'body', 'mech': 'body', 'vehicule': 'vehicle', 'abilites': 'abilities',
         'shieldprop': 'shield', 'level3': 'body'}
ASSET = re.compile(r'(?:_(?:skinnedmesh(?:proxy|poxy)|rigandmodel))?_(?:vis|shield|(?:bonus)?health[a-z]*?|attachable)?'
                   r'(?:unlockasset|visualasset|attachableasset|attachment)(?:_(\w+))?$')


def _parse(kind, bundle):
    """(team, group, npc, slot, item) for an NPC bundle, or None."""
    low = bundle.lower()
    if kind == 'gw1':
        match = GW1_AI.match(low)
        if not match:
            return None
        npc, folder, stem = match.groups()
        team = 'Plants' if npc in GW1_PLANTS or npc.startswith('peashooter') else 'Zombies'
        group = 'Deployable' if 'turret' in npc or team == 'Plants' else 'AI'
        if npc in ('gargantuan', 'yeti', 'zomboss', 'mech_zombie'):
            group = 'Boss'
        return team, group, npc, folder or 'body', stem
    match = GW_NPC.match(low)
    if not match:
        return None
    team, rest = match.groups()
    parts = rest.split('/')
    if parts[0] == 'playable' or len(parts) < 2:
        return None
    group = GROUPS.get(parts[0])
    if group:
        parts = parts[1:]
    elif team == 'gnome':
        group = 'Horde'  # GW2 gnomes: characters/gnome/<npc>/<slot>/...
    else:
        return None
    if len(parts) < 2:
        return None
    npc, dirs, stem = parts[0], parts[1:-1], parts[-1]
    if dirs:
        slot = dirs[0]
    else:  # futureimp/futureimp_body_futureimp_unlockasset: the slot is in the file name
        found = re.match(r'^[a-z0-9]+_([a-z]+)_', stem)
        slot = found.group(1) if found else 'body'
    team = {'plant': 'Plants', 'plants': 'Plants', 'zombie': 'Zombies', 'gnome': 'Gnomes'}[team]
    return team, group, npc, slot, stem


def _labels(npc, folder, slot, stem):
    """Item names from shortest to fullest; the first one not taken is used."""
    item = ASSET.sub(lambda m: '_' + m.group(1) if m.group(1) else '', stem)
    item = re.sub(r'^(?:%s|%s)_' % (re.escape(npc), re.escape(npc.replace('_', ''))), '', item)
    short = re.sub(r'^(?:[a-z0-9]+_)?(?:%s)_?' % '|'.join(re.escape(w) for w in sorted({folder, slot, folder.rstrip('s')}, key=len, reverse=True)), '', item)
    return [title(short) or 'Default', title(item) or 'Default', title(stem)]


def discover(game):
    kind = game_kind(game)
    npcs = {}
    for name, sb in bundle_names(game, kind):
        parsed = _parse(kind, name)
        if not parsed:
            continue
        team, group, npc, slot, stem = parsed
        folder, slot = slot, SLOTS.get(slot, slot)
        if slot in SKIPPED or stem.endswith('_bounds'):
            continue
        entry = npcs.setdefault(npc, {'name': title(npc), 'id': npc, 'team': team, 'group': group, 'slots': {}})
        items = entry['slots'].setdefault(slot, [])
        if any(i['bundle'].lower() == name.lower() for i in items):
            continue
        taken = {i['name'] for i in items}
        labels = _labels(npc, folder, slot, stem)
        label = next((l for l in labels if l not in taken), labels[0])
        if label in taken:  # BFN keeps GW2's copies beside its own
            label += ' (GW2)' if '/gw2_characters/' in name.lower() else ' (%d)' % len(items)
        items.append({'name': label, 'bundle': name, 'sb': sb, 'character': npc})
    owner_abilities(game, kind, npcs)
    playable = {c['id'] for c in cosmetics.discover(game)['characters']}
    for oid, entry in objects(game, kind, set(npcs) | playable).items():
        npcs.setdefault(oid, entry)
    for entry in npcs.values():
        for items in entry['slots'].values():
            items.sort(key=lambda i: i['name'])
    order = {'Horde': 0, 'AI': 0, 'Boss': 1, 'Deployable': 2, 'Vehicle': 3, 'Ability': 4}
    return {'game': kind, 'characters': sorted(npcs.values(), key=lambda c: (c['team'], order.get(c['group'], 9), c['name']))}


# Where skeleton-only characters live, and the group each folder means.
OBJECT_GROUPS = {'simple': 'Ability', 'simpleobject': 'Ability', 'vehicle': 'Vehicle', 'boss': 'Boss', 'subboss': 'Boss',
                 'turret': 'Deployable', 'horde': 'Horde', 'ai': 'AI'}
OBJECT_SKIP = re.compile(r'(^|/)(fx|vfx|ui|levels?|levels_sandbox|worlds|sound|online)/|emitters|vfx_|collision|_lod\d|'
                         r'shadow|debris|destruction|/playable/|_1p_|firstperson|/_old/')
FOLDER_WORDS = {'rigandmodel', 'body', 'base', 'prop', 'mech', 'costume', 'meshes', 'abilities'}
# Pickups and level props that happen to share these folders are not characters.
NOT_OBJECTS = {'bagofcoins', 'rotatingcoin', 'spinningdiamond'}
# Abilities whose meshes go by another name than their skeleton folder.
OBJECT_ALIASES = {'cactusdrone': ['garlicdrone', 'cactusgarlic'], 'engineer_bullhorn_mine': ['sonicmine', 'bullhorn'],
                  'pirateparrot': ['parrotvehicle'], 'engineer_drone': ['engineerdrone'], 'tallnuts': ['tallnut_four'],
                  'flax_canon': ['flaxcannon']}
# Words in an object's name that only say whose ability it is.
OWNER_WORDS = ('engineer', 'cactus', 'pirate', 'peashooter', 'chomper', 'sunflower', 'scientist', 'soldier', 'allstar',
               'rose', 'citron', 'imp', 'zombie', 'plant')
# GW1 keeps every object under one folder; these are the zombie side.
GW1_ZOMBIE_OBJECTS = {'revivalspring', 'teleporter', 'engineer_drone', 'engineer_bullhorn_mine', 'allstarimpgrenade',
                      'allstar_wall', 'pirate_imp', 'engineer_pileofjunk', 'shield_decoy', 'mech_zombie_imp',
                      'armgrenade', 'tactical_cuke'}


def _ebx_names(game, kind):
    """Every EBX name of a game (GW1/GW2 from the shared index; BFN from the bundle manifests)."""
    if kind == 'bfn':
        import bfn
        archive = bfn.Archive(game)
        names = set()
        for sb in archive.sbs:
            for entry in archive.toc(sb)['bundles'].values():
                try:
                    listed = archive.bundle(entry)
                except (ValueError, struct.error, IndexError):
                    continue
                names.update(a['name'].lower() for a in listed if a['kind'] == 'ebx')
        return names
    import gw2
    return {loc[3]['name'].lower() for loc in gw2.cached_dependency_index(Path(game))['ebx'].values()}


def _object_folder(name):
    """(team word, group word, id, folder prefix) of a character-like asset path, or None."""
    parts = name.split('/')
    if parts[0] == '_pvz' and len(parts) > 4 and parts[1:3] == ['animations', 'characters'] and parts[3] in ('simpleobject', 'ai'):
        return 'plants', parts[3], parts[4], '/'.join(parts[:5]) + '/'
    if re.fullmatch(r'(gw2_)?characters(_cu\d+)?', parts[0]) and len(parts) > 4 and parts[2] in OBJECT_GROUPS:
        return parts[1], parts[2], parts[3], '/'.join(parts[:4]) + '/'
    # BFN hero abilities: characters/<team>/playable/<hero>/abilities/<ability>/
    if re.fullmatch(r'characters(_cu\d+)?', parts[0]) and len(parts) > 6 and parts[2] == 'playable' and parts[4] == 'abilities':
        return parts[1], 'simple', parts[5], '/'.join(parts[:6]) + '/'
    return None


def _keys(oid):
    """Names an object's held meshes may carry: its own, without its owner's name, and known aliases."""
    keys = {norm(oid)} if len(norm(oid)) >= 5 else set()
    for word in OWNER_WORDS:  # 'pirateparrot' -> 'parrot' only when what is left is still specific
        rest = norm(oid[len(word):]) if oid.startswith(word) else ''
        if len(rest) >= 7:
            keys.add(rest)
    keys |= {norm(a) for a in OBJECT_ALIASES.get(oid, [])}
    return keys


def objects(game, kind, known):
    """Entries for characters that have a skeleton and meshes but no unlock bundle: ability objects such as
    the Chili Bean bomb or Potato Mine, drones, and the odd boss or vehicle. A character's meshes and skeleton
    may sit in different folders of the same name (GW2 keeps some skeletons in the old _pvz tree), so every
    folder named after it counts. Items name their meshes, which the cosmetics export reads directly.
    Abilities also take the meshes under weapons/ named after them (the version held in hand)."""
    names = _ebx_names(game, kind)
    known = {norm(k) for k in known}
    folders = {}
    for name in names:
        found = _object_folder(name)
        if not found:
            continue
        team, group, oid, prefix = found
        info = folders.setdefault(oid, {'team': team, 'groups': set(), 'prefixes': set(), 'skeleton': False})
        info['groups'].add(group)
        info['prefixes'].add(prefix)
        if name.endswith('skeleton'):
            info['skeleton'] = True
        if not team.startswith('plant') and not team.startswith('gnome') and team not in ('simpleobject', 'ai'):
            info['team'] = team
    skeletons = [n for n in names if n.endswith('skeleton')]
    for oid, info in folders.items():  # a hero ability folder may borrow a skeleton kept elsewhere
        if not info['skeleton'] and any(k in norm(n.rsplit('/', 1)[-1]) for n in skeletons for k in _keys(oid) if len(k) >= 8):
            info['skeleton'] = True
    meshes = sorted(n for n in names if n.endswith('_mesh') and not OBJECT_SKIP.search(n.replace('/playable/', '/')))
    weapons = [n for n in meshes if '/weapons/' in n or n.startswith(('gameplay/', '_pvz/weapons/', '_pvz/vehicles/'))
               or '/abilities/' in n]
    out = {}
    for oid, info in folders.items():
        if norm(oid) in known or oid in NOT_OBJECTS:
            continue
        if not info['skeleton'] and kind != 'gw1':
            continue  # GW1 keeps some objects' skeletons elsewhere (Pirate Imp, Gnome Bomb): their meshes still count
        groups = info['groups']
        group = 'Ability' if groups & {'simple', 'simpleobject'} else OBJECT_GROUPS.get(sorted(groups)[0], 'AI')
        prefixes = sorted(info['prefixes'])
        own = [(n, next(p for p in prefixes if n.startswith(p))) for n in meshes if any(n.startswith(p) for p in prefixes)]
        keys = _keys(oid)
        def matches(n):
            folder = re.search(r'/abilities/([^/]+)/', n)
            if folder:  # another hero's ability folder: only when that folder is this ability
                return norm(folder.group(1)) in keys
            return any(k in norm(n.rsplit('/', 1)[-1]) for k in keys)
        held = [n for n in weapons if matches(n) and not any(n == m for m, _ in own)]
        if own and group != 'Ability':
            held = []  # bosses and vehicles have their own meshes; projectiles named after them are noise
        if not own and not held:
            continue
        team = info['team']
        team = 'Zombies' if team.startswith('zombie') or oid in GW1_ZOMBIE_OBJECTS else             'Gnomes' if team.startswith('gnome') or oid.startswith('gnome') else 'Plants'
        entry = out.setdefault(oid, {'name': title(oid), 'id': oid, 'team': team, 'group': group, 'slots': {}})
        for mesh, prefix in own + [(n, None) for n in held]:
            leaf = re.sub(r'_mesh$', '', mesh.rsplit('/', 1)[-1])
            if prefix is None:
                slot = 'held'
            else:
                rest = mesh[len(prefix):]
                folder = rest.split('/')[0] if '/' in rest else 'body'
                slot = 'body' if folder in FOLDER_WORDS else folder
            label = title(re.sub(r'^(%s|%s)_?(body_)?' % (re.escape(oid), re.escape(oid.replace('_', ''))), '', leaf)) or 'Default'
            items = entry['slots'].setdefault(slot, [])
            if any(i['asset'] == mesh for i in items):
                continue
            if any(i['name'] == label for i in items):
                label = title(leaf)
            items.append({'name': label, 'bundle': 'asset:' + mesh, 'asset': mesh, 'sb': '', 'character': oid})
    return out


def owner_abilities(game, kind, npcs):
    """BFN keeps NPC ability meshes (rock throws, grenades) under <npc>/abilities/: add them to that NPC."""
    for name in sorted(_ebx_names(game, kind)) if kind == 'bfn' else ():
        match = re.match(r'^characters(?:_cu\d+)?/[^/]+/[^/]+/([^/]+)/abilities/([^/]+)/.*_mesh$', name)
        if not match or OBJECT_SKIP.search(name) or match.group(1) not in npcs:
            continue
        npc, ability = match.groups()
        items = npcs[npc]['slots'].setdefault('abilities', [])
        label = title(re.sub(r'^%s_abilit(?:y|ies)_' % re.escape(npc), '', name.rsplit('/', 1)[-1][:-5]))
        if any(i.get('asset') == name for i in items):
            continue
        items.append({'name': label, 'bundle': 'asset:' + name, 'asset': name, 'sb': '', 'character': npc})


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('action', choices=('discover', 'export', 'convert'))
    p.add_argument('--game')
    p.add_argument('--out', required=True)
    p.add_argument('--items', help='JSON list of {name, bundle, sb, character} from discover')
    p.add_argument('--names', help='BFN type name table')
    p.add_argument('--export')
    p.add_argument('--textures')
    p.add_argument('--assets')
    a = p.parse_args()
    if a.action == 'discover':
        Path(a.out).write_text(json.dumps(discover(a.game), indent=1), encoding='utf-8')
    elif a.action == 'export':
        cosmetics.export(a.game, json.loads(Path(a.items).read_text(encoding='utf-8')), a.out, a.names)
    else:
        scene, report = cosmetics.convert(a.export, a.textures, a.assets, a.out)
        print('NPC meshes:', report['meshes'], 'errors:', len(report['errors']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
