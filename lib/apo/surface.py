"""Microsoft Surface APO: find the config bound to this device and read it.

The Surface Pro 9 (Intel) package ships its speaker voicing in
`SurfaceAPO_1284.json`, not in the DAX3 XML beside it. That XML switches off its
audio-optimizer, IEQ and graphic EQ in every profile (#113). Other Surface models
are unread. `SurfaceAPOExtension.inf` binds the JSON to one HD-Audio
hardware ID. The speaker's Realtek driver runs it as the mode and endpoint
effects (MFX, EFX), after Dolby's stream effect (SFX). Research `r-surface-apo-efx` holds the evidence and
the open questions for each mapping below.

The binding is read from the `.inf`: hardware ID → install section → AddReg
→ the config-filename property. The `_<id>` suffix of the JSON's name is a
Microsoft naming habit, not the binding, so it is never matched on.

Only the `R/EFX` chain is read:

- `MainEQ` is a biquad cascade. It becomes the layer's EQ, which
  `lib/preset/` folds into the FIR.
- `VolumeDepMBDRC4` becomes a multiband compressor, at volume state 0.
- `Crystal` becomes a multiband limiter, with one band per resonance.
- The `VolumeDep` shelves, hold times, per-band output limits and the
  `OutputLimiter` are not reproduced. They are listed in `ApoLayer.notes`.

Stdlib-only, like `lib/apo/layer.py`.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from lib.apo.layer import (ApoLayer, BandDynamics, Biquad, DynBand,
                           UnsupportedApoConfig)

LABEL = "Microsoft Surface APO"

# The device-property key SurfaceAPOExtension.inf stores the config filename
# under (`PKEY_SurfaceApoConfigFilename` in its [Strings]).
_CONFIG_FILENAME_PKEY = "{c1f75c4c-3243-11ea-850d-2e728ce88125},0"

# The HD-Audio function the DAX3 XML is named after, e.g.
# DEV_0274_SUBSYS_10EC1284_PCI_SUBSYS_72708086.xml → ("0274", "10EC1284").
_XML_DEV_SUBSYS_RE = re.compile(r"DEV_([0-9A-F]{4})_SUBSYS_([0-9A-F]{8})",
                                re.IGNORECASE)

_PACKAGE_DIR_PREFIX = "surfaceapoextension"


def find(xml_path: Path) -> ApoLayer | None:
    """The Surface APO layer bound to the device *xml_path* tunes, if any.

    Looks beside the XML's own package: in an extracted MSI that is the
    sibling `surfaceapoextension/` folder, and in a Windows DriverStore it is
    the sibling `surfaceapoextension.inf_<arch>_<hash>/`. Returns None when
    no `.inf` there binds this device. Raises `UnsupportedApoConfig` when
    one does, but its config can't be read.
    """
    m = _XML_DEV_SUBSYS_RE.search(Path(xml_path).name)
    if not m:
        return None  # SoundWire and Apple tunings carry no HD-Audio DEV id
    dev, subsys = m.group(1).upper(), m.group(2).upper()

    bound = []
    for inf in _candidate_infs(Path(xml_path)):
        hit = _bound_config(inf, dev, subsys)
        if hit:
            bound.append((_driver_version(inf), inf) + hit)
    if not bound:
        return None
    _, inf, hwid, config_name = max(bound, key=lambda b: b[0])
    config = inf.parent / config_name
    if not config.is_file():
        raise UnsupportedApoConfig(config, f"{inf.name} binds it, but the "
                                   "file is missing")
    return parse_config(config, inf, hwid)


def _candidate_infs(xml_path: Path) -> list[Path]:
    """Surface APO `.inf` files in the folders beside the XML's package."""
    dirs = [xml_path.parent]
    for root in {xml_path.parent, xml_path.parent.parent}:
        try:
            dirs += [d for d in root.iterdir() if d.is_dir() and
                     d.name.lower().startswith(_PACKAGE_DIR_PREFIX)]
        except OSError:
            continue
    infs = []
    for d in dict.fromkeys(dirs):
        try:
            infs += [f for f in d.iterdir() if f.suffix.lower() == ".inf" and
                     f.name.lower().startswith(_PACKAGE_DIR_PREFIX)]
        except OSError:
            continue
    return sorted(infs)


# --- .inf reading -----------------------------------------------------------

