"""Map effects (tools/bfn_map_effects.py) recorded for Garden Warfare maps, from synthetic documents; no game data."""
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import bfn_map_effects


def gw_converter(objects):
    def resolve(doc, ref):
        return objects.get((ref or {}).get('id'))
    archive = types.SimpleNamespace(resolve=resolve, documents={})
    return types.SimpleNamespace(archive=archive, assets=Path(tempfile.mkdtemp()) / 'a' / 'b', errors=[])


class GardenWarfareEffectChecks(unittest.TestCase):
    def setUp(self):
        blueprint = {'guid': 'fx1', 'objects': [{'$type': 'EffectBlueprint'}]}
        self.c = gw_converter({'fx': (blueprint, blueprint['objects'][0])})
        self.effects = bfn_map_effects.MapEffects(self.c, None, game='gw')

    def test_ambient_effects_are_recorded_at_their_place(self):
        parent = np.eye(4)
        parent[:3, 3] = (10, 0, 0)
        self.effects.add({}, {'AutoStart': True, 'Blueprint': {'id': 'fx'},
                              'BlueprintTransform': {'trans': {'x': 1, 'y': 2, 'z': 3}}}, parent, 'layer')
        (doc, world, source, triggered), = self.effects.pending
        self.assertFalse(triggered)
        self.assertEqual(doc['guid'], 'fx1')
        self.assertEqual(world[:3, 3].tolist(), [11.0, 2.0, 3.0])
        self.assertEqual(self.effects.cache.name, 'gw_effects')

    def test_effects_gameplay_starts_are_skipped(self):
        self.effects.add({}, {'AutoStart': False, 'Blueprint': {'id': 'fx'}}, np.eye(4), 'layer')
        self.assertEqual(self.effects.pending, [])
        self.assertEqual(self.effects.counts['effects_triggered_skipped'], 1)

    def test_unresolved_blueprints_are_reported(self):
        with self.assertRaises(ValueError):
            self.effects.add({}, {'AutoStart': True, 'Blueprint': {'id': 'missing'}}, np.eye(4), 'layer')

    def test_gameplay_one_shots_are_left_out(self):
        import json
        shapes = {'loop': (True, 4.0, True), 'smoke': (False, 16.0, False), 'flash': (False, 1.4, False),
                  'fired': (False, 9.0, True)}
        for name, (loop, duration, _) in shapes.items():
            path = self.c.assets / ('effects/frostbite/%s.json' % name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'loop': loop, 'duration': duration}))
        kept = {name for name, (_, _, triggered) in shapes.items()
                if not self.effects.gameplay('effects/frostbite/%s.json' % name, triggered)}
        self.assertEqual(kept, {'loop', 'smoke'})

    def test_the_map_effects_setting(self):
        import os
        from unittest import mock
        for mode, placed in (('none', 0), ('ambient', 0), ('all', 1)):
            with mock.patch.dict(os.environ, {'AURA_MAP_EFFECTS': mode}):
                effects = bfn_map_effects.MapEffects(self.c, None, game='gw')
            effects.add({}, {'AutoStart': False, 'Blueprint': {'id': 'fx'}}, np.eye(4), 'layer')
            self.assertEqual(len(effects.pending), placed, mode)
        with mock.patch.dict(os.environ, {'AURA_MAP_EFFECTS': 'none'}):
            effects = bfn_map_effects.MapEffects(self.c, None, game='gw')
        effects.add({}, {'AutoStart': True, 'Blueprint': {'id': 'fx'}}, np.eye(4), 'layer')
        self.assertEqual(effects.pending, [])

    def test_the_decals_setting(self):
        import os
        from unittest import mock
        from convert import decals_on
        with mock.patch.dict(os.environ, {'AURA_MAP_DECALS': '0'}):
            self.assertFalse(decals_on())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('AURA_MAP_DECALS', None)
            self.assertTrue(decals_on())

    def test_the_map_converter_converts_garden_warfare_effects(self):
        _, converter = self.effects.module()
        self.assertIs(converter, self.c)


if __name__ == '__main__':
    unittest.main()
