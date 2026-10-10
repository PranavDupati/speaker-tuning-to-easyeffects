"""Where the checkout itself is, for the files that ship alongside the code.

A path relative to the repo rather than to the user's home has to resolve from
``__file__``, and the entry points can afford
``Path(__file__).resolve().parent`` only because they sit at the root — a
module one directory deeper needs ``parent.parent``, two deeper needs three
hops, and each of those is a silent breakage waiting for the next move.

So the walk-up is written once, here, and everything else asks. Stdlib-only,
like the rest of what ``ee_to_pipewire.py`` imports at startup.

One caller: ``lib/driver_package.py``, whose default download cache is the
checkout's ``driver-cache/``. The scripts under ``tools/`` each walk up from
their own depth instead, because they also insert the result into
``sys.path`` before any ``lib`` import can happen: docs/code-organisation.md
"The rules that keep provenance".

It is the checkout a user cloned or unzipped, the only place the files that
ship with the code exist. Nothing is installed, so there is no prefix to
search and no fallback to make. Its one data use is the download cache,
``driver-cache/`` (gitignored): kept in the checkout rather than under
``$XDG_CACHE_HOME`` so that a bare run from the project root finds what an
earlier run unpacked, through the same probe that finds a hand-unpacked
download there.
"""

from pathlib import Path

__all__ = ["REPO_ROOT"]

# lib/paths.py -> lib/ -> the checkout root.
REPO_ROOT = Path(__file__).resolve().parent.parent
