"""Archive concurrency, source precedence and malformed-stream regression checks."""
import io
import os
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import gw2
import bfn
from archive_parallel import ordered_map, worker_count


def block(payload, compression=0):
    raw = zlib.compress(payload) if compression == 2 else payload
    return struct.pack('>I', len(payload)) + struct.pack('<H', compression) + struct.pack('>H', len(raw)) + raw


class ParallelChecks(unittest.TestCase):
    def test_ordered_workers_overlap_and_propagate_failure(self):
        barrier = threading.Barrier(2)
        def work(i):
            barrier.wait(timeout=3)
            return i
        self.assertEqual(list(ordered_map(work, range(6), 2)), list(range(6)))
        def fail(i):
            if i == 2:
                raise ValueError('broken archive')
            return i
        with self.assertRaisesRegex(ValueError, 'broken archive'):
            list(ordered_map(fail, range(20), 3))

    def test_queue_is_bounded_even_for_an_unbounded_source(self):
        produced = []
        def inputs():
            for i in range(10000):
                produced.append(i)
                yield i
        results = ordered_map(lambda x: x, inputs(), 2)
        self.assertEqual(next(results), 0)
        self.assertLessEqual(len(produced), 5)
        results.close()
        self.assertEqual(worker_count(1), 1)
        self.assertEqual(worker_count(1000), 16)

    def test_skip_checks_ranges_and_matches_decompress_offset(self):
        raw = block(b'a'*200) + block(b'hello'*100, 2)
        r = gw2.Reader(raw)
        gw2.skip_asset(r, 700)
        reference = gw2.Reader(raw)
        self.assertEqual(gw2.decompress(reference, 700), b'a'*200+b'hello'*100)
        self.assertEqual(r.at, reference.at)
        for broken in (raw[:-1], b'', block(b'abc')[:7]):
            with self.assertRaises(ValueError):
                gw2.skip_asset(gw2.Reader(broken), 700)
        with self.assertRaises(ValueError):
            gw2.skip_asset(gw2.Reader(b'abc'), 4, decoded=True)

    def test_lz4_fallback_handles_overlapping_matches(self):
        with patch.object(gw2, 'fast_lz4', None):
            # Four literals followed by a 1,019-byte match, distance 4.
            raw = b'\x4fabcd\x04\x00'+bytes([255, 255, 255, 235])
            expected = (b'abcd'*256)[:1023]
            self.assertEqual(gw2.lz4(raw, len(expected)), expected)
            with self.assertRaises(ValueError):
                gw2.lz4(b'\x10a\x00\x00', 5)

    def test_bfn_independent_files_can_read_concurrently(self):
        archive = bfn.Archive.__new__(bfn.Archive)
        barrier = threading.Barrier(2)
        class Handle:
            def seek(self, offset):
                self.offset = offset
            def read(self, count):
                barrier.wait(timeout=3)
                return b'x'*count
        archive.handles = {'a': Handle(), 'b': Handle()}
        archive.lock = threading.Lock()
        archive.read_locks = {'a': threading.Lock(), 'b': threading.Lock()}
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda name: archive.read(name, 0, 3), ['a', 'b']))
        self.assertEqual(results, [b'xxx', b'xxx'])

    def test_bfn_shared_handle_keeps_seek_and_read_atomic(self):
        archive = bfn.Archive.__new__(bfn.Archive)
        archive.handles = {'file': io.BytesIO(bytes(range(256))*8)}
        archive.lock = threading.Lock()
        archive.read_locks = {'file': threading.Lock()}
        offsets = list(range(250))*2
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda at: archive.read('file', at, 6), offsets))
        self.assertEqual(results, [(bytes(range(256))*8)[at:at+6] for at in offsets])

    def test_dependency_memory_cache_invalidates_when_archive_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'Data/Win32/test.toc'
            source.parent.mkdir(parents=True)
            source.write_bytes(b'toc')
            source.with_suffix('.sb').write_bytes(b'sb')
            empty = {'ebx': {}, 'resource': {}, 'chunk': {}}
            gw2._DEPENDENCY_MEMORY.clear()

            with patch.dict(os.environ, AURA_IMPORT_CACHE=str(root/'cache')):
                with patch.object(gw2, 'dependency_index', return_value=empty) as index:
                    gw2.cached_dependency_index(root)
                    gw2.cached_dependency_index(root)
                    self.assertEqual(index.call_count, 1)
                    source.with_suffix('.sb').write_bytes(b'changed archive')
                    gw2.cached_dependency_index(root)
                    self.assertEqual(index.call_count, 2)
            gw2._DEPENDENCY_MEMORY.clear()

    def test_parallel_index_keeps_source_precedence_and_larger_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root/'Data/Win32'
            folder.mkdir(parents=True)
            for name in ('b', 'a'):
                (folder/(name+'.toc')).touch()
            def scan(toc):
                return {'ebx': {}, 'chunk': {}, 'resource': {
                    'equal': (toc, 0, 5, {'original_size': 5}),
                    'larger': (toc, 0, 10, {'original_size': 1 if toc.stem == 'a' else 10})}}
            with patch.object(gw2, '_index_toc', scan):
                index = gw2.dependency_index(root, jobs=2)
            self.assertEqual(index['resource']['equal'][0].stem, 'a')
            self.assertEqual(index['resource']['larger'][0].stem, 'b')

    def test_export_skips_duplicates_and_preserves_inventory_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root/'game/Data/Win32'
            data.mkdir(parents=True)
            (data/'globals.toc').touch()
            (data/'level.toc').touch()
            assets = [{'kind': 'chunk', 'name': 'first', 'guid': '1'*32,
                       'sha1': 'a'*40, 'original_size': 3},
                      {'kind': 'chunk', 'name': 'second', 'guid': '2'*32,
                       'sha1': 'b'*40, 'original_size': 4}]
            raw = block(b'one')+block(b'two!')
            def bundles(game, toc):
                return iter([({'id': toc.stem}, raw, False)])
            def metadata(raw):
                return assets, gw2.Reader(raw)
            with patch.object(gw2, 'bundles_with_patches', bundles), patch.object(gw2, 'bundle', metadata):
                with patch.object(gw2, 'decompress', wraps=gw2.decompress) as decoder:
                    serial = gw2.export(root/'game', 'Win32/level', root/'serial', jobs=1)
                    self.assertEqual(decoder.call_count, 2)
                parallel = gw2.export(root/'game', 'Win32/level', root/'parallel', jobs=4)
            self.assertEqual(serial, parallel)
            self.assertEqual([a['name'] for a in parallel['assets']], ['first', 'second'])
            for asset in parallel['assets']:
                self.assertEqual((root/'serial'/asset['path']).read_bytes(), (root/'parallel'/asset['path']).read_bytes())
            # Patched bundles have already been decoded. Never try to inflate
            # their concatenated raw assets a second time in worker threads.
            def patched_bundles(game, toc):
                return iter([({'id': toc.stem}, b'onetwo!', True)])
            with patch.object(gw2, 'bundles_with_patches', patched_bundles), patch.object(gw2, 'bundle', metadata):
                with patch.object(gw2, 'decompress', side_effect=AssertionError('Already decoded')):
                    patched = gw2.export(root/'game', 'Win32/level', root/'patched', jobs=4)
            self.assertEqual(serial, patched)


if __name__ == '__main__':
    unittest.main()
