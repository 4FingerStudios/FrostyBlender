"""Battle for Neighborville effects placed in a map, as 'frostbite_effect' places.

EffectReferenceObjectData places an EffectBlueprint. Each blueprint is converted once with
tools/effects.py (effect_data and its BfnConverter, owned by the effects tools) into
effects/frostbite/<hash>.json, which the add-on's effects_build.py plays. Effects that wait for
gameplay to start them (AutoStart off: capture zones, pickups, abilities) are skipped unless
AURA_BFN_MAP_EFFECTS=all, since a map at rest never shows them.
"""
import hashlib
import json
import os

import numpy as np


class MapEffects:
    def __init__(self, converter, textures):
        self.c = converter
        self.textures = textures
        self.pending = []
        self.data = {}            # blueprint guid -> (relative path or None, name)
        self.everything = os.environ.get('AURA_BFN_MAP_EFFECTS', '').lower() == 'all'
        self.counts = {'effect_references': 0, 'effects_placed': 0, 'effects_triggered_skipped': 0,
                       'effect_blueprints': 0, 'effects_empty': 0}
        self._effects = None

    def add(self, doc, obj, matrix, source):
        """Record one EffectReferenceObjectData; places are made after the map is cropped."""
        self.counts['effect_references'] += 1
        if not obj.get('AutoStart', True) and not self.everything:
            self.counts['effects_triggered_skipped'] += 1
            return
        found = self.c.resolve(doc, obj.get('Blueprint'))
        if not found or found[0]['objects'][0].get('$type') != 'EffectBlueprint':
            raise ValueError('Effect blueprint unresolved')
        from convert_legacy import transform
        world = matrix @ transform(obj.get('BlueprintTransform', {}))
        if not np.isfinite(world).all():
            raise ValueError('Effect transform is not finite')
        self.pending.append((found[0], world, source))

    def module(self):
        if self._effects is None:
            import effects
            self._effects = (effects, effects.BfnConverter(self.c.archive, self.textures, self.c.assets))
        return self._effects

    def convert(self, doc):
        """effects/frostbite/<hash>.json for one blueprint, or None when it has nothing to show."""
        if doc['guid'] in self.data:
            return self.data[doc['guid']]
        name = next((a['name'] for a, d in self.c.archive.documents.values() if d is doc), doc['guid'])
        title = name.split('/')[-1]
        effects, converter = self.module()
        try:
            data = effects.effect_data(self.c.archive, converter, doc, title)
        except Exception as error:  # one effect must not stop the map
            self.c.errors.append({'stage': 'effect', 'source': name, 'error': str(error)})
            data = None
        rel = None
        if data and (data['emitters'] or data['lights']):
            rel = 'effects/frostbite/%s.json' % hashlib.sha1(name.lower().encode()).hexdigest()[:16]
            (self.c.assets / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.c.assets / rel).write_text(json.dumps(data), encoding='utf-8')
            self.counts['effect_blueprints'] += 1
        elif data is not None:
            self.counts['effects_empty'] += 1
        self.data[doc['guid']] = (rel, name)
        return self.data[doc['guid']]

    def build(self, kept):
        """Places for the recorded effects whose position `kept(x, z)` accepts."""
        out = []
        for doc, world, source in self.pending:
            if not kept(world[0, 3], world[2, 3]):
                continue
            rel, name = self.convert(doc)
            if not rel:
                continue
            out.append({'kind': 'frostbite_effect', 'name': name.split('/')[-1], 'matrix': world.tolist(),
                        'tags': {'effect': rel, 'source': name, 'level_instance': source}})
            self.counts['effects_placed'] += 1
        _, converter = self._effects or (None, None)
        if converter is not None and hasattr(converter, 'finish_textures'):
            converter.finish_textures()
        return out
