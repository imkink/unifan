"""Host probe bookkeeping/reconnection tests; never access actual hardware."""
import unittest
from unittest.mock import Mock, patch

try:
    from tools import https_probe as probe
except ImportError:
    probe = None


@unittest.skipIf(probe is None, "Install requirements-dev.txt for HTTPS probe tests")
class HTTPSProbeTests(unittest.TestCase):
    def test_empty_summary(self):
        result = probe.summarize([])
        self.assertEqual(result["requests"], 0)
        self.assertIsNone(result["response_p95_ms"])

    def test_failures_are_not_success_timings(self):
        result = probe.summarize([
            {"status": 200, "elapsed_ms": 10, "handshake_ms": 4},
            {"status": 200, "elapsed_ms": 30, "handshake_ms": 8},
            {"status": 401, "elapsed_ms": 1, "handshake_ms": 1},
            {"error": "ConnectionResetError"},
        ])
        self.assertEqual(result["successful"], 2)
        self.assertEqual(result["failures"], 2)
        self.assertEqual(result["response_p50_ms"], 20)
        self.assertEqual(result["response_p95_ms"], 30)
        self.assertEqual(result["handshake_p95_ms"], 8)

    def test_reconnection_reopens_descriptor(self):
        stale, good = Mock(), Mock()
        stale.enter_raw_repl.side_effect = OSError("USB disconnected")
        with patch.object(probe, "SerialTransport", side_effect=[stale, good]), \
                patch.object(probe.time, "sleep"):
            self.assertIs(probe.connect_raw("test-port"), good)
        stale.serial.close.assert_called_once()
        good.serial.close.assert_not_called()

    def test_reconnection_is_bounded(self):
        failed = Mock()
        failed.enter_raw_repl.side_effect = OSError("USB disconnected")
        with patch.object(probe, "SerialTransport", return_value=failed) as constructor, \
                patch.object(probe.time, "sleep"):
            with self.assertRaises(OSError):
                probe.connect_raw("test-port")
        self.assertEqual(constructor.call_count, 4)
        self.assertEqual(failed.serial.close.call_count, 4)
