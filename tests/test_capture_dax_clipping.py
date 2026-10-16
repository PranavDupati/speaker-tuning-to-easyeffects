"""capture_dax's clipping advice: only tell the user to lower the volume when
the volume is actually in the loopback.

On the dev X1 Yoga the endpoint volume never reaches the WASAPI loopback
(Dolby off reads 0.0 dB at 20 % and 50 %), so "lower system volume" sent a
capture session through four volume settings that all clipped identically.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "tools" / "measure_dax"))
import capture_dax as cd  # noqa: E402


def test_no_clipping_no_advice():
    assert cd.clipping_advice(0, 0.0, -10.5) is None


def test_volume_not_in_loopback_says_lowering_wont_help():
    msg = cd.clipping_advice(12, 0.02, -10.5)
    assert "won't change this" in msg
    assert "lower system volume" not in msg


def test_volume_in_loopback_keeps_the_lower_volume_advice():
    assert "lower system volume" in cd.clipping_advice(12, -10.0, -10.5)


def test_unknown_baseline_explains_how_to_tell():
    msg = cd.clipping_advice(12, None, -10.5)
    assert "Capture the off profile first" in msg


def test_full_volume_cannot_tell_the_cases_apart():
    # At 100 % a 0 dB off gain says nothing about where the volume sits.
    assert "Capture the off profile first" in cd.clipping_advice(12, 0.0, 0.0)


def test_off_gain_round_trips_through_the_sidecar(tmp_path, capsys):
    rng = np.random.default_rng(0)
    stim = (0.5 * rng.standard_normal((4800, 2))).clip(-0.9, 0.9)
    stim_peak_db = 20 * np.log10(np.max(np.abs(stim)))
    off = cd.post_capture_checks("off", "pink", "pink", stim, 48000, tmp_path,
                                 False, stim_peak_db=stim_peak_db,
                                 endpoint_db=-10.5)
    assert abs(off["loopback_gain_db"]) < 1e-6
    wavfile.write(str(tmp_path / "loopback_pink_off.wav"), 48000,
                  stim.astype(np.float32))
    (tmp_path / "loopback_pink_off.json").write_text(
        json.dumps({"capture": off}))
    clipped = stim.copy()
    clipped[:3] = 1.0
    cd.post_capture_checks("movie", "pink", "pink", clipped, 48000, tmp_path,
                           False, endpoint_db=-10.5)
    captured = capsys.readouterr()
    assert "won't change this" in captured.out + captured.err
