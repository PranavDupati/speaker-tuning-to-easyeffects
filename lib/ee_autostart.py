"""What starts EasyEffects at login, and whether it runs in service mode.

The ``--doctor`` Background service check and the generator's autoload Tip
both ask it: EasyEffects' own two toggles are one answer, but a desktop
session can launch it instead, through an XDG autostart entry or a
compositor's startup file (issue #117). This module reads those, and the
running process's arguments, and returns plain facts;
`lib/report/environment.py` turns them into the verdict.

Stdlib-only, like `ee_paths` and `ee_socket` beside it. Tools run through
`lib/tool_env.py` (``pgrep``, ``systemctl``) and system files are read
through `lib/host.py` (``/etc/xdg`` and the compositors' system configs);
the user's own files are under $XDG_CONFIG_HOME (`lib/xdg.py`) or, for sway's
and i3's legacy ``~/.sway`` and ``~/.i3`` configs, the home folder.
"""

from __future__ import annotations

import glob
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lib import doctor, ee_paths, ee_socket, host, tool_env, xdg


@dataclass(frozen=True)
class _Session:
    """Where one compositor reads its startup commands.

    ``configs`` are the candidate files in the compositor's own search order,
    and it uses the first that exists: "~/" names one under the home folder,
    anything else one under $XDG_CONFIG_HOME. ``directives`` are the line
    patterns whose capture is a command run at startup, each flagged True when
    the capture is a single quoted shell string. ``include`` is the pattern of
    a line that pulls in more config, its capture the path(s), flagged True
    when it lists several shell-expanded paths. ``example`` is the line that
    starts EasyEffects there, in that file's syntax, as a template:
    ``{command}`` is the command, ``{words}`` its words each quoted. ``replaced_by`` is a file
    whose presence means ``configs`` is not read at all. ``system_configs``
    are the system-wide files the compositor falls back to, in its order: a
    new user file replaces the one it was reading rather than adding to it
    (sway(1), i3(1), labwc-config(5)), so with only a system file the advice is
    to copy it to the user's path first."""
    configs: tuple[str, ...]
    directives: tuple[tuple[str, bool], ...]
    example: str
    include: tuple[str, bool] = ("", False)
    replaced_by: str = ""
    system_configs: tuple[str, ...] = ()


# The arguments EasyEffects writes into its own autostart entry when both of
# its Background Service toggles are on (src/autostart.cpp).
_ARGS = "--hide-window --service-mode"


_EXEC = ((r"^[ \t]*exec(?:_always)?[ \t](.*)$", False),)
_INCLUDE = (r"^[ \t]*include[ \t]+(.+?)[ \t]*$", True)

# Keyed by the process name each compositor's package installs (Debian and
# Arch file lists). Verified against each project's documentation and source:
# Hyprland's legacy config manager (v0.54.0, v0.56.2) queues exec, execr,
# exec-once and execr-once to run at first launch, its wiki gives exec and
# exec-once an optional "[rules] " prefix and has "source = path" (globs
# allowed) pull in another file, and v0.56.2 loads hyprland.lua in place of
# hyprland.conf when both exist; sway(1) and sway(5) (search order; exec runs
# at startup; "include <paths...>", relative to the parent config and shell
# expanded); i3(1) and the i3 user guide (search order; exec runs at startup,
# exec_always also on restart; the same include); niri "Miscellaneous
# configuration" (spawn-at-startup, and spawn-sh-at-startup since 25.08);
# labwc-config(5) (autostart is a shell script run at launch); river(1) (init
# is a shell script run at startup) and riverctl(1) (spawn runs its one
# argument with /bin/sh -c).
_SESSIONS = {
    "Hyprland": _Session(("hypr/hyprland.conf",),
                         ((r"^[ \t]*exec(?:-once)?[ \t]*=[ \t]*(?:\[[^\]]*\][ \t]*)?(.*)$", False),
                          (r"^[ \t]*execr(?:-once)?[ \t]*=(.*)$", False)),
                         "exec-once = {command}",
                         include=(r"^[ \t]*source[ \t]*=[ \t]*(.+?)[ \t]*$", False),
                         replaced_by="hypr/hyprland.lua"),
    "sway": _Session(("~/.sway/config", "sway/config", "~/.i3/config",
                      "i3/config"), _EXEC, "exec {command}", include=_INCLUDE,
                     system_configs=("/etc/sway/config", "/etc/i3/config")),
    # i3(1): $XDG_CONFIG_HOME/i3/config, then ~/.i3/config, and only then the
    # system /etc/xdg/i3/config and /etc/i3/config.
    # --no-startup-id: i3's user guide warns of a watch cursor for 60 seconds
    # otherwise, for a program whose window never appears.
    "i3": _Session(("i3/config", "~/.i3/config"), _EXEC,
                   "exec --no-startup-id {command}",
                   include=_INCLUDE,
                   system_configs=("/etc/xdg/i3/config", "/etc/i3/config")),
    "niri": _Session(("niri/config.kdl",),
                     ((r"^[ \t]*spawn-at-startup[ \t](.*)$", False),
                      (r"^[ \t]*spawn-sh-at-startup[ \t](.*)$", True)),
                     "spawn-at-startup {words}"),
    # A shell script run line by line, so the command goes to the background.
    "labwc": _Session(("labwc/autostart",), ((r"^(.*)$", False),),
                      "{command} &",
                      system_configs=("/etc/xdg/labwc/autostart",)),
    "river": _Session(("river/init",),
                      ((r"^[ \t]*riverctl[ \t]+spawn[ \t](.*)$", True),
                       (r"^(.*)$", False)),
                      'riverctl spawn "{command}"'),
}
# Hyprland's package installs the binary under both names.
_SESSION_PROCESSES = {"hyprland": "Hyprland", **{name: name for name in _SESSIONS}}

