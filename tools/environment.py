"""A level's sun, sky, fog and lights as plain values for the importer (frostbite_environment and
frostbite_light places). Shared by gw2_convert.py (GW1/GW2) and convert_legacy.py (Battle for Neighborville).

Battle for Neighborville lights are physically based: the sun in lux, point (sphere), spot and rectangle lights
in lumens (Frostbite's PBR units: a spot's intensity is lumens / pi per steradian, a point's lumens / 4 pi).
pbr_light_values gives their radiant power in watts (lumens / 683, and four times that for a spot, since Blender
spreads a spot's power like a point light's). scale_lights then scales every light by the same factor as the sun,
so the sun lands at the brightness GW1/GW2 maps use and the lights keep the game's ratios to it.
"""

LUMENS_PER_WATT = 683.0
BLENDER_SUN = 2.0  # W/m2: what the importer gives a full-strength game sun (2 x its brightest colour channel)


def vec(value):
    return [float(value.get(axis, 0.0)) for axis in 'xyz'] if isinstance(value, dict) else None


def pick(environments, resolve, sky_texture, physical=False):
    """The main visual environment's values: of the (priority, source, (doc, blueprint)) references the walk
    found, the one with an outdoor light, preferring one with a sky and fog, then the highest priority.
    resolve(doc, ref) gives (doc, object) or None; sky_texture(doc, ref) gives an asset path or None.
    physical: the sun's intensity is in lux (Battle for Neighborville). Empty when the level has none."""
    def components(target):
        doc, blueprint = target
        entity = resolve(doc, blueprint.get('Object'))
        if not entity:
            return {}
        found = {}
        for ref in entity[1].get('Components', []) or []:
            part = resolve(entity[0], ref)
            if part and not part[1].get('Excluded'):
                found[part[1]['$type']] = part
        return found
    # The map's main environment has a sky; smaller ones (VisEnv_..._MiningArea) only nudge the light
    # inside an area, so they lose to it whatever their priority.
    best = None
    for priority, source, target in sorted(environments, key=lambda e: -e[0]):
        parts = components(target)
        if 'OutdoorLightComponentData' not in parts:
            continue
        rank = ('SkyComponentData' in parts, 'FogComponentData' in parts, priority)
        if best is None or rank > best[0]:
            best = (rank, target, parts)
    if best is None:
        return {}
    _, target, parts = best
    light = parts['OutdoorLightComponentData'][1]
    out = {'environment': target[1].get('Name', ''),
           'sun_color': vec(light.get('SunColor')), 'sky_color': vec(light.get('SkyColor')),
           'ground_color': vec(light.get('GroundColor')),
           'sun_rotation': [float(light.get('SunRotationX', 0.0)), float(light.get('SunRotationY', 45.0))]}
    if physical and light.get('SunIntensity'):
        out['sun_lux'] = float(light['SunIntensity'])
    if 'SkyComponentData' in parts:
        sdoc, sky = parts['SkyComponentData']
        out['sky_brightness'] = float(sky.get('BrightnessScale', 1.0))
        path = sky_texture(sdoc, sky.get('PanoramicTexture')) if sky.get('PanoramicTexture') else None
        if path:
            out['sky_texture'] = path
            out['sky_uv'] = [float(sky.get(k, d)) for k, d in (('PanoramicUVMinX', 0.0), ('PanoramicUVMaxX', 1.0),
                                                               ('PanoramicUVMinY', 0.0), ('PanoramicUVMaxY', 1.0))]
            out['sky_rotation'] = float(sky.get('PanoramicRotation', 0.0))
            out['sky_tile'] = float(sky.get('PanoramicTileFactor', 1.0))
    if 'FogComponentData' in parts:
        fog = parts['FogComponentData'][1]
        out['fog_color'] = vec(fog.get('FogColor'))
        out['fog_start'] = float(fog.get('Start', 0.0))
        out['fog_end'] = float(fog.get('End', 0.0))
        curve = fog.get('Curve')
        if isinstance(curve, dict):
            out['fog_curve'] = [float(curve.get(axis, 0.0)) for axis in 'xyzw']
    return {k: v for k, v in out.items() if v is not None}


def grade(environments, resolve, lut):
    """The level's colour grade: of the visual environments with colour correction, the one the level gives the
    highest priority (gameplay states such as Overtime are excluded or referenced at priority 0; underwater
    ones only apply under water). Its grading LUT as an image (lut(doc, ref) gives an asset path or None), and
    brightness, contrast and saturation where they aren't neutral. Empty when the level has none."""
    best = None
    for priority, source, target in environments:
        if 'underwater' in (target[1].get('Name') or '').lower():
            continue
        entity = resolve(target[0], target[1].get('Object'))
        for ref in (entity[1].get('Components', []) or []) if entity else []:
            part = resolve(entity[0], ref)
            if part and part[1].get('$type') == 'ColorCorrectionComponentData' and part[1].get('Enable', True) \
                    and not part[1].get('Excluded') and (best is None or priority > best[0]):
                best = (priority, target, part)
    if best is None:
        return {}
    _, target, (doc, cc) = best
    out = {'grade_environment': target[1].get('Name', '')}
    # Battle for Neighborville names its table HdrColorGradingLut, Garden Warfare ColorGradingTexture.
    table = cc.get('HdrColorGradingLut') or cc.get('ColorGradingTexture')
    if cc.get('ColorGradingEnable') and isinstance(table, dict) and table.get('$pointer', 1):
        path = lut(doc, table)
        if path:
            out['grade_lut'] = path
    for key, name in (('Brightness', 'grade_brightness'), ('Contrast', 'grade_contrast'), ('Saturation', 'grade_saturation')):
        value = vec(cc.get(key))
        if value and any(abs(v - 1) > 1e-4 for v in value):
            out[name] = value
    return out if len(out) > 1 else {}


