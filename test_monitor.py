import copy
import json
from pathlib import Path
import tempfile
import unittest
import monitor as m


def sample(value='a', version='4.6.53'):
    return {'version': version, 'records': {'character/1511': value},
            'labels': {'character/1511': '角色：星神★阿哈'}}


class MonitorTests(unittest.TestCase):
    def test_baseline_is_silent(self):
        def forbidden(*args):
            self.fail('Initial scan must not email')
        state, status = m.process({}, sample(), 2, forbidden)
        self.assertEqual(status, 'baseline_created')
        self.assertEqual(state['baseline'], sample())

    def test_confirmation_and_dedup(self):
        sent = []
        state = {'baseline': sample(), 'candidate': None}
        state, status = m.process(state, sample('b'), 2, lambda *a: sent.append(a))
        self.assertEqual(status, 'pending_confirmation')
        self.assertEqual(sent, [])
        state, status = m.process(state, sample('b'), 2, lambda *a: sent.append(a))
        self.assertEqual(status, 'notified')
        state, status = m.process(state, sample('b'), 2, lambda *a: sent.append(a))
        self.assertEqual(status, 'unchanged')
        self.assertEqual(len(sent), 1)

    def test_transient_change_does_not_notify(self):
        state = {'baseline': sample(), 'candidate': None}
        state, _ = m.process(state, sample('b'), 2)
        state, status = m.process(state, sample(), 2)
        self.assertEqual(status, 'unchanged')
        self.assertIsNone(state['candidate'])
        state, status = m.process(state, sample('b'), 2)
        self.assertEqual(status, 'pending_confirmation')

    def test_mail_failure_preserves_state(self):
        state = {'baseline': sample(), 'candidate': None}
        before = copy.deepcopy(state)
        def fail(*args):
            raise OSError('Mail unavailable')
        with self.assertRaises(OSError):
            m.process(state, sample('b'), 1, fail)
        self.assertEqual(state, before)

    def test_moving_candidate_resets_count(self):
        state = {'baseline': sample(), 'candidate': None}
        state, _ = m.process(state, sample('b'), 2)
        state, status = m.process(state, sample('c'), 2)
        self.assertEqual(status, 'pending_confirmation')
        self.assertEqual(state['candidate']['count'], 1)

    def test_content_hash_ignores_key_order(self):
        self.assertEqual(m.digest({'a': 1, 'b': 2}), m.digest({'b': 2, 'a': 1}))

    def test_snapshot_checks_details_and_manifest_consistency(self):
        manifest = {'hsr': {'latest': '4.6.53', 'available': ['4.6.53'],
                            'new': {'character': [1511]}}}
        def getter(url):
            if url == m.MANIFEST:
                return manifest
            if '/zh/' in url:
                return {'name': '星神★阿哈', 'skills': {'damage': 100}}
            return {'1511': {'baseType': 'Elation'}}
        snap = m.snapshot({}, getter)
        self.assertIn('character/1511', snap['records'])
        def fail_detail(url):
            if '/zh/' in url:
                raise OSError('404')
            return getter(url)
        with self.assertRaises(OSError):
            m.snapshot({}, fail_detail)
        calls = []
        def changing(url):
            if url == m.MANIFEST:
                calls.append(url)
                if len(calls) > 1:
                    return {'hsr': {'latest': '4.6.54'}}
            return getter(url)
        with self.assertRaises(ValueError):
            m.snapshot({}, changing)

    def test_state_atomic_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            state = {'baseline': sample(), 'candidate': None}
            m.write_state(path, state)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), state)
            self.assertFalse(path.with_suffix('.tmp').exists())


if __name__ == '__main__':
    unittest.main()