# The name EasyEffects gives the entry it writes for its own 'Autostart on
# login' toggle: APPLICATION_ID (src/autostart.cpp).
_EE_OWN_ENTRIES = {"com.github.wwmm.easyeffects.desktop"}
_SERVICE_FLAGS = {"--service-mode", "--gapplication-service"}


@dataclass(frozen=True)
class Launch:
    """What starts EasyEffects at login, as `background_launch_source` found it.

    ``source`` names it for the report: "" when nothing was found, or when
    the only launcher is the toggle.
    ``service_mode`` is whether its command turns service mode on.
    ``is_toggle`` marks EasyEffects' own autostart entry: the 'Autostart on
    login' toggle itself, written as soon as it is switched on, while the rc
    that records the toggle is only saved later. ``autostart_entry`` marks a
    launcher that is an XDG autostart entry. ``compositor`` names this
    session's ``_SESSIONS`` compositor, "" on any other desktop. Those
    compositors run autostart entries only through a helper such as uwsm or
    ``dex -a``, so an autostart entry there, the toggle's included, launches
    EasyEffects only if one is set up. A session that runs them shows it:
    systemd.special(7) has a desktop opt in to systemd's autostart generator
    by wanting xdg-desktop-autostart.target, so with that target active, or a
    startup file that runs ``dex -a``, ``compositor`` is "".
    ``startup_hint`` is the instruction leading the line that starts
    EasyEffects from the compositor's startup file and the line, ("", "")
    when there is no file the line can safely be added to."""
    source: str = ""
    service_mode: bool = False
    is_toggle: bool = False
    autostart_entry: bool = False
    compositor: str = ""
    startup_hint: tuple[str, str] = ("", "")


Process = tuple[str, list[str]]


def background_launch_source(processes: list[Process] | None = None) -> Launch:
    """What launches EasyEffects at login. Its ``source`` is "" if none found.

    Reads the startup file of this session's ``_SESSIONS`` compositor with the
    files it includes, then XDG autostart entries from $XDG_CONFIG_HOME and
    $XDG_CONFIG_DIRS. Best effort: a launcher outside those (a systemd user
    unit, say), a wrapper ``_simple_commands`` doesn't know, a Hyprland Lua
    config, or a desktop-specific ``OnlyShowIn``/``NotShowIn`` or ``TryExec``
    entry is not evaluated."""
    config = xdg.config_home()
    compositors = sorted(_session_compositors(
        own_processes() if processes is None else processes))
    # EasyEffects' own entry is the toggle whatever else launches it.
    found = _enabled_entries(config)
    is_toggle = any(entry.name in _EE_OWN_ENTRIES for entry, _ in found)
    # More than one only with $XDG_CURRENT_DESKTOP unset and a nested or stray
    # compositor running beside the session's, so each one's file is read.
    paths, runs_entries = {}, False
    for name in compositors:
        session = _SESSIONS[name]
        paths[name] = path = _session_startup_file(session, config)
        # With no file of the user's, the compositor reads its system one,
        # which may itself start EasyEffects or an autostart helper (i3's
        # shipped config runs dex --autostart).
        read = path or _session_system_file(session, config)
        for file, text in _session_files(session, read) if read else ():
            argv = _session_file_argv(session, text)
            if argv:
                return Launch(doctor.tilde(file),
                              not _SERVICE_FLAGS.isdisjoint(argv),
                              is_toggle=is_toggle, compositor=name)
            runs_entries = runs_entries or _session_runs_entries(session, text)
    compositor = compositors[0] if compositors else ""
    hint = (_startup_hint(compositor, config, paths[compositor])
            if compositor else ("", ""))
    if compositor and (runs_entries or _active_targets(
            {"xdg-desktop-autostart.target"})):
        compositor, hint = "", ("", "")
    if not found:
        return Launch(compositor=compositor, startup_hint=hint)
    others = [entry for entry, _ in found if entry.name not in _EE_OWN_ENTRIES]
    return Launch(doctor.tilde(others[0]) if others else "",
                  any(not _SERVICE_FLAGS.isdisjoint(words) for _, words in found),
                  is_toggle=is_toggle, autostart_entry=True,
                  compositor=compositor, startup_hint=hint)


