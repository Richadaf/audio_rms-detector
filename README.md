# rms-scan

[![CI](https://github.com/richadaf/RFX-core/actions/workflows/ci.yml/badge.svg)](https://github.com/richadaf/RFX-core/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/rms-scan.svg)](https://pypi.org/project/rms-scan/)

`rms-scan` is a small offline CLI that analyzes a media file with FFmpeg `astats` and reports:

- Overall/average RMS: `lavfi.astats.Overall.RMS_level` (dBFS)
- Highest RMS window: `lavfi.astats.Overall.RMS_peak` (dBFS)
- Pass/fail against a configurable RMS window (default: `[-23, -18]` dBFS)

It also prints per-channel RMS details when available.

## Why / use cases

- Broadcast/podcast loudness spot-checks (quick RMS window compliance checks)
- Batch validation of large media libraries
- CI gating (fail builds when audio is consistently too quiet/loud)

## Requirements

- Python 3.10+
- FFmpeg + FFprobe in `PATH` (or pass explicit paths via `--ffmpeg` / `--ffprobe`)

Install FFmpeg:

- macOS (Homebrew): `brew install ffmpeg`
- Debian/Ubuntu: `sudo apt install ffmpeg`

## Install

From project root:

```bash
pip install .
```

After install, the console script is available as:

```bash
rms-scan --help
```

You can also run directly:

```bash
python3 rms_scan.py --help
```

## Usage

```bash
rms-scan <path-to-audio-or-video-file>
```

Options:

- `--min` minimum RMS threshold in dBFS (default: `-23`)
- `--max` maximum RMS threshold in dBFS (default: `-18`)
- `--json` machine-readable output
- `--verbose` print raw astats lines being parsed
- `--ffmpeg <path>` override ffmpeg binary
- `--ffprobe <path>` override ffprobe binary

## Example (human output)

```text
$ rms-scan program.wav
File: program.wav
Duration: 3728.41 s
Audio: 48000 Hz, 2 ch, stereo
Overall RMS_level: -21.34 dBFS
Overall RMS_peak: -18.62 dBFS
Spec window: [-23.0, -18.0] dBFS
Result: PASS
Details:
  Channel 1 RMS_level: -21.20 dBFS
  Channel 1 RMS_peak: -18.55 dBFS
  Channel 2 RMS_level: -21.48 dBFS
  Channel 2 RMS_peak: -18.70 dBFS
```

## Example (FAIL + suggested gain)

```text
$ rms-scan quiet_mix.mp3
File: quiet_mix.mp3
Duration: 912.03 s
Audio: 44100 Hz, 2 ch, stereo
Overall RMS_level: -24.10 dBFS
Overall RMS_peak: -20.80 dBFS
Spec window: [-23.0, -18.0] dBFS
Result: FAIL
Suggested gain change: +3.6 dB
Details:
  Channel 1 RMS_level: -24.05 dBFS
  Channel 1 RMS_peak: -20.77 dBFS
  Channel 2 RMS_level: -24.15 dBFS
  Channel 2 RMS_peak: -20.84 dBFS
```

`Suggested gain change` targets midpoint `-20.5 dBFS` using:

```text
target - measured_rms_level
```

## Example (`--json`)

```json
{
  "channel_layout": "stereo",
  "channels": 2,
  "details": {
    "per_channel": {
      "1": {
        "RMS_level_dbfs": -21.2,
        "RMS_peak_dbfs": -18.55
      },
      "2": {
        "RMS_level_dbfs": -21.48,
        "RMS_peak_dbfs": -18.7
      }
    }
  },
  "duration_seconds": 3728.41,
  "file": "program.wav",
  "overall": {
    "RMS_level": {
      "last_dbfs": -21.34,
      "max_observed_dbfs": -21.34,
      "selected_dbfs": -21.34
    },
    "RMS_peak": {
      "last_dbfs": -18.62,
      "max_observed_dbfs": -18.62,
      "selected_dbfs": -18.62
    }
  },
  "pass": true,
  "range": {
    "max_dbfs": -18.0,
    "min_dbfs": -23.0
  },
  "sample_rate_hz": 48000,
  "suggested_gain_change_db": -0.84,
  "target_midpoint_dbfs": -20.5
}
```

## Exit Codes

- `0`: PASS (Overall RMS_level in range)
- `1`: FAIL (outside range)
- `2`: ffmpeg/ffprobe missing
- `3`: astats parse failure
- `4`: invalid input file

## FFmpeg Analysis Method

The tool uses FFprobe first for metadata and then analyzes audio without rendering output:

- disables non-audio streams: `-vn -sn -dn`
- null muxer output: `-f null -`
- astats filter with metadata keys:
  - preferred: `astats=metadata=1:reset=0:measure_overall=RMS_level+RMS_peak:measure_perchannel=RMS_level+RMS_peak,ametadata=print`
  - fallback for older builds: `astats=metadata=1:reset=0,ametadata=print`

This processes as fast as decode/filter speed allows (typically faster-than-real-time on modern machines).

## Troubleshooting

### `Error: Unable to find ffmpeg/ffprobe`

- Install FFmpeg package (`brew install ffmpeg` or `sudo apt install ffmpeg`)
- Or pass binary paths with `--ffmpeg` and `--ffprobe`

### Parse failure (exit code `3`)

If `lavfi.astats.Overall.RMS_level` / `RMS_peak` are not found:

- Re-run with `--verbose` to inspect parsed lines
- Check filter availability:
  - `ffmpeg -filters | grep astats`
  - `ffmpeg -filters | grep ametadata`
- Try a newer FFmpeg build (some builds vary in filter/metadata behavior)

### Input fails validation (exit code `4`)

- Verify file exists and contains an audio stream
- Confirm ffprobe can read it:
  - `ffprobe -v error -show_streams -show_format <file>`

## Development

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -p "test_*.py"
python -m build
python -m twine check dist/* 
```
