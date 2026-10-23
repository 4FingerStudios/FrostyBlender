"""Convert base-game Plants vs. Zombies Garden Warfare 2 and Battle for Neighborville levels,
as exported by aura_frostbite_reader --level, into Aura meshes and a scene manifest.
All outputs are local game assets. Never commit them to the plugin repository.
"""
import argparse, hashlib, json, os, re, struct
from pathlib import Path
import numpy as np
from convert import Archive, png, write_mesh, write_skin, read_attr, skin_attrs
import bfn_maps
import bfn_terrain
import bfn_decals
import bfn_map_effects
import bfn_water

MESHSET_TYPE = 0x49B156D4
# Section stride, vertex declaration offset and stream slots of each game's MeshSet sections.
MESH_LAYOUT = {'gw2': (192, 0x34, 4), 'bfn': (224, 0x40, 16)}
# Material texture parameters, by lower-cased name, in order of preference.
ROLES = {
    'base': ['color', '__color', 'colortexture', 'diffuse', '_basecolor', '_diffuse', 'base_cstr', 'layer1_color', 'material2_color'],
    'normal': ['normal', '__normaltint', 'normaltexture', 'packednormaltexture', 'base_normhao', 'material2_normal'],
    'mask': ['asm', '__rsmemissive', 'rsmtexture', '_rsm', 'material2_asm'],
}
ALPHA_HINTS = ('foliage', 'grass', 'leaf', 'leaves', 'vegetation', 'vegitation', 'fern', 'ivy', 'decal', 'hedge', 'bush', 'flower', 'vine')
ENVIRONMENT_ROOTS = ('LevelData', 'SubWorldData', 'DetachedSubWorldData', 'WorldPartData', 'LayerData', 'PrefabBlueprint', 'SpatialPrefabBlueprint')
# Battle for Neighborville streams most world geometry from gameplay logic instead of AutoLoad,
# so its sub-worlds are chosen by name: everything except logic, game modes, cinematics and tests.
BFN_SKIPPED = re.compile(r'^objective|(^|_)(cine|cinematics|gamelogi?c|gamemode|audio|heatmap|test|ui|loadscreen|loading|movie|'
                         r'weeklyevents?|lwe|bosscrates?|minigame|minigamelogic|objectivelogic|activity|bossfight|'
                         r'coopmode|playground|festival|feastivus\d*|foodfight\d*|holdingpen|externalviewport|characterscreenshot|'
                         r'freeroam|autoplayers|soloplay|areatelemetry|challengeboard|parallaxcubemapcapture|whitebox)(_|$)')


def transform(value):
    """A LinearTransform as a column-vector 4x4 matrix (columns: right, up, forward, translation)."""
    m = np.eye(4, dtype=np.float64)
    for i, key in enumerate(('right', 'up', 'forward', 'trans')):
        v = value.get(key, value.get(key.capitalize(), {}))
        m[:3, i] = [v.get('x', 0), v.get('y', 0), v.get('z', 0)]
    return m


def cstring(data, at):
    return data[at:data.index(b'\0', at)].decode('utf8', 'replace')


