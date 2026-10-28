"""Battle for Neighborville decal volumes as projected decal meshes.

An EnvironmentDecalVolumeData is a box (its Transform scales a -1..1 cube) that the game projects its
template's texture through, onto whatever lies inside: paint, cracks, puddles, signs. Blender has no
deferred decals, so the converter clips the map's own triangles to each box, gives the clipped
surface UVs from the box's projection, lifts it a little off the surface and draws it with an
alpha-blended material ('surface': 'decal', built by the add-on's decal_materials.py).
Decals of one template are merged into one mesh.
"""
import collections
import hashlib
import json
import os

import numpy as np

from convert import write_mesh

# The volume is the -1..1 cube of fx/_meshes/volumedecal/meshp_volumedecal_box_lit_mesh. Local Y is
# the thin side in 99% of Pressure Pier's volumes and points up on ground decals: the projection axis.
HALF = 1.0
PROJECT = 1
LIFT = 0.012        # metres the decal floats above its surface
FACING = 0.25       # faces must point along +Y by at least this much: like the game's deferred decals, not their backs
WORLD_CACHE = 512   # surfaces whose world triangles are kept while decals are projected
BASE_KEYS = ('colortexture', 'texture', 'color', 'basecolor', 'diffuse')
NORMAL_KEYS = ('packeddecalnormaltexture', 'normaltexture', 'normal')


def _vectors(shader):
    out = {}
    for p in shader.get('VectorParameters', []) or []:
        v = p.get('Value') or {}
        out[(p.get('ParameterName') or '').lower()] = [v.get(k, 0.0) for k in 'xyzw']
    return out


def _clip(poly, axis, sign):
    """Sutherland-Hodgman: keep the part of a convex polygon (n x 3, local space) with sign*p[axis] <= HALF."""
    out = []
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        da, db = HALF - sign * a[axis], HALF - sign * b[axis]
        if da >= 0:
            out.append(a)
        if (da >= 0) != (db >= 0):
            out.append(a + (b - a) * (da / (da - db)))
    return out


