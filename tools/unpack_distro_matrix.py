#!/usr/bin/env python3
"""Check the driver-download unpackers' install hints on real distributions.

For each distribution image, in a throwaway podman container: ask
`lib/packages.py` for the command that installs innoextract, 7-Zip and
msitools there, from this checkout's `lib/`, mounted read-only. Run exactly
that command, minus `sudo`. Then confirm that `lib/driver_package.py` finds
each binary and that 7-Zip lists a zip the probe builds. This is the
end-to-end proof that a printed name installs the binary the unpacker looks
for: a package can exist without it, as Debian 12's `7zip` ships only `7zz`.

Not a test tier: it needs the network and several minutes, and the facts it
checks are per-release packaging. Run it by hand after changing the unpacker
rows in `lib/packages.py`, and record the date and results in that module's
provenance note. Nothing on the host is touched besides podman's image store.

    python3 tools/unpack_distro_matrix.py              # every row
    python3 tools/unpack_distro_matrix.py debian-12    # one row
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# name: (image, install a bare python3 as root). Nothing else, so the row
# shows what the printed command alone brings in.
_APT = ("apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install "
        "-y -qq --no-install-recommends python3 >/dev/null")
_DNF = "dnf -y -q install python3 >/dev/null"
ROWS = {
    "debian-12": ("docker.io/library/debian:12", _APT),
    "debian-stable": ("docker.io/library/debian:stable-slim", _APT),
    "ubuntu-24.04": ("docker.io/library/ubuntu:24.04", _APT),
    "ubuntu-26.04": ("docker.io/library/ubuntu:26.04", _APT),
    "fedora-44": ("registry.fedoraproject.org/fedora:44", _DNF),
    # Without EPEL, then with it: the not-installable copy, then the names.
    "rocky-9": ("docker.io/rockylinux/rockylinux:9", _DNF),
    "rocky-9-epel": ("docker.io/rockylinux/rockylinux:9",
                     _DNF + " && dnf -y -q install epel-release >/dev/null"),
    "arch": ("docker.io/library/archlinux:latest",
             "pacman -Sy --noconfirm --needed python >/dev/null"),
    "alpine-edge": ("docker.io/library/alpine:edge", "apk add -q python3"),
    "opensuse-leap-16": ("registry.opensuse.org/opensuse/leap:16.0",
                         "zypper -n -q install python3 >/dev/null"),
}

# Runs inside the container, as root, from the read-only mount.
_PROBE = '''
import sys, tempfile, zipfile
from pathlib import Path
sys.path.insert(0, "/src")
from lib import driver_package, packages, tool_env

keys = (packages.INNOEXTRACT, packages.SEVENZIP, packages.MSITOOLS)
fam = packages.family()
if sys.argv[1] == "before":
    for key in keys:
        if not packages.names([key], fam):
            print(f"{key}: no name ({packages.UNPACKAGED.get((key, fam))})")
        elif packages.unavailable(key, fam):
            print(f"{key}: not installable here, as the unpacker says")
        else:
            print("INSTALL=" + packages.install_command([key], fam))
    sys.exit(0)
for label, binary in (("innoextract", "innoextract"),
                      ("7-Zip", driver_package._sevenzip()),
                      ("msitools", "msiextract")):
    found = binary and tool_env.which(binary)
    version = driver_package._tool_version(
        [binary] + ([] if binary.startswith("7z") else ["--version"])
    ) if found else ""
    print(f"{label}: {'found ' + binary if found else 'NOT FOUND'} {version}")
exe = driver_package._sevenzip()
if exe:
    tmp = Path(tempfile.mkdtemp())
    with zipfile.ZipFile(tmp / "t.zip", "w") as zf:
        zf.writestr("d/DEV_0274_SUBSYS_10EC1284.xml", "<x/>")
    kind, members = driver_package._7z_list(exe, tmp / "t.zip")
    print("7-Zip lists a zip:", kind, [m.path for m in members])
'''

_INNER = r'''
set -e
{install}
echo "--- printed"
python3 /m/probe.py before | tee /tmp/before.txt
sed -n 's/^INSTALL=sudo //p' /tmp/before.txt | while read -r cmd; do
  echo "--- running: $cmd"
  case "$cmd" in
    apt*) DEBIAN_FRONTEND=noninteractive $cmd -y -qq >/dev/null ;;
    apk*) $cmd -q ;;
    dnf*) $cmd -y -q >/dev/null ;;
    pacman*) $cmd --noconfirm >/dev/null ;;
    zypper*) zypper -n -q install ${{cmd#zypper install }} >/dev/null ;;
    *) $cmd ;;
  esac || echo "(install failed: $?)"
done
echo "--- after"
python3 /m/probe.py after
'''


def run_row(image: str, install: str, work: Path) -> str:
    """One distribution's transcript, or why it couldn't run."""
    work.chmod(0o755)
    (work / "probe.py").write_text(_PROBE)
    (work / "inner.sh").write_text(_INNER.format(install=install))
    argv = ["podman", "run", "--rm", "--security-opt", "label=disable",
            "-v", f"{ROOT / 'lib'}:/src/lib:ro", "-v", f"{work}:/m:ro",
            image, "sh", "/m/inner.sh"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=900)
    except subprocess.TimeoutExpired:
        return "timed out after 900 s"
    out = "\n".join(line for line in proc.stdout.splitlines()
                    if not line.startswith("INSTALL="))
    if proc.returncode != 0:
        out += f"\n(exit {proc.returncode})\n{proc.stderr.strip()[-600:]}"
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("rows", nargs="*",
                        help=f"rows to run (default: all): {', '.join(ROWS)}")
    parser.add_argument("--out-dir", type=Path,
                        default=ROOT / "localresearch" / "unpack-distro-matrix",
                        help="where the transcript is written")
    args = parser.parse_args(argv)
    unknown = [r for r in args.rows if r not in ROWS]
    if unknown:
        parser.error(f"unknown row(s): {', '.join(unknown)}")
    if shutil.which("podman") is None:
        parser.error("podman not found")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    report = []
    for name in args.rows or ROWS:
        image, install = ROWS[name]
        print(f"=== {name} ({image})", flush=True)
        with tempfile.TemporaryDirectory() as tmp:
            text = run_row(image, install, Path(tmp))
        print(text, flush=True)
        report.append(f"=== {name} ({image})\n{text}\n")
        (args.out_dir / "matrix.txt").write_text("\n".join(report))
    print(f"\nWrote {args.out_dir / 'matrix.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
