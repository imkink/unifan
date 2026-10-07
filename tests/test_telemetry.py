"""模拟硬件接口与采样骨架测试；不需要传感器、PWM 或真实 GPIO。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware/app_a"))
import app_config
from hardware import create_hardware, SimulatedSensor, SimulatedFans
from telemetry import TelemetryService
import telemetry as telemetry_module
from control import compute_group_targets


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tick = 1000
        context = patch.object(telemetry_module.time, "ticks_ms", lambda: self.tick, create=True)
        context.start()
        self.addCleanup(context.stop)
        context = patch.object(telemetry_module.time, "ticks_diff", lambda a, b: a - b, create=True)
        context.start()
        self.addCleanup(context.stop)
        self.sensor, self.fans = create_hardware()
        self.service = TelemetryService(self.sensor, self.fans)

    async def test_snapshot_marks_all_values_as_simulated(self):
        await self.service.initialize()
        state = self.service.snapshot()
        self.assertTrue(state["simulated"])
        self.assertFalse(state["hardware_ready"])
        self.assertFalse(state["control_policy_ready"])
        self.assertEqual(state["source"], "simulated")
        self.assertEqual(len(state["fans"]), 6)
        self.assertTrue(state["environment"]["simulated"])
        self.assertTrue(all(fan["simulated"] for fan in state["fans"]))

    async def test_group_output_changes_only_associated_simulated_fans(self):
        self.fans.set_group_pwm(2, 50)
        await self.service.initialize()
        fans = self.service.snapshot()["fans"]
        self.assertEqual([fan["pwm_percent"] for fan in fans], [30, 30, 30, 30, 50, 50])
        self.assertEqual([fan["rpm"] for fan in fans], [600, 600, 600, 600, 1000, 1000])

    async def test_change_environment_does_not_imply_implemented_curve_control(self):
        self.sensor.set_environment(60, 70)
        await self.service.initialize()
        state = self.service.snapshot()
        self.assertEqual(state["environment"]["temperature_c"], 60)
        self.assertEqual(state["fans"][0]["pwm_percent"], 30)
        with self.assertRaises(NotImplementedError):
            compute_group_targets(60, [], [30, 30])

    async def test_snapshot_is_not_mutable_internal_state(self):
        await self.service.initialize()
        state = self.service.snapshot()
        state["environment"]["temperature_c"] = 0
        state["fans"][0]["rpm"] = 999
        again = self.service.snapshot()
        self.assertEqual(again["environment"]["temperature_c"], 26)
        self.assertEqual(again["fans"][0]["rpm"], 600)

    async def test_health_depends_on_recent_sampling(self):
        self.assertFalse(self.service.healthy())
        await self.service.initialize()
        self.assertTrue(self.service.healthy())
        self.tick += app_config.SAMPLE_STALE_MS
        self.assertFalse(self.service.healthy())
        self.assertTrue(self.service.snapshot()["stale"])
        await self.service.sample_once()
        self.assertTrue(self.service.healthy())
        self.assertEqual(self.service.snapshot()["sequence"], 2)

    async def test_failed_read_does_not_publish_partial_snapshot(self):
        await self.service.initialize()
        self.sensor.set_environment(40, 50)
        async def fail():
            raise OSError("simulated sensor failure")
        self.fans.read = fail
        with self.assertRaises(OSError):
            await self.service.sample_once()
        state = self.service.snapshot()
        self.assertEqual(state["sequence"], 1)
        self.assertEqual(state["environment"]["temperature_c"], 26)

    async def test_real_mode_explicitly_fails_instead_of_fabricating_data(self):
        sensor, fans = create_hardware(SimpleNamespace(HARDWARE_MODE="hardware"))
        self.assertFalse(sensor.simulated)
        self.assertFalse(fans.simulated)
        with self.assertRaises(NotImplementedError):
            await TelemetryService(sensor, fans).initialize()
        with self.assertRaises(NotImplementedError):
            fans.set_group_pwm(1, 30)
        with self.assertRaises(ValueError):
            create_hardware(SimpleNamespace(HARDWARE_MODE="typo"))

    def test_invalid_simulation_inputs_rejected(self):
        for temperature, humidity in ((float('nan'), 50), (20, 101), (True, 50)):
            with self.assertRaises(ValueError):
                SimulatedSensor(temperature, humidity)
        for group, percent in ((0, 50), (3, 50), (True, 50), (1, -1), (1, 101)):
            with self.assertRaises(ValueError):
                self.fans.set_group_pwm(group, percent)

    async def test_periodic_sampling_runs_and_cancels_cleanly(self):
        settings = SimpleNamespace(SAMPLE_INTERVAL_SECONDS=0.001, SAMPLE_STALE_MS=1000)
        service = TelemetryService(self.sensor, self.fans, settings)
        await service.initialize()
        task = asyncio.create_task(service.run())
        try:
            await asyncio.sleep(0.01)
            self.assertGreater(service.snapshot()["sequence"], 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