def _read_inf(path: Path) -> dict[str, list[str]]:
    """An .inf as {lowercased section name: its lines}, comments stripped.

    Windows writes .inf files in UTF-16 as often as in ANSI, so the encoding
    follows the byte-order mark. `[Strings]` tokens (`%NAME%`) are
    substituted throughout.
    """
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        line = _strip_comment(line).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1].strip().lower(), [])
        elif current is not None:
            current.append(line)
    strings = {}
    for line in sections.get("strings", []):
        key, sep, value = line.partition("=")
        if sep:
            strings[key.strip().lower()] = value.strip().strip('"')

    def subst(line: str) -> str:
        # A substituted value keeps its quotes when it holds a comma, as the
        # PKEY names do ("{guid},0"), so `_fields` still splits it as one.
        def value(m):
            v = strings.get(m.group(1).lower())
            if v is None:
                return m.group(0)
            return f'"{v}"' if "," in v else v
        return re.sub(r"%([^%]+)%", value, line)
    return {name: [subst(l) for l in lines] for name, lines in sections.items()}


def _strip_comment(line: str) -> str:
    """The line up to its first `;` outside double quotes."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ";" and not quoted:
            return line[:i]
    return line


def _fields(value: str) -> list[str]:
    """Comma-separated .inf fields, unquoted; a quoted comma stays inside."""
    fields, current, quoted = [], [], False
    for ch in value:
        if ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            fields.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    fields.append("".join(current).strip())
    return fields


def _bound_config(inf: Path, dev: str, subsys: str
                  ) -> tuple[str, str] | None:
    """(hardware ID, config filename) when *inf* binds DEV/SUBSYS, else None.

    Walks [Manufacturer] → model sections → the install section named for a
    hardware ID that carries `DEV_<dev>&SUBSYS_<subsys>` → its AddReg
    sections → the HKR line setting the config-filename property.
    """
    sections = _read_inf(inf)
    want = f"dev_{dev}&subsys_{subsys}".lower()
    for line in sections.get("manufacturer", []):
        _, _, value = line.partition("=")
        base, *decorations = _fields(value)
        for model in [base] + [f"{base}.{d}" for d in decorations]:
            for entry in sections.get(model.lower(), []):
                _, _, rhs = entry.partition("=")
                install, *hwids = _fields(rhs)
                hwid = next((h for h in hwids if want in h.lower()), None)
                if not hwid:
                    continue
                name = _config_name(sections, install)
                if name:
                    return hwid, name
    return None


def _config_name(sections: dict[str, list[str]], install: str) -> str | None:
    """The config filename an install section's AddReg sets, if any."""
    for suffix in (".nt", ".ntamd64", ""):
        for line in sections.get(f"{install}{suffix}".lower(), []):
            key, _, value = line.partition("=")
            if key.strip().lower() != "addreg":
                continue
            for addreg in _fields(value):
                for reg in sections.get(addreg.lower(), []):
                    f = _fields(reg)
                    if len(f) >= 5 and f[2].lower() == _CONFIG_FILENAME_PKEY:
                        # The value is a DriverStore path such as
                        # %13%\SurfaceAPO_1284.json; %13% is the package dir.
                        return re.split(r"[\\/]", f[4])[-1]
    return None


def _driver_version(inf: Path) -> tuple[int, ...]:
    """[Version] DriverVer as a sortable tuple, () when absent."""
    for line in _read_inf(inf).get("version", []):
        key, _, value = line.partition("=")
        if key.strip().lower() == "driverver":
            ver = _fields(value)[-1]
            return tuple(int(p) for p in re.findall(r"\d+", ver))
    return ()


# --- config reading ---------------------------------------------------------

# The only rate the shipped R/EFX chain is defined at, and the pipeline's.
_SAMPLE_RATE = 48000

# The DRC's tables carry one row per volume state. State 0 reads as full
# volume (unvalidated): ValueTable steps down from -2.75 dB and the low shelf
# rises with the state. Full volume is the only state a filter placed before
# the speaker's volume control sees.
_VOLUME_STATE = 0

_DRC_BANDS = 4


