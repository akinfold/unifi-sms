"""Test isolated registry readiness without starting a registry."""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch


MODULE_PATH = Path(__file__).with_name('smoke_release_manifest.py')
spec = importlib.util.spec_from_file_location('smoke_release_manifest', MODULE_PATH)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class RegistryReadinessTests(unittest.TestCase):
    def test_connection_reset_is_retried_until_ready(self):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        with patch.object(smoke.time, 'monotonic', side_effect=[0, 0, 0.1, 0.3]), \
             patch.object(smoke.time, 'sleep') as sleep, \
             patch.object(smoke, 'urlopen', side_effect=[ConnectionResetError(), response]) as urlopen:
            smoke.wait_for_registry(5108)
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(urlopen.call_args.args, ('http://127.0.0.1:5108/v2/',))
        sleep.assert_called_once_with(0.2)

    def test_deadline_bounds_retry_and_raises(self):
        with patch.object(smoke.time, 'monotonic', side_effect=[0, 19.9, 19.95, 20]), \
             patch.object(smoke.time, 'sleep') as sleep, \
             patch.object(smoke, 'urlopen', side_effect=ConnectionResetError()) as urlopen:
            with self.assertRaisesRegex(RuntimeError, 'isolated test registry did not start'):
                smoke.wait_for_registry(5108)
        self.assertAlmostEqual(urlopen.call_args.kwargs['timeout'], 0.1)
        self.assertAlmostEqual(sleep.call_args.args[0], 0.05)
        self.assertEqual(urlopen.call_count, 1)


if __name__ == '__main__':
    unittest.main()