class Decals:
    def __init__(self, converter):
        self.c = converter
        self.pending = []
        self.counts = {'decal_volumes': 0, 'decals_placed': 0, 'decals_empty': 0, 'decals_untextured': 0}
        self.templates = {}
        self._world = collections.OrderedDict()  # surface index -> world triangles (see world_triangles)

    # ---- collection during the scene walk ----
    def add(self, doc, obj, matrix, source):
        if not obj.get('IsEnabled', True) or float(obj.get('Alpha', 1) or 0) <= 0:
            self.c.counts['skipped_hidden'] += 1
            return
        found = self.c.resolve(doc, obj.get('Template'))
        if not found:
            raise ValueError('Decal volume template unresolved')
        from convert_legacy import transform
        world = matrix @ transform(obj.get('Transform', {}))
        if not np.isfinite(world).all() or abs(np.linalg.det(world[:3, :3])) < 1e-9:
            raise ValueError('Decal volume transform is degenerate')
        self.pending.append({'template': found, 'matrix': world, 'row': int(obj.get('Row', 0) or 0),
                             'column': int(obj.get('Column', 0) or 0), 'alpha': float(obj.get('Alpha', 1)),
                             'source': source})
        self.counts['decal_volumes'] += 1

    # ---- materials ----
    def material(self, template):
        tdoc, tobj = template
        key = tdoc['guid'] + ':' + str(tobj.get('$guid'))
        if key in self.templates:
            return self.templates[key]
        shader = tobj.get('Shader') or {}
        params = {(p.get('ParameterName') or '').lower(): p for p in shader.get('TextureParameters', []) or []}
        textures, names, base_alpha = {}, {}, 0.0
        for role, keys in (('base', BASE_KEYS), ('normal', NORMAL_KEYS)):
            for k in keys:
                if k in params:
                    result, name = self.c.texture(tdoc, params[k].get('Value'))
                    if result:
                        textures[role], names[role] = result[0], name
                        if role == 'base':
                            base_alpha = result[1]
                    break
        vectors = _vectors(shader)
        rows, columns = (vectors.get('atlas_rowscolumns') or [1, 1])[:2]
        tint = (vectors.get('tint_color') or [1, 1, 1, 1])[:3] + [1]
        title = (tobj.get('Name') or 'decal').split('/')[-1]
        obj = {'format': 'aura-frostbite-material', 'version': 1, 'profile': 'bfn', 'surface': 'decal',
               'name': title, 'textures': textures, 'texture_sources': names, 'tint': tint,
               'parameters': [tint, [.6, 0, 1 if 'normal' in textures else 0, 1], [1, 1, 0, 0], [0, 0, 0, 0]],
               'double_sided': True, 'blend': 'alpha',
               # Without decal_materials.py the generic shader cuts the decal out at this alpha.
               'alpha_cutoff': .1, 'texture_srgb': {'base': True, 'normal': False, 'mask': False},
               'normal_packing': 'bfn_green_alpha', 'base_alpha_coverage': base_alpha,
               'source_decal_template': tobj.get('Name', ''),
               'surface_approximation': 'Map triangles clipped to the decal volume and alpha blended; '
                   'the native deferred decal blend and its mask channels are not executed.'}
        digest = hashlib.sha256(json.dumps([key, textures], sort_keys=True).encode()).hexdigest()[:20]
        rel = 'materials/frostbite/decal_' + digest + '.json'
        path = self.c.assets / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, indent=2), encoding='utf-8')
        self.c.materials['decal:' + digest] = rel
        atlas = (max(1, int(round(rows or 1))), max(1, int(round(columns or 1))))
        self.templates[key] = (rel, atlas, bool(textures.get('base')), title)
        return self.templates[key]

    # ---- projection ----
    def surfaces(self, places):
        """World AABBs of the placed meshes the decals can land on."""
        by_rel = {entry[0]: entry for entry in self.c.meshes.values()}
        items = []
        for p in places:
            if p.get('kind') != 'frostbite_mesh':
                continue
            tags = p.get('tags', {})
            if tags.get('surface') in ('water', 'decal'):
                continue
            entry = by_rel.get(tags.get('mesh'))
            if entry is None:
                continue
            m = np.array(p['matrix'])
            lo, hi = entry[2].min(axis=0), entry[2].max(axis=0)
            corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
            world = corners @ m[:3, :3].T + m[:3, 3]
            items.append((world.min(axis=0), world.max(axis=0), entry, m))
        if not items:
            return None
        return (np.array([i[0] for i in items]), np.array([i[1] for i in items]), items)

    def world_triangles(self, i, items):
        """A surface's world triangles, their bounds, unit normals and doubled areas, kept for the next decals
        (sorted by position, neighbours share surfaces); the least recently used are let go past WORLD_CACHE."""
        cached = self._world.get(i)
        if cached is not None:
            self._world.move_to_end(i)
            return cached
        _, _, entry, m = items[i]
        world = (entry[2] @ m[:3, :3].T.astype(np.float32) + m[:3, 3].astype(np.float32))[entry[3]]
        normal = np.cross(world[:, 1] - world[:, 0], world[:, 2] - world[:, 0])
        length = np.linalg.norm(normal, axis=1)
        cached = (world, world.min(axis=1), world.max(axis=1), normal / np.maximum(length, 1e-12)[:, None], length)
        self._world[i] = cached
        if len(self._world) > WORLD_CACHE:
            self._world.popitem(last=False)
        return cached

    def project(self, decal, found):
        """One decal's surface in its local space: (triangles fully inside the volume as an (n, 3, 3) array with
        their normals, clipped polygons as (polygon, normal) pairs)."""
        lo, hi, items = found
        D = decal['matrix']
        inv = np.linalg.inv(D)
        reach = np.abs(D[:3, :3]).sum(axis=1) * HALF
        centre = D[:3, 3]
        candidates = np.where(((lo <= centre + reach) & (hi >= centre - reach)).all(axis=1))[0]
        axis_world = D[:3, PROJECT] / np.linalg.norm(D[:3, PROJECT])
        full_tris, full_normals, polys = [], [], []
        for i in candidates:
            world, wlo, whi, normal, length = self.world_triangles(i, items)
            # Triangles near the volume's world box that face the projection; then the exact test, in its space.
            pick = ((whi >= centre - reach) & (wlo <= centre + reach)).all(axis=1)
            pick &= (normal @ axis_world > FACING) & (length > 1e-10)
            if not pick.any():
                continue
            tri = world[pick].astype(np.float64) @ inv[:3, :3].T + inv[:3, 3]
            hit = ((tri.max(axis=1) >= -HALF) & (tri.min(axis=1) <= HALF)).all(axis=1)
            tri, n = tri[hit], normal[pick][hit].astype(np.float64)
            inside = (np.abs(tri) <= HALF).all(axis=(1, 2))
            full_tris.append(tri[inside])
            full_normals.append(n[inside])
            for t, tn in zip(tri[~inside], n[~inside]):
                poly = list(t)
                for axis in range(3):
                    for sign in (1, -1):
                        poly = _clip(poly, axis, sign)
                        if len(poly) < 3:
                            break
                    if len(poly) < 3:
                        break
                if len(poly) >= 3:
                    polys.append((np.array(poly), tn))
        full = (np.concatenate(full_tris), np.concatenate(full_normals)) if full_tris else (np.zeros((0, 3, 3)), np.zeros((0, 3)))
        return full, polys

    def build(self, places, kept=lambda x, z: True):
        """Decal places for the decals at positions `kept(x, z)` accepts whose volumes reach a placed surface."""
        if not self.pending:
            return []
        found = self.surfaces(places)
        self._world = collections.OrderedDict()
        groups = {}
        u_axis, v_axis = [a for a in range(3) if a != PROJECT]
        # Neighbouring decals land on the same surfaces: in position order the surface cache keeps hitting.
        for decal in sorted(self.pending, key=lambda d: (round(d['matrix'][0, 3] / 64), round(d['matrix'][2, 3] / 64))):
            if not kept(decal['matrix'][0, 3], decal['matrix'][2, 3]):
                continue
            rel, (rows, columns), textured, title = self.material(decal['template'])
            if not textured:
                self.counts['decals_untextured'] += 1
                continue
            (tris, tri_normals), polys = self.project(decal, found) if found else ((np.zeros((0, 3, 3)), None), [])
            if not polys and not len(tris):
                self.counts['decals_empty'] += 1
                continue
            D = decal['matrix']
            row, column = decal['row'] % rows, decal['column'] % columns
            group = groups.setdefault(rel, {'title': title, 'positions': [], 'normals': [], 'uv': [], 'indices': [],
                                            'sources': set(), 'count': 0, 'size': 0})
            group['count'] += 1
            group['sources'].add(decal['source'])

            def add(points, normals, triangles):
                """Lift into world space beside the surface, atlas UVs from the volume's local position."""
                group['indices'].append(triangles + group['size'])
                group['positions'].append(points @ D[:3, :3].T + D[:3, 3] + normals * LIFT)
                group['normals'].append(normals)
                group['uv'].append(np.stack([(points[:, u_axis] / (2 * HALF) + .5 + column) / columns,
                                             (.5 + points[:, v_axis] / (2 * HALF) + row) / rows], axis=1))
                group['size'] += len(points)
            if len(tris):
                add(tris.reshape(-1, 3), np.repeat(tri_normals, 3, axis=0), np.arange(len(tris) * 3).reshape(-1, 3))
            for poly, normal in polys:  # fan-triangulate each clipped convex polygon
                k = np.arange(1, len(poly) - 1)
                add(poly, np.tile(normal, (len(poly), 1)), np.stack([np.zeros_like(k), k, k + 1], axis=1))
        out = []
        for rel, g in groups.items():
            positions = np.concatenate(g['positions']).astype(np.float64)
            centre = (positions.min(axis=0) + positions.max(axis=0)) * .5
            section = {'name': g['title'], 'positions': (positions - centre).astype(np.float32),
                       'normals': np.concatenate(g['normals']).astype(np.float32),
                       'uv': np.concatenate(g['uv']).astype(np.float32),
                       'indices': np.concatenate(g['indices']).reshape(-1).astype(np.uint32),
                       'color': np.ones((len(positions), 3), np.float32), 'palette': np.zeros(len(positions), np.float32),
                       'material_id': 0}
            key = 'bfn_decal_' + os.path.basename(rel)[len('decal_'):-len('.json')]
            mesh = 'meshes/frostbite/' + key + '.amsh'
            vertices, triangles = write_mesh(self.c.assets / mesh, [section])
            self.c.counts['vertices'] += vertices
            self.c.counts['triangles'] += triangles
            self.c.meshes[key] = (mesh, [rel], section['positions'], section['indices'].reshape(-1, 3))
            matrix = np.eye(4)
            matrix[:3, 3] = centre
            out.append({'kind': 'frostbite_mesh', 'name': 'Decal ' + g['title'], 'matrix': matrix.tolist(),
                        'tags': {'mesh': mesh, 'material_0': rel, 'surface': 'decal', 'decals': g['count'],
                                 'source': g['title'], 'source_level': sorted(g['sources'])[0]},
                        'values': {'collision': 0}})
            self.counts['decals_placed'] += g['count']
        return out
