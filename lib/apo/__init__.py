"""Everything that reads a vendor APO config shipped beside the DAX3 XML.

Some OEMs voice the speaker outside Dolby, in their own audio processing
object, with its own per-device config file. Microsoft's Surface APO is the
first such format read here. This package turns that file into an `ApoLayer`
the preset builder can stack on top of the Dolby chain.

Deliberately empty of code, like `lib/dax/__init__.py`. A re-export here would
drag every sibling in behind any single import and make cycles reachable
(`tests/test_layout.py`). Callers import the submodule they want by name
(`from lib.apo import discover`).
"""
