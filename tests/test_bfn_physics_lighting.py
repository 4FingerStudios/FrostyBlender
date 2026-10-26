"""Battle for Neighborville collision (tools/bfn_physics.py) and lighting rules (tools/environment.py);
synthetic data only, no game files."""
import math
import struct
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import bfn_physics
import environment


def volume(vertices, triangles):
    a, b, c = vertices[triangles[:, 0]], vertices[triangles[:, 1]], vertices[triangles[:, 2]]
    return float(np.einsum('ij,ij->i', a, np.cross(b, c)).sum() / 6)


CUBE = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], float)


def physics_resource(instances):
    """A version 2 physics resource with one box hull and the given (kind, position, half/w, radius) instances."""
    header = struct.pack('<12I', 2, 0, 1, len(instances), 1, 8, 24, 0, 0, 8, 0, 0)
    shape = bytes(0x28) + struct.pack('<3I', 8, 24, 8) + bytes(4)
    body = header + shape
    body += bytes((-len(body)) % 16)
    for kind, position, w, radius in instances:
        entry = struct.pack('<4f4f', *position, w, 0, 0, 0, 1) + struct.pack('<f I', radius, kind)
        entry += bytes(0x34 - len(entry)) + struct.pack('<I', 0) + bytes(8)
        body += entry
    body += np.c_[CUBE, np.zeros(8)].astype('<f4').tobytes()
    return body + bytes(8 * 8 + 24 * 8 + 4)


class PhysicsChecks(unittest.TestCase):
    def test_hull_of_a_cube_is_twelve_outward_triangles(self):
        points = np.vstack([CUBE, [[0.2, 0.1, -0.3]]])  # an inside point is not a hull vertex
        vertices, triangles = bfn_physics.convex_hull(points)
        self.assertEqual(len(triangles), 12)
        self.assertAlmostEqual(volume(vertices, triangles), 8.0, places=4)

    def test_flat_points_have_no_hull(self):
        self.assertIsNone(bfn_physics.convex_hull([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0]]))

    def test_capsule_runs_along_y_and_points_outward(self):
        vertices, triangles = bfn_physics.round_mesh(0.5, 2.0)
        self.assertAlmostEqual(vertices[:, 1].max(), 2.5)
        self.assertAlmostEqual(vertices[:, 1].min(), -2.5)
        exact = math.pi * 0.25 * 4.0 + 4 / 3 * math.pi * 0.125
        self.assertTrue(0.8 * exact < volume(vertices, triangles) < exact)

    def test_resource_places_hull_and_sphere_instances(self):
        data = physics_resource([(3, (0, 5, 0), 1, 0), (0, (10, 0, 0), 1, 1.0)])
        self.assertEqual(len(bfn_physics.shapes(data)), 2)
        section = bfn_physics.collision_sections(data)[0]
        positions, triangles = section['positions'], section['indices'].reshape(-1, 3).astype(np.int64)
        self.assertAlmostEqual(float(positions[:, 1].max()), 6.0, places=4)
        self.assertAlmostEqual(float(positions[:, 0].max()), 11.0, places=4)
        self.assertTrue(8.0 < volume(positions.astype(float), triangles) < 8.0 + 4 / 3 * math.pi)

    def test_unknown_layouts_are_skipped(self):
        data = bytearray(physics_resource([(3, (0, 0, 0), 1, 0)]))
        struct.pack_into('<I', data, 4, 1)  # an extra section this reader doesn't know
        self.assertIsNone(bfn_physics.shapes(bytes(data)))


class LightingChecks(unittest.TestCase):
    def test_lights_the_game_switches_on_are_skipped(self):
        light = {'$type': 'PbrSphereLightEntityData', 'Enabled': False, 'Intensity': 1000.0,
                 'Color': {'x': 1, 'y': 1, 'z': 1}}
        self.assertIsNone(environment.pbr_light_values(light))

    def test_spot_lumens_become_four_times_point_watts(self):
        common = {'Enabled': True, 'Intensity': 683.0, 'Color': {'x': 1, 'y': 0.5, 'z': 0}}
        point = environment.pbr_light_values(dict(common, **{'$type': 'PbrSphereLightEntityData'}))
        spot = environment.pbr_light_values(dict(common, **{'$type': 'PbrSpotLightEntityData', 'OuterAngle': 60.0}))
        self.assertAlmostEqual(point['watts'], 1.0)
        self.assertAlmostEqual(spot['watts'], 4.0)
        self.assertEqual(point['color'], [1.0, 0.5, 0.0])
        self.assertEqual(spot['cone_outer'], 60.0)

    def test_lights_scale_with_the_sun(self):
        places = [{'kind': 'frostbite_light', 'values': {'watts': 100.0}}]
        factor = environment.scale_lights(places, {'sun_lux': 68300.0})
        self.assertAlmostEqual(factor, environment.BLENDER_SUN / 100.0)
        self.assertAlmostEqual(places[0]['values']['watts'], 2.0)

    def test_main_environment_wins_over_area_ones(self):
        docs = {
            'main': [{'$type': 'OutdoorLightComponentData', 'SunColor': {'x': 1, 'y': 1, 'z': 1}, 'SunIntensity': 5000.0},
                     {'$type': 'SkyComponentData'}, {'$type': 'FogComponentData', 'Start': 10.0, 'End': 90.0}],
            'area': [{'$type': 'OutdoorLightComponentData', 'SunColor': {'x': 0, 'y': 1, 'z': 0}}],
        }

        def resolve(doc, ref):
            if ref == 'entity':
                return doc, {'Components': list(range(len(docs[doc])))}
            return doc, docs[doc][ref]
        found = environment.pick([(10, 's', ('area', {'Object': 'entity', 'Name': 'Area'})),
                                  (1, 's', ('main', {'Object': 'entity', 'Name': 'Main'}))],
                                 resolve, lambda doc, ref: None, physical=True)
        self.assertEqual(found['environment'], 'Main')
        self.assertEqual(found['sun_lux'], 5000.0)
        self.assertEqual((found['fog_start'], found['fog_end']), (10.0, 90.0))


if __name__ == '__main__':
    unittest.main()
