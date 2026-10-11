import importlib.util
from pathlib import Path
import unittest
import tempfile
from unittest import mock

spec = importlib.util.spec_from_file_location('relay_speed', Path(__file__).resolve().parents[1] / 'relay-speed.py')
speed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(speed)


class SpeedTests(unittest.TestCase):
    def test_all_backup_paths_are_rejected(self):
        with self.assertRaises(ValueError):
            speed.validate({'paths': [{'name': 'wg_data_1', 'port': 13021, 'backup': True}]})

    def test_bootstrap_waits_without_writing_or_reloading(self):
        with mock.patch.object(speed, 'Path') as path, mock.patch.object(speed.subprocess, 'run') as run:
            path.return_value.read_text.return_value = 'TRANSPORT=backhaul\n'
            self.assertFalse(speed.bootstrap({'bootstrap': True}, False))
            path.return_value.write_text.assert_not_called()
            run.assert_not_called()

    def test_bootstrap_validates_then_only_gracefully_reloads_haproxy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = root / 'relay.conf'; relay.write_text('TRANSPORT=backhaul\n')
            haproxy = root / 'haproxy.cfg'; haproxy.write_text('old config\n')
            def mapped(value):
                return {'/etc/isaho-relay.conf': relay, '/etc/haproxy/haproxy.cfg': haproxy}.get(str(value), Path(value))
            with mock.patch.object(speed, 'Path', side_effect=mapped), mock.patch.object(speed.subprocess, 'check_output', return_value='new config\n'), mock.patch.object(speed.subprocess, 'run') as run:
                self.assertTrue(speed.bootstrap({'bootstrap': True, 'state_dir': directory}, True))
                self.assertIn('SPEED_AUTO=1', relay.read_text())
                self.assertEqual('new config\n', haproxy.read_text())
                self.assertEqual(['systemctl', 'reload', 'haproxy'], run.call_args_list[-1].args[0])

    def test_failed_bootstrap_preserves_working_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);relay=root/'relay.conf';relay.write_text('TRANSPORT=backhaul\n')
            haproxy=root/'haproxy.cfg';haproxy.write_text('old config\n')
            def mapped(value):
                return {'/etc/isaho-relay.conf': relay, '/etc/haproxy/haproxy.cfg': haproxy}.get(str(value), Path(value))
            with mock.patch.object(speed, 'Path', side_effect=mapped), mock.patch.object(speed.subprocess, 'check_output', return_value='invalid\n'), mock.patch.object(speed.subprocess, 'run', side_effect=RuntimeError('validation failed')):
                with self.assertRaises(RuntimeError):speed.bootstrap({'bootstrap': True, 'state_dir': directory}, True)
            self.assertEqual('TRANSPORT=backhaul\n', relay.read_text())
            self.assertEqual('old config\n', haproxy.read_text())

    def test_one_success_never_promotes_an_unproven_path(self):
        paths = [{'name': 'wg_data_1'}]
        state = {'samples': {'wg_data_1': [{'time': 100, 'ok': True, 'mbps': 100}]}}
        self.assertEqual(speed.scores(state, paths, 110), {})

    def test_failed_latest_sample_never_promotes(self):
        paths = [{'name': 'wg_data_1'}]
        state = {'samples': {'wg_data_1': [{'time': 100, 'ok': True, 'mbps': 100},
                    {'time': 110, 'ok': True, 'mbps': 100}, {'time': 120, 'ok': False}]}}
        self.assertEqual(speed.scores(state, paths, 130), {})

    def test_stale_scores_are_not_used(self):
        state = {'samples': {'wg_data_1': [{'time': 100, 'ok': True, 'mbps': 100}] * 2}}
        self.assertEqual(speed.scores(state, [{'name': 'wg_data_1'}], 5000), {})

    def test_small_difference_does_not_flap(self):
        self.assertEqual(speed.choose({'preferred': 'a', 'switched_at': 0},
                                     {'a': 100, 'b': 115}, {'a', 'b'}, 1000), ('a', False))

    def test_hold_down(self):
        self.assertEqual(speed.choose({'preferred': 'a', 'switched_at': 950},
                                     {'a': 100, 'b': 200}, {'a', 'b'}, 1000), ('a', False))

    def test_failure_bypasses_hold_down(self):
        self.assertEqual(speed.choose({'preferred': 'a', 'switched_at': 950},
                                     {'a': 100, 'b': 200}, {'b'}, 1000), ('b', True))

    def test_rotation_keeps_fallback_without_disabling_sessions(self):
        self.assertEqual(speed.plan('a', {'a': 100, 'b': 90, 'c': 30}, ['a', 'b', 'c']),
                         {'a': 200, 'b': 180, 'c': 1})

    def test_unmanaged_server_is_rejected(self):
        with self.assertRaises(ValueError):
            speed.validate({'paths': [{'name': 'bad; shutdown sessions', 'port': 13021}]})


if __name__ == '__main__':
    unittest.main()
