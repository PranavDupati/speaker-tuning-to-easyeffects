"""Find the vendor APO layer, if any, that belongs beside a DAX3 XML.

One finder per supported format, tried in order; the first that binds the
device wins. The generator calls `find_for_xml` once it has picked the XML,
before its deferred DSP imports, so this stays stdlib-only.
"""

from __future__ import annotations

from pathlib import Path

from lib.apo import surface
from lib.apo.layer import ApoLayer

FINDERS = (surface.find,)


def find_for_xml(xml_path: Path) -> ApoLayer | None:
    """The first vendor APO layer bound to *xml_path*'s device, or None.

    A finder's `UnsupportedApoConfig` propagates: a config that binds this
    device but can't be read is worth telling the user about.
    """
    for finder in FINDERS:
        layer = finder(Path(xml_path))
        if layer is not None:
            return layer
    return None