def desktop_runs_autostart_entries() -> bool:
    """Whether $XDG_CURRENT_DESKTOP names a desktop other than the
    ``_SESSIONS`` compositors, which run autostart entries themselves."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    return bool(desktop) and not _session_compositors([])


def _enabled_entries(config: Path) -> list[tuple[Path, list[str]]]:
    """The enabled autostart entries that run EasyEffects, with their command
    words."""
    entries = _xdg_autostart_entries(config)
    return [(entry, words) for entry in entries.values()
            if (words := _desktop_entry_words(_read_text(entry), entry.name))]


def _startup_hint(compositor: str, config: Path,
                  path: Path | None) -> tuple[str, str]:
    """The instruction leading the line that starts EasyEffects on
    ``compositor``'s session, and the line, or ("", "") when no file can
    safely take it. ``path`` is the startup file the compositor reads, None
    if none of the user's."""
    session = _SESSIONS[compositor]
    if path:
        return f"In {doctor.tilde(path)}, add:", startup_line(compositor)
    if session.replaced_by and _is_file(config / session.replaced_by):
        return "", ""
    user = config / next(name for name in session.configs if not name.startswith("~/"))
    system = next((name for name in session.system_configs
                   if _is_file(host.path(name))), "")
    if not system:
        return "", ""
    return (f"Copy {system} to {doctor.tilde(user)}, then add to the copy:",
            startup_line(compositor))


def startup_line(compositor: str) -> str:
    """The line that starts EasyEffects from ``compositor``'s startup file.

    Where only the Flatpak is installed there is no ``easyeffects`` on PATH,
    so the line runs it through ``flatpak run``."""
    command = (f"flatpak run {ee_paths.FLATPAK_APP_ID} {_ARGS}"
               if tool_env.which("easyeffects") is None
               and ee_paths.flatpak_app_installed()
               else f"easyeffects {_ARGS}")
    return _SESSIONS[compositor].example.format(
        command=command, words=" ".join(f'"{w}"' for w in command.split()))


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _glob(folder: Path, pattern: str) -> list[Path]:
    """``folder.glob(pattern)``, sorted, empty where the folder can't be read."""
    try:
        return sorted(folder.glob(pattern))
    except OSError:
        return []


def _active_targets(targets: set[str]) -> set[str] | None:
    """The ones of these user systemd targets that are active now, or None
    when systemctl can't answer (no user bus, as over ssh)."""
    names = sorted(targets)
    try:
        proc = tool_env.run(["systemctl", "--user", "is-active", *names],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, timeout=2, check=False)
    except (subprocess.SubprocessError, OSError):
        return None
    states = proc.stdout.split()
    if len(states) != len(names):
        return None
    return {name for name, state in zip(names, states) if state == "active"}


def own_processes() -> list[Process]:
    """This user's EasyEffects and ``_SESSIONS`` compositor processes, as
    (argv[0]'s base name, argv), from one ``pgrep -a``. Empty when pgrep
    can't answer."""
    try:
        proc = tool_env.run(["pgrep", "-a", "-x", "-u", str(os.getuid()),
                             "|".join([ee_socket.PROCESS_PATTERN,
                                       *_SESSION_PROCESSES])],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, timeout=2, check=False)
    except (subprocess.SubprocessError, OSError):
        return []
    return [(argv[0].rsplit("/", 1)[-1], argv)
            for line in proc.stdout.splitlines()
            if (argv := line.split()[1:])]


def _session_compositors(processes: list[Process]) -> set[str]:
    """The ``_SESSIONS`` compositors this session runs.

    $XDG_CURRENT_DESKTOP decides when it is set: the Desktop Entry spec has the
    login manager set it from the session file's DesktopNames, and it names
    this session rather than any process that happens to run, such as a
    nested compositor. Its components are matched without case. When it is
    unset, as it can be for a compositor started from a console, the process
    table decides, by process name."""
    by_lower = {name.lower(): _SESSION_PROCESSES[name] for name in _SESSION_PROCESSES}
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    if desktop:
        return {by_lower[part.lower()] for part in desktop.split(":")
                if part.lower() in by_lower}
    return {_SESSION_PROCESSES[name] for name, _ in processes
            if name in _SESSION_PROCESSES}


