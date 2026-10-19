"""Static GW2 environment conversion; authored gameplay and Havok are not ported."""
import json
import hashlib
import struct
from pathlib import Path
import numpy as np
from convert import Archive, Converter, matrix, read_attr, write_mesh, write_skin, skin_attrs
from gw2_terrain import terrain_tree, tile_heights, height_section
from gw2_groups import group_instances, gw1_part_poses


class SkinnedMeshUnsupported(ValueError):
    """Animated geometry is outside this static environment importer."""


class LazyDocument(dict):
    """Read referenced EBX documents on demand, rather than opening every asset."""
    def __init__(self, path):
        self.path = path
        self.loaded = False

    def __getitem__(self, key):
        if not self.loaded:
            self.update(json.loads(self.path.read_text()))
            self.loaded = True
        return super().__getitem__(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


class Gw2Archive(Archive):
    def __init__(self, root):
        self.root = Path(root)
        self.inventory = json.loads((self.root / 'inventory.json').read_text())
        self.assets = self.inventory['assets']
        self.resources, self.chunks, self.documents, self.by_guid, self.shaders = {}, {}, {}, {}, {}
        for asset in self.assets:
            if 'bytes' not in asset:
                continue
            if asset['kind'] == 'resource':
                self.resources[asset['resource_id']] = asset
            elif asset['kind'] == 'chunk':
                self.chunks[asset['guid']] = asset
            if 'document' in asset:
                doc = LazyDocument(self.root / asset['document'])
                self.documents[asset['name']] = (asset, doc)
                self.by_guid[asset.get('guid') or doc['guid']] = (asset, doc)


def mesh_sections(archive, resource, skinned=False):
    """Drawable LOD0 sections. Skinned meshes (characters, cosmetics) share the rigid layout;
    with skinned=True they come back in their bind pose with a 'skin' of skeleton bone indices
    and weights: a section's byte at +43 counts its bone palette, a u16 list at the offset in +44."""
    data = archive.data(resource)
    if len(data) >= 40 and not 0 < struct.unpack_from('<Q', data, 32)[0] < len(data):
        lod = struct.unpack_from('>I', data, 32)[0]
        if 0 < lod and lod + 4 <= len(data) and struct.unpack_from('>I', data, lod)[0] == 1 and not skinned:
            raise SkinnedMeshUnsupported('Xenon skinned mesh requires animation support')
        from gw1_xenon import mesh_sections as xenon_sections
        return xenon_sections(archive, resource, skinned)
    if len(data) < 128:
        raise ValueError('Truncated GW2 MeshSet')
    lod = struct.unpack_from('<Q', data, 32)[0]
    mesh_type, instances, count, table = struct.unpack_from('<IIIQ', data, lod)
    if mesh_type == 1 and not skinned:
        raise SkinnedMeshUnsupported('GW2 skinned mesh requires animation support')
    if mesh_type not in (0, 1, 2) or not 0 < count <= 4096:
        raise ValueError('GW2 mesh is not a static/composite environment mesh')
    # Garden Warfare 1 MeshSets have no index format field (indices are 16-bit) and their
    # section fields after the material sit 4 bytes earlier.
    gw1 = getattr(archive, 'inventory', {}).get('profile') == 'gw1'
    if gw1:
        index_size, vertex_size = struct.unpack_from('<II', data, lod + 76)
        fmt, guid, shift = 33, data[lod + 88:lod + 104].hex(), 4
    else:
        fmt, index_size, vertex_size = struct.unpack_from('<III', data, lod + 84)
        guid, shift = data[lod + 96:lod + 112].hex(), 0
    if guid not in archive.chunks:
        raise ValueError('GW2 geometry chunk missing: ' + guid)
    geometry = archive.data(archive.chunks[guid])
    if vertex_size + index_size > len(geometry):
        raise ValueError('GW2 geometry chunk is truncated')
    results = []
    for i in range(count):
        at = table + i * 192
        if at + 192 > len(data):
            raise ValueError('GW2 mesh section table overrun')
        name_offset = struct.unpack_from('<Q', data, at + 8)[0]
        material = struct.unpack_from('<I', data, at + 16)[0]
        triangles, first, offset, vertices = struct.unpack_from('<4I', data, at + 24 - shift)
        if not vertices or not triangles:
            continue
        if data[at + 41 - shift] != 3:
            raise ValueError('GW2 section does not contain triangle primitives')
        declaration = at + 52 - shift
        elements, streams = data[declaration + 72:declaration + 74]
        if not 0 < streams <= 4 or not 0 < elements <= 16:
            raise ValueError('Invalid GW2 vertex declaration')
        bases, strides = [], []
        for stream in range(streams):
            stride = data[declaration + 64 + stream * 2]
            bases.append(offset)
            strides.append(stride)
            offset += stride * vertices
        if offset > vertex_size:
            raise ValueError('GW2 vertex stream overrun')
        attrs = {}
        for element in range(elements):
            usage, fmt_attr, attr_offset, stream = data[declaration + element * 4:declaration + element * 4 + 4]
            if stream >= streams:
                raise ValueError('GW2 vertex stream index invalid')
            if usage in (1, 6, 33) or skinned and usage in (2, 3, 4, 5):
                attrs[usage] = read_attr(geometry, bases[stream], vertices, strides[stream], fmt_attr, attr_offset)
        if 33 not in attrs:
            # No texture coordinates: a depth/shadow proxy over the visible sections (Rose's robe had one
            # that covered it in the corner texel's green).
            continue
        dtype = '<u2' if fmt == 33 else '<u4' if fmt == 46 else None
        if dtype is None:
            raise ValueError('Unsupported GW2 index format')
        indices = np.frombuffer(geometry, dtype=dtype, count=triangles * 3,
                                offset=vertex_size + first * np.dtype(dtype).itemsize).astype(np.uint32)
        if indices.max() >= vertices:
            raise ValueError('Invalid GW2 section index')
        positions = attrs[1][:, :3].copy()
        normals = attrs.get(6)
        if normals is None:
            normals = np.zeros((vertices, 3), np.float32)
            tris = indices.reshape(-1, 3)
            faces = np.cross(positions[tris[:, 1]] - positions[tris[:, 0]], positions[tris[:, 2]] - positions[tris[:, 0]])
            for corner in range(3):
                np.add.at(normals, tris[:, corner], faces)
        normals = normals[:, :3].copy()
        normals /= np.maximum(np.linalg.norm(normals, axis=1), 1e-12)[:, None]
        name = data[name_offset:data.index(b'\0', name_offset)].decode('utf-8')
        skin = None
        if skinned and mesh_type == 1:
            bone_count, bone_list = data[at + 43 - shift], struct.unpack_from('<I', data, at + 44 - shift)[0]
            if bone_list + bone_count * 2 > len(data):
                raise ValueError('GW2 section bone palette overrun')
            skin = skin_attrs(attrs, struct.unpack_from('<%dH' % bone_count, data, bone_list), vertices)
        results.append({'skin': skin, 'name': name, 'positions': positions, 'normals': normals,
                        'uv': attrs.get(33, np.zeros((vertices, 2), np.float32))[:, :2].copy(),
                        'indices': indices, 'color': np.ones((vertices, 3), np.float32),
                        'palette': np.zeros(vertices, np.float32), 'material_id': material})
    if not results:
        raise ValueError('GW2 mesh has no visible triangle sections')
    return results


class Gw2Converter(Converter):
    def __init__(self, archive, textures, assets):
        super().__init__(archive, textures, assets)
        self.variations = {}
        self.skipped = {}
        self.skipped_meshes = {}
        self.floor_meshes = {}
        self.terrain_tiles = 0
        self.grouped_instances = 0
        self.unplaced_group_instances = []
        for asset, doc in archive.documents.values():
            if asset.get('root_type') != 'MeshVariationDatabase':
                continue
            for entry in doc['objects'][0].get('Entries', []):
                mesh = archive.resolve(doc, entry.get('Mesh'))
                if mesh:
                    self.variations[(mesh[0]['guid'], entry.get('VariationAssetNameHash', 0))] = (doc, entry)

    def material_for(self, mesh, slot, variation):
        shader_doc = mesh[0]
        material = self.archive.resolve(shader_doc, mesh[1].get('Materials', [])[slot])
        shader = material[1].get('Shader', {}) if material else {}
        parameters = [(material[0], item) for item in shader.get('TextureParameters', [])] if material else []
        graph = self.archive.resolve(material[0], shader.get('Shader')) if material else None
        graph_name = graph[1].get('Name', '') if graph else ''
        shader_layers = [shader]
        variation_shader = None
        selected = self.variations.get((mesh[0]['guid'], variation)) or self.variations.get((mesh[0]['guid'], 0))
        if selected and slot < len(selected[1].get('Materials', [])):
            selected_material = selected[1]['Materials'][slot]
            override = self.archive.resolve(selected[0], selected_material.get('MaterialVariation'))
            if override:
                variation_shader = override[1].get('Shader', {})
                shader_layers.append(variation_shader)
                parameters += [(override[0], item) for item in variation_shader.get('TextureParameters', [])]
                override_graph = self.archive.resolve(override[0], variation_shader.get('Shader'))
                if override_graph:
                    graph_name = override_graph[1].get('Name', graph_name)
            parameters += [(selected[0], item) for item in selected_material.get('TextureParameters', [])]
        cutoff = .5 if 'alphatest' in graph_name.lower() else -1
        bindings = {}
        texture_asset_names = {}
        unresolved = []
        for doc, parameter in parameters:
            texture = self.archive.resolve(doc, parameter.get('Value'))
            key = parameter.get('ParameterName', '')
            if texture:
                bindings[key] = texture[0]['guid']
                texture_asset_names[key] = texture[1].get('Name', '')
            elif parameter.get('Value', {}).get('$pointer'):
                unresolved.append(key)
        paths = {}
        # Garden Warfare 1 blend shaders name their first layer Material1_*.
        for role, names in [('base', ('Color', 'Diffuse', 'BaseColor', 'Albedo', 'DiffuseTexture', 'Material1_Diffuse', 'PositiveDiffuse')),
                            ('normal', ('Normal', 'NormalMap', 'NormalTexture', 'Material1_Normal', 'PositiveNormal')),
                            ('mask', ('ASM', 'Mask', 'Specular')),
                            ('emissive', ('Emissive', 'EmissiveColor')),
                            # Read by the add-on's optional game shaders (see materials.py there).
                            ('asm', ('ASM',)), ('ett', ('ETT',)),
                            ('specular_masks', ('Specular_Masks', 'SpecularMasks', 'RoughnessSpecTint'))]:
            guid = next((bindings[name] for name in names if name in bindings), None)
            if guid:
                path = self.texture(guid)
                if path:
                    paths[role] = path
                else:
                    unresolved.append(role + ':' + guid)
        # Keep every authored binding, including GW1's PSA, detail, animated
        # light masks, quadrant textures and the second layer of blend shaders.
        named_textures = {}
        for name, guid in bindings.items():
            path = self.texture(guid)
            if path:
                named_textures[name] = path
        for role, name in (('psa', 'PSA'), ('detail', 'Detail_Map'), ('light_mask', 'Light_Mask')):
            if name in named_textures:
                paths[role] = named_textures[name]
        if unresolved:
            # A texture the game files do not hold must not cost the whole mesh: keep the rest.
            self.errors.append({'stage': 'material', 'name': mesh[1].get('Name', ''),
                                'error': 'GW2 material textures unresolved: ' + ', '.join(unresolved)})
        vectors = {p.get('ParameterName', ''): [float(p.get('Value', {}).get(c, 0)) for c in 'xyzw']
                   for layer in shader_layers for p in layer.get('VectorParameters', []) if p.get('ParameterName')}
        tint = next((vectors[name] for name in ('Tint_Color', 'Tint_Colour', 'ColorTint')
                     if name in vectors), [1, 1, 1, 1])
        content = {'textures': paths, 'named_textures': named_textures,
                   'texture_bindings': bindings, 'texture_asset_names': texture_asset_names, 'source_shader': shader,
                   'source_shader_variation': variation_shader,
                   'shader_name': graph_name, 'alpha_cutoff': cutoff, 'tint': tint, 'vectors': vectors}
        digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:20]
        if digest in self.materials:
            return self.materials[digest]
        rel = 'materials/frostbite/gw2_' + digest + '.json'
        content.update({'format': 'aura-frostbite-material', 'version': 1, 'name': mesh[1].get('Name', ''),
                        'profile': self.archive.inventory.get('profile', 'gw2'), 'double_sided': True,
                        'parameters': [tint, [.7, 0, 1, 0], [1, 1, 0, 0], [0, 0, 0, 0]]})
        path = self.assets / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content, indent=2), encoding='utf-8')
        self.materials[digest] = rel
        return rel

    def convert_static(self, mesh, variation):
        resource_id = mesh[1]['MeshSetResource']['$resource']
        key = (resource_id, variation)
        if key in self.meshes:
            return self.meshes[key]
        resource = self.archive.resources.get(resource_id)
        if not resource:
            raise ValueError('GW2 MeshSet resource unavailable: ' + resource_id)
        sections = mesh_sections(self.archive, resource, getattr(self, 'skinned', False))
        materials = [self.material_for(mesh, section['material_id'], variation) for section in sections]
        rel = f'meshes/frostbite/gw2_{resource_id}.amsh'
        vertices, triangles = write_mesh(self.assets / rel, sections)
        write_skin((self.assets / rel).with_suffix('.skin'), sections)
        self.counts['vertices'] += vertices
        self.counts['triangles'] += triangles
        self.meshes[key] = (rel, materials)
        return rel, materials

    def convert_terrain(self, terrain):
        resource_id = terrain[1]['TerrainStreamingTreeResource']['$resource']
        resource = self.archive.resources.get(resource_id)
        if resource is None:
            raise ValueError('GW2 terrain streaming resource unavailable')
        tree = terrain_tree(self.archive.data(resource))
        # Terrain shaders combine many splat layers. Use an authored grass
        # surface while retaining the actual heightfield, not a flat proxy.
        textures = {}
        for role, suffix in (('base','_color'), ('normal','_normal')):
            choices = [(name, asset) for name, (asset, doc) in self.archive.documents.items()
                       if asset.get('root_type') == 'TextureAsset' and 'terrain/' in name
                       and 'grass' in name and name.endswith(suffix)]
            choices.sort(key=lambda item:('medievalgrassa' not in item[0], item[0]))
            if choices:
                path = self.texture(choices[0][1]['guid'])
                if not path:
                    raise ValueError('GW2 terrain surface texture unavailable: '+choices[0][0])
                textures[role] = path
        if 'base' not in textures:
            # Garden Warfare 1 names terrain surfaces trn_<name>_d / _n, or <name>_tile.
            names = {name: asset for name, (asset, doc) in self.archive.documents.items()
                     if asset.get('root_type') == 'TextureAsset' and name.startswith('_pvz/terrain/')}
            bases = [n for n in names if n.endswith(('_d', '_tile'))
                     and not any(k in n for k in ('scatter', '_wet', '_snow', 'eash'))]
            bases.sort(key=lambda n: ('grass' not in n, 'sand' not in n, n))
            for base in bases[:1]:
                textures = {'base': self.texture(names[base]['guid'])}
                normal = base[:-2] + '_n' if base.endswith('_d') else None
                if normal in names:
                    textures['normal'] = self.texture(names[normal]['guid'])
                textures = {k: v for k, v in textures.items() if v}
        material = f'materials/frostbite/gw2_terrain_{resource_id}.json'
        content = {'format': 'aura-frostbite-material', 'version': 1, 'profile': 'gw2',
                   'name': 'GW2 terrain surface', 'textures': textures,
                   'double_sided': False, 'alpha_cutoff': -1,
                   'parameters': [[1,1,1,1] if textures else [.32,.39,.22,1], [.8,0,1,0], [1,1,0,0], [0,0,0,0]],
                   'source_terrain': terrain[1].get('Name'),
                   'surface_approximation': 'Authored grass texture; native terrain splat shaders are not executed.'}
        target = self.assets/material
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(content,indent=2),encoding='utf-8')
        self.materials['terrain:'+resource_id] = material
        result = []
        leaves = []
        # Tiles inherit heights they do not store from their nearest ancestor.
        def walk(tile, parent):
            raw = None
            if tile['size']:
                chunk = self.archive.chunks.get(tile['guid'])
                if chunk is None:
                    raise ValueError('GW2 terrain height chunk unavailable: '+tile['guid'])
                raw = self.archive.data(chunk)
            heights = tile_heights(raw, tile, tree, parent)
            here = (heights, tile['rect']) if heights is not None else parent
            if not tile['children']:
                leaves.append((tile, heights))
            for child in tile['children']:
                walk(child, here)
        walk(tree['root'], None)
        for tile, heights in leaves:
            section = height_section(None, tile, tree, heights=heights)
            if section is None:
                continue
            lo, hi = section['positions'].min(axis=0), section['positions'].max(axis=0)
            centre = (lo+hi)*.5
            section['positions'] -= centre
            rect_hash = hashlib.sha256(struct.pack('<4f',*tile['rect'])).hexdigest()[:8]
            rel = f"meshes/frostbite/gw2_terrain_{tile['guid']}_{rect_hash}.amsh"
            vertices, triangles = write_mesh(self.assets/rel,[section])
            self.counts['vertices'] += vertices
            self.counts['triangles'] += triangles
            self.meshes[('terrain',tile['guid'],rect_hash)] = (rel,[material])
            local = np.eye(4)
            local[:3,3] = centre
            result.append((rel,material,local,tile['guid']))
        self.terrain_tiles += len(result)
        return result

    def floor_at(self, x, z, ceiling=float('inf')):
        heights = []
        for path, transform in self.collision_geometry:
            if path not in self.floor_meshes:
                raw = (self.assets / path).read_bytes()
                header = struct.unpack_from('<16I6f', raw)
                count, indices = header[4:6]
                positions = np.frombuffer(raw, '<f4', count=count*12, offset=96).reshape(-1,12)[:,:3].copy()
                offset = (96 + count * 48 + 15) & ~15
                triangles = np.frombuffer(raw, '<u4', count=indices, offset=offset).reshape(-1,3).copy()
                self.floor_meshes[path] = (positions, triangles, np.array(header[16:19]), np.array(header[19:22]))
            positions, indices, lo, hi = self.floor_meshes[path]
            centre = transform[:3,:3] @ ((lo+hi)*.5) + transform[:3,3]
            radius = np.abs(transform[:3,:3]) @ ((hi-lo)*.5)
            if not centre[0]-radius[0] <= x <= centre[0]+radius[0] or not centre[2]-radius[2] <= z <= centre[2]+radius[2]:
                continue
            world = positions @ transform[:3,:3].T + transform[:3,3]
            a,b,c = world[indices[:,0]],world[indices[:,1]],world[indices[:,2]]
            den = (b[:,2]-c[:,2])*(a[:,0]-c[:,0])+(c[:,0]-b[:,0])*(a[:,2]-c[:,2])
            normal = np.cross(b-a,c-a)
            up = normal[:,1]/np.maximum(np.linalg.norm(normal,axis=1),1e-12)
            valid = (np.abs(den)>1e-8) & (up>=.35)
            # Match Aura's character floor query: centre plus a 0.1 m footprint.
            for dx,dz in ((0,0),(.1,0),(-.1,0),(0,.1),(0,-.1)):
                xx,zz=x+dx,z+dz
                alpha,beta = np.zeros(len(indices)),np.zeros(len(indices))
                alpha[valid] = ((b[valid,2]-c[valid,2])*(xx-c[valid,0])+(c[valid,0]-b[valid,0])*(zz-c[valid,2]))/den[valid]
                beta[valid] = ((c[valid,2]-a[valid,2])*(xx-c[valid,0])+(a[valid,0]-c[valid,0])*(zz-c[valid,2]))/den[valid]
                gamma = 1-alpha-beta
                y = alpha*a[:,1]+beta*b[:,1]+gamma*c[:,1]
                inside = valid & (alpha>=0) & (beta>=0) & (gamma>=0) & (y<=ceiling)
                heights.extend(y[inside].tolist())
        if not heights:
            raise ValueError('GW2 editor spawn has no generated collision floor')
        return max(heights)

    def scene(self, map_name):
        gw1 = getattr(self.archive, 'inventory', {}).get('profile') == 'gw1'
        places = []
        starts = []
        roots = 0
        visited_layers = set()
        def visit(doc, obj, transform, source, stack, variation=0):
            if len(stack) > 64:
                raise ValueError('GW2 prefab nesting exceeds limit')
            kind = obj.get('$type')
            if obj.get('Excluded') or obj.get('Enabled') is False:
                return
            if kind in ('ObjectReferenceObjectData', 'SpatialPrefabReferenceObjectData', 'SubWorldReferenceObjectData',
                        'ReferenceObjectData', 'SpatialReferenceObjectData', 'LogicPrefabReferenceObjectData',
                        'WorldPartReferenceObjectData'):
                if not obj.get('Blueprint', {}).get('$pointer'):
                    return
                target = self.archive.resolve(doc, obj.get('Blueprint'))
                if not target:
                    raise ValueError('GW2 blueprint unresolved')
                identity = (target[0]['guid'], target[1].get('$guid'))
                if identity in stack:
                    raise ValueError('GW2 prefab cycle')
                selected = self.archive.resolve(doc, obj.get('ObjectVariation'))
                if selected:
                    variation = selected[1].get('NameHash', variation)
                local = matrix(obj['BlueprintTransform']) if 'BlueprintTransform' in obj else np.eye(4)
                visit(*target, transform @ local, source, stack + [identity], variation)
            elif kind == 'ObjectBlueprint':
                target = self.archive.resolve(doc, obj.get('Object'))
                if target:
                    visit(*target, transform, source, stack, variation)
            elif kind in ('SpatialPrefabBlueprint', 'LogicPrefabBlueprint', 'WorldPartData', 'SubWorldData'):
                if kind in ('WorldPartData', 'SubWorldData'):
                    identity = (doc['guid'], transform.tobytes())
                    if identity in visited_layers:
                        return
                    visited_layers.add(identity)
                for ref in obj.get('Objects', []):
                    target = self.archive.resolve(doc, ref)
                    if target:
                        try:
                            visit(*target, transform, source, stack, variation)
                        except (ValueError, KeyError, IndexError, struct.error) as error:
                            self.errors.append({'stage': 'placement', 'layer': source,
                                                'entity': target[1].get('$guid'), 'error': str(error)})
            elif kind == 'StaticModelGroupEntityData':
                members = obj.get('MemberDatas', [])
                physical = sum(member['InstanceCount'] for member in members if member.get('PhysicsPartCountPerInstance'))
                poses, bodies, havok = [], [], None
                if physical:
                    for ref in obj.get('Components', []):
                        component = self.archive.resolve(doc, ref)
                        if not component or component[1]['$type'] != 'StaticModelGroupPhysicsComponentData':
                            continue
                        for field in ('ClientBody', 'ServerBody'):
                            body = self.archive.resolve(component[0], component[1].get(field))
                            if body:
                                havok = self.archive.resolve(body[0], body[1].get('Asset'))
                                if havok:
                                    break
                    if havok is None:
                        # Some cooked builds retain the asset in the document
                        # but omit the component's body reference.
                        havok = next(((doc, item) for item in doc['objects'] if item['$type']=='GroupHavokAsset'), None)
                    if havok is None:
                        raise ValueError('GW2 grouped placement physics unavailable')
                    resource = self.archive.resources[havok[1]['Resource']['$resource']]
                    if gw1:
                        # Garden Warfare 1 instances are addressed by physics part index, not order.
                        poses = gw1_part_poses(self.archive.data(resource), resource['metadata'],
                                               '>' if self.archive.inventory.get('platform') == 'xenon' else '<')
                    else:
                        poses, bodies = group_instances(self.archive.data(resource), resource['metadata'])
                        if len(poses) != physical:
                            raise ValueError('GW2 grouped instance count disagrees with cooked placements')
                cursor = 0
                for member in members:
                    authored = member.get('InstanceTransforms', [])
                    count = member['InstanceCount']
                    has_physics = bool(member.get('PhysicsPartCountPerInstance'))
                    mesh = self.archive.resolve(doc, member.get('MeshAsset'))
                    if mesh is None:
                        raise ValueError('GW2 grouped mesh asset unresolved')
                    if not authored and not has_physics:
                        self.unplaced_group_instances.append({'layer':source, 'mesh':mesh[1].get('Name'), 'instances':count})
                        self.errors.append({'stage':'placement','layer':source,
                                            'error':'GW2 grouped model has no authored or cooked placement transforms',
                                            'mesh':mesh[1].get('Name'),'instances':count})
                        continue
                    if authored and len(authored) != count:
                        raise ValueError('GW2 grouped authored transform count mismatch')
                    for i in range(count):
                        if authored:
                            local = matrix(authored[i])
                        elif gw1:
                            part = member.get('PhysicsPartRange', {}).get('First', 0) + i * max(1, member['PhysicsPartCountPerInstance'])
                            if part not in poses:
                                raise ValueError('GW1 grouped physics part %d has no cooked placement' % part)
                            local = poses[part]
                        else:
                            local = poses[cursor].copy()
                        if has_physics:
                            if not authored and not gw1:
                                index = int(bodies[cursor])
                                assets = havok[1]['ExternalAssets']
                                if index >= len(assets):
                                    raise ValueError('GW2 grouped body reference out of range')
                                body = self.archive.resolve(havok[0], assets[index])
                                if body:
                                    local = local @ np.linalg.inv(matrix(body[1]['Transform']))
                            cursor += 1
                        variations = member.get('InstanceObjectVariation', [])
                        selected = variations[i] if i < len(variations) else variation
                        enabled = member.get('InstanceEnabled', [])
                        if i < len(enabled) and not enabled[i]:
                            continue
                        identity = {'right':{'x':1},'up':{'y':1},'forward':{'z':1},'trans':{}}
                        visit(doc, {'$type':'StaticModelEntityData', 'Mesh':member['MeshAsset'],
                                    'Transform':identity}, transform @ matrix(obj['Transform']) @ local, source, stack, selected)
                        self.grouped_instances += 1
            elif kind in ('WaterEntityData', 'WaterInteractEntityData') and obj.get('Visible', True):
                from gw_water import convert_water
                path, material = convert_water(self, doc, obj)
                world = transform @ matrix(obj['Transform'])
                places.append({'kind': 'frostbite_mesh', 'name': 'Water surface',
                               'matrix': world.tolist(), 'values': {'collision': 0},
                               'tags': {'mesh': path, 'material_0': material, 'surface': 'water',
                                        'source': doc['guid'], 'source_level': source,
                                        'level_instance': source, 'profile': self.archive.inventory.get('profile', 'gw2')}})
            elif kind == 'TerrainEntityData' and obj.get('Visible',True):
                terrain = self.archive.resolve(doc,obj.get('TerrainAsset'))
                if terrain is None:
                    raise ValueError('GW2 terrain asset unresolved')
                world = transform @ matrix(obj['Transform'])
                for path,material,local,guid in self.convert_terrain(terrain):
                    placement = world @ local
                    tags = {'mesh':path,'material_0':material,'source':terrain[1].get('Name',''),
                            'source_level':source,'level_instance':source,'profile':'gw2','terrain_chunk':guid}
                    places.append({'kind':'frostbite_mesh','name':'GW2 terrain tile','matrix':placement.tolist(),
                                   'tags':tags,'values':{'collision':0}})
                    places.append({'kind':'frostbite_collision','name':'GW2 terrain collider','matrix':placement.tolist(),
                                   'tags':{'mesh':path,'level_instance':source,'profile':'gw2','collision_source':'heightfield'},
                                   'values':{'collision':1}})
                    self.collision_geometry.append((path,placement))
            elif kind in ('StaticModelEntityData', 'DynamicModelEntityData', 'MeshProxyEntityData') and obj.get('Visible', True):
                mesh = self.archive.resolve(doc, obj.get('Mesh'))
                if not mesh:
                    raise ValueError('GW2 mesh asset unresolved')
                if gw1 and 'backdrop_cards' in mesh[1].get('Name', '').lower():
                    # GW1 game-mode sub-levels each place the painted spawn-screen backdrops: boxes, not world geometry.
                    self.skipped['GW1 spawn-screen backdrop'] = self.skipped.get('GW1 spawn-screen backdrop', 0) + 1
                    return
                try:
                    path, materials = self.convert_static(mesh, variation)
                except SkinnedMeshUnsupported:
                    name = mesh[1].get('Name', mesh[0]['guid'])
                    self.skipped_meshes[name] = self.skipped_meshes.get(name, 0) + 1
                    self.skipped['SkinnedMesh'] = self.skipped.get('SkinnedMesh', 0) + 1
                    return
                world = transform @ matrix(obj['Transform'])
                tags = {'mesh': path, 'source': mesh[1].get('Name', ''), 'source_level': source,
                        'level_instance': source, 'profile': 'gw2'}
                tags.update({f'material_{i}': material for i, material in enumerate(materials)})
                places.append({'kind': 'frostbite_mesh', 'name': mesh[1].get('Name', '').split('/')[-1],
                               'matrix': world.tolist(), 'tags': tags, 'values': {'collision': 0}})
                # GW2 Havok is a different format. Generate a static triangle
                # collider explicitly rather than claiming to decode native physics.
                if not any(part in mesh[1].get('Name','').lower() for part in ('cloudsgeo','skydome','skybox','light_shadowmap')):
                    places.append({'kind': 'frostbite_collision', 'name': 'GW2 static surface collider',
                                   'matrix': world.tolist(), 'tags': {'mesh': path, 'level_instance': source,
                                                                   'profile': 'gw2', 'collision_source': 'render_mesh'},
                                   'values': {'collision': 1}})
                    self.collision_geometry.append((path, world))
            elif kind in ('PVZCharacterSpawnReferenceObjectData','AlternateSpawnEntityData'):
                local = obj.get('BlueprintTransform',obj.get('Transform'))
                if local:
                    starts.append((transform @ matrix(local))[:3,3])
            else:
                self.skipped[kind] = self.skipped.get(kind, 0) + 1
        layers = sorted(self.archive.documents.items(), key=lambda item:item[1][0].get('root_type') != 'SubWorldData')
        for name, (asset, doc) in layers:
            if asset.get('root_type') not in ('WorldPartData','SubWorldData') or map_name.lower() not in name.lower():
                continue
            if asset.get('root_type') == 'WorldPartData' and any(identity[0]==doc['guid'] for identity in visited_layers):
                continue
            roots += 1
            try:
                visit(doc, doc['objects'][0], np.eye(4), name, [])
            except (ValueError, KeyError, IndexError, struct.error) as error:
                self.errors.append({'stage': 'placement', 'layer': name, 'error': str(error)})
        if not places:
            raise ValueError('GW2 map contains no convertible static geometry')
        centres = np.array([np.array(p['matrix'])[:3, 3] for p in places if p['kind']=='frostbite_mesh'])
        spawn = np.median(centres, axis=0).tolist()
        for candidate in sorted(starts,key=lambda value:float(np.sum((value[[0,2]]-np.array(spawn)[[0,2]])**2))):
            try:
                floor = self.floor_at(candidate[0], candidate[2], candidate[1]+2)
                if abs(floor-candidate[1]) < 5:
                    spawn = [float(candidate[0]),floor+.5,float(candidate[2])]
                    break
            except ValueError:
                continue
        else:
            try:
                spawn[1] = self.floor_at(spawn[0], spawn[2], spawn[1]+2) + .5
            except ValueError:
                spawn = centres[0].tolist()
                spawn[1] = self.floor_at(spawn[0], spawn[2], spawn[1]+2) + .5
        places.insert(0, {'kind': 'frostbite_environment', 'name': 'GW2 outdoor settings', 'at': [0, 0, 0],
                          'values': {'stream_load_distance': 2048, 'stream_unload_distance': 2400, 'sun_intensity': 2.4}, 'tags': {}})
        places.insert(0, {'kind': 'spawn', 'name': 'GW2 editor spawn', 'at': spawn, 'values': {}, 'tags': {}})
        report = {'profile': 'gw2', 'map': map_name, 'layers': roots, 'placements': len(places),
                  'meshes': len(self.meshes), 'materials': len(self.materials), 'textures': len(self.texture_paths),
                  'counts': self.counts, 'errors': self.errors, 'skipped_entities': self.skipped,
                  'skipped_skinned_meshes': self.skipped_meshes,
                  'terrain_tiles': self.terrain_tiles,
                  'grouped_instances': self.grouped_instances,
                  'unplaced_group_instances': self.unplaced_group_instances,
                  'archive_errors': self.archive.inventory['errors'],
                  'limitations': ['Installed base archives and patch deltas; static environment conversion.',
                                  'Static environment and material texture bindings; shaders use Aura approximations.',
                                  'Collision is generated from render triangles, not native Havok physics.',
                                  'Terrain heightfields are translated; native terrain splat layers, holes and scatter vegetation are not yet translated.',
                                  'Water exports a bounded plane and native shader sidecars; Blender approximates the surface without game wave simulation.',
                                  'Animated characters and original gameplay are not translated.']}
        return {'format': 'aura-frostbite-scene', 'version': 1, 'name': 'GW2_' + map_name, 'places': places}, report


def main():
    import argparse
    parser = argparse.ArgumentParser()
    for name in ('export', 'textures', 'assets', 'out', 'map'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    converter = Gw2Converter(Gw2Archive(args.export), args.textures, args.assets)
    try:
        scene, report = converter.scene(args.map)
    finally:
        converter.finish_textures()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(scene, indent=2), encoding='utf-8')
    out.with_suffix('.report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('GW2 static placements:', len(scene['places']), 'errors:', len(report['errors']))
    return bool(report['errors'])


if __name__ == '__main__':
    raise SystemExit(main())
