"""Gameplay marker rules (tools/markers.py); no game data."""
import sys
import unittest
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import markers


def at(x, y, z):
    m = np.eye(4)
    m[:3, 3] = x, y, z
    return m


class MarkerChecks(unittest.TestCase):
    def test_spawns_split_into_player_and_ai_by_blueprint(self):
        marks = markers.Markers('gw2')
        marks.spawn({'Team': 2}, at(0, 0, 0), 'levels/x/herbal')
        marks.spawn({'Team': 1}, at(5, 0, 0), 'levels/x/herbal', 'gameplay/soldiers/ai/aizombie_turret_toxic')
        names = {(p['tags']['category'], p['name']) for p in marks.places}
        self.assertEqual(names, {('player_spawn', 'Plants spawn'), ('ai_spawn', 'Zombie Turret Toxic')})
        self.assertEqual(marks.places[0]['tags']['layer'], 'herbal')

    def test_prefabs_match_by_name_and_repeat_placements_collapse(self):
        marks = markers.Markers('gw2')
        self.assertTrue(marks.prefab('gameplay/complexobjects/plantteleporterexit', at(1, 2, 3), 'a'))
        self.assertTrue(marks.prefab('gameplay/complexobjects/plantteleporterexit', at(1, 2, 3), 'b'))
        self.assertFalse(marks.prefab('gameplay/gamemodes/capturepointflaglocation', at(0, 0, 0), 'a'))
        self.assertEqual(marks.counts, {'teleporter': 1})

    def test_battle_for_neighborville_layers_name_team_and_mode(self):
        self.assertEqual(markers.bfn_mode('gamelogic_gb_plantspawns'), 'Gnome Bomb')
        self.assertEqual(markers.bfn_mode('gmaelogic_suburb_plantspawns'), 'Suburbination')
        self.assertEqual(markers.bfn_team('gamelogic_tvm_zombiespawns'), 'Zombies')
        self.assertIsNone(markers.bfn_team('gamelogic_tvm_neutralspawns'))
        marks = markers.Markers('bfn')
        marks.spawn({'Team': 0}, at(0, 0, 0), 'l', team='Plants', mode='Gnome Bomb')
        marks.spawn({'Team': 0}, at(0, 9, 0), 'l', team='Plants', mode='Gnome Bomb')
        marks.keep(lambda x, y, z: y < 5)  # the second is parked in the air
        self.assertEqual([p['name'] for p in marks.places], ['Plants spawn (Gnome Bomb)'])
        self.assertEqual(marks.counts, {'player_spawn': 1})


if __name__ == '__main__':
    unittest.main()
