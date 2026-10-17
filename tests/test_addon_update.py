"""Run with python -m unittest discover -s tests -p test_addon_update.py -v."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


update = load('addon_update_checks', ROOT / 'frostbite_importer/addon_update.py')


class ArchiveChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / 'updates'
        self.cache.mkdir()
        self.target = self.root / 'installed'
        self.target.mkdir()
        (self.target / 'blender_manifest.toml').write_text('version = "1.10.2"')
        (self.target / 'old.py').write_text('old')
        self.archive = self.cache / 'update.zip'

    def pack(self, **changes):
        files = {'blender_manifest.toml': 'id = "frostbite_importer"\nversion = "1.10.3"\nblender_version_min = "4.2.0"',
                 '__init__.py': '# new', 'app_link.py': '# link', 'addon_update.py': '# updater'}
        files.update(changes)
        with zipfile.ZipFile(self.archive, 'w') as z:
            for name, data in files.items():
                z.writestr(name, data)
        return 'sha256:' + hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def install(self, digest, **options):
        args = dict(archive=self.archive, digest=digest, expected_version='1.10.3',
                    current_version='1.10.2', blender_version=(4, 2, 0), cache=self.cache, target=self.target)
        args.update(options)
        return update.install(**args)

    def assert_original(self):
        self.assertEqual((self.target / 'old.py').read_text(), 'old')
        self.assertFalse((self.target / '__init__.py').exists())

    def test_install_replaces_package_without_stale_files(self):
        self.assertEqual(self.install(self.pack()), '1.10.3')
        self.assertEqual((self.target / '__init__.py').read_text(), '# new')
        self.assertFalse((self.target / 'old.py').exists())
        self.assertFalse(list(self.root.glob('.frostbite-*')))

    def test_reject_bad_digest(self):
        self.pack()
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.install('sha256:' + '0' * 64)
        self.assert_original()

    def test_reject_missing_digest(self):
        self.pack()
        with self.assertRaisesRegex(ValueError, 'Missing update checksum'):
            self.install(None)

    def test_reject_outside_cache(self):
        digest = self.pack()
        other = self.root / 'update.zip'
        other.write_bytes(self.archive.read_bytes())
        with self.assertRaisesRegex(ValueError, 'updates folder'):
            self.install(digest, archive=other)

    def test_reject_wrong_manifest(self):
        for manifest in ('id="another_addon"\nversion="1.10.3"', 'id="frostbite_importer"\nversion="1.10.4"'):
            with self.subTest(manifest=manifest), self.assertRaisesRegex(ValueError, 'manifest'):
                self.install(self.pack(**{'blender_manifest.toml': manifest}))
            self.assert_original()

    def test_reject_no_updater(self):
        digest = self.pack()
        with zipfile.ZipFile(self.archive) as z:
            files = {n: z.read(n) for n in z.namelist() if n != 'addon_update.py'}
        with zipfile.ZipFile(self.archive, 'w') as z:
            for name, data in files.items():
                z.writestr(name, data)
        digest = 'sha256:' + hashlib.sha256(self.archive.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'updater support'):
            self.install(digest)

    def test_reject_traversal_and_windows_paths(self):
        for name in ('../escaped.py', '/escaped.py', 'C:/escaped.py', 'nested\\..\\..\\escaped.py', 'name./file.py'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Unsafe'):
                self.install(self.pack(**{name: 'bad'}))
            self.assert_original()

    def test_reject_symlink(self):
        self.pack()
        with zipfile.ZipFile(self.archive, 'a') as z:
            entry = zipfile.ZipInfo('link')
            entry.external_attr = 0o120777 << 16
            z.writestr(entry, '../escaped')
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            self.install('sha256:' + hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def test_reject_case_collision(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.install(self.pack(**{'APP_LINK.PY': 'bad'}))

    def test_reject_unsupported_blender(self):
        with self.assertRaisesRegex(ValueError, 'newer Blender'):
            self.install(self.pack(), blender_version=(4, 1, 0))
        manifest = 'id="frostbite_importer"\nversion="1.10.3"\nblender_version_max="4.2.0"'
        with self.assertRaisesRegex(ValueError, 'does not support'):
            self.install(self.pack(**{'blender_manifest.toml': manifest}))

    def test_reject_equal_or_older_version(self):
        for installed in ('1.10.3', '1.11.0'):
            with self.assertRaisesRegex(ValueError, 'already'):
                self.install(self.pack(), current_version=installed)

    def test_restore_after_commit_failure(self):
        digest = self.pack()
        original = Path.rename

        def rename(path, destination):
            if path.name.startswith('.frostbite-update-') and not path.name.endswith('-backup'):
                raise OSError('Simulated locked destination')
            return original(path, destination)

        with patch.object(Path, 'rename', rename), self.assertRaisesRegex(OSError, 'locked'):
            self.install(digest)
        self.assert_original()
        self.assertFalse(list(self.root.glob('.frostbite-*')))


class BridgeChecks(unittest.TestCase):
    def setUp(self):
        package = types.ModuleType('frostbite_update_test')
        package.__path__ = [str(ROOT / 'frostbite_importer')]
        package.addon_update = update
        bpy = types.SimpleNamespace(app=types.SimpleNamespace(version=(4, 2, 0), version_string='4.2.0'))
        self.modules = patch.dict(sys.modules, {'bpy': bpy, 'frostbite_update_test': package})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.bridge = load('frostbite_update_test.app_link', ROOT / 'frostbite_importer/app_link.py')
        self.bridge._snap = lambda: None
        self.bridge._snapshot['tabs'] = {'maps': {'busy': False}}

    def test_busy_blender_rejects_install(self):
        self.bridge._snapshot['tabs']['maps']['busy'] = True
        with patch.object(update, 'install') as install:
            self.assertIn('finish', self.bridge._run({'kind': 'addon_update'}))
            install.assert_not_called()

    def test_install_job_done_and_version_frozen_until_restart(self):
        old_version = self.bridge._version()
        request = {'kind': 'addon_update', 'archive': 'test.zip', 'digest': 'sha256:test', 'version': '1.10.3'}
        self.bridge._jobs['1'] = {'state': 'queued', 'kind': 'addon_update'}
        self.bridge._requests.put(('1', request))
        with patch.object(update, 'install', return_value='1.10.3'):
            self.bridge.tick()
        self.assertEqual(self.bridge._jobs['1']['state'], 'done')
        self.assertEqual(self.bridge._restart_required, '1.10.3')
        self.assertEqual(self.bridge._version(), old_version)
        self.assertIn('restart Blender', self.bridge._run({'kind': 'maps'}))

    def test_install_exception_reported_as_failed_job(self):
        self.bridge._jobs['1'] = {'state': 'queued', 'kind': 'addon_update'}
        self.bridge._requests.put(('1', {'kind': 'addon_update', 'archive': 'x', 'digest': 'x', 'version': 'x'}))
        with patch.object(update, 'install', side_effect=ValueError('bad checksum')):
            self.bridge.tick()
        self.assertEqual(self.bridge._jobs['1']['state'], 'failed')
        self.assertIn('checksum', self.bridge._jobs['1']['message'])

    def test_http_capability_headers_and_update_queue(self):
        server = self.bridge.Server(('127.0.0.1', 0), self.bridge.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:%s' % server.server_address[1]
        with self.assertRaises(HTTPError) as error:
            urlopen(url + '/ping')
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        headers = {'X-FrostyBlender': '1', 'Content-Type': 'application/json'}
        with urlopen(Request(url + '/ping', headers=headers)) as response:
            self.assertTrue(json.load(response)['addon_update'])
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(url + '/ping', headers=dict(headers, Origin='https://example.com')))
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        payload = json.dumps({'archive': 'test.zip', 'digest': 'sha256:test', 'version': '1.10.3'}).encode()
        with urlopen(Request(url + '/addon-update', data=payload, headers=headers)) as response:
            self.assertEqual(response.status, 202)
        _, request = self.bridge._requests.get_nowait()
        self.assertEqual(request['kind'], 'addon_update')
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(url + '/addon-update', data=b'[]', headers=headers))
        self.assertEqual(error.exception.code, 400)
        error.exception.close()


if __name__ == '__main__':
    unittest.main()
