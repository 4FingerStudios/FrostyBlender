"""Skinned objects placed in GW1/GW2 maps: flags, banners, windmills, machines and animated props.

A map places them like static models (a StaticModelEntityData whose Mesh is a SkinnedMeshAsset). The
blueprint's ModelAnimationEntityData points at an AntAnimationSetAsset, which names the SkeletonAsset and
the AntState bank holding the clip, and the entity names its DefaultAnimation (usually looping). Each such
object converts in its bind pose with a .skin of bone weights, a skeleton JSON (cosmetics.Rig), and, when
the game animates it, one decoded clip (animations.py's format) for the add-on to loop on its own armature.
"""
import json
import math
import re
import struct
from pathlib import Path

import ant
from animations import clean, face_kind
from cosmetics import Rig

ANIMATION_TYPES = ('ModelAnimationEntityData',)
CLIP_TYPES = ('VbrAnimationAsset', 'CurveAnimationAsset', 'DctAnimationAsset', 'RawAnimationAsset')


def _euler(x, y, z):
    """Quaternion (x, y, z, w) of CurveAnimationAsset angles: rotate about X, then Y, then Z (q = qz qy qx)."""
    cx, sx, cy, sy, cz, sz = (f(a / 2) for a in (x, y, z) for f in (math.cos, math.sin))
    return [sx * cy * cz - cx * sy * sz, cx * sy * cz + sx * cy * sz, cx * cy * sz - sx * sy * cz,
            cx * cy * cz + sx * sy * sz]


def curve_frames(db, anim):
    """Decode a CurveAnimationAsset (GW2 prop clips) to clip_frames' format. Its Values are a key-major table of
    channels; channel and constant offsets index a pose buffer of 4-float slots per joint: Euler rotations
    (radians, the 4th float unused), then scales, then parent-local translations, in skeleton joint order."""
    rig = next((o for o in db.rigs), None)
    skeleton = db.get((rig or {}).get('Skeleton')) or next((o for o in db.objects.values() if o['__type'] == 'SkeletonAsset'), None)
    joints = [j.get('JointName', 'Joint_%d' % i) for i, j in enumerate((skeleton or {}).get('Joints') or [])]
    rotations, vectors = int(anim.get('NumRotations', 0)), int(anim.get('NumVectors', 0))
    if not joints or rotations != len(joints) or vectors != 2 * rotations:
        raise ValueError('Curve clip %s does not match its skeleton' % anim.get('__name'))
    groups = anim.get('ChannelGroups') or []
    if len(groups) != 1:
        raise NotImplementedError('Curve clips with %d channel groups are not decoded yet' % len(groups))
    offsets, keys = list(anim.get('ChannelOffsets') or []), list(anim.get('Keys') or [])
    values = anim.get('Values') or []
    if len(values) != len(keys) * len(offsets) or not keys:
        raise ValueError('Curve clip %s is truncated' % anim.get('__name'))
    size = 4 * (rotations + vectors)
    base = [0.0] * size
    for at, value in zip(anim.get('ConstOffsets') or [], anim.get('Consts') or []):
        base[at] = float(value)
    for at in offsets:
        if not 0 <= at < size:
            raise ValueError('Curve clip channel outside its pose')
    rows = []
    for k in range(len(keys)):
        row = list(base)
        for i, at in enumerate(offsets):
            row[at] = float(values[k * len(offsets) + i])
        rows.append(row)
    end = max(int(anim.get('EndFrame') or 0), keys[-1] + 1)
    frames, k = [], 0
    for frame in range(end):  # keys may skip frames: interpolate between them
        while k + 1 < len(keys) and keys[k + 1] <= frame:
            k += 1
        a, b = rows[k], rows[min(k + 1, len(rows) - 1)]
        span = keys[min(k + 1, len(keys) - 1)] - keys[k]
        t = min(max((frame - keys[k]) / span, 0.0), 1.0) if span > 0 else 0.0
        row = [p + (q - p) * t for p, q in zip(a, b)]
        pose = {}
        for j, name in enumerate(joints):
            r, sc, tr = row[4 * j:4 * j + 3], row[4 * (rotations + j):4 * (rotations + j) + 3],                 row[4 * (2 * rotations + j):4 * (2 * rotations + j) + 3]
            pose[name] = {'r': _euler(*r), 't': tr, 's': sc}
        frames.append(pose)
    return {'name': db.clip_name(anim), 'fps': float(anim.get('FPS') or 30.0), 'additive': False,
            'rig': (rig or {}).get('__name', ''), 'frames': frames}