def mesh_sections(data, geometry_of, game, skinned=False):
    """Drawable LOD0 sections of a Garden Warfare 2 or Battle for Neighborville MeshSet. With
    skinned=True, Battle for Neighborville sections also carry a 'skin' of skeleton bone ids and
    weights; their vertices hold bone ids directly."""
    stride, decl_at, slots = MESH_LAYOUT[game]
    lod = next((x for x in struct.unpack_from('<6Q', data, 0x20) if x), 0)
    if not lod or lod + 0x70 > len(data): raise ValueError('MeshSet has no LOD')
    count, table = struct.unpack_from('<II', data, lod + 8)
    index_format, index_size, vertex_size = struct.unpack_from('<III', data, lod + 0x54)
    if count > 512 or table + count * stride > len(data): raise ValueError('MeshSet section table out of range')
    guid = data[lod + 0x60:lod + 0x70].hex()
    geometry = geometry_of(guid)
    if geometry is None: raise ValueError('Geometry chunk missing: ' + guid)
    if len(geometry) < vertex_size + index_size: raise ValueError('Geometry chunk too short')
    # 46 is Battle for Neighborville's 32-bit index format, used by its largest meshes.
    dtype = {33: '<u2', 32: '<u4', 46: '<u4'}.get(index_format & 0xFF)
    if not dtype: raise ValueError(f'Unsupported index format {index_format}')
    results = []
    for i in range(count):
        s = table + i * stride
        name = cstring(data, struct.unpack_from('<Q', data, s + 8)[0])
        material, _, tris, first, voff, vc = struct.unpack_from('<6I', data, s + 0x10)
        # Unnamed sections are depth and shadow proxies with positions only.
        if not name or not tris or not vc: continue
        decl = s + decl_at
        elements = [tuple(data[decl + j * 4:decl + j * 4 + 4]) for j in range(16)]
        strides = struct.unpack_from(f'<{slots}H', data, decl + 64)
        element_count, stream_count = data[decl + 64 + 2 * slots], data[decl + 65 + 2 * slots]
        bases, base = [], voff
        for j in range(stream_count):
            bases.append(base); base += strides[j] * vc
        if base > vertex_size: raise ValueError('Vertex streams exceed buffer')
        attrs = {}
        for usage, fmt, offset, stream in elements[:element_count]:
            if (usage in (1, 6, 33) or skinned and usage in (2, 3, 4, 5)) and usage not in attrs and stream < stream_count:
                attrs[usage] = read_attr(geometry, bases[stream], vc, strides[stream], fmt, offset)
        if 1 not in attrs: continue
        isz = np.dtype(dtype).itemsize
        indices = np.frombuffer(geometry, dtype=dtype, count=tris * 3, offset=vertex_size + first * isz).astype(np.uint32)
        if indices.max() >= vc: raise ValueError('Section indices are not local')
        pos = attrs[1][:, :3].copy()
        uv = attrs[33][:, :2].copy() if 33 in attrs else np.zeros((vc, 2), np.float32)
        normal = attrs.get(6)
        if normal is not None:
            normal = normal[:, :3].copy()
            lengths = np.linalg.norm(normal, axis=1)
            if not np.isfinite(lengths).all() or (lengths < 0.5).mean() > 0.25: normal = None
            else: normal /= np.maximum(lengths, 1e-12)[:, None]
        if normal is None:
            normal = np.zeros((vc, 3), np.float32); tri = indices.reshape(-1, 3)
            face = np.cross(pos[tri[:, 1]] - pos[tri[:, 0]], pos[tri[:, 2]] - pos[tri[:, 0]])
            for j in range(3): np.add.at(normal, tri[:, j], face)
            lengths = np.linalg.norm(normal, axis=1); normal /= np.maximum(lengths, 1e-12)[:, None]; normal[lengths < 1e-12] = [0, 1, 0]
        skin = None
        if skinned and game == 'bfn':
            skin = skin_attrs(attrs, None, vc)
        results.append({'skin': skin, 'name': name, 'material': material, 'positions': pos, 'uv': uv, 'normals': normal,
                        'color': np.ones((vc, 3), np.float32), 'palette': np.zeros(vc, np.float32), 'indices': indices})
    return results


