"""Battle for Neighborville decal volumes as projected decal meshes.

An EnvironmentDecalVolumeData is a box (its Transform scales a -1..1 cube) that the game projects its
template's texture through, onto whatever lies inside: paint, cracks, puddles, signs. Blender has no
deferred decals, so the converter clips the map's own triangles to each box, gives the clipped
surface UVs from the box's projection, lifts it a little off the surface and draws it with an
alpha-blended material ('surface': 'decal', built by the add-on's decal_materials.py).
Decals of one template are merged into one mesh.
"""
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

    def project(self, decal, found):
        """Clipped, lifted triangles (world positions, normals, UVs) of one decal."""
        lo, hi, items = found
        D = decal['matrix']
        inv = np.linalg.inv(D)
        reach = np.abs(D[:3, :3]).sum(axis=1) * HALF
        centre = D[:3, 3]
        candidates = np.where(((lo <= centre + reach) & (hi >= centre - reach)).all(axis=1))[0]
        axis_world = D[:3, PROJECT] / np.linalg.norm(D[:3, PROJECT])
        polys = []
        for i in candidates:
            _, _, entry, m = items[i]
            to_local = inv @ m
            local = entry[2] @ to_local[:3, :3].T + to_local[:3, 3]
            tri = local[entry[3]]
            tmin, tmax = tri.min(axis=1), tri.max(axis=1)
            hit = ((tmax >= -HALF) & (tmin <= HALF)).all(axis=1)
            if not hit.any():
                continue
            tri = tri[hit]
            world = entry[2][entry[3][hit]] @ m[:3, :3].T + m[:3, 3]
            normal = np.cross(world[:, 1] - world[:, 0], world[:, 2] - world[:, 0])
            length = np.linalg.norm(normal, axis=1)
            normal = normal / np.maximum(length, 1e-12)[:, None]
            facing = (normal @ axis_world > FACING) & (length > 1e-10)
            inside = (np.abs(tri) <= HALF).all(axis=(1, 2))
            for t, n, full in zip(tri[facing], normal[facing], inside[facing]):
                poly = list(t)
                if not full:
                    for axis in range(3):
                        for sign in (1, -1):
                            poly = _clip(poly, axis, sign)
                            if len(poly) < 3:
                                break
                        if len(poly) < 3:
                            break
                if len(poly) >= 3:
                    polys.append((np.array(poly), n))
        return polys

    def build(self, places, kept=lambda x, z: True):
        """Decal places for the decals at positions `kept(x, z)` accepts whose volumes reach a placed surface."""
        if not self.pending:
            return []
        found = self.surfaces(places)
        groups = {}
        u_axis, v_axis = [a for a in range(3) if a != PROJECT]
        for decal in self.pending:
            if not kept(decal['matrix'][0, 3], decal['matrix'][2, 3]):
                continue
            rel, (rows, columns), textured, title = self.material(decal['template'])
            if not textured:
                self.counts['decals_untextured'] += 1
                continue
            polys = self.project(decal, found) if found else []
            if not polys:
                self.counts['decals_empty'] += 1
                continue
            D = decal['matrix']
            row, column = decal['row'] % rows, decal['column'] % columns
            group = groups.setdefault(rel, {'title': title, 'positions': [], 'normals': [], 'uv': [], 'indices': [],
                                            'sources': set(), 'count': 0})
            group['count'] += 1
            group['sources'].add(decal['source'])
            for poly, normal in polys:
                # Fan-triangulate the clipped convex polygon.
                base = len(group['positions'])
                world = poly @ D[:3, :3].T + D[:3, 3] + normal * LIFT
                u = (poly[:, u_axis] / (2 * HALF) + .5 + column) / columns
                v = (.5 + poly[:, v_axis] / (2 * HALF) + row) / rows
                group['positions'].extend(world)
                group['normals'].extend([normal] * len(poly))
                group['uv'].extend(np.stack([u, v], axis=1))
                for k in range(1, len(poly) - 1):
                    group['indices'].extend((base, base + k, base + k + 1))
        out = []
        for rel, g in groups.items():
            positions = np.array(g['positions'], np.float64)
            centre = (positions.min(axis=0) + positions.max(axis=0)) * .5
            section = {'name': g['title'], 'positions': (positions - centre).astype(np.float32),
                       'normals': np.array(g['normals'], np.float32), 'uv': np.array(g['uv'], np.float32),
                       'indices': np.array(g['indices'], np.uint32),
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
