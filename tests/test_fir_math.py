"""FIR generator and curve interpolation.

`make_fir` is the load-bearing DSP step: it turns a target dB curve into
a minimum-phase impulse response via cepstral processing. The naive
inverse-FFT-of-magnitude approach produces a linear-phase FIR with
audible pre-ringing, so the cepstral path here must be preserved.
"""

import numpy as np
import pytest

from lib.preset.fir import (
    FIR_LENGTH,
    FIR_TARGET_RANGE_DB,
    SAMPLE_RATE,
    biquad_cascade_db,
    design_target_db,
    interpolate_curve_db,
    make_fir,
)
from tests.conftest import (
    SYNTHETIC_FREQS_20,
    fir_freq_response_db,
    is_minimum_phase,
    rbj_bell,
    synthetic_surface_eq,
)


# --- interpolate_curve_db ---

def test_interpolate_flat_input_flat_output():
    band_freqs = np.array(SYNTHETIC_FREQS_20, dtype=float)
    gains = np.full_like(band_freqs, 3.5)
    out = interpolate_curve_db(band_freqs, gains, np.array([100.0, 1000.0, 8000.0]))
    np.testing.assert_allclose(out, 3.5)


def test_interpolate_extrapolates_flat_at_edges():
    band_freqs = np.array([100.0, 1000.0, 10000.0])
    gains = np.array([-3.0, 0.0, 6.0])
    # Below lowest band → first gain; above highest band → last gain
    below = interpolate_curve_db(band_freqs, gains, np.array([10.0]))[0]
    above = interpolate_curve_db(band_freqs, gains, np.array([20000.0]))[0]
    assert below == pytest.approx(-3.0)
    assert above == pytest.approx(6.0)


def test_interpolate_monotone_input_monotone_output():
    band_freqs = np.array(SYNTHETIC_FREQS_20, dtype=float)
    gains = np.linspace(-6.0, 6.0, len(band_freqs))
    fft_freqs = np.geomspace(50, 16000, 64)
    out = interpolate_curve_db(band_freqs, gains, fft_freqs)
    diffs = np.diff(out)
    assert (diffs >= -1e-9).all()


def test_interpolate_log_frequency_spacing():
    """Interpolation is in log-frequency: the geometric mean of two
    adjacent bands should sit at the arithmetic mean of their dB values.
    """
    band_freqs = np.array([1000.0, 2000.0])
    gains = np.array([0.0, 6.0])
    mid = np.sqrt(1000.0 * 2000.0)
    out = interpolate_curve_db(band_freqs, gains, np.array([mid]))[0]
    assert out == pytest.approx(3.0, abs=1e-6)


# --- make_fir ---

def test_make_fir_length_and_dtype():
    fir, peak_db = make_fir(SYNTHETIC_FREQS_20, [0.0] * 20)
    assert fir.shape == (FIR_LENGTH,)
    assert np.isfinite(fir).all()
    assert np.isfinite(peak_db)


def test_make_fir_flat_curve_is_unit_impulse():
    """A 0 dB target everywhere yields an impulse at n=0 with no other
    energy and a flat (0 dB) frequency response.
    """
    fir, peak_db = make_fir(SYNTHETIC_FREQS_20, [0.0] * 20)
    # impulse should sit at sample 0
    assert np.argmax(np.abs(fir)) == 0
    assert fir[0] == pytest.approx(1.0, abs=1e-3)
    other_energy = np.sum(np.abs(fir[1:]))
    assert other_energy < 1e-3
    assert peak_db == pytest.approx(0.0, abs=1e-6)


def test_make_fir_normalises_peak_to_zero_db():
    """Peak normalisation (when enabled) should bring the FFT magnitude
    peak to ~0 dBFS regardless of the target curve's level.
    """
    gains = [0, 0, 0, 0, 6, 8, 6, 4, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    fir, _peak_db = make_fir(SYNTHETIC_FREQS_20, gains)
    _freqs, mag_db = fir_freq_response_db(fir, fs=SAMPLE_RATE)
    assert mag_db.max() == pytest.approx(0.0, abs=0.05)


def test_make_fir_non_normalised_preserves_target_level():
    """With normalize=False the unity-curve FIR is still the impulse
    (level preserved), and a +6 dB target peak shows up as +6 dB.
    """
    fir_flat, peak_flat = make_fir(SYNTHETIC_FREQS_20, [0.0] * 20, normalize=False)
    assert peak_flat == pytest.approx(0.0, abs=1e-6)
    assert fir_flat[0] == pytest.approx(1.0, abs=1e-3)

    gains = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6, 6, 0, 0, 0, 0, 0, 0, 0, 0]
    _fir, peak_db = make_fir(SYNTHETIC_FREQS_20, gains, normalize=False)
    assert peak_db == pytest.approx(6.0, abs=0.5)