def havok_instances(data, expected):
    """Instance transforms of a Garden Warfare 2 static model group's physics: 128-byte shape
    instances holding three rotation rows, a translation, a scale and the physics part index."""
    floats = np.frombuffer(data[:len(data) // 4 * 4], dtype='<f4')
    def rotation(at):
        if at + 128 > len(data): return None
        m = floats[at // 4:at // 4 + 12].reshape(3, 4)[:, :3].astype(np.float64)
        if not np.isfinite(m).all() or np.abs(m @ m.T - np.eye(3)).max() > 2e-3: return None
        return m
    at = 0
    while at + 128 <= len(data):
        if rotation(at) is None: at += 16; continue
        run, start = [], at
        while rotation(at) is not None: run.append(at); at += 128
        if len(run) == expected:
            out = {}
            for r in run:
                m = rotation(r); trans = floats[r // 4 + 12:r // 4 + 15]; scale = floats[r // 4 + 16:r // 4 + 19]
                part = struct.unpack_from('<H', data, r + 0x54)[0]
                mat = np.eye(4); mat[:3, :3] = (m * scale[:, None]).T; mat[:3, 3] = trans
                out[part] = mat
            if len(out) == expected: return out
        at = max(at, start + 16)
    raise ValueError(f'No physics instance table with {expected} entries')


class LegacyConverter:
    def __init__(self, archive, textures, assets, game, everything=False, choice=None):
        self.archive = archive; self.game = game; self.everything = everything
        # Battle for Neighborville: one map of a level file ('tvm', 'davesmansion-night'; tools/bfn_maps.py).
        self.choice = choice; self.area = bfn_maps.chosen(choice)[0]
        self.subworlds = {n.split('/')[-1] for n, (a, d) in archive.documents.items() if a.get('root_type') == 'SubWorldData'}
        self.texdir = Path(textures); self.texture_info = json.loads((self.texdir / 'textures.json').read_text())['textures']
        self.assets = Path(assets); self.meshes = {}; self.materials = {}; self.texture_paths = {}; self.errors = []
        self.counts = {'triangles': 0, 'vertices': 0, 'instances': 0, 'skipped_hidden': 0, 'terrain_tiles': 0}
        self.by_name = {name: entry for name, entry in archive.documents.items()}
        self.variations = self.material_variations()
        self.floor = []
        self.terrains = set()
        # Battle for Neighborville water materials, decal volumes and placed effects (bfn_*.py).
        self.water = set()
        self.water_entities = []
        self.decals = bfn_decals.Decals(self)
        self.effects = bfn_map_effects.MapEffects(self, textures)

    # ---- references ----
    def resolve(self, doc, pointer):
        found = self.archive.resolve(doc, pointer)
        return found if found and found[1] is not None else None

    def material_variations(self):
        """Garden Warfare 2 keeps mesh texture bindings in each subworld's MeshVariationDatabase."""
        out = {}
        for name, (asset, doc) in self.archive.documents.items():
            if asset.get('root_type') != 'MeshVariationDatabase': continue
            for entry in doc['objects'][0].get('Entries', []):
                if entry.get('VariationAssetNameHash'): continue
                for material in entry.get('Materials', []):
                    ref = material.get('Material')
                    if not ref or ref.get('$pointer') != 2: continue
                    key = doc['imports'][ref['index']]['object']
                    out.setdefault(key, (doc, material.get('TextureParameters', [])))
        return out

    # ---- textures and materials ----
    def texture(self, doc, pointer):
        found = self.resolve(doc, pointer)
        if not found: return None, None
        return self.texture_object(found[1])

    def texture_object(self, obj):
        rid = (obj.get('Resource') or {}).get('$resource')
        info = self.texture_info.get(rid)
        if not info: return None, obj.get('Name', '')
        if rid not in self.texture_paths:
            raw = (self.texdir / info['path']).read_bytes(); w, h = struct.unpack_from('<II', raw)
            pixels = raw[8:]
            rel = 'textures/frostbite/' + rid + '.png'
            png(self.assets / rel, w, h, pixels)
            alpha = np.frombuffer(pixels, np.uint8)[3::4]
            self.texture_paths[rid] = (rel, float((alpha < 128).mean()))
        return self.texture_paths[rid], obj.get('Name', '')

    def material(self, mesh_doc, mesh_obj, index, section_name):
        materials = mesh_obj.get('Materials', [])
        found = self.resolve(mesh_doc, materials[index]) if index < len(materials) else None
        params = []
        if found:
            mat_doc, mat = found
            if self.game == 'bfn':
                graph = bfn_water.graph_name(self, mat_doc, mat)
                if bfn_water.is_water(graph):
                    rel = bfn_water.material(self, mat, graph, section_name)
                    self.water.add(rel)
                    return rel
            shader = mat.get('Shader') or {}
            # Battle for Neighborville materials often name a SurfaceShaderPreset as their shader,
            # which holds the textures. Then the mesh variation, then the material's own override them.
            preset, depth = self.resolve(mat_doc, shader.get('Shader')), 0
            while preset and preset[1].get('$type') == 'SurfaceShaderPreset' and depth < 4:
                inner = preset[1].get('ShaderPreset') or {}
                params = [(preset[0], p) for p in inner.get('TextureParameters', [])] + params
                preset, depth = self.resolve(preset[0], inner.get('Shader')), depth + 1
            if mat.get('$guid') in self.variations:
                var_doc, var_params = self.variations[mat['$guid']]
                params += [(var_doc, p) for p in var_params]
            params += [(mat_doc, p) for p in shader.get('TextureParameters', [])]
        # A later source overrides an earlier one only where its texture resolves.
        by_name = {}
        for d, p in params:
            if self.resolve(d, p.get('Value')) or (p.get('ParameterName') or '').lower() not in by_name:
                by_name[(p.get('ParameterName') or '').lower()] = (d, p)
        textures, names, transparency = {}, {}, 0.0
        for role, keys in ROLES.items():
            for key in keys:
                if key in by_name:
                    d, p = by_name[key]
                    result, tex_name = self.texture(d, p.get('Value'))
                    if result:
                        textures[role] = result[0]; names[role] = tex_name
                        if role == 'base': transparency = result[1]
                    break
        digest = hashlib.sha256(json.dumps([textures, section_name.lower()], sort_keys=True).encode()).hexdigest()[:20]
        if digest in self.materials: return self.materials[digest]
        params_rows = [[1, 1, 1, 1], [.7, 0, 1, 0], [1, 1, 0, 0], [0, 0, 0, 0]]
        # Battle for Neighborville packs its tangent-space normal in green and alpha.
        if self.game == 'bfn' and 'normal' in textures: params_rows[1][3] = 1
        hinted = any(h in (section_name + ' ' + names.get('base', '')).lower() for h in ALPHA_HINTS)
        # Character textures use alpha for masks, so cosmetics cut out only where the name says so.
        cutoff = .333 if transparency > .02 and (hinted or (transparency > .2 and getattr(self, 'cutouts', True))) else -1
        obj = {'format': 'aura-frostbite-material', 'version': 1, 'name': section_name, 'textures': textures,
               'texture_sources': names, 'parameters': params_rows, 'double_sided': True, 'alpha_cutoff': cutoff,
               'texture_srgb': {'base': True, 'normal': False, 'mask': False}}
        rel = 'materials/frostbite/' + digest + '.json'
        path = self.assets / rel; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(obj, indent=2))
        self.materials[digest] = rel
        return rel

    # ---- meshes ----
    def mesh(self, mesh_doc, mesh_obj):
        rid = (mesh_obj.get('MeshSetResource') or {}).get('$resource')
        if rid in self.meshes: return self.meshes[rid]
        res = self.archive.resources.get(rid)
        if not res: raise ValueError('Mesh resource missing: ' + mesh_obj.get('Name', '?'))
        def geometry(guid):
            chunk = self.archive.chunks.get(guid)
            return self.archive.data(chunk) if chunk else None
        sections = mesh_sections(self.archive.data(res), geometry, self.game, getattr(self, 'skinned', False))
        if not sections: raise ValueError('Mesh has no drawable sections: ' + mesh_obj.get('Name', '?'))
        mats = [self.material(mesh_doc, mesh_obj, s['material'], s['name']) for s in sections]
        rel = 'meshes/frostbite/' + rid + '.amsh'
        verts, tris = write_mesh(self.assets / rel, sections)
        write_skin((self.assets / rel).with_suffix('.skin'), sections)
        self.counts['vertices'] += verts; self.counts['triangles'] += tris
        allpos = np.concatenate([s['positions'] for s in sections]); allidx = []
        base = 0
        for s in sections: allidx.append(s['indices'] + base); base += len(s['positions'])
        self.meshes[rid] = (rel, mats, allpos.astype(np.float32), np.concatenate(allidx).reshape(-1, 3))
        return self.meshes[rid]

    def place(self, places, mesh_ref, doc, matrix, source):
        found = self.resolve(doc, mesh_ref)
        if not found: raise ValueError('Mesh asset unresolved')
        mesh_doc, mesh_obj = found
        if mesh_obj.get('$type') not in ('RigidMeshAsset', 'CompositeMeshAsset'): return
        rel, mats, positions, triangles = self.mesh(mesh_doc, mesh_obj)
        if not np.isfinite(matrix).all(): raise ValueError('Placement transform is not finite')
        name = mesh_obj.get('Name', '').split('/')[-1]
        tags = {'mesh': rel, 'source': mesh_obj.get('Name', '')}
        tags.update({f'material_{i}': m for i, m in enumerate(mats)})
        if self.water and all(m in self.water for m in mats):
            # A water surface: drawn, but neither walked on nor a spawn floor.
            tags['surface'] = 'water'
            # Seas are large meshes whose pivot can lie outside a cropped area they cover.
            world = positions @ matrix[:3, :3].T + matrix[:3, 3]
            bounds = (*world[:, [0, 2]].min(axis=0).tolist(), *world[:, [0, 2]].max(axis=0).tolist())
            places.append({'kind': 'frostbite_mesh', 'name': name, 'matrix': matrix.tolist(), 'tags': tags, 'values': {'collision': 0},
                           '_source': source, '_bounds': bounds})
            self.counts['instances'] += 1
            return
        places.append({'kind': 'frostbite_mesh', 'name': name, 'matrix': matrix.tolist(), 'tags': tags, 'values': {'collision': 1},
                       '_source': source})
        self.floor.append((rid_of(mesh_obj), matrix))
        self.counts['instances'] += 1

    # ---- terrain ----
    def terrain_material(self, terrain_obj):
        """An authored terrain grass surface: native terrain splat shaders are not executed."""
        key = 'terrain:' + (terrain_obj.get('Name') or '')
        if key in self.materials: return self.materials[key]
        textures = {}
        names = sorted(n for n, (a, d) in self.archive.documents.items() if a.get('root_type') == 'TextureAsset'
                       and '/terrain/terrain_' in n and 'grass' in n and 'scatter' not in n and n.endswith('_color'))
        names.sort(key=lambda n: ('grass_02' not in n, n))
        for name in names[:1]:
            for role, suffix in (('base', '_color'), ('normal', '_normal')):
                entry = self.by_name.get(name[:-len('_color')] + suffix)
                result = self.texture_object(entry[1]['objects'][0])[0] if entry else None
                if result: textures[role] = result[0]
        params_rows = [[1, 1, 1, 1] if 'base' in textures else [.32, .39, .22, 1], [.8, 0, 1, 1 if 'normal' in textures else 0], [1, 1, 0, 0], [0, 0, 0, 0]]
        obj = {'format': 'aura-frostbite-material', 'version': 1, 'name': 'BFN terrain surface', 'textures': textures,
               'texture_sources': {'base': names[0]} if names else {}, 'parameters': params_rows, 'double_sided': False,
               'alpha_cutoff': -1, 'texture_srgb': {'base': True, 'normal': False, 'mask': False},
               'source_terrain': terrain_obj.get('Name'),
               'surface_approximation': 'Authored grass texture; native terrain splat shaders are not executed.'}
        rel = 'materials/frostbite/bfn_terrain_' + hashlib.sha256(key.encode()).hexdigest()[:20] + '.json'
        path = self.assets / rel; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(obj, indent=2))
        self.materials[key] = rel
        return rel

    def terrain(self, places, doc, obj, matrix, source):
        if not obj.get('Visible', True): self.counts['skipped_hidden'] += 1; return
        found = self.resolve(doc, obj.get('TerrainAsset'))
        if not found: raise ValueError('Terrain asset unresolved')
        terrain_obj = found[1]
        rid = (terrain_obj.get('TerrainStreamingTreeResource') or {}).get('$resource')
        if rid in self.terrains: return
        self.terrains.add(rid)
        res = self.archive.resources.get(rid)
        if not res: raise ValueError('Terrain streaming tree resource missing: ' + (terrain_obj.get('Name') or '?'))
        tree = bfn_terrain.terrain_tree(self.archive.data(res))
        material = self.terrain_material(terrain_obj)
        world = matrix @ transform(obj.get('Transform', {}))
        spacing = float(os.environ.get('AURA_BFN_TERRAIN_SPACING', '2'))
        missing = 0
        for n in bfn_terrain.tiles(tree, spacing):
            chunk = None
            if n['heights'] is None:
                found = self.archive.chunks.get(n['guid'])
                if not found:
                    missing += 1
                    continue
                chunk = self.archive.data(found)
            section = bfn_terrain.tile_section(tree, n, chunk)
            if section is None: continue
            lo, hi = section['positions'].min(axis=0), section['positions'].max(axis=0)
            centre = (lo + hi) * .5
            section['positions'] -= centre
            key = 'bfn_terrain_%s_%s' % (rid, hashlib.sha256(struct.pack('<4f', *n['rect'])).hexdigest()[:8])
            rel = 'meshes/frostbite/' + key + '.amsh'
            verts, tris = write_mesh(self.assets / rel, [section])
            self.counts['vertices'] += verts; self.counts['triangles'] += tris
            self.meshes[key] = (rel, [material], section['positions'], section['indices'].reshape(-1, 3))
            local = np.eye(4); local[:3, 3] = centre
            placement = world @ local
            places.append({'kind': 'frostbite_mesh', 'name': 'BFN terrain tile', 'matrix': placement.tolist(),
                           'tags': {'mesh': rel, 'material_0': material, 'source': terrain_obj.get('Name', ''),
                                    'source_level': source, 'terrain_rect': list(n['rect'])},
                           'values': {'collision': 1}, '_source': source})
            self.floor.append((key, placement))
            self.counts['terrain_tiles'] += 1
        if missing:
            self.errors.append({'stage': 'terrain', 'source': source,
                                'error': '%d streamed terrain tile chunks missing from the export' % missing})

    # ---- scene graph ----
    def environment(self, ref):
        """Subworlds that load with the level in every mode: the map itself, not game-mode setups."""
        if self.everything: return True
        if self.game == 'bfn':
            leaf = (ref.get('BundleName') or '').replace('\\', '/').split('/')[-1].lower()
            return bool(leaf) and not BFN_SKIPPED.search(leaf) and bfn_maps.keeps(leaf, self.subworlds, self.choice)
        return ref.get('AutoLoad', True) and not ref.get('InclusionSettings')

    def walk(self, places, doc, obj, matrix, source, depth=0, stack=()):
        if depth > 32 or obj is None: return
        kind = obj.get('$type', '')
        key = (doc['guid'], obj.get('$guid') or id(obj))
        if key in stack: return
        stack = stack + (key,)
        try:
            if kind in ENVIRONMENT_ROOTS:
                for ref in obj.get('Objects', []) or []:
                    child = self.resolve(doc, ref)
                    if child: self.walk(places, child[0], child[1], matrix, source, depth + 1, stack)
                # Battle for Neighborville terrain layers list no objects; the terrain entity is
                # only in the partition.
                if self.game == 'bfn' and doc['objects'] and obj is doc['objects'][0]:
                    for other in doc['objects']:
                        if other.get('$type') == 'TerrainEntityData':
                            self.walk(places, doc, other, matrix, source, depth + 1, stack)
            elif kind == 'EffectReferenceObjectData' and self.game == 'bfn':
                if not obj.get('Excluded'): self.effects.add(doc, obj, matrix, source)
            elif kind == 'WaterEntityData' and self.game == 'bfn':
                self.water_entities.append((doc, obj, matrix @ transform(obj.get('Transform', {}))))
            elif kind == 'EnvironmentDecalVolumeData' and self.game == 'bfn':
                self.decals.add(doc, obj, matrix, source)
            elif kind.endswith('ReferenceObjectData'):
                if obj.get('Excluded'): return
                if kind == 'SubWorldReferenceObjectData' and not self.environment(obj): return
                child = self.resolve(doc, obj.get('Blueprint'))
                if not child and obj.get('BundleName'):
                    entry = self.by_name.get(obj['BundleName'].lower())
                    if entry and entry[1]['objects']: child = (entry[1], entry[1]['objects'][0])
                if not child: return
                local = matrix @ transform(obj.get('BlueprintTransform', {}))
                name = (obj.get('BundleName') or '').lower() or child[0].get('name') or source
                self.walk(places, child[0], child[1], local, name if kind == 'SubWorldReferenceObjectData' else source, depth + 1, stack)
            elif kind == 'ObjectBlueprint':
                child = self.resolve(doc, obj.get('Object'))
                if child: self.walk(places, child[0], child[1], matrix, source, depth + 1, stack)
            elif kind == 'StaticModelEntityData':
                if not obj.get('Visible', True): self.counts['skipped_hidden'] += 1; return
                self.place(places, obj.get('Mesh'), doc, matrix @ transform(obj.get('Transform', {})), source)
            elif kind == 'StaticModelGroupEntityData':
                self.group(places, doc, obj, matrix @ transform(obj.get('Transform', {})), source)
            elif kind == 'TerrainEntityData' and self.game == 'bfn':
                self.terrain(places, doc, obj, matrix, source)
        except Exception as e:
            self.errors.append({'stage': 'placement', 'type': kind, 'source': source, 'error': str(e)})

    def group(self, places, doc, obj, matrix, source):
        members = obj.get('MemberDatas', []) or []
        physics = None
        expected = sum(m.get('InstanceCount', 0) for m in members if len(m.get('InstanceTransforms', []) or []) != m.get('InstanceCount', 0))
        if expected:
            havok = next((o for o in doc['objects'] if o.get('$type') == 'GroupHavokAsset'), None)
            rid = ((havok or {}).get('Resource') or {}).get('$resource')
            res = self.archive.resources.get(rid)
            if not res: raise ValueError('Static model group transforms need its GroupHavokAsset resource')
            physics = havok_instances(self.archive.data(res), expected)
        for member in members:
            try:
                count = member.get('InstanceCount', 0)
                explicit = member.get('InstanceTransforms', []) or []
                visible = member.get('Visible')
                part_range = member.get('PhysicsPartRange') or {}
                per = max(1, member.get('PhysicsPartCountPerInstance', 1))
                for i in range(count):
                    if isinstance(visible, list) and len(visible) == count and not visible[i]:
                        self.counts['skipped_hidden'] += 1; continue
                    if len(explicit) == count: local = transform(explicit[i])
                    else:
                        part = part_range.get('First', 0) + i * per
                        if part not in physics: raise ValueError(f'Physics instance {part} missing')
                        local = physics[part]
                    self.place(places, member.get('MeshAsset'), doc, matrix @ local, source)
            except Exception as e:
                self.errors.append({'stage': 'group member', 'source': source, 'error': str(e)})

    # ---- one map of a bigger level ----
    def anchor_points(self):
        """Spawns and points of interest of the sub-worlds a choice's area names (a region's gamemode_tvm),
        which are game logic and so not placed."""
        points, seen = [], set()
        queue = [(entry[1], np.eye(4)) for name, entry in self.by_name.items()
                 if entry[0].get('root_type') == 'SubWorldData' and bfn_maps.anchor(name.split('/')[-1], self.area)]
        while queue:
            doc, matrix = queue.pop()
            if doc['guid'] in seen: continue
            seen.add(doc['guid'])
            for obj in doc['objects']:
                kind = obj.get('$type', '')
                if kind in ('SubWorldReferenceObjectData', 'LayerReferenceObjectData'):
                    child = self.resolve(doc, obj.get('Blueprint'))
                    entry = self.by_name.get((obj.get('BundleName') or '').lower())
                    target = child[0] if child else entry[1] if entry else None
                    if target: queue.append((target, matrix @ transform(obj.get('BlueprintTransform', {}))))
                elif kind.endswith('SpawnEntityData') or kind == 'PointOfInterestEntityData':
                    at = matrix @ transform(obj.get('Transform', {}))
                    points.append((at[0, 3], at[2, 3]))
        return points

    def crop(self, places):
        """Keep the placements around the chosen area: its own sub-world's placements, or else its spawns."""
        own = [(p['matrix'][0][3], p['matrix'][2][3]) for p in places
               if bfn_maps.anchor((p.get('_source') or '').split('/')[-1], self.area)]
        box = bfn_maps.bounds(own or self.anchor_points())
        if not box: raise ValueError('The level has nothing named %r to crop the map to' % self.area)
        self.crop_box = box
        kept = [p for p in places if bfn_maps.inside(box, p['matrix'][0][3], p['matrix'][2][3])
                or '_bounds' in p and p['_bounds'][0] <= box[2] and box[0] <= p['_bounds'][2]
                and p['_bounds'][1] <= box[3] and box[1] <= p['_bounds'][3]]
        self.floor = [(rid, m) for rid, m in self.floor if bfn_maps.inside(box, m[0, 3], m[2, 3])]
        self.counts['cropped'] = len(places) - len(kept)
        self.counts['crop_bounds'] = [round(v, 1) for v in box]
        return kept

    # ---- spawn ----
    def floor_at(self, x, z):
        best = None
        for rid, matrix in self.floor:
            rel, mats, positions, triangles = self.meshes[rid]
            lo, hi = positions.min(axis=0), positions.max(axis=0)
            corners = np.array([[a, b, c] for a in (lo[0], hi[0]) for b in (lo[1], hi[1]) for c in (lo[2], hi[2])]) @ matrix[:3, :3].T + matrix[:3, 3]
            lo, hi = corners.min(axis=0), corners.max(axis=0)
            if not (lo[0] <= x <= hi[0] and lo[2] <= z <= hi[2]): continue
            world = positions @ matrix[:3, :3].T + matrix[:3, 3]
            a, b, c = world[triangles[:, 0]], world[triangles[:, 1]], world[triangles[:, 2]]
            den = (b[:, 2] - c[:, 2]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 2] - c[:, 2]); ok = np.abs(den) > 1e-8
            if not ok.any(): continue
            a, b, c, den = a[ok], b[ok], c[ok], den[ok]
            n = np.cross(b - a, c - a); up = np.abs(n[:, 1]) > 0.7 * np.maximum(np.linalg.norm(n, axis=1), 1e-12)
            a, b, c, den = a[up], b[up], c[up], den[up]
            if not len(a): continue
            u = ((b[:, 2] - c[:, 2]) * (x - c[:, 0]) + (c[:, 0] - b[:, 0]) * (z - c[:, 2])) / den
            v = ((c[:, 2] - a[:, 2]) * (x - c[:, 0]) + (a[:, 0] - c[:, 0]) * (z - c[:, 2])) / den
            w = 1 - u - v; inside = (u >= 0) & (v >= 0) & (w >= 0)
            if not inside.any(): continue
            y = (u * a[:, 1] + v * b[:, 1] + w * c[:, 1])[inside]
            # A walkable floor: the highest surface facing up.
            top = float(y.max())
            best = top if best is None else max(best, top)
        return best

    def flat_floor(self, x, z):
        """A floor height the editor's 0.2 m spawn probe agrees with."""
        heights = [self.floor_at(x + dx, z + dz) for dx, dz in ((0, 0), (.2, 0), (-.2, 0), (0, .2), (0, -.2))]
        if any(h is None for h in heights) or max(heights) - min(heights) > .05: return None
        return heights[0]

    def scene(self, root_name, title):
        entry = self.by_name.get(root_name.lower())
        if not entry: raise ValueError('Level root document missing: ' + root_name)
        places = []
        self.walk(places, entry[1], entry[1]['objects'][0], np.eye(4), root_name)
        if self.area: places = self.crop(places)
        for p in places: p.pop('_source', None); p.pop('_bounds', None)
        if not places: raise ValueError('No map placements were converted')
        positions = np.array([np.array(p['matrix'])[:3, 3] for p in places])
        spawn = np.median(positions, axis=0)
        floor = None
        order = np.argsort(np.linalg.norm(positions[:, [0, 2]] - spawn[[0, 2]], axis=1))
        for candidate in [spawn] + [positions[i] for i in order[:300]]:
            floor = self.flat_floor(candidate[0], candidate[2])
            if floor is not None: spawn = np.array(candidate, dtype=np.float64); break
        if floor is None: raise ValueError('No flat floor found for the editor spawn; choose a spawn explicitly')
        spawn[1] = floor + 0.5
        if self.game == 'bfn':
            # Decals and effects come after the spawn is chosen, so they do not move it.
            box = getattr(self, 'crop_box', None)
            kept = (lambda x, z: bfn_maps.inside(box, x, z)) if box else (lambda x, z: True)
            places += bfn_water.sea(self, self.water_entities, places, box)
            places += self.decals.build(places, kept)
            places += self.effects.build(kept)
            self.counts.update(self.decals.counts)
            self.counts.update(self.effects.counts)
            self.counts['water_surfaces'] = sum(p.get('tags', {}).get('surface') == 'water' for p in places)
            self.counts['sea_levels'] = [p['tags']['sea_level'] for p in places if 'sea_level' in p.get('tags', {})]
        places.insert(0, {'kind': 'frostbite_environment', 'name': title + ' outdoor settings', 'at': [0, 0, 0], 'tags': {},
                          'values': {'stream_load_distance': 256, 'stream_unload_distance': 320, 'sun_intensity': 2.4}})
        places.insert(0, {'kind': 'spawn', 'name': title + ' editor spawn', 'at': spawn.tolist(), 'tags': {}, 'values': {}})
        report = {'map': root_name, 'game': self.game, 'placements': len(places), 'meshes': len(self.meshes), 'materials': len(self.materials),
                  'textures': len(self.texture_paths), 'counts': self.counts, 'errors': self.errors,
                  'limitations': (['Static meshes, terrain heightfields, water surfaces, decal volumes and ambient effects; gameplay entities are not converted.',
                                   'Water uses the authored water meshes with an animated Blender water shader; depth colour, foam and flow are not executed.',
                                   'Decal volumes are projected onto the map triangles inside them and alpha blended; native decal masks are not executed.',
                                   'Effects that gameplay starts (AutoStart off) are skipped; set AURA_BFN_MAP_EFFECTS=all to place them.']
                                  if self.game == 'bfn' else
                                  ['Static meshes and Battle for Neighborville terrain heightfields only: water, decals, effects and gameplay entities are not converted.']) + [
                                  'Terrain uses one authored grass surface; native terrain splat layers, holes and scatter meshes are not translated.',
                                  'Rendered triangles serve as collision; original Havok collision is not translated.',
                                  'Materials use base colour and normal maps with Aura shading approximations.',
                                  'Game-mode sublevels are skipped unless --everything is given.']}
        return {'format': 'aura-frostbite-scene', 'version': 1, 'name': title, 'places': places}, report


def rid_of(mesh_obj):
    return (mesh_obj.get('MeshSetResource') or {}).get('$resource')


def level_root(level):
    """The reader's level key names the root document: Win32/levels/a/a and win32/levels/a both give levels/a/a."""
    parts = [p for p in level.lower().replace('\\', '/').split('/') if p]
    if parts and parts[0] == 'win32': parts = parts[1:]
    if len(parts) == 2: parts.append(parts[1])
    return '/'.join(parts[:3])


def title_of(level):
    leaf = level_root(level).split('/')[-1]
    return ' '.join(w.capitalize() for w in re.sub(r'^(level|dsub)_', '', leaf).split('_'))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--export', required=True); p.add_argument('--textures', required=True); p.add_argument('--assets', required=True)
    p.add_argument('--out', required=True); p.add_argument('--level', required=True); p.add_argument('--title')
    p.add_argument('--everything', action='store_true', help='also place game-mode sublevels')
    p.add_argument('--variant', help="Battle for Neighborville: one map of the level file, e.g. 'tvm' or 'davesmansion-night'")
    a = p.parse_args()
    if a.variant and not re.fullmatch(r'[0-9a-z-]+', a.variant): raise SystemExit('Invalid map variant')
    archive = Archive(a.export)
    game = archive.inventory.get('game')
    if game not in MESH_LAYOUT: raise SystemExit('Export is not a Garden Warfare 2 or Battle for Neighborville level')
    converter = LegacyConverter(archive, a.textures, a.assets, game, a.everything, a.variant if game == 'bfn' else None)
    title = a.title
    if not title and game == 'bfn' and archive.inventory.get('game_root'):
        title = bfn_maps.title(archive.inventory['game_root'], level_root(a.level).split('/')[-1], a.variant)
    scene, report = converter.scene(level_root(a.level), title or title_of(a.level))
    report['variant'] = a.variant
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(scene, indent=2)); out.with_suffix('.report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k not in ['errors', 'limitations']}, indent=2)); print('Errors:', len(report['errors']))


if __name__ == '__main__':
    main()