def parse_config(config: Path, inf: Path, hwid: str) -> ApoLayer:
    """Read a Surface APO JSON's R/EFX chain into an `ApoLayer`."""
    try:
        text = config.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise UnsupportedApoConfig(config, f"it can't be read ({e})") from e
    try:
        chain = _efx_chain(json.loads(text))
    except (ValueError, KeyError, TypeError, IndexError,
            StopIteration) as e:
        raise UnsupportedApoConfig(
            config, f"its contents don't parse ({e or 'no InitialValueStore'})"
        ) from e
    if chain is None:
        raise UnsupportedApoConfig(
            config, f"no {_SAMPLE_RATE // 1000} kHz R/EFX render chain")

    notes: list[str] = []
    eq_left, eq_right = _main_eq(config, chain.get("MainEQ"))
    # Hold times: neither LSP stage has one, so every block's go into one note.
    held = [n for n in ("VolumeDepMBDRC4", "Crystal")
            if _enabled(chain.get(n))
            and any(h for h in chain[n].get("HoldTimeMs", []))]
    if held:
        notes.append("how long its dynamics hold before letting go, which "
                     "the compressor stage used has no setting for "
                     f"({'/'.join(held)} hold times)")
    dynamics = []
    drc = chain.get("VolumeDepMBDRC4")
    if _enabled(drc):
        dynamics.append(_drc(config, drc, notes))
    crystal = chain.get("Crystal")
    if _enabled(crystal):
        dynamics.append(_crystal(config, crystal, notes))
    shelves = [n for n in ("VolumeDepLS", "VolumeDepHS")
               if _enabled(chain.get(n))]
    if shelves:
        notes.append("the bass and treble shelves Windows sets by volume, "
                     "since neither chain this tool builds sees the "
                     f"speaker's volume ({'/'.join(shelves)})")
    limiter = chain.get("OutputLimiter")
    if _enabled(limiter):
        lookahead = _first(limiter, "LookaheadTimeMs", 0.0)
        acts = (_first(limiter, "Ratio", 1.0) != 1.0
                or _first(limiter, "ThresholdDb", 0.0) < 0.0)
        if acts:
            notes.append(
                "its final limiter (threshold "
                f"{_first(limiter, 'ThresholdDb', 0.0):g} dB, ratio "
                f"{_first(limiter, 'Ratio', 1.0):g}), which the preset's own "
                "limiter replaces (OutputLimiter)")
        elif lookahead > 0:
            notes.append(
                f"its final limiter's {lookahead:g} ms look-ahead, left out "
                "so the chain adds no delay (OutputLimiter)")
    return ApoLayer(
        label=LABEL, config_path=config, inf_path=inf,
        hardware_id=hwid, sample_rate=_SAMPLE_RATE,
        eq_left=eq_left, eq_right=eq_right, dynamics=tuple(dynamics),
        notes=tuple(notes))


def _efx_chain(doc: dict) -> dict[str, dict] | None:
    """{block name: {parameter: value}} for the 48 kHz R/EFX chain."""
    store = next(e for e in doc["entities"]
                 if e["name"] == "InitialValueStore")
    for node in store["children"]:
        if node["name"] != "R/EFX":
            continue
        blocks = {b["name"]: {p["name"]: p["value"] for p in b["children"]}
                  for b in node["children"] if b["type"] == "complex"}
        if any(fmt.get("sample_rate") == _SAMPLE_RATE
               for b in blocks.values()
               for fmt in b.get("InputFormats", [])):
            return blocks
    return None


def _enabled(block: dict | None) -> bool:
    return bool(block) and bool(_first(block, "Enabled", False))


def _first(block: dict, key: str, default):
    value = block.get(key, default)
    return value[0] if isinstance(value, list) and value else value


def _main_eq(config: Path, block: dict | None
             ) -> tuple[tuple[Biquad, ...], tuple[Biquad, ...]]:
    """MainEQ as per-channel biquad sections, identities dropped.

    The coefficient list interleaves the two channels section by section:
    left section 0, right section 0, left section 1, …
    """
    if not _enabled(block):
        return (), ()
    coeffs = block.get("Coefficients", [])
    if not coeffs or len(coeffs) % 10:
        raise UnsupportedApoConfig(
            config, f"MainEQ holds {len(coeffs)} coefficients, not a whole "
            "number of stereo biquad pairs")
    sections = [tuple(float(c) for c in coeffs[i:i + 5])
                for i in range(0, len(coeffs), 5)]
    identity = (1.0, 0.0, 0.0, 0.0, 0.0)

    def active(chan):
        return tuple(s for s in chan if s != identity)
    return active(sections[0::2]), active(sections[1::2])


