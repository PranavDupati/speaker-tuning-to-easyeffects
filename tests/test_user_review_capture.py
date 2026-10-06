"""tools/user_review_capture.py's staging layout for --beside."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import user_review_capture as urc  # noqa: E402


def test_bare_xml_stages_flat():
    xml = Path("/x/pkg/dax3extrtk/DEV_TEST.xml")
    assert urc._stage_entries(xml, []) == [("DEV_TEST.xml", xml)]


def test_beside_keeps_the_package_layout():
    """A run that looks next to the XML's own folder must find the extra
    folder there, so the XML moves down one level, under its real folder
    name."""
    xml = Path("/x/pkg/dax3extrtk/DEV_TEST.xml")
    apo = Path("/x/pkg/surfaceapoextension")
    assert urc._stage_entries(xml, [apo]) == [
        ("dax3extrtk/DEV_TEST.xml", xml), ("surfaceapoextension", apo)]