def _name(text):
    return re.sub(r'[^\w.-]+', '_', text or '')[:80]


class MapRigs:
    def __init__(self, converter):
        self.converter, self.archive = converter, converter.archive
        self.kind = 'gw1' if getattr(self.archive, 'inventory', {}).get('profile') == 'gw1' else 'gw2'
        self.rigs, self.banks, self.clips, self.bound = {}, {}, {}, {}
        self.skeleton_names = None
        self.placed = self.rigged = self.animated = 0
        self.by_mesh, self.clip_uses, self.unrigged, self.unanimated = {}, {}, {}, {}

    # ------------------------------------------------------------ lookups
    def animation_of(self, doc, obj):
        """The ModelAnimationEntityData driving a model: one of its components, else one linked to it."""
        for ref in obj.get('Components', []):
            part = self.archive.resolve(doc, ref)
            if part and part[1].get('$type') in ANIMATION_TYPES:
                return part
        for item in doc.get('objects', []):
            for link in item.get('LinkConnections', []) if item.get('$type', '').endswith('Blueprint') else ():
                source, target = self.archive.resolve(doc, link.get('Source')), self.archive.resolve(doc, link.get('Target'))
                if source and target and target[1] is obj and source[1].get('$type') in ANIMATION_TYPES:
                    return source
        return None

    def skeleton_doc(self, mesh, animation_set, bone_count=0):
        """The SkeletonAsset of the object's animation set, else the one named after its mesh, else the one in its
        folder (GW1 keeps teleporter_skeleton beside teleporterexit_animobject_mesh) with the model's bone count."""
        if animation_set:
            found = self.archive.resolve(animation_set[0], animation_set[1].get('SkeletonAsset'))
            if found:
                return found
        if self.skeleton_names is None:
            self.skeleton_names = {}
            for name, (asset, doc) in self.archive.documents.items():
                if asset.get('root_type') == 'SkeletonAsset':
                    self.skeleton_names[name.lower()] = doc
        stem = re.sub(r'_mesh$', '', mesh[1].get('Name', '').lower())
        for candidate in (stem + '_skeleton', re.sub(r'_(animated|animobject|skinnedobject)$', '', stem) + '_skeleton'):
            doc = self.skeleton_names.get(candidate)
            if doc is not None:
                return doc, doc['objects'][0]
        folder = stem.rpartition('/')[0] + '/'
        near = [doc for name, doc in self.skeleton_names.items() if name.rpartition('/')[0] + '/' == folder]
        if bone_count:
            near = [doc for doc in near if len(doc['objects'][0].get('BoneNames') or []) == bone_count] or near
        if len(near) != 1 and bone_count:
            # Elsewhere, named like the mesh (capturegarden_scarecrow_mainstreet -> capturegarden_scarecrow_suburbia)
            prefix = '_'.join(stem.rpartition('/')[2].split('_')[:2]) + '_'
            near = [doc for name, doc in self.skeleton_names.items() if name.rpartition('/')[2].startswith(prefix)
                    and len(doc['objects'][0].get('BoneNames') or []) >= bone_count]  # the model may use fewer
        return (near[0], near[0]['objects'][0]) if len(near) == 1 else None

    def skeleton(self, found):
        """Relative path of the skeleton JSON, written once per SkeletonAsset."""
        doc, obj = found
        key = doc['guid']
        if key not in self.rigs:
            name = obj.get('Name', key)
            rig = Rig(name.split('/')[-1], doc)
            rel = 'skeletons/frostbite/%s_map_%s.json' % (self.kind, _name(name.split('/')[-1]).lower())
            rig.write(self.converter.assets / rel)
            self.rigs[key] = (rig, rel)
        return self.rigs[key]

    def database(self, animation_set):
        resource_id = (animation_set[1].get('AssetBankResource') or {}).get('$resource')
        if resource_id not in self.banks:
            self.banks[resource_id] = None
            resource = self.archive.resources.get(resource_id)
            if resource is None:
                raise ValueError('AntState bank %s is not in the map export' % resource_id)
            self.banks[resource_id] = ant.Database([ant.read_bank(self.archive.data(resource))])
        if self.banks[resource_id] is None:
            raise ValueError('AntState bank %s could not be read' % resource_id)
        return self.banks[resource_id]

    def clip(self, entity, animation_set):
        """Relative path of the entity's default clip, decoded once; None when the object has no clip."""
        wanted = (entity[1].get('DefaultAnimation') or '').strip()
        if not wanted or not animation_set:
            return None
        key = ((animation_set[1].get('AssetBankResource') or {}).get('$resource'), wanted.lower())
        if key in self.clips:
            return self.clips[key]
        self.clips[key] = None
        db = self.database(animation_set)
        anims = [o for o in db.objects.values() if o['__type'] in CLIP_TYPES]
        names = {clean(db.clip_name(a)).lower(): a for a in anims}
        anim = names.get(wanted.lower()) or names.get(clean(wanted).lower())
        if anim is None and len(anims) == 1:
            anim = anims[0]  # a prop's own bank holds just its clip, whatever the entity calls it
        if anim is None:
            raise ValueError('Clip %s is not in its AntState bank (%s)' % (wanted, ', '.join(sorted(names)) or 'no clips'))
        if anim['__type'] == 'CurveAnimationAsset':
            clip = curve_frames(db, anim)
            face, face_only = False, False
        else:
            clip = ant.clip_frames(db, anim, self.kind)
            face, face_only = face_kind(clip['name'], db.channels(anim, self.kind)[0])
        controller = db.clips_of.get(anim['__guid']) or {}
        # The controller's time scale stretches the keys (ThemePark flags: 359 keys over 716 ticks at 30 fps).
        clip['fps'] = float(controller.get('FPS') or clip.get('fps') or 30.0) * float(controller.get('TimeScale') or 1.0)
        clip.update(format='aura-frostbite-animation', version=1, name=clean(clip['name']), game=self.kind,
                    face=face, face_only=face_only, looping=bool(entity[1].get('Looping', True)))
        rel = 'clips/frostbite/%s_map_%s.json' % (self.kind, _name(clip['name']).lower())
        path = self.converter.assets / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(clip), encoding='utf-8')
        self.clips[key] = rel
        return rel

    # ------------------------------------------------------------ placement
    def convert(self, doc, obj, mesh, variation):
        """(mesh path, materials, extra tags) of a skinned model placed in the map."""
        converter = self.converter
        previous = getattr(converter, 'skinned', False)
        converter.skinned = True
        try:
            path, materials = converter.convert_static(mesh, variation)
        finally:
            converter.skinned = previous
        name = mesh[1].get('Name', mesh[0]['guid'])
        self.placed += 1
        self.by_mesh[name] = self.by_mesh.get(name, 0) + 1
        tags = {}
        entity = self.animation_of(doc, obj)
        animation_set = self.archive.resolve(entity[0], entity[1].get('AnimationSet')) if entity else None
        skin = (converter.assets / path).with_suffix('.skin')
        found = self.skeleton_doc(mesh, animation_set, int(obj.get('BoneCount') or 0))
        try:
            if found is None:
                raise ValueError('no SkeletonAsset found')
            if not skin.is_file():
                raise ValueError('mesh has no bone weights')
            rig, rel = self.skeleton(found)
            if self.bound.get(skin) != rel:
                if skin in self.bound:
                    raise ValueError('mesh is bound to another skeleton')
                rig.bind(skin, mesh[1], name.split('/')[-1])
                self.bound[skin] = rel
                rig.write(converter.assets / rel)  # procedural bones the bind added
            tags.update(skin=Path(path).with_suffix('.skin').as_posix(), skeleton=rel, rig_instance=1)
            self.rigged += 1
        except (ValueError, KeyError, TypeError, struct.error) as error:
            self.unrigged[name] = str(error)
            return path, materials, tags
        try:
            clip = self.clip(entity, animation_set) if entity else None
        except (ValueError, KeyError, IndexError, TypeError, NotImplementedError, struct.error) as error:
            clip = None
            converter.errors.append({'stage': 'clip', 'name': name, 'error': str(error)})
        if clip:
            tags['clip'] = clip
            self.animated += 1
            self.clip_uses[clip] = self.clip_uses.get(clip, 0) + 1
        else:
            self.unanimated[name] = self.unanimated.get(name, 0) + 1
        return path, materials, tags

    def report(self):
        return {'placed': self.placed, 'rigged': self.rigged, 'animated': self.animated,
                'skeletons': len(self.rigs), 'clips': self.clip_uses, 'meshes': self.by_mesh,
                'posed_without_clip': self.unanimated, 'unrigged': self.unrigged}
