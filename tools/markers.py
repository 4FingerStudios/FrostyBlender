"""Gameplay markers: where a map's spawns, teleporters, purchase spots and vehicles are, placed as
'frostbite_marker' empties. The converters walk the level as usual and report each spawn entity and each
prefab they pass through; the rules here decide which of those are markers and what they are called.

Capture points and gnome bomb sites are not placed: Garden Warfare parks those prefabs off the map and its
game-mode logic moves them into place at run time.
"""
import re

import numpy as np

# Spawn entities' Team field (Garden Warfare 1 and 2, Battle for Neighborville).
TEAMS = {1: 'Zombies', 2: 'Plants'}

# (pattern on the referenced prefab's name, category, label). The first match wins.
PREFABS = [
    (re.compile(r'(^|/)plantteleporterentrance[^/]*$'), 'teleporter', 'Plant teleporter entrance'),
    (re.compile(r'(^|/)plantteleporterexit[^/]*$'), 'teleporter', 'Plant teleporter exit'),
    (re.compile(r'(^|/)zombieteleporterentrance[^/]*$'), 'teleporter', 'Zombie teleporter entrance'),
    (re.compile(r'(^|/)zombieteleporterexit[^/]*$'), 'teleporter', 'Zombie teleporter exit'),
    (re.compile(r'(^|/)purchasable(rose|plant)?turret[^/]*$'), 'purchase', 'Plant turret spot'),
    (re.compile(r'(^|/)purchasableplantminion[^/]*$'), 'purchase', 'Plant minion spot'),
    (re.compile(r'(^|/)purchasablezombieturret[^/]*$'), 'purchase', 'Zombie turret spot'),
    (re.compile(r'(^|/)purchasablezombieminion[^/]*$'), 'purchase', 'Zombie minion spot'),
    (re.compile(r'(^|/)purchasableplantorzombieturret[^/]*$'), 'purchase', 'Turret spot'),
    (re.compile(r'(^|/)[^/]*extractionzone[^/]*_prefab$'), 'objective', 'Extraction zone'),
    (re.compile(r'(^|/)hub_collectable_gnome_\d+$'), 'collectible', 'Collectible gnome'),
]

CATEGORIES = {'player_spawn': 'Player spawns', 'ai_spawn': 'AI spawns', 'vehicle_spawn': 'Vehicle spawns',
              'teleporter': 'Teleporters', 'purchase': 'Purchase spots', 'objective': 'Objectives',
              'collectible': 'Collectibles'}


def _words(leaf):
    return ' '.join(w.capitalize() for w in leaf.split('_') if w)


def ai_label(blueprint):
    """'gameplay/soldiers/ai/aizombie_turret_toxic' -> 'Zombie Turret Toxic'."""
    leaf = blueprint.rsplit('/', 1)[-1].lower()
    leaf = re.sub(r'^ai(?=plant|zombie)', '', leaf)
    leaf = re.sub(r'^(plant|zombie)(?=[a-z])', r'\1_', leaf)
    # A turret's upgrade levels ('..._engineer_2') stand on one spot: one marker.
    return _words(re.sub(r'(_base|_\d+)$', '', leaf))


class Markers:
    """Collects markers while a converter walks a level; the same marker reached twice is kept once."""

    def __init__(self, profile):
        self.profile = profile
        self.places, self.seen = [], set()
        self.counts = {}

    def add(self, category, label, world, layer, mode=None, **tags):
        if mode:
            label = '%s (%s)' % (label, mode)
        at = np.asarray(world)[:3, 3]
        key = (category, label, tuple(np.round(at, 1)))
        if key in self.seen:
            return
        self.seen.add(key)
        self.counts[category] = self.counts.get(category, 0) + 1
        tags = {k: v for k, v in tags.items() if v not in (None, '')}
        tags.update({'category': category, 'layer': (layer or '').split('/')[-1], 'profile': self.profile})
        self.places.append({'kind': 'frostbite_marker', 'name': label, 'matrix': np.asarray(world).tolist(),
                            'tags': tags, 'values': {}})

    def spawn(self, obj, world, layer, blueprint=None, team=None, mode=None):
        """A character spawn: a player spawn when it names no AI soldier, else an AI spawn."""
        team = team or TEAMS.get(obj.get('Team'))
        lower = (blueprint or '').lower()
        if '/ai/' in lower:
            self.add('ai_spawn', ai_label(blueprint), world, layer, mode, team=team, blueprint=blueprint)
        else:
            self.add('player_spawn', '%s spawn' % team if team else 'Spawn', world, layer, mode, team=team,
                     blueprint=blueprint)

    def vehicle(self, obj, world, layer, blueprint=None):
        label = _words((blueprint or 'vehicle').rsplit('/', 1)[-1]) + ' spawn' if blueprint else 'Vehicle spawn'
        self.add('vehicle_spawn', label, world, layer, team=TEAMS.get(obj.get('Team')), blueprint=blueprint)

    def prefab(self, name, world, layer, mode=None):
        """A reference to a prefab: a marker when the prefab is one of the PREFABS."""
        lower = (name or '').lower()
        for pattern, category, label in PREFABS:
            if pattern.search(lower):
                self.add(category, label, world, layer, mode, blueprint=name)
                return True
        return False

    def keep(self, test):
        """Drop the markers test(x, y, z) rejects (outside a cropped map, or parked away from any ground)."""
        kept = [p for p in self.places if test(*np.asarray(p['matrix'])[:3, 3])]
        self.counts = {}
        for p in kept:
            category = p['tags']['category']
            self.counts[category] = self.counts.get(category, 0) + 1
        self.places = kept


# Battle for Neighborville game-mode sub-worlds and layers ('gamelogic_gb_plantspawns'; one is spelled 'gmaelogic').
BFN_MODE_LAYER = re.compile(r'^(gamemode|gamelogic|gmaelogic)_')
BFN_MODES = {'gb': 'Gnome Bomb', 'gnomebomb': 'Gnome Bomb', 'suburb': 'Suburbination', 'suburbination': 'Suburbination',
             'tvm': 'Team Vanquish', 'tvc': 'Team Vanquish', 'ops': 'Ops', 'freeroam': 'Free Roam'}


def bfn_mode(leaf):
    """'gamelogic_gb_plantspawns' -> 'Gnome Bomb'."""
    word = leaf.split('_')[1] if '_' in leaf else ''
    if re.fullmatch(r'lwe\d+', word):
        return 'Weekly Event'
    return BFN_MODES.get(word, word.capitalize() or None)


def bfn_team(leaf):
    """Battle for Neighborville spawns all say Team 0; their layer's name says whose they are."""
    return 'Plants' if 'plant' in leaf else 'Zombies' if 'zombie' in leaf else None