def _session_startup_file(session: _Session, config: Path) -> Path | None:
    """The file the compositor reads its startup commands from: the first of
    its ``configs`` that exists, or None."""
    if session.replaced_by and _is_file(config / session.replaced_by):
        return None
    for name in session.configs:
        path = (Path.home() / name[2:] if name.startswith("~/")
                else config / name)
        if _is_file(path):
            return path
    return None


def _session_system_file(session: _Session, config: Path) -> Path | None:
    """The system config the compositor falls back to without a user file."""
    if session.replaced_by and _is_file(config / session.replaced_by):
        return None
    return next((host.path(name) for name in session.system_configs
                 if _is_file(host.path(name))), None)


def _session_files(session: _Session, path: Path,
                   seen: set[Path] | None = None) -> list[tuple[Path, str]]:
    """The startup file and the files it includes, recursively, each with its
    text.

    Paths are expanded as the compositor does (~, $VARS, globs) and taken
    relative to the including file; a file already read isn't read again."""
    seen = set() if seen is None else seen
    try:
        real = path.resolve()
    except (OSError, RuntimeError):   # a symlink loop, before Python 3.13
        return []
    if real in seen:
        return []
    seen.add(real)
    text = _read_text(path)
    pattern, several = session.include
    files = [(path, text)]
    for value in re.findall(pattern, text, re.M) if pattern else ():
        try:
            names = shlex.split(value) if several else [value]
        except ValueError:
            continue
        for name in names:
            expanded = os.path.expandvars(os.path.expanduser(name))
            # An absolute system path is read like any other host file.
            where = (host.path(expanded) if os.path.isabs(expanded)
                     else path.parent / expanded)
            for match in sorted(glob.glob(str(where))):
                files += _session_files(session, Path(match), seen)
    return files


def _session_runs_entries(session: _Session, text: str) -> bool:
    """Whether a startup file runs XDG autostart entries itself: through
    dex -a/--autostart (dex(1)), or by starting xdg-desktop-autostart.target,
    the opt-in to systemd's autostart generator (systemd.special(7))."""
    for command in _directive_commands(session, text):
        for words in _simple_commands(command):
            name = words[0].rsplit("/", 1)[-1]
            if name == "dex" and {"-a", "--autostart"} & set(words):
                return True
            if (name == "systemctl" and "start" in words
                    and "xdg-desktop-autostart.target" in words):
                return True
    return False


def _directive_commands(session: _Session, text: str) -> list[str]:
    """The commands a startup file's directives run, a quoted shell string
    unwrapped into the command line it holds."""
    commands = []
    for pattern, shell_string in session.directives:
        for command in re.findall(pattern, text, re.M):
            if shell_string:
                try:
                    command = " ".join(shlex.split(command, comments=True))
                except ValueError:
                    continue
            commands.append(command)
    return commands


def _session_file_argv(session: _Session, text: str) -> list[str] | None:
    for command in _directive_commands(session, text):
        argv = _easyeffects_argv(command)
        if argv:
            return argv
    return None


def _xdg_autostart_entries(config: Path) -> dict[str, Path]:
    """Autostart entries by file name, resolved as the XDG autostart spec does.

    A more important file replaces a less important one of the same name, even
    one that disables it. The user's directory is the most important, then the
    absolute directories of $XDG_CONFIG_DIRS in order, /etc/xdg when it is
    unset."""
    system = [Path(d) for d in
              (os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":")
              if Path(d).is_absolute()]
    folders = [host.path(d) / "autostart" for d in reversed(system)]
    entries: dict[str, Path] = {}
    for folder in [*folders, config / "autostart"]:
        for entry in _glob(folder, "*.desktop"):
            entries[entry.name] = entry
    return entries


