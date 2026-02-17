#!/usr/bin/env python3
"""Offline RMS scanner based on FFmpeg astats."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence


EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_MISSING_BIN = 2
EXIT_PARSE_FAILURE = 3
EXIT_INVALID_INPUT = 4

TARGET_MIDPOINT_DBFS = -20.5
METRICS = ("RMS_level", "RMS_peak")

_DB_VALUE_RE = r"[-+]?(?:inf|\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
_KEYED_METRIC_RE = re.compile(
    rf"lavfi\.astats\.(?P<scope>Overall|(?:[A-Za-z]+\.)?\d+)\."
    rf"(?P<metric>RMS_level|RMS_peak)\s*[:=]\s*(?P<value>{_DB_VALUE_RE})",
    re.IGNORECASE,
)
_OVERALL_SUMMARY_RE = re.compile(
    rf"Overall\s+RMS\s+(?P<kind>level|peak)(?:\s+dB)?\s*:\s*(?P<value>{_DB_VALUE_RE})",
    re.IGNORECASE,
)
_CHANNEL_HEADER_RE = re.compile(r"Channel\s*:\s*(?P<channel>\d+)", re.IGNORECASE)
_CHANNEL_SUMMARY_RE = re.compile(
    rf"RMS\s+(?P<kind>level|peak)(?:\s+dB)?\s*:\s*(?P<value>{_DB_VALUE_RE})",
    re.IGNORECASE,
)


class RMSScanError(Exception):
    """Base class for CLI errors."""


class MissingBinaryError(RMSScanError):
    """Raised when ffmpeg/ffprobe is not available."""


class InvalidInputError(RMSScanError):
    """Raised when input media is invalid or unreadable."""


@dataclass
class MetricTrack:
    """Stores last-seen and max-observed values for a metric."""

    last: Optional[float] = None
    max_observed: Optional[float] = None

    def update(self, value: float) -> None:
        self.last = value
        if self.max_observed is None or value > self.max_observed:
            self.max_observed = value


def _new_metric_map() -> dict[str, MetricTrack]:
    return {metric: MetricTrack() for metric in METRICS}


@dataclass
class ParsedAstats:
    """Parsed RMS data from FFmpeg astats output."""

    overall: dict[str, MetricTrack] = field(default_factory=_new_metric_map)
    channels: dict[str, dict[str, MetricTrack]] = field(default_factory=dict)
    matched_lines: list[str] = field(default_factory=list)

    def selected_overall(self, metric: str) -> Optional[float]:
        track = self.overall.get(metric)
        if track is None:
            return None
        return track.last if track.last is not None else track.max_observed

    def selected_channel(self, channel: str, metric: str) -> Optional[float]:
        channel_metrics = self.channels.get(channel)
        if channel_metrics is None:
            return None
        track = channel_metrics.get(metric)
        if track is None:
            return None
        return track.last if track.last is not None else track.max_observed


@dataclass
class ProbeInfo:
    """Basic audio stream metadata extracted from ffprobe."""

    duration_seconds: Optional[float]
    sample_rate_hz: Optional[int]
    channels: Optional[int]
    channel_layout: Optional[str]


def parse_astats_output(lines: Iterable[str]) -> ParsedAstats:
    """Parse astats output, handling keyed metadata and summary-line formats."""

    result = ParsedAstats()
    current_channel: Optional[str] = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        matched_keyed_metric = False
        for match in _KEYED_METRIC_RE.finditer(line):
            metric = match.group("metric")
            parsed_value = _parse_db_value(match.group("value"))
            if parsed_value is None:
                continue

            scope_kind, channel_id = _normalize_scope(match.group("scope"))
            if scope_kind == "overall":
                result.overall[metric].update(parsed_value)
                matched_keyed_metric = True
            elif scope_kind == "channel" and channel_id is not None:
                channel_map = _get_or_create_channel(result, channel_id)
                channel_map[metric].update(parsed_value)
                matched_keyed_metric = True

        if matched_keyed_metric:
            result.matched_lines.append(line)
            continue

        header_match = _CHANNEL_HEADER_RE.search(line)
        if header_match:
            current_channel = str(int(header_match.group("channel")))
            result.matched_lines.append(line)
            continue

        overall_match = _OVERALL_SUMMARY_RE.search(line)
        if overall_match:
            metric = _summary_kind_to_metric(overall_match.group("kind"))
            parsed_value = _parse_db_value(overall_match.group("value"))
            if parsed_value is not None:
                result.overall[metric].update(parsed_value)
                result.matched_lines.append(line)
            continue

        channel_metric_match = _CHANNEL_SUMMARY_RE.search(line)
        if channel_metric_match and current_channel is not None:
            metric = _summary_kind_to_metric(channel_metric_match.group("kind"))
            parsed_value = _parse_db_value(channel_metric_match.group("value"))
            if parsed_value is not None:
                channel_map = _get_or_create_channel(result, current_channel)
                channel_map[metric].update(parsed_value)
                result.matched_lines.append(line)

    return result


def _normalize_scope(raw_scope: str) -> tuple[str, Optional[str]]:
    lower = raw_scope.lower()
    if lower == "overall":
        return "overall", None
    channel_match = re.search(r"(\d+)", raw_scope)
    if channel_match:
        return "channel", str(int(channel_match.group(1)))
    return "unknown", None


def _summary_kind_to_metric(kind: str) -> str:
    return "RMS_level" if kind.lower() == "level" else "RMS_peak"


def _get_or_create_channel(result: ParsedAstats, channel_id: str) -> dict[str, MetricTrack]:
    channel_map = result.channels.get(channel_id)
    if channel_map is None:
        channel_map = _new_metric_map()
        result.channels[channel_id] = channel_map
    return channel_map


def _parse_db_value(raw: str) -> Optional[float]:
    value = raw.strip().lower()
    if value in {"inf", "+inf"}:
        return float("inf")
    if value == "-inf":
        return float("-inf")
    try:
        return float(value)
    except ValueError:
        return None


def _resolve_binary(name: str, override: Optional[str]) -> str:
    candidate = override or name
    resolved = shutil.which(candidate)
    if resolved:
        return resolved

    path_candidate = Path(candidate).expanduser()
    if path_candidate.exists() and path_candidate.is_file() and os.access(path_candidate, os.X_OK):
        return str(path_candidate)

    install_hint = "brew install ffmpeg" if name in {"ffmpeg", "ffprobe"} else "Install required tool."
    raise MissingBinaryError(
        f"Unable to find `{name}`. Install FFmpeg (macOS: `{install_hint}`, Debian/Ubuntu: `sudo apt install ffmpeg`)."
    )


def _run_ffprobe(ffprobe_bin: str, input_path: Path) -> ProbeInfo:
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(input_path),
    ]
    try:
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise MissingBinaryError(f"`{ffprobe_bin}` is not executable.") from exc

    if completed.returncode != 0:
        raise InvalidInputError(
            f"ffprobe could not read input file.\nffprobe stderr: {completed.stderr.strip() or '(empty)'}"
        )

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise InvalidInputError("ffprobe returned invalid JSON output.") from exc

    streams = payload.get("streams", []) or []
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio_stream is None:
        raise InvalidInputError("No audio stream found in input file.")

    duration = _safe_float((payload.get("format") or {}).get("duration"))
    sample_rate = _safe_int(audio_stream.get("sample_rate"))
    channels = _safe_int(audio_stream.get("channels"))
    channel_layout = audio_stream.get("channel_layout")

    return ProbeInfo(
        duration_seconds=duration,
        sample_rate_hz=sample_rate,
        channels=channels,
        channel_layout=str(channel_layout) if channel_layout else None,
    )


def _run_ffmpeg_astats(ffmpeg_bin: str, input_path: Path) -> list[str]:
    analysis_filters = [
        "astats=metadata=1:reset=0:measure_overall=RMS_level+RMS_peak:measure_perchannel=RMS_level+RMS_peak,ametadata=print",
        "astats=metadata=1:reset=0,ametadata=print",
    ]
    last_lines: list[str] = []

    for index, filter_graph in enumerate(analysis_filters):
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-v",
            "info",
            "-nostats",
            "-i",
            str(input_path),
            "-vn",
            "-sn",
            "-dn",
            "-map",
            "0:a:0?",
            "-af",
            filter_graph,
            "-f",
            "null",
            "-",
        ]

        try:
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except FileNotFoundError as exc:
            raise MissingBinaryError(f"`{ffmpeg_bin}` is not executable.") from exc

        last_lines = _collect_output_lines(completed.stdout, completed.stderr)
        if completed.returncode == 0:
            return last_lines

        if index == 0 and _looks_like_measure_option_error(completed.stderr + "\n" + completed.stdout):
            continue

        return last_lines

    return last_lines


def _looks_like_measure_option_error(output: str) -> bool:
    lowered = output.lower()
    hints = (
        "measure_overall",
        "measure_perchannel",
        "option not found",
        "error setting option",
        "unable to parse option value",
    )
    return any(token in lowered for token in hints)


def _collect_output_lines(stdout: str, stderr: str) -> list[str]:
    lines: list[str] = []
    if stderr:
        lines.extend(stderr.splitlines())
    if stdout:
        lines.extend(stdout.splitlines())
    return lines


def _safe_float(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: object) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_within_range(value: float, min_dbfs: float, max_dbfs: float) -> bool:
    if not math.isfinite(value):
        return False
    return min_dbfs <= value <= max_dbfs


def _suggest_gain_change_db(value: Optional[float], target_dbfs: float) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return target_dbfs - value


def _format_db(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "-inf" if value < 0 else "inf"
    return f"{value:.2f}"


def _json_db(value: Optional[float]) -> Optional[float | str]:
    if value is None:
        return None
    if math.isinf(value):
        return "-inf" if value < 0 else "inf"
    return value


def _build_json_payload(
    input_path: Path,
    probe: ProbeInfo,
    parsed: ParsedAstats,
    min_dbfs: float,
    max_dbfs: float,
    passed: bool,
    suggested_gain: Optional[float],
) -> dict[str, object]:
    channel_payload: dict[str, dict[str, Optional[float | str]]] = {}
    for channel_id in sorted(parsed.channels, key=lambda c: int(c)):
        channel_payload[channel_id] = {
            "RMS_level_dbfs": _json_db(parsed.selected_channel(channel_id, "RMS_level")),
            "RMS_peak_dbfs": _json_db(parsed.selected_channel(channel_id, "RMS_peak")),
        }

    return {
        "file": str(input_path),
        "duration_seconds": probe.duration_seconds,
        "sample_rate_hz": probe.sample_rate_hz,
        "channels": probe.channels,
        "channel_layout": probe.channel_layout,
        "overall": {
            "RMS_level": {
                "last_dbfs": _json_db(parsed.overall["RMS_level"].last),
                "max_observed_dbfs": _json_db(parsed.overall["RMS_level"].max_observed),
                "selected_dbfs": _json_db(parsed.selected_overall("RMS_level")),
            },
            "RMS_peak": {
                "last_dbfs": _json_db(parsed.overall["RMS_peak"].last),
                "max_observed_dbfs": _json_db(parsed.overall["RMS_peak"].max_observed),
                "selected_dbfs": _json_db(parsed.selected_overall("RMS_peak")),
            },
        },
        "range": {"min_dbfs": min_dbfs, "max_dbfs": max_dbfs},
        "target_midpoint_dbfs": TARGET_MIDPOINT_DBFS,
        "pass": passed,
        "suggested_gain_change_db": suggested_gain,
        "details": {"per_channel": channel_payload},
    }


def _print_human_output(
    input_path: Path,
    probe: ProbeInfo,
    parsed: ParsedAstats,
    min_dbfs: float,
    max_dbfs: float,
    passed: bool,
    suggested_gain: Optional[float],
) -> None:
    overall_level = parsed.selected_overall("RMS_level")
    overall_peak = parsed.selected_overall("RMS_peak")

    print(f"File: {input_path}")
    if probe.duration_seconds is None:
        print("Duration: n/a")
    else:
        print(f"Duration: {probe.duration_seconds:.2f} s")

    audio_parts = []
    if probe.sample_rate_hz is not None:
        audio_parts.append(f"{probe.sample_rate_hz} Hz")
    if probe.channels is not None:
        audio_parts.append(f"{probe.channels} ch")
    if probe.channel_layout:
        audio_parts.append(probe.channel_layout)
    print(f"Audio: {', '.join(audio_parts) if audio_parts else 'n/a'}")

    print(f"Overall RMS_level: {_format_db(overall_level)} dBFS")
    print(f"Overall RMS_peak: {_format_db(overall_peak)} dBFS")
    print(f"Spec window: [{min_dbfs:.1f}, {max_dbfs:.1f}] dBFS")
    print(f"Result: {'PASS' if passed else 'FAIL'}")

    if not passed:
        if suggested_gain is None:
            print("Suggested gain change: n/a (RMS_level is not finite)")
        else:
            print(f"Suggested gain change: {suggested_gain:+.1f} dB")

    print("Details:")
    if not parsed.channels:
        print("  (no per-channel RMS metrics found)")
        return

    for channel_id in sorted(parsed.channels, key=lambda c: int(c)):
        channel_level = parsed.selected_channel(channel_id, "RMS_level")
        channel_peak = parsed.selected_channel(channel_id, "RMS_peak")
        print(f"  Channel {channel_id} RMS_level: {_format_db(channel_level)} dBFS")
        print(f"  Channel {channel_id} RMS_peak: {_format_db(channel_peak)} dBFS")


def _print_verbose_lines(parsed: ParsedAstats, all_lines: list[str], to_stderr: bool) -> None:
    stream = sys.stderr if to_stderr else sys.stdout
    selected_lines = parsed.matched_lines
    if not selected_lines:
        selected_lines = [line for line in all_lines if "astats" in line.lower()]

    print("Raw astats lines parsed:", file=stream)
    if not selected_lines:
        print("  (none found)", file=stream)
        return
    for line in selected_lines:
        print(f"  {line}", file=stream)


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="rms-scan",
        description="Analyze media with FFmpeg astats and report RMS stats.",
    )
    parser.add_argument("input_path", help="Path to audio or video file")
    parser.add_argument("--min", dest="min_dbfs", type=float, default=-23.0, help="Minimum RMS dBFS (default: -23)")
    parser.add_argument("--max", dest="max_dbfs", type=float, default=-18.0, help="Maximum RMS dBFS (default: -18)")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--verbose", action="store_true", help="Print raw astats lines that were parsed")
    parser.add_argument("--ffmpeg", default=None, help="Path to ffmpeg binary")
    parser.add_argument("--ffprobe", default=None, help="Path to ffprobe binary")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    input_path = Path(args.input_path).expanduser()
    if args.min_dbfs > args.max_dbfs:
        print("Error: --min must be <= --max.", file=sys.stderr)
        return EXIT_INVALID_INPUT
    if not input_path.exists() or not input_path.is_file():
        print(f"Error: input file does not exist or is not a file: {input_path}", file=sys.stderr)
        return EXIT_INVALID_INPUT

    try:
        ffmpeg_bin = _resolve_binary("ffmpeg", args.ffmpeg)
        ffprobe_bin = _resolve_binary("ffprobe", args.ffprobe)
    except MissingBinaryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_MISSING_BIN

    try:
        probe = _run_ffprobe(ffprobe_bin, input_path)
    except MissingBinaryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_MISSING_BIN
    except InvalidInputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_INVALID_INPUT

    try:
        raw_lines = _run_ffmpeg_astats(ffmpeg_bin, input_path)
    except MissingBinaryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_MISSING_BIN

    parsed = parse_astats_output(raw_lines)
    overall_level = parsed.selected_overall("RMS_level")
    overall_peak = parsed.selected_overall("RMS_peak")

    if args.verbose:
        _print_verbose_lines(parsed, raw_lines, to_stderr=args.json)

    if overall_level is None or overall_peak is None:
        print(
            "Error: could not parse required astats keys "
            "(lavfi.astats.Overall.RMS_level / RMS_peak).",
            file=sys.stderr,
        )
        print("Troubleshooting:", file=sys.stderr)
        print("  - Ensure ffmpeg includes astats and ametadata filters.", file=sys.stderr)
        print("  - Try a newer ffmpeg build (`brew install ffmpeg` or `sudo apt install ffmpeg`).", file=sys.stderr)
        print("  - Re-run with --verbose to inspect parsed lines.", file=sys.stderr)
        return EXIT_PARSE_FAILURE

    passed = _is_within_range(overall_level, args.min_dbfs, args.max_dbfs)
    suggested_gain = _suggest_gain_change_db(overall_level, TARGET_MIDPOINT_DBFS)

    if args.json:
        payload = _build_json_payload(
            input_path=input_path,
            probe=probe,
            parsed=parsed,
            min_dbfs=args.min_dbfs,
            max_dbfs=args.max_dbfs,
            passed=passed,
            suggested_gain=suggested_gain,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_human_output(
            input_path=input_path,
            probe=probe,
            parsed=parsed,
            min_dbfs=args.min_dbfs,
            max_dbfs=args.max_dbfs,
            passed=passed,
            suggested_gain=suggested_gain,
        )

    return EXIT_PASS if passed else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