def _state_row(config: Path, block: dict, key: str, notes: list[str],
               name: str) -> list[float]:
    """The first volume state's row of a per-state, per-band table."""
    values = [float(v) for v in block[key]]
    if len(values) % _DRC_BANDS:
        raise UnsupportedApoConfig(
            config, f"{name}.{key} holds {len(values)} values, not a "
            f"multiple of {_DRC_BANDS} bands")
    rows = [values[i:i + _DRC_BANDS]
            for i in range(0, len(values), _DRC_BANDS)]
    states_note = ("its compressor's settings for other volume states; "
                   f"state 0, read as full volume, is used ({name})")
    if any(r != rows[0] for r in rows) and states_note not in notes:
        notes.append(states_note)
    return rows[_VOLUME_STATE]


def _drc(config: Path, block: dict, notes: list[str]) -> BandDynamics:
    """VolumeDepMBDRC4 as a 4-band compressor at full volume.

    PreGain boosts a band before its compressor. The LSP equivalent is the
    same gain on the detector (sidechain preamp) and on the output (makeup).
    A band with ratio 1 and no pregain does nothing. When the band below it
    is inert too, the two pass audio alike, so their split is dropped.
    """
    name = "VolumeDepMBDRC4"
    xovers = [float(f) for f in block["CrossoverFreqs"]]
    if len(xovers) != _DRC_BANDS - 1:
        raise UnsupportedApoConfig(
            config, f"{name} has {len(xovers)} crossovers, expected "
            f"{_DRC_BANDS - 1}")
    threshold = _state_row(config, block, "ThresholdDb", notes, name)
    ratio = _state_row(config, block, "Ratio", notes, name)
    pregain = _state_row(config, block, "PreGainDb", notes, name)
    limit = _state_row(config, block, "OutputLimit", notes, name)
    attack = [float(v) for v in block["AttackTimeMs"]]
    release = [float(v) for v in block["ReleaseTimeMs"]]

    bands: list[DynBand] = []
    splits: list[float] = []
    for i in range(_DRC_BANDS):
        live = ratio[i] != 1.0 or pregain[i] != 0.0
        band = DynBand(enabled=live, threshold_db=threshold[i],
                       ratio=ratio[i], attack_ms=attack[i],
                       release_ms=release[i], pregain_db=pregain[i])
        if i and not live and not bands[-1].enabled:
            continue  # two inert neighbours pass audio alike: one band
        if i:
            splits.append(xovers[i - 1])
        bands.append(band)
    if any(v != 0.0 for v in limit):
        notes.append(f"its compressor's per-band output limits ({name})")
    return BandDynamics(name="drc", crossovers_hz=tuple(splits),
                        bands=tuple(bands), detection="RMS", knee_db=0.0)


def _crystal(config: Path, block: dict, notes: list[str]) -> BandDynamics:
    """Crystal's resonance limiters as one multiband limiter.

    Each resonance (F0, Bandwidth, Limit) becomes a band whose detector
    listens to F0 ± Bandwidth/2 and limits at Limit. The resonances overlap,
    and a multiband split cannot, so the bands split at the geometric
    midpoints between neighbouring F0s. An inert band sits below the first
    resonance's range and another above the last, so the rest of the
    spectrum passes untouched.
    """
    name = "Crystal"
    f0 = [float(v) for v in block["F0"]]
    bw = [float(v) for v in block["Bandwidth"]]
    lim = [float(v) for v in block["Limit"]]
    attack = [float(v) for v in block["AttackTimeMs"]]
    release = [float(v) for v in block["ReleaseTimeMs"]]
    n = len(f0)
    # LSP's compressor carries 8 bands, two of which bracket the resonances.
    if not 1 <= n <= 6 or not all(len(x) == n
                                  for x in (bw, lim, attack, release)):
        raise UnsupportedApoConfig(
            config, f"{name} has {n} resonances; 1 to 6 with matching "
            "Bandwidth/Limit/attack/release lists are supported")
    lows = [max(f - b / 2, 10.0) for f, b in zip(f0, bw)]
    highs = [f + b / 2 for f, b in zip(f0, bw)]
    inert = DynBand(enabled=False, threshold_db=0.0, ratio=1.0,
                    attack_ms=attack[0], release_ms=release[0])
    splits = ([lows[0]] +
              [math.sqrt(a * b) for a, b in zip(f0, f0[1:])] +
              [highs[-1]])
    bands = ([inert] +
             [DynBand(enabled=True, threshold_db=lim[i], ratio=100.0,
                      attack_ms=attack[i], release_ms=release[i],
                      sidechain_hz=(lows[i], highs[i]))
              for i in range(n)] +
             [inert])
    return BandDynamics(name="crystal",
                        crossovers_hz=tuple(round(s, 1) for s in splits),
                        bands=tuple(bands), detection="Peak", knee_db=0.0)
