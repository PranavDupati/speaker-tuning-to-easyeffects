# Vendor APO layers: speaker voicing that ships outside the DAX3 XML

## Where this stands

[reference.md](../reference.md) covers what the converter emits.

- **Surface Pro 9 (Intel) voices its speaker in Microsoft's Surface APO**, not
  in Dolby. Its DAX3 XML switches off its audio-optimizer, IEQ and graphic EQ
  in every profile ([Surface APO](#r-surface-apo-efx)).
- **`--enable vendor-apo`** translates that config's `R/EFX` chain. It is
  opt-in and unvalidated: no Windows capture exists for any Surface
  ([Surface APO](#r-surface-apo-efx)).

Open:

- Whether other Surface models ship a `SurfaceAPO_*.json`, and with the same
  block set. Each would be a second-device check on the parser.
- Fortemedia's render APO ships a per-device speaker file,
  `SAMSfpaspk_<SUBSYS>.dat`, in an opaque binary format. One Yoga Slim 7 ProX
  14ARH7 package's `OemXAudioExtFM_L.inf` names 96 of them. It is
  undecoded.

<a id="r-surface-apo-efx"></a>

## Microsoft's Surface APO: the endpoint chain after Dolby

The Surface Pro 9 (Intel) speaker voicing lives in `SurfaceAPO_1284.json`,
which Windows runs as the endpoint effect after Dolby. Conditions: package
`SurfacePro9_Win11_22631_26.091.15297.0.msi` (SHA-256 `7913b90e…`), Realtek
ALC274, `SUBSYS_10EC1284`; read 2026-10-06; issue #113.

**The DAX3 XML switches its voicing off.** In all 10 profiles of
`DEV_0274_SUBSYS_10EC1284_PCI_SUBSYS_72708086.xml`, `tuning-cp` has
`audio-optimizer-enable` 0 with zero bands, `ieq-enable` 0 and
`graphic-equalizer-enable` 0. The converter still applies the IEQ at its 10%
floor. The regulator thresholds are all 0 dB high and −12 dB low
(`array_20_n192`). The converter emits no regulator in 6 of the 9 profiles
other than `off`. In `personalize_user1`–`3` it emits a live limiter at
0 dBFS. The volume leveler is enabled in those 9 profiles, and an HDA preset
ships it off unless `--enable autogain`. 18 of the package's 25 XMLs have the
audio-optimizer, IEQ and graphic EQ off in every profile, counted with
`xml.etree` over `tuning-cp`.

**The binding.** `SurfaceAPOExtension.inf` binds
`HDAUDIO\FUNC_01&VEN_10EC&DEV_0274&SUBSYS_10EC1284` (and its `INTELAUDIO\`
form) to an install section. That section's AddReg sets the property
`{c1f75c4c-3243-11ea-850d-2e728ce88125},0` (`PKEY_SurfaceApoConfigFilename`)
to `%13%\SurfaceAPO_1284.json`. The `lib/apo/surface.py` finder follows that
path through the `.inf`, not the filename.

**The order.** The speaker's Realtek driver, `EHDXSSTMD3A4.inf`, gives the
speaker topology three APO presets. `SysFx` puts Dolby's APO in the stream
slot (SFX), and `SAFApo_SPK3` puts the Surface APO in the mode and endpoint
slots (MFX, EFX). Windows runs stream, then mode, then endpoint effects, so the
Surface chain processes Dolby's output.

**The R/EFX blocks, in file order** (48 kHz; L and R identical in this
file):

| Block | Content | Translation |
|---|---|---|
| `VolumeControl` | per-channel gain, `UseDependentGain` | none |
| `MainEQ` | 16 biquads (b0, b1, b2, a1, a2), L/R interleaved; 5 active per channel | folded into the FIR |
| `VolumeDepLS`, `VolumeDepHS` | one shelf per channel for each of 10 volume states | none |
| `VolumeDepMBDRC4` | crossovers 100/600/2750 Hz; 10 identical states | `multiband_compressor#2` |
| `Crystal` | 5 resonances: F0 160–844 Hz, bandwidth 120–300 Hz, Limit −2.7 to −18.5 dB | `multiband_compressor#3` |
| `OutputLimiter` | threshold 0 dB, ratio 1, pregain 0, 10 ms look-ahead | none; `limiter#0` stays last |

The `R/MFX/*` per-mode EQs are identity in the DEFAULT, RAW, COMMS, MOVIE and
MEDIA modes, at both 48 and 44.1 kHz. Only NOTIFICATION is not.

**MainEQ's response**, computed from the coefficients with
`scipy.signal.sosfreqz`:

| Hz | 30 | 50 | 80 | 300 | 1000 | 2000 | 3000 | 4000 | 6000 | 8000 | 12000 | 20000 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| dB | −18.8 | −6.0 | −1.2 | 0.0 | −0.5 | −3.3 | −7.4 | −9.7 | −2.7 | +1.4 | +3.4 | +3.0 |

All five active sections have their poles inside the unit circle and their
zeros inside or on it, to coefficient precision. Rounding splits the double
zero at DC in one high-pass section into a reciprocal pair at |z| = 1.00014
and 0.99986. The cascade is therefore minimum-phase above about 1 Hz, so a
minimum-phase FIR of the same magnitude reproduces its phase as well.

**The volume-dependent shelves** were evaluated the same way, at 50 Hz and
10 kHz:

| State | 0 | 1 | 3 | 5 | 7 | 9 |
|---|---|---|---|---|---|---|
| LS @ 50 Hz | 0.0 | +1.5 | +4.5 | +7.5 | +12.4 | +17.1 |
| HS @ 10 kHz | +0.9 | +1.8 | +3.6 | +5.3 | +7.0 | +8.8 |

`ValueTable` holds 9 steps from −2.75 to −24.75 dB, which would select among
the 10 states.

**Folding MainEQ into the FIR** needed a floor. The FIR is 4096 taps; the
errors below were graded on a 32768-point grid from 20 Hz to 20 kHz, with
numpy and scipy, 2026-10-06. "Own target" grades each design against the
floored curve it was asked for; "raw MainEQ" grades it against the unfloored
cascade:

| Target | Worst vs own target | Worst vs raw MainEQ | Within 0.5 dB of raw from |
|---|---|---|---|
| raw MainEQ | 14.8 dB at 53 Hz | 14.8 dB at 53 Hz | — |
| hard clamp 40 dB under the peak | 1.00 dB at 21 Hz | 1.00 dB at 21 Hz | 22 Hz |
| smooth floor, 40 dB under | 0.32 dB at 21 Hz | 1.42 dB at 21 Hz | 23 Hz |
| smooth floor, 50 dB under | 1.32 dB at 21 Hz | 1.19 dB at 21 Hz | 66 Hz |
| smooth floor, 60 dB under | 1.55 dB at 21 Hz | 1.54 dB at 21 Hz | 54 Hz |
| smooth floor, 40 dB under, 8192 taps | 0.10 dB at 21 Hz | not measured | not measured |

The high-pass's null at DC is what the cepstral design aliases. The converter
ships the smooth 40 dB floor at 4096 taps (`FIR_TARGET_RANGE_DB`).

**The dynamics mappings are hypotheses.** The DRC's per-band `PreGainDb`
becomes the same gain on the detector and the makeup, which is algebraically
pre-gain-then-compress. Neighbouring bands with ratio 1 and no pregain are
merged. Each
Crystal resonance becomes a limiter band with its detector narrowed to
F0 ± Bandwidth/2, split at the geometric midpoints between F0s. "Limit"
could be a level ceiling or a maximum cut, and the file holds no separate
threshold. Ratio 100, knee 0 dB, RMS detection for the DRC and Peak for
Crystal are converter choices; the config states none of them.

Open:

- `VolumeControl` sits first in the chain, so on Windows the dynamics likely
  see the signal after the system volume. EasyEffects and the PipeWire chain
  run before the speaker's volume, so their dynamics behave like Windows at
  full volume, read as state 0. Even there Windows' high shelf adds +0.9 dB at
  10 kHz, which the preset leaves out.
- The volume-dependent shelves are not reproduced. Neither routing lets a
  filter see the speaker volume (ee-to-pipewire.md "Smart-filter routing").
  If listeners report "thin at low volume", the candidate is a PipeWire-only
  follower that sets the shelf state from the speaker volume.
- Crystal's semantics, the DRC's level reference, crossover type,
  `OutputLimit` and the 50–100 ms hold times are unknown.
- `surfacedspextension/saflibadl.bin` (the Intel DSP side of the EFX, whose
  proxy the APO names), `RenderHeadroom=3` in that package's `.inf`, and
  Realtek's `RTAIODAT.DAT` are opaque. Whether they add processing is unknown.
- The fold puts MainEQ before Dolby's dynamics; Windows runs it after them.
  That is exact while the preset carries no Dolby dynamics: the default HDA
  run in the 6 profiles without a regulator. It is not exact with
  `--enable autogain`, or in `personalize_user1`–`3`.
- `make_fir` normalises the folded FIR to its peak, so the level reaching the
  vendor DRC's thresholds is not the level Windows feeds it.
  `--enable level-restore` gives the peak back; which matches Windows is
  unmeasured.
- On the PipeWire chain, `--enable virtual-bass` sums its wet branch after the
  whole chain, so Dolby's virtual-bass harmonics skip the vendor EQ and
  dynamics. On Windows the Surface EFX processes all of Dolby's output.
- The channel order of the interleave is unread: L and R are identical here.
