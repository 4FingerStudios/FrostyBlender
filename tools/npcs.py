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
import sys
from pathlib import Path

import cosmetics
from cosmetics import bundle_names, game_kind, title

GW1_AI = re.compile(r'^win32/_pvz/animations/characters/ai/([^/]+)/(?:([^/]+)/)?([^/]+)_bpb$')
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
    for entry in npcs.values():
        for items in entry['slots'].values():
            items.sort(key=lambda i: i['name'])
    order = {'Horde': 0, 'AI': 0, 'Boss': 1, 'Deployable': 2, 'Vehicle': 3}
    return {'game': kind, 'characters': sorted(npcs.values(), key=lambda c: (c['team'], order.get(c['group'], 9), c['name']))}


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