def test_make_fir_response_tracks_target_curve():
    """Sample the synthesised FIR at the band centres and check the
    magnitude lines up with the target dB values (after peak normalisation
    subtracts a constant offset).
    """
    gains = [0, 0, -3, -3, 0, 0, 4, 6, 4, 0, 0, -2, -2, 0, 0, 0, 0, 0, 0, 0]
    fir, _peak_db = make_fir(SYNTHETIC_FREQS_20, gains)
    freqs, mag_db = fir_freq_response_db(fir, fs=SAMPLE_RATE)

    # Peak-normalised, so subtract 0 dB peak (which is the max of `gains`).
    target_offset = max(gains)
    for f, expected in zip(SYNTHETIC_FREQS_20, gains):
        if f >= SAMPLE_RATE / 2:
            continue
        idx = int(np.argmin(np.abs(freqs - f)))
        # Interp on a log axis is inexact at the edges; tolerate ~1.5 dB.
        assert mag_db[idx] == pytest.approx(expected - target_offset, abs=1.5), \
            f"FIR magnitude at {f} Hz: got {mag_db[idx]:.2f} dB, expected {expected - target_offset:.2f} dB"


@pytest.mark.parametrize("gains", [
    [0.0] * 20,
    [0, 0, 4, 4, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],   # bass lift
    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6, 8, 6, 0, 0],   # treble lift
    [0, 0, -3, -2, 0, 2, 4, 2, 0, -2, -1, 0, 1, 2, 3, 2, 0, 0, 0, 0],
])
def test_make_fir_is_minimum_phase(gains):
    """Cepstral construction must produce a minimum-phase IR (causal
    cepstrum). A naive iFFT of the magnitude would fail this — that's
    the whole point of the cepstral processing.
    """
    fir, _ = make_fir(SYNTHETIC_FREQS_20, gains)
    assert is_minimum_phase(fir, tol=1e-3), \
        "make_fir produced a non-minimum-phase IR — has the cepstral processing been simplified out?"


# --- helper validation ---

def test_is_minimum_phase_helper_rejects_linear_phase_fir():
    """Sanity-check on the cepstral-causality helper used above: a
    deliberately linear-phase FIR (naive iFFT of a magnitude curve)
    must *not* pass is_minimum_phase. If this regresses, the rest of
    the minimum-phase tests are vacuously true.
    """
    fft_freqs = np.fft.rfftfreq(4096, d=1.0 / 48000)
    target = np.full_like(fft_freqs, 1.0, dtype=float)
    target[100:200] = 2.0  # arbitrary bump on a flat magnitude
    spectrum = target.astype(complex)
    fir_naive = np.fft.fftshift(np.fft.irfft(spectrum, n=4096))
    assert not is_minimum_phase(fir_naive, tol=1e-3)


# --- inter-band ripple ---

def test_make_fir_interband_ripple_bounded():
    """The realized FIR magnitude must track the linear-in-dB target
    *between* the 20 band centers, not just at them — the symptom half
    of the IEQ bell-stacking trap (a PEQ-bell realisation of the same
    curve leaves 10-16 dB inter-band ripple; the cepstral FIR must not).
    Evaluated on a zero-padded 65536-point grid so behavior between the
    4096-point design bins is visible.
    """
    gains = [0.0, -10.0, 19.0, -16.0, 8.0, -10.0, 14.0, -3.0, 6.0, -12.0,
             10.0, -8.0, 12.0, -15.0, 5.0, -7.0, 9.0, -20.0, 4.0, -26.0]
    fir, _ = make_fir(SYNTHETIC_FREQS_20, gains, normalize=False)
    n_fft = 65536
    mag_db = 20.0 * np.log10(
        np.maximum(np.abs(np.fft.rfft(fir, n=n_fft)), 1e-12))
    f = np.fft.rfftfreq(n_fft, d=1.0 / SAMPLE_RATE)
    target = interpolate_curve_db(
        np.array(SYNTHETIC_FREQS_20, dtype=float),
        np.array(gains, dtype=float), f)
    # Two regimes: below ~500 Hz the 11.7 Hz design-bin spacing is sparse
    # relative to the band intervals, so a deliberately extreme curve
    # (±19 dB adjacent-band swings) measures up to ~2.4 dB between bins;
    # above 500 Hz the bins are dense and the construction tracks within
    # a fraction of a dB. The bounds below leave headroom over those
    # measured values while staying far under the 10–16 dB failure mode.
    lo_band = (f >= SYNTHETIC_FREQS_20[0]) & (f < 500.0)
    hi_band = (f >= 500.0) & (f <= SYNTHETIC_FREQS_20[-1])
    err_lo = np.abs(mag_db[lo_band] - target[lo_band])
    err_hi = np.abs(mag_db[hi_band] - target[hi_band])
    assert float(np.max(err_lo)) < 4.0, (
        f"LF inter-band ripple {np.max(err_lo):.2f} dB at "
        f"{f[lo_band][np.argmax(err_lo)]:.0f} Hz")
    assert float(np.max(err_hi)) < 1.0, (
        f"HF inter-band ripple {np.max(err_hi):.2f} dB at "
        f"{f[hi_band][np.argmax(err_hi)]:.0f} Hz")