GW_RGBA8 = 18   # Garden Warfare's texture format for its grading tables


def lut_chunk(header):
    """The chunk GUID (hex) of a grading table's texels: at header byte 32 for Garden Warfare, 36 for Battle for
    Neighborville (reader layout)."""
    import struct
    if len(header) < 52:
        return None
    at = 32 if struct.unpack_from('<I', header, 12)[0] == GW_RGBA8 else 36
    return header[at:at + 16].hex()


def grading_lut(header, data):
    """A colour grading LUT as a 2D image for the importer: (width n * n, height n, RGBA bytes), the n blue slices
    side by side, red across each slice and green up the image. Battle for Neighborville's GradingLutAsset is a 3D
    texture, n x n x n, 10:10:10:2 unsigned, green rows stored top down; Garden Warfare's ColorGradingTexture is a
    3D texture of format 18 (8-bit RGBA) with its size four bytes earlier in the header, green rows bottom up.
    None for any other layout."""
    import struct
    import numpy as np
    if len(header) < 52 or struct.unpack_from('<I', header, 8)[0] != 2:
        return None
    gw = struct.unpack_from('<I', header, 12)[0] == GW_RGBA8
    n, height, depth = struct.unpack_from('<3H', header, 18 if gw else 22)
    if not 2 <= n <= 128 or n != height or n != depth or len(data) < n ** 3 * 4 or (not gw and len(data) != n ** 3 * 4):
        return None
    if gw:
        texels = np.frombuffer(data[:n ** 3 * 4], np.uint8).reshape(n, n, n, 4)[:, ::-1]   # green rows top down
        rgb = texels[..., :3].astype(np.float32) / 255
    else:
        words = np.frombuffer(data, '<u4').reshape(n, n, n).astype(np.uint32)     # [blue][green, top down][red]
        rgb = np.stack([words & 1023, (words >> 10) & 1023, (words >> 20) & 1023], -1).astype(np.float32) / 1023
    image = np.full((n, n * n, 4), 255, np.uint8)
    # Rows top down are green 1..0, as the game stores them; columns are blue slice * n + red.
    image[..., :3] = np.round(rgb.transpose(1, 0, 2, 3).reshape(n, n * n, 3) * 255)
    return n * n, n, image.tobytes()


PBR_LIGHTS = {'PbrSphereLightEntityData': 'POINT', 'PbrSpotLightEntityData': 'SPOT',
              'PbrRectangularLightEntityData': 'AREA'}


def pbr_light_values(obj):
    """A Battle for Neighborville light as plain values, its power in physical watts (see scale_lights).
    None for lights the game switches on itself (Enabled off) or that give no light."""
    if not obj.get('Enabled', True) or float(obj.get('Dimmer', 1.0)) <= 0:
        return None
    color = vec(obj.get('Color')) or [1.0, 1.0, 1.0]
    peak = max(color)
    lumens = float(obj.get('Intensity', 0.0)) * float(obj.get('Dimmer', 1.0)) * peak
    if peak <= 0 or lumens <= 0:
        return None
    kind = PBR_LIGHTS[obj['$type']]
    out = {'type': kind, 'color': [c / peak for c in color], 'radius': float(obj.get('AttenuationRadius', 10.0)),
           'watts': lumens / LUMENS_PER_WATT * (4.0 if kind == 'SPOT' else 1.0)}
    if kind == 'POINT':
        out['size'] = 2.0 * float(obj.get('SphereRadius', 0.0))
    elif kind == 'SPOT':
        out['size'] = 2.0 * float(obj.get('DiscRadius', 0.0))
        out['cone_outer'] = float(obj.get('OuterAngle', 45.0))
        out['cone_inner'] = float(obj.get('InnerAngle', 0.0))
    else:
        out['width'], out['height'] = float(obj.get('Width', 1.0)), float(obj.get('Height', 1.0))
    return out


def scale_lights(places, environment):
    """Scale physically based lights by the factor that brings the sun to BLENDER_SUN (its lux / 683 in W/m2),
    so they keep the game's balance with it. Without an environment sun, a 5000 lux daylight is assumed."""
    sun = float(environment.get('sun_lux') or 5000.0) / LUMENS_PER_WATT
    factor = BLENDER_SUN / max(sun, 1e-6)
    for place in places:
        values = place.get('values', {})
        if place.get('kind') == 'frostbite_light' and 'watts' in values:
            values['watts'] = round(values['watts'] * factor, 4)
    return factor
