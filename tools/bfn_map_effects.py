"""Effects placed in a map, as 'frostbite_effect' places: Battle for Neighborville (convert_legacy.py) and
Garden Warfare 1 and 2 (gw2_convert.py, game='gw').

EffectReferenceObjectData places an EffectBlueprint. Each blueprint is converted once with
tools/effects.py (effect_data and its BfnConverter, owned by the effects tools) into
effects/frostbite/<hash>.json, which the add-on's effects_build.py plays (Garden Warfare effects are
converted by the map's own Gw2Converter). Effects that wait for gameplay to start them (AutoStart off: capture
zones, pickups, abilities) are skipped unless AURA_MAP_EFFECTS=all (the add-on's Map effects setting), since a map at rest never shows them; none places none.

Simulating an effect runs its emitters' compute shaders on the CPU, seconds per effect, and maps share their
effects. So each converted effect is kept, with the files it uses, in <output folder>/.cache/bfn_effects,
(bfn_effects or gw_effects), keyed by the blueprint and a hash of the simulation code: an import that meets it again copies it instead.
"""
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import numpy as np

SHORT_ONE_SHOT = 5.0  # seconds: Garden Warfare one-shots this short are gameplay events (pickups, flashes)
ASSET_REF = re.compile(r'"((?:textures|meshes|materials)/frostbite/[^"]+\.(?:png|amsh|json))"')


def _files(text, root):
    """The asset files an effect uses, under root: its textures, particle meshes and their materials, and the
    textures those materials use."""
    found, queue = set(), list(ASSET_REF.findall(text or ''))
    while queue:
        rel = queue.pop()
        if rel in found:
            continue
        found.add(rel)
        if rel.endswith('.json'):
            try:
                queue += ASSET_REF.findall((root / rel).read_text(encoding='utf-8'))
            except OSError:
                pass
    return found


def _code_version():
    """Changes whenever the effect simulation does, so stale cached effects are never used."""
    digest = hashlib.sha1()
    for name in ('effects.py', 'emitter_graph.py', 'dxbc.py', 'bfn_map_effects.py'):
        path = Path(__file__).with_name(name)
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


