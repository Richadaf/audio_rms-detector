"""Tests for rms_scan parser behavior."""

from __future__ import annotations

import math
import unittest

import rms_scan


class ParseAstatsOutputTests(unittest.TestCase):
    def test_parses_overall_and_channel_key_value_lines(self) -> None:
        lines = [
            "frame:0 pts:0 pts_time:0 lavfi.astats.Overall.RMS_level=-24.0",
            "lavfi.astats.Overall.RMS_peak=-22.0",
            "lavfi.astats.1.RMS_level=-23.2",
            "lavfi.astats.1.RMS_peak=-19.9",
            "something else lavfi.astats.Overall.RMS_level: -21.5",
        ]

        result = rms_scan.parse_astats_output(lines)

        self.assertAlmostEqual(result.overall["RMS_level"].last, -21.5)
        self.assertAlmostEqual(result.overall["RMS_level"].max_observed, -21.5)
        self.assertAlmostEqual(result.overall["RMS_peak"].last, -22.0)
        self.assertIn("1", result.channels)
        self.assertAlmostEqual(result.channels["1"]["RMS_level"].last, -23.2)
        self.assertAlmostEqual(result.channels["1"]["RMS_peak"].last, -19.9)

    def test_handles_negative_infinity(self) -> None:
        lines = [
            "lavfi.astats.Overall.RMS_level=-inf",
            "lavfi.astats.Overall.RMS_peak=-inf",
        ]

        result = rms_scan.parse_astats_output(lines)
        self.assertTrue(math.isinf(result.overall["RMS_level"].last))
        self.assertLess(result.overall["RMS_level"].last, 0.0)
        self.assertEqual(result.selected_overall("RMS_peak"), float("-inf"))

    def test_tracks_last_seen_and_max_observed(self) -> None:
        lines = [
            "lavfi.astats.Overall.RMS_peak=-30.0",
            "lavfi.astats.Overall.RMS_peak=-25.0",
            "lavfi.astats.Overall.RMS_peak=-27.0",
        ]

        result = rms_scan.parse_astats_output(lines)

        self.assertAlmostEqual(result.overall["RMS_peak"].last, -27.0)
        self.assertAlmostEqual(result.overall["RMS_peak"].max_observed, -25.0)
        self.assertAlmostEqual(result.selected_overall("RMS_peak"), -27.0)

    def test_parses_fallback_summary_lines(self) -> None:
        lines = [
            "[Parsed_astats_0 @ 0x0] Overall RMS level dB: -20.6",
            "[Parsed_astats_0 @ 0x0] Overall RMS peak dB: -18.4",
            "[Parsed_astats_0 @ 0x0] Channel: 2",
            "[Parsed_astats_0 @ 0x0] RMS level dB: -21.1",
            "[Parsed_astats_0 @ 0x0] RMS peak dB: -18.8",
        ]

        result = rms_scan.parse_astats_output(lines)

        self.assertAlmostEqual(result.overall["RMS_level"].last, -20.6)
        self.assertAlmostEqual(result.overall["RMS_peak"].last, -18.4)
        self.assertIn("2", result.channels)
        self.assertAlmostEqual(result.channels["2"]["RMS_level"].last, -21.1)
        self.assertAlmostEqual(result.channels["2"]["RMS_peak"].last, -18.8)


class ParseEbur128OutputTests(unittest.TestCase):
    def test_parses_integrated_lufs_from_summary_lines(self) -> None:
        lines = [
            "[Parsed_ebur128_0 @ 0x0] Summary:",
            "  Integrated loudness:",
            "    I:         -16.7 LUFS",
            "    Threshold: -26.8 LUFS",
            "  Loudness range:",
            "    LRA:         1.2 LU",
        ]

        result = rms_scan.parse_ebur128_output(lines)

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.integrated_lufs, -16.7)


if __name__ == "__main__":
    unittest.main()
