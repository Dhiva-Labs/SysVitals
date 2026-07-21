#!/usr/bin/env python3
"""Unit tests for the NVMe SMART/health log parser and lifetime projection."""
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "sysvitals", os.path.join(ROOT, "sysvitals.py"))
sv = importlib.util.module_from_spec(spec)
sys.modules["sysvitals"] = sv
spec.loader.exec_module(sv)


def le(buf, offset, nbytes, value):
    buf[offset:offset + nbytes] = int(value).to_bytes(nbytes, "little")


class ParseNvmeSmartTest(unittest.TestCase):
    def setUp(self):
        self.d = bytearray(512)

    def build(self, **overrides):
        d = self.d
        d[0] = overrides.get("critical_warning", 0x02)
        le(d, 1, 2, overrides.get("temp_k", 306))
        d[3] = overrides.get("avail_spare", 100)
        d[4] = overrides.get("spare_thresh", 10)
        d[5] = overrides.get("percent_used", 6)
        le(d, 32, 16, overrides.get("data_units_read", 30_000_000))
        le(d, 48, 16, overrides.get("data_units_written", 25_000_000))
        le(d, 64, 16, overrides.get("host_reads", 400_000_000))
        le(d, 80, 16, overrides.get("host_writes", 321_000_000))
        le(d, 96, 16, overrides.get("busy_minutes", 5000))
        le(d, 112, 16, overrides.get("power_cycles", 800))
        le(d, 128, 16, overrides.get("power_on_hours", 4000))
        le(d, 144, 16, overrides.get("unsafe_shutdowns", 25))
        le(d, 160, 16, overrides.get("media_errors", 3))
        le(d, 176, 16, overrides.get("error_log_entries", 120))
        le(d, 192, 4, overrides.get("warn_temp_minutes", 7))
        le(d, 196, 4, overrides.get("crit_temp_minutes", 1))
        le(d, 200, 2, overrides.get("sensor1_k", 306))
        le(d, 202, 2, overrides.get("sensor2_k", 310))
        return bytes(d)

    def test_all_fields_decoded_correctly(self):
        r = sv.parse_nvme_smart(self.build())
        self.assertEqual(r["critical_warning"], 0x02)
        self.assertEqual(r["warnings"], ["temperature out of range"])
        self.assertEqual(r["temp_c"], 33)
        self.assertEqual(r["avail_spare"], 100)
        self.assertEqual(r["spare_thresh"], 10)
        self.assertEqual(r["percent_used"], 6)
        self.assertEqual(r["life_left"], 94)
        self.assertEqual(r["data_read"], 30_000_000 * 512000)
        self.assertEqual(r["data_written"], 25_000_000 * 512000)
        self.assertEqual(r["host_reads"], 400_000_000)
        self.assertEqual(r["host_writes"], 321_000_000)
        self.assertEqual(r["busy_minutes"], 5000)
        self.assertEqual(r["power_cycles"], 800)
        self.assertEqual(r["power_on_hours"], 4000)
        self.assertEqual(r["unsafe_shutdowns"], 25)
        self.assertEqual(r["media_errors"], 3)
        self.assertEqual(r["error_log_entries"], 120)
        self.assertEqual(r["warn_temp_minutes"], 7)
        self.assertEqual(r["crit_temp_minutes"], 1)
        self.assertEqual(r["sensor_temps"], [33, 37])

    def test_healthy_drive_has_no_warnings(self):
        r = sv.parse_nvme_smart(self.build(critical_warning=0x00))
        self.assertEqual(r["warnings"], [])

    def test_multiple_warning_bits_all_decoded(self):
        r = sv.parse_nvme_smart(self.build(critical_warning=0x01 | 0x04))
        self.assertIn("spare below threshold", r["warnings"])
        self.assertIn("NVM reliability degraded", r["warnings"])
        self.assertEqual(len(r["warnings"]), 2)

    def test_life_left_floors_at_zero(self):
        r = sv.parse_nvme_smart(self.build(percent_used=150))
        self.assertEqual(r["life_left"], 0)

    def test_zero_sensor_readings_excluded(self):
        d = bytearray(self.build())
        le(d, 202, 2, 0)  # second sensor unpopulated
        r = sv.parse_nvme_smart(bytes(d))
        self.assertEqual(r["sensor_temps"], [33])


class LifeProjectionTest(unittest.TestCase):
    def test_projects_years_remaining(self):
        text = sv.life_projection(6, 4000)
        self.assertIn("projected", text)
        self.assertIn("y left", text)

    def test_zero_wear_too_early(self):
        self.assertIn("too early", sv.life_projection(0, 4000))

    def test_zero_hours_too_early(self):
        self.assertIn("too early", sv.life_projection(6, 0))

    def test_very_low_wear_rate_reports_cap(self):
        # 1% worn after 40,000h powered-on projects to centuries remaining
        self.assertIn(">99y", sv.life_projection(1, 40000))


if __name__ == "__main__":
    unittest.main()