class MapEffects:
    def __init__(self, converter, textures, game='bfn'):
        self.c = converter
        self.game = game
        self.textures = textures
        self.pending = []
        self.data = {}            # blueprint guid -> (relative path or None, name)
        # Map settings: AURA_MAP_EFFECTS none (no effects), ambient (default) or all (gameplay effects too).
        mode = (os.environ.get('AURA_MAP_EFFECTS') or os.environ.get('AURA_BFN_MAP_EFFECTS') or 'ambient').lower()
        self.none = mode == 'none'
        self.everything = mode == 'all'
        self.counts = {'effect_references': 0, 'effects_placed': 0, 'effects_triggered_skipped': 0,
                       'effect_blueprints': 0, 'effects_empty': 0}
        self._effects = None
        self._shape = {}          # effect file -> (loops, duration)
        cache = os.environ.get('AURA_BFN_EFFECT_CACHE')
        folder = 'bfn_effects' if game == 'bfn' else 'gw_effects'
        self.cache = (Path(cache).parent / folder if cache and game != 'bfn' else Path(cache)) if cache else             Path(self.c.assets).resolve().parent.parent / '.cache' / folder
        self.version = _code_version()

    def entry_name(self, guid):
        """The cache file of a blueprint. Garden Warfare keys it also by which of its emitters and textures this export
        holds, so a map exported again with more of them (atlas pixels, say) is not served the poorer conversion."""
        if self.game == 'bfn':
            return '%s_%s.json' % (guid, self.version)
        return '%s_%s_%s.json' % (guid, self.version, self.fingerprint(guid))

    def fingerprint(self, guid):
        archive = self.c.archive
        found = archive.by_guid.get(guid)
        parts, seen, queue = [], set(), [found[1]] if found else []
        while queue and len(seen) < 256:
            doc = queue.pop()
            if doc.get('guid') in seen:
                continue
            seen.add(doc.get('guid'))
            for ref in doc.get('imports', []):
                item = archive.by_guid.get(ref.get('file'))
                root = item[1]['objects'][0] if item and item[1].get('objects') else {}
                kind = root.get('$type', '')
                state = '-' if not item else '+'
                if kind in ('ScalableEmitterDocument', 'EmitterDocument', 'GpuRuntimeEmitterDocument'):
                    queue.append(item[1])
                elif kind in ('TextureAsset', 'AtlasTextureAsset'):
                    rid = (root.get('Resource') or {}).get('$resource')
                    resource = archive.resources.get(rid)
                    if kind == 'TextureAsset':
                        state += 'p' if rid in getattr(self.c, 'texture_info', {}) else 'x'
                    elif resource:
                        try:
                            header = (archive.root / resource['path']).read_bytes()[:32]
                            state += 'p' if header[16:32].hex() in archive.chunks else 'x'
                        except (OSError, KeyError):
                            state += 'x'
                parts.append(ref.get('file', '') + state)
        return hashlib.sha1('|'.join(sorted(parts)).encode()).hexdigest()[:10]

    def cached(self, guid, rel):
        """A cached conversion of this blueprint, copied into this import with its files; None when there is none,
        'empty' when the blueprint has nothing to show."""
        entry = self.cache / self.entry_name(guid)
        try:
            text = entry.read_text(encoding='utf-8')
            if text == 'null':
                return 'empty'
            for used in _files(text, self.cache):
                target = Path(self.c.assets) / used
                if not target.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(self.cache / used, target)
            (Path(self.c.assets) / rel).parent.mkdir(parents=True, exist_ok=True)
            (Path(self.c.assets) / rel).write_text(text, encoding='utf-8')
        except OSError:
            return None
        self.counts['effects_from_cache'] = self.counts.get('effects_from_cache', 0) + 1
        return rel

    def remember(self, guid, text):
        """Keep a fresh conversion (text None: nothing to show) and its files for later imports."""
        try:
            for used in _files(text, Path(self.c.assets)):
                source, target = Path(self.c.assets) / used, self.cache / used
                if source.is_file() and not target.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
            self.cache.mkdir(parents=True, exist_ok=True)
            (self.cache / self.entry_name(guid)).write_text(text or 'null', encoding='utf-8')
        except OSError:
            pass  # a cache that can't be written only costs the next import time

    def add(self, doc, obj, matrix, source, triggered=False):
        """Record one EffectReferenceObjectData; places are made after the map is cropped. triggered: the
        level's logic sends the effect events (Garden Warfare marks gameplay effects AutoStart too)."""
        self.counts['effect_references'] += 1
        if self.none:
            self.counts['effects_switched_off'] = self.counts.get('effects_switched_off', 0) + 1
            return
        if not obj.get('AutoStart', True) and not self.everything:
            self.counts['effects_triggered_skipped'] += 1
            return
        resolve = getattr(self.c, 'resolve', None) or self.c.archive.resolve
        found = resolve(doc, obj.get('Blueprint'))
        if not found or found[0]['objects'][0].get('$type') != 'EffectBlueprint':
            raise ValueError('Effect blueprint unresolved')
        if self.game == 'bfn':
            from convert_legacy import transform
        else:
            from convert import matrix as transform
        world = matrix @ transform(obj.get('BlueprintTransform') or obj.get('Transform') or {})
        if not np.isfinite(world).all():
            raise ValueError('Effect transform is not finite')
        self.pending.append((found[0], world, source, triggered))

    def module(self):
        if self._effects is None:
            import effects
            # Garden Warfare effects read their textures and meshes through the map's own converter.
            self._effects = (effects, effects.BfnConverter(self.c.archive, self.textures, self.c.assets)
                             if self.game == 'bfn' else self.c)
        return self._effects

    def convert(self, doc):
        """effects/frostbite/<hash>.json for one blueprint, or None when it has nothing to show."""
        if doc['guid'] in self.data:
            return self.data[doc['guid']]
        name = next((a['name'] for a, d in self.c.archive.documents.values() if d is doc), doc['guid'])
        title = name.split('/')[-1]
        rel = 'effects/frostbite/%s.json' % hashlib.sha1(name.lower().encode()).hexdigest()[:16]
        hit = self.cached(doc['guid'], rel)
        if hit:
            self.data[doc['guid']] = (None if hit == 'empty' else rel, name)
            return self.data[doc['guid']]
        effects, converter = self.module()
        try:
            data = effects.effect_data(self.c.archive, converter, doc, title)
        except Exception as error:  # one effect must not stop the map
            self.c.errors.append({'stage': 'effect', 'source': name, 'error': str(error)})
            data = None
            failed = True
        else:
            failed = False
        if data and (data['emitters'] or data['lights']):
            text = json.dumps(data)
            (self.c.assets / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.c.assets / rel).write_text(text, encoding='utf-8')
            self.counts['effect_blueprints'] += 1
            self.remember(doc['guid'], text)
        else:
            rel = None
            if data is not None:
                self.counts['effects_empty'] += 1
            if not failed:
                self.remember(doc['guid'], None)
        self.data[doc['guid']] = (rel, name)
        return self.data[doc['guid']]

    def gameplay(self, rel, triggered):
        """A Garden Warfare one-shot that gameplay fires: the level's logic sends it events, or it is a short
        burst (pickups, spawn flashes, explosions). Loops and long one-shots (wreck smoke) are ambient."""
        if rel not in self._shape:
            try:
                data = json.loads((Path(self.c.assets) / rel).read_text(encoding='utf-8'))
                self._shape[rel] = (bool(data.get('loop')), float(data.get('duration', 0)))
            except (OSError, ValueError):
                self._shape[rel] = (True, 0.0)
        loop, duration = self._shape[rel]
        return not loop and (triggered or duration < SHORT_ONE_SHOT)

    def build(self, kept):
        """Places for the recorded effects whose position `kept(x, z)` accepts."""
        out = []
        for doc, world, source, triggered in self.pending:
            if not kept(world[0, 3], world[2, 3]):
                continue
            rel, name = self.convert(doc)
            if not rel:
                continue
            if self.game != 'bfn' and not self.everything and self.gameplay(rel, triggered):
                self.counts['effects_triggered_skipped'] += 1
                continue
            out.append({'kind': 'frostbite_effect', 'name': name.split('/')[-1], 'matrix': world.tolist(),
                        'tags': {'effect': rel, 'source': name, 'level_instance': source}})
            self.counts['effects_placed'] += 1
        _, converter = self._effects or (None, None)
        if converter is not None and converter is not self.c and hasattr(converter, 'finish_textures'):
            converter.finish_textures()
        return out