def _desktop_entry_words(text: str, name: str) -> list[str] | None:
    """The words of the EasyEffects command an enabled autostart entry runs,
    or None.

    ``Hidden=true`` is the autostart spec's off switch, and systemd's
    autostart generator also skips ``X-systemd-skip=true``. GNOME's session
    reads ``X-GNOME-Autostart-enabled``, which its Startup Applications switch
    writes. EasyEffects' own entry is recognised by its file name whatever its
    command, since a Flatpak's runs ``flatpak run …``. The Desktop Entry spec
    ignores whitespace around "=".
    """
    for key, off in (("Hidden", "true"), ("X-systemd-skip", "true"),
                     ("X-GNOME-Autostart-enabled", "false")):
        if re.search(rf"^{key}[ \t]*=[ \t]*{off}[ \t]*$", text, re.M | re.I):
            return None
    commands = re.findall(r"^Exec[ \t]*=[ \t]*(.*)$", text, re.M)
    for command in commands:
        argv = _easyeffects_argv(command)
        if argv:
            return argv
    if name in _EE_OWN_ENTRIES and commands:
        return commands[0].split()
    return None


def _easyeffects_argv(command: str) -> list[str] | None:
    """The argv a command line starts EasyEffects with, or None.

    It starts EasyEffects when one of its simple commands runs the
    ``easyeffects`` executable, ``flatpak run`` with EasyEffects' app id, or
    EasyEffects' desktop entry through uwsm. So ``pkill easyeffects;
    easyeffects --service-mode`` launches it, while ``pkill easyeffects``,
    ``easyeffects --quit``, ``easyeffects-presets`` or a keybind such as
    ``riverctl map … spawn easyeffects`` does not."""
    for words in _simple_commands(command):
        executable, rest = words[0].rsplit("/", 1)[-1], words[1:]
        # flatpak run [options] com.github.wwmm.easyeffects [args]: the app's
        # own arguments follow its id.
        if (executable == "flatpak" and "run" in rest
                and ee_paths.FLATPAK_APP_ID in rest):
            argv = ["easyeffects", *rest[rest.index(ee_paths.FLATPAK_APP_ID) + 1:]]
        elif executable == "easyeffects" or executable.split(":")[0] in _EE_OWN_ENTRIES:
            argv = ["easyeffects", *rest]
        else:
            continue
        if not {"-q", "--quit"} & set(argv[1:]):
            return argv
    return None


def _simple_commands(command: str) -> list[list[str]]:
    """The simple commands a shell command line runs, each as its words from
    the executable on.

    A ``#`` comment ends the line; ``;``, ``&&``, ``|`` and the like separate
    commands; ``_WRAPPERS`` prefixes with their options and ``VAR=value``
    assignments are dropped; and a ``sh -c`` string (``bash -lc`` too) is read
    as a command line of its own."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    commands: list[list[str]] = []
    words: list[str] = []
    for token in [*tokens, ";"]:
        if not (token and set(token) <= set(";&|()")):
            words.append(token)
            continue
        words, group = [], _without_prefixes(words)
        if not group:
            continue
        rest = group[1:]
        if (group[0].rsplit("/", 1)[-1] in ("sh", "bash", "dash", "zsh") and rest
                and re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", rest[0])):
            commands += _simple_commands(rest[1]) if len(rest) > 1 else []
        else:
            commands.append(group)
    return commands


# Prefixes that run the command after them, each with its options that take a
# value as the next word: nohup, setsid, exec and env; and uwsm, which the
# Hyprland wiki recommends and whose README gives `uwsm app [-s slice] [-t
# scope|service] -- {executable|entry.desktop[:action]} [args ...]`, with
# uwsm-app as "a drop-in replacement of `uwsm app`".
_WRAPPERS = {
    "nohup": set(), "setsid": set(), "exec": set(), "env": set(),
    "uwsm": {"-s", "-t"}, "uwsm-app": {"-s", "-t"},
}


def _without_prefixes(words: list[str]) -> list[str]:
    i, takes_value = 0, set()
    while i < len(words):
        word, base = words[i], words[i].rsplit("/", 1)[-1]
        if base in _WRAPPERS:
            takes_value = _WRAPPERS[base]
        elif not (word == "--" or re.match(r"^\w+=", word)
                  or (word == "app" and i and words[i - 1].endswith("uwsm"))):
            if not word.startswith("-"):
                break
            i += 1 if "=" in word or word not in takes_value else 2
            continue
        i += 1
    return words[i:]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def service_mode_launch(processes: list[Process] | None = None) -> bool:
    """True when this user's running EasyEffects was started in service mode.

    ``--service-mode``, or the deprecated ``--gapplication-service`` that
    home-manager passes to EasyEffects before 8, turns service mode on for that
    process, whoever started it (``src/command_line_parser.cpp``). EasyEffects
    also writes ``--service-mode`` into its own autostart entry when service
    mode is on (``src/autostart.cpp``). False when nothing runs or pgrep can't
    answer."""
    return any(name in ee_socket.PROCESS_NAMES and bool(_SERVICE_FLAGS & set(argv))
               for name, argv in (own_processes() if processes is None
                                  else processes))
