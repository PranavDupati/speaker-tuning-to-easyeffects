"""The analytical EE-chain model reads the channel it is asked for.

`tools/measure_ee/compare_ee_analytical.py` once modelled every channel
with the .irs left column, and modelled a flat EQ when handed "L" instead
of "left". Both read as plausible curves, so nothing failed.
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.io import wavfile

from tools.measure_ee.compare_ee_analytical import (
    build_analytical_model, model_total_db,
)

FREQS = np.array([100.0, 1000.0, 5000.0])


def _preset(left_gain_db: float) -> dict:
    band = {"type": "Bell", "frequency": 1000.0, "q": 1.0,
            "gain": left_gain_db, "slope": "x1", "mode": "RLC (BT)"}
    flat = dict(band, gain=0.0)
    return {"output": {
        "plugins_order": ["convolver#0", "equalizer#0"],
        "convolver#0": {"input-gain": 0.0, "output-gain": 0.0},
        "equalizer#0": {"num-bands": 1, "input-gain": 0.0,
                        "output-gain": 0.0,
                        "left": {"band0": band}, "right": {"band0": flat}},
    }}


@pytest.fixture
def irs(tmp_path):
    """Unit impulse on L, the same at −6 dB on R."""
    taps = np.zeros((256, 2), np.float32)
    taps[0] = [1.0, 0.5]
    p = tmp_path / "k.irs"
    wavfile.write(str(p), 48000, taps)
    return p


@pytest.mark.parametrize("name", ["L", "l", "left"])
def test_left_aliases_agree(irs, name):
    model = build_analytical_model(_preset(6.0), irs, name, FREQS)
    assert model.convolver_db == pytest.approx([0.0] * 3, abs=1e-6)
    assert model_total_db(model)[1] == pytest.approx(6.0, abs=0.05)


@pytest.mark.parametrize("name", ["R", "right"])
def test_right_reads_its_own_fir_column_and_bands(irs, name):
    model = build_analytical_model(_preset(6.0), irs, name, FREQS)
    assert model.convolver_db == pytest.approx(
        [20 * np.log10(0.5)] * 3, abs=1e-6)
    assert model.eqs_db[0] == pytest.approx([0.0] * 3, abs=1e-6)


def test_unknown_channel_is_an_error(irs):
    with pytest.raises(ValueError, match="channel"):
        build_analytical_model(_preset(6.0), irs, "FL", FREQS)
