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