# --- vendor APO EQ fold ---

def _hpf4_and_bells():
    """A 4th-order 50 Hz high-pass plus a dip and a lift: the shape of a
    laptop speaker correction, with values invented for the test."""
    from scipy.signal import butter
    sos = butter(4, 50.0, btype="highpass", fs=SAMPLE_RATE, output="sos")
    sections = [tuple(float(x) for x in (*s[:3], *s[4:])) for s in sos]
    for f0, g, q in ((3500.0, -9.0, 1.0), (11000.0, 4.0, 0.7)):
        b, a = rbj_bell(f0, g, q)
        sections.append(tuple(float(x) for x in (*b, *a[1:])))
    return sections


def test_biquad_cascade_db_matches_scipy():
    from scipy.signal import sosfreqz
    sections = _hpf4_and_bells()
    f = np.geomspace(20, 20000, 200)
    sos = np.array([[b0, b1, b2, 1.0, a1, a2]
                    for b0, b1, b2, a1, a2 in sections])
    _, h = sosfreqz(sos, worN=f, fs=SAMPLE_RATE)
    np.testing.assert_allclose(biquad_cascade_db(sections, f),
                               20 * np.log10(np.abs(h)), atol=1e-6)


def test_make_fir_without_eq_sections_is_bit_identical():
    gains = [0, 0, -3, -2, 0, 2, 4, 2, 0, -2, -1, 0, 1, 2, 3, 2, 0, 0, 0, 0]
    plain, peak = make_fir(SYNTHETIC_FREQS_20, gains)
    folded, folded_peak = make_fir(SYNTHETIC_FREQS_20, gains, eq_sections=())
    assert np.array_equal(plain, folded) and peak == folded_peak


@pytest.mark.parametrize("sections", [_hpf4_and_bells(),
                                      synthetic_surface_eq()])
def test_folded_eq_tracks_its_target_between_bins(sections):
    """The fold's whole risk is between the 11.7 Hz design bins, where a
    high-pass's DC null aliases. Graded on a dense grid from 20 Hz, shape
    only, against the floored target the design asks for."""
    gains = [0.5] * 20
    fir, _ = make_fir(SYNTHETIC_FREQS_20, gains, eq_sections=sections)
    n_fft = 8 * FIR_LENGTH
    f = np.fft.rfftfreq(n_fft, d=1.0 / SAMPLE_RATE)
    band = (f >= 20.0) & (f <= 20000.0)
    got = 20 * np.log10(np.abs(np.fft.rfft(fir, n=n_fft))[band] + 1e-12)
    want = design_target_db(SYNTHETIC_FREQS_20, gains, f[band], sections)
    err = np.abs((got - got.max()) - (want - want.max()))
    assert err.max() < 0.5, f"{err.max():.2f} dB at {f[band][err.argmax()]:.0f} Hz"


def test_folded_target_floor_sits_range_below_its_peak():
    sections = _hpf4_and_bells()
    f = np.geomspace(1, 24000, 4000)
    t = design_target_db(SYNTHETIC_FREQS_20, [0.0] * 20, f, sections)
    assert t.min() == pytest.approx(t.max() - FIR_TARGET_RANGE_DB, abs=0.1)


def test_folded_target_floor_does_not_depend_on_the_grid():
    """The correction check grades on band and dense grids; it must see the
    floor the filter was designed with, set by the peak over the FFT bins,
    even when its own grid misses that peak (a boost above 20 kHz here)."""
    b, a = rbj_bell(21000.0, 9.0, 2.0)
    sections = _hpf4_and_bells() + [tuple(float(x) for x in (*b, *a[1:]))]
    sub = np.array([20.0, 30.0, 1000.0, 10000.0])
    bins = np.fft.rfftfreq(FIR_LENGTH, d=1.0 / SAMPLE_RATE)
    alone = design_target_db(SYNTHETIC_FREQS_20, [0.0] * 20, sub, sections)
    with_peak = design_target_db(SYNTHETIC_FREQS_20, [0.0] * 20,
                                 np.concatenate([sub, bins]), sections)
    np.testing.assert_allclose(alone, with_peak[:len(sub)], atol=1e-9)


def test_folded_fir_stays_minimum_phase():
    fir, _ = make_fir(SYNTHETIC_FREQS_20, [0.0] * 20,
                      eq_sections=_hpf4_and_bells())
    assert is_minimum_phase(fir, tol=1e-3)
