"""Vendor APO layers bound to real corpus XMLs.

Walks every discovered XML, not one per distinct file: a layer belongs to the
package beside the XML, so two byte-identical XMLs can differ in what binds
them. Each distinct (XML, config) pair runs once. Skips cleanly where no
vendor config sits beside any discovered XML, which is every corpus that
holds no Surface package.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from lib.apo import discover as apo_discover
from lib.apo import layer as apo_layer
from lib.dax.parse import DB_FIXED_POINT_SCALE, parse_xml
from lib.pipewire.conf import build_chain
from lib.preset import emit, fir
from lib.preset.build import make_preset
from lib.report import messages
from tests.corpus.test_corpus import DISCOVERED


def _beside_a_vendor_package(xmls: list[Path]) -> list[Path]:
    """The XMLs whose package sits next to a vendor APO package folder.

    One directory listing per package parent instead of a finder call per
    XML: collection runs in every xdist worker, over thousands of paths.
    """
    verdict: dict[Path, bool] = {}
    kept = []
    for xml in xmls:
        root = xml.parent.parent
        if root not in verdict:
            try:
                verdict[root] = any(
                    d.name.lower().startswith("surfaceapoextension")
                    for d in root.iterdir())
            except OSError:
                verdict[root] = False
        if verdict[root]:
            kept.append(xml)
    return kept


def _bound_pairs() -> list[tuple[Path, object]]:
    seen, pairs = set(), []
    for xml in _beside_a_vendor_package(DISCOVERED):
        try:
            layer = apo_discover.find_for_xml(xml)
        except apo_layer.UnsupportedApoConfig as exc:
            layer = exc
        if layer is None:
            continue
        cfg = getattr(layer, "config_path", None) or layer.path
        key = (hashlib.sha256(xml.read_bytes()).digest(),
               hashlib.sha256(Path(cfg).read_bytes()).digest()
               if Path(cfg).is_file() else str(cfg))
        if key not in seen:
            seen.add(key)
            pairs.append((xml, layer))
    return pairs


PAIRS = _bound_pairs()


@pytest.mark.skipif(not PAIRS, reason="no vendor APO config beside any "
                    "discovered XML")
@pytest.mark.parametrize("xml_path,layer", PAIRS,
                         ids=[p.name for p, _ in PAIRS])
def test_bound_layer_reads_folds_and_converts(xml_path, layer, tmp_path):
    assert not isinstance(layer, apo_layer.UnsupportedApoConfig), (
        f"a real config binds {xml_path.name} but can't be read: {layer}")
    tuning = parse_xml(xml_path)
    scale = tuning.ieq_amount / 100.0
    ao = np.array(tuning.ao_left) / DB_FIXED_POINT_SCALE
    fft_freqs = np.fft.rfftfreq(fir.FIR_LENGTH, d=1.0 / fir.SAMPLE_RATE)
    for key in messages.VOICING_CURVES.values():
        if key not in tuning.curves:
            continue
        combined = (np.array(tuning.curves[key]) / DB_FIXED_POINT_SCALE
                    * scale + ao)
        taps, _ = fir.make_fir(tuning.freqs, combined,
                               eq_sections=layer.eq_left)
        worst = emit._worst_shape_error(taps, combined, 0.0, tuning.freqs,
                                        fft_freqs, rows=False,
                                        eq_sections=layer.eq_left)
        assert worst <= emit.FIR_VERIFY_OK_DB, (
            f"{xml_path.name} {key}: fold misses its target by {worst:.2f} dB")
    preset, emitted = make_preset(
        "Corpus", tuning.peq_filters, tuning.vol_leveler,
        tuning.dialog_enhancer, tuning.mb_comp, tuning.regulator,
        tuning.freqs, volmax_boost=tuning.volmax_boost,
        enabled={apo_layer.FLAG}, apo=layer)
    assert f"{apo_layer.FLAG}-active" in emitted
    (tmp_path / "Corpus.irs").write_bytes(b"")
    build_chain(preset, tmp_path)  # raises on a misplaced stage
