#!/usr/bin/env python3
"""
MicroPythonOS Build & Flash Tool
Cross-platform TUI for building and flashing MicroPythonOS firmware.

Usage:
    python tools/mpos_tool.py          # launch TUI
    python tools/mpos_tool.py --help   # show help

Platform support:
    Linux / macOS  — full build + flash
    Windows        — flash only  (build requires WSL; with WSL: full support)

Dependencies (auto-installed on first run):
    pip install textual pyserial esptool
"""

from __future__ import annotations

# ─── Bootstrap: auto-install missing dependencies ────────────────────────────

import subprocess
import sys
import os


def _bootstrap() -> None:
    needed: list[str] = []
    for pip_name, import_name in [
        ("textual>=0.40.0", "textual"),
        ("pyserial",        "serial"),
        ("esptool",         "esptool"),
    ]:
        try:
            __import__(import_name)
        except ImportError:
            needed.append(pip_name)

    if not needed:
        return

    print("\n  MicroPythonOS Tool — missing dependencies:")
    for n in needed:
        print(f"    • {n}")
    ans = input("\n  Install now with pip? [Y/n] ").strip().lower()
    if ans in ("", "y", "yes"):
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet"] + needed
        )
        os.execv(sys.executable, [sys.executable] + sys.argv)
    else:
        print("  Aborted.  Run:  pip install textual pyserial esptool")
        sys.exit(1)


_bootstrap()

# ─── Standard library ────────────────────────────────────────────────────────

import asyncio
import os
import platform
import re
import time
from pathlib import Path

# ─── Third-party ─────────────────────────────────────────────────────────────

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    Button, Footer, Header, Label, ProgressBar, RichLog,
    Rule, Select, Static,
)

try:
    import serial.tools.list_ports as _serial_ports
    _HAS_SERIAL = True
except ImportError:
    _HAS_SERIAL = False

# ─── Paths ───────────────────────────────────────────────────────────────────

def _find_repo_root() -> Path:
    """Walk up from this file until we find the repo root (has scripts/build_mpos.sh)."""
    candidate = Path(__file__).resolve().parent
    for _ in range(6):
        if (candidate / "scripts" / "build_mpos.sh").exists():
            return candidate
        candidate = candidate.parent
    raise RuntimeError(
        "Cannot find MicroPythonOS repo root.\n"
        "Run this tool from within the cloned repository."
    )


REPO_ROOT      = _find_repo_root()
BUILD_SCRIPT   = REPO_ROOT / "scripts" / "build_mpos.sh"
LVGL_DIR       = REPO_ROOT / "lvgl_micropython"
MERGED_BIN_DIR = LVGL_DIR / "build"
ESP32_BUILD    = LVGL_DIR / "lib" / "micropython" / "ports" / "esp32"


def _strip_file(p: Path) -> int:
    """Strip \\r from a single file. Returns 1 if modified, 0 otherwise."""
    try:
        raw = p.read_bytes()
        if b"\r" in raw:
            p.write_bytes(raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
            return 1
    except OSError:
        pass
    return 0


# Map patch filename → working directory used when applying the patch
# (must match the pushd targets in scripts/build_mpos.sh).
_PATCH_WORKDIRS: dict[str, Path] = {
    "imgfont_set_range.patch":          LVGL_DIR / "lib" / "lvgl",
    "esp32_uart_repl_runtime.patch":    LVGL_DIR / "lib" / "micropython",
    "esp32_inisetup_warn_and_format.patch": LVGL_DIR / "lib" / "micropython",
}


def _fix_patch_targets(patch_file: Path, workdir: Path) -> int:
    """
    Parse *patch_file* for '+++ b/…' lines and strip CRLF from every
    file they reference (resolved relative to *workdir*).
    Patch is applied with -p1, so the leading 'b/' component is stripped.
    """
    fixed = 0
    try:
        for raw_line in patch_file.read_bytes().split(b"\n"):
            line = raw_line.rstrip(b"\r").decode("utf-8", errors="replace")
            if not line.startswith("+++ "):
                continue
            path_part = line[4:]                       # drop '+++ '
            if path_part.startswith("b/"):
                path_part = path_part[2:]              # strip -p1 prefix
            elif path_part.startswith("a/"):
                continue                               # unusual; skip
            path_part = path_part.split("\t")[0].strip()
            if path_part in ("/dev/null", ""):
                continue
            target = workdir / path_part
            if target.exists():
                fixed += _strip_file(target)
    except OSError:
        pass
    return fixed


def _is_windows_fs(repo_root: Path) -> bool:
    """Return True when the repo lives on a Windows NTFS filesystem.

    This covers both native Windows Python and WSL paths (/mnt/c/…, /mnt/d/…).
    """
    return _OS == "Windows" or str(repo_root).startswith("/mnt/")


def _fix_crlf(repo_root: Path) -> int:
    """
    Strip Windows carriage returns (\\r) from shell scripts, patch files,
    the C/H source files those patches target, and — on Windows filesystems —
    the ESP-IDF submodule's shell scripts.  Returns the count of files that
    were actually modified.

    This is necessary when the repository lives on a Windows NTFS filesystem
    (e.g. /mnt/c/… in WSL): git on Windows checks out text files with CRLF
    line endings.  Shell scripts with CRLF can't be executed by bash; patch
    files with CRLF cause 'different line endings' hunk failures when their
    target C files also have CRLF.  ESP-IDF's own install/export scripts fail
    with '/usr/bin/env: bash\\r: No such file or directory'.
    """
    fixed = 0

    # ── Shell scripts in scripts/ ────────────────────────────────────────────
    for p in (repo_root / "scripts").glob("*.sh"):
        fixed += _strip_file(p)

    # ── Patch files + their C/H targets ─────────────────────────────────────
    lvgl_dir = repo_root / "lvgl_micropython"
    for patch_file in lvgl_dir.glob("*.patch"):
        fixed += _strip_file(patch_file)
        workdir = _PATCH_WORKDIRS.get(patch_file.name)
        if workdir and workdir.is_dir():
            fixed += _fix_patch_targets(patch_file, workdir)

    # ── ESP-IDF + submodule shell scripts (Windows NTFS only) ────────────────
    # git on Windows checks out ALL text files with CRLF, including every .sh
    # inside lvgl_micropython/lib/esp-idf and lvgl_micropython/lib/micropython.
    # We use esp-idf/install.sh as a canary: if it still has CRLF we do a full
    # recursive sweep; subsequent builds are O(1) because the canary is clean.
    if _is_windows_fs(repo_root):
        canary = lvgl_dir / "lib" / "esp-idf" / "install.sh"
        lib_dir = lvgl_dir / "lib"
        if canary.exists() and lib_dir.is_dir():
            try:
                if b"\r" in canary.read_bytes():
                    for p in lib_dir.rglob("*.sh"):
                        fixed += _strip_file(p)
            except OSError:
                pass

    return fixed

# ─── Build targets / chip types / flash sizes ────────────────────────────────

BUILD_TARGETS: list[tuple[str, str]] = [
    # ── ESP32-S3 boards ──────────────────────────────────────────────────────
    ("Spotpear ESP32-S3-N16R8  1.28\" Round LCD",  "spotpear"),
    ("ESP32-S3  SPIRAM-OCT  16 MB  [generic]",     "esp32s3"),
    ("unPhone               [unphone]",             "unphone"),
    # ── ESP32 boards ─────────────────────────────────────────────────────────
    ("ESP32     SPIRAM      16 MB  [esp32]",        "esp32"),
    ("ESP32     No-PSRAM     4 MB  [esp32-small]",  "esp32-small"),
    ("LilyGO T4             [lilygo_t4]",           "lilygo_t4"),
    # ── Desktop ──────────────────────────────────────────────────────────────
    ("Linux desktop         [unix]",                "unix"),
    ("macOS desktop         [macos]",               "macos"),
]

# Map TUI target IDs → build_mpos.sh target names
# (multiple TUI entries can share the same script target)
_TARGET_SCRIPT_MAP: dict[str, str] = {
    "spotpear": "esp32s3",   # same binary; board detected at runtime
}

CHIP_TYPES: list[tuple[str, str]] = [
    ("ESP32-S3  [esp32s3]", "esp32s3"),
    ("ESP32     [esp32]",   "esp32"),
    ("ESP32-S2  [esp32s2]", "esp32s2"),
    ("ESP32-C3  [esp32c3]", "esp32c3"),
    ("Auto-detect",         "auto"),
]

FLASH_SIZES: list[tuple[str, str]] = [
    ("16 MB", "16MB"),
    (" 8 MB",  "8MB"),
    (" 4 MB",  "4MB"),
]

# Target name → substring found in merged binary filenames
_TARGET_BIN_TAG: dict[str, str] = {
    "spotpear":   "S3-SPIRAM_OCT",   # same binary as esp32s3
    "esp32s3":    "S3-SPIRAM_OCT",
    "esp32":      "GENERIC-SPIRAM",
    "esp32-small":"GENERIC-4",
    "unphone":    "unphone",
    "lilygo_t4":  "lilygo",
}

# ─── Platform helpers ────────────────────────────────────────────────────────

_OS = platform.system()   # "Linux" | "Darwin" | "Windows"


def _is_wsl_available() -> bool:
    if _OS != "Windows":
        return False
    try:
        r = subprocess.run(
            ["wsl.exe", "bash", "-c", "echo ok"],
            capture_output=True, timeout=6, text=True,
        )
        return r.returncode == 0 and "ok" in r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _windows_to_wsl(path: Path) -> str:
    """Convert  C:\\foo\\bar  →  /mnt/c/foo/bar"""
    drive = path.drive.rstrip(":").lower()
    rest  = str(path).replace(str(path.drive), "").replace("\\", "/")
    return f"/mnt/{drive}{rest}"


def build_command(target: str) -> list[str] | None:
    """Return the command list to invoke build_mpos.sh, or None if impossible."""
    # Resolve any TUI-specific alias to the actual script target
    script_target = _TARGET_SCRIPT_MAP.get(target, target)
    if _OS in ("Linux", "Darwin"):
        return ["bash", str(BUILD_SCRIPT), script_target]
    if _is_wsl_available():
        wsl_script = _windows_to_wsl(BUILD_SCRIPT)
        wsl_root   = _windows_to_wsl(REPO_ROOT)
        # BASHOPTS=igncr is set via the environment (see _run); pass it here
        # too so the wsl.exe-spawned bash inherits it from the command line.
        cmd = (
            f"cd '{wsl_root}' && "
            f"BASHOPTS=igncr bash '{wsl_script}' {script_target}"
        )
        return ["wsl.exe", "bash", "-c", cmd]
    return None


def find_serial_ports() -> list[tuple[str, str]]:
    """Return [(device, description)] for all available serial ports."""
    if _HAS_SERIAL:
        return [
            (p.device, p.description or p.device)
            for p in _serial_ports.comports()
        ]
    # Fallback without pyserial
    import glob as _g
    if _OS == "Windows":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DEVICEMAP\SERIALCOMM",
            )
            devices, i = [], 0
            while True:
                try:
                    devices.append(winreg.EnumValue(key, i)[1]); i += 1
                except OSError:
                    break
            return [(d, d) for d in sorted(devices)]
        except Exception:
            return []
    elif _OS == "Darwin":
        devs = _g.glob("/dev/cu.usbserial-*") + _g.glob("/dev/cu.SLAB*")
    else:
        devs = _g.glob("/dev/ttyUSB*") + _g.glob("/dev/ttyACM*")
    return [(d, d) for d in sorted(devs)]


def find_firmware_files() -> list[tuple[str, str]]:
    """
    Return [(label, abs_path)] for flashable .bin files.
    Merged images (address 0x0) are listed first, then individual app bins.

    Searched locations (in order):
      1. lvgl_micropython/build/*.bin           — merged images from make.py
      2. lvgl_micropython/lib/…/esp32/build-*   — board build dirs:
           • micropython.bin     (app-only binary)
           • *.bin ≥ 256 KB      (merged/combined images produced in same dir)
      3. <repo_root>/*.bin                      — output of make_image.sh
    """
    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(label: str, path: Path) -> None:
        key = str(path.resolve())
        if key not in seen and path.is_file():
            seen.add(key)
            results.append((label, str(path)))

    # 1 ── Merged images in lvgl_micropython/build/ ───────────────────────────
    #      make.py writes lvgl_micropy_<BOARD>-<VARIANT>-<SIZE>.bin here
    if MERGED_BIN_DIR.is_dir():
        for p in sorted(MERGED_BIN_DIR.glob("*.bin")):
            _add(f"[merged]  {p.name}", p)

    # 2 ── Board-specific ESP-IDF build directories ───────────────────────────
    if ESP32_BUILD.is_dir():
        for build_dir in sorted(ESP32_BUILD.glob("build-*")):
            if not build_dir.is_dir():
                continue
            # app-only binary (flash at 0x20000)
            _add(f"[app]     {build_dir.name}", build_dir / "micropython.bin")
            # merged binaries sitting directly in the build dir (≥ 256 KB to
            # avoid bootloader.bin, partition-table.bin, etc.)
            for p in sorted(build_dir.glob("*.bin")):
                if p.name == "micropython.bin":
                    continue
                try:
                    if p.stat().st_size >= 256 * 1024:
                        _add(f"[merged]  {p.name}", p)
                except OSError:
                    pass

    # 3 ── Any .bin at repo root (make_image.sh output) ───────────────────────
    for p in sorted(REPO_ROOT.glob("*.bin")):
        _add(f"[root]    {p.name}", p)

    return results


def find_esptool_python() -> str:
    """Return a Python executable that has esptool installed."""
    # Try the current interpreter first
    try:
        subprocess.run(
            [sys.executable, "-m", "esptool", "version"],
            capture_output=True, timeout=5, check=True,
        )
        return sys.executable
    except Exception:
        pass
    # Try ESP-IDF venv
    import glob as _g
    pat = (
        str(Path.home() / ".espressif" / "python_env" / "*" / "Scripts" / "python.exe")
        if _OS == "Windows"
        else str(Path.home() / ".espressif" / "python_env" / "*" / "bin" / "python")
    )
    for py in sorted(_g.glob(pat)):
        try:
            subprocess.run([py, "-m", "esptool", "version"],
                           capture_output=True, timeout=5, check=True)
            return py
        except Exception:
            continue
    return sys.executable   # fallback


def _colorize(line: str) -> Text:
    """Apply Rich styling to a single output line."""
    low = line.lower()
    if any(k in low for k in ("error:", "error !", "cmake error", " failed")):
        return Text(line, style="bold red")
    if any(k in low for k in ("warning:", "warn ")):
        return Text(line, style="yellow")
    if any(k in low for k in ("writing at", "erasing flash", "hash of data")):
        return Text(line, style="cyan")
    if any(k in low for k in ("successfully", "done.", "finished", "hard reset")):
        return Text(line, style="bold green")
    if line.startswith("+"):   # bash set -x trace
        return Text(line, style="dim")
    return Text(line)


# ─── App CSS ─────────────────────────────────────────────────────────────────

_CSS = """
Screen { background: $surface; }

#layout { height: 1fr; }

#sidebar {
    width: 38;
    min-width: 38;
    padding: 0 1;
    border-right: solid $primary-darken-2;
    overflow-y: auto;
}
#sidebar Label { color: $text-muted; margin-top: 1; }
.section  { color: $accent; text-style: bold; margin-top: 1; }
#sidebar Button { width: 100%; margin-top: 1; }

#btn-build-flash {
    background: $success-darken-1;
    color: $text;
    border: tall $success;
}
#btn-build-flash:hover  { background: $success; }
#btn-build-flash:disabled {
    background: $surface;
    color: $text-disabled;
    border: tall $surface-lighten-2;
}

#port-row        { height: 3; margin-top: 1; }
#port-row Select { width: 1fr; }
#btn-refresh     { width: 5; min-width: 5; margin: 0 0 0 1; }

#main-panel { width: 1fr; padding: 0 1; }
#statusbar  { height: 1; margin: 1 0; color: $text-muted; }
#status-dot { width: 3; }
#status-text{ width: 1fr; }

#progress {
    margin: 0 0 1 0;
    display: none;
}
#log {
    height: 1fr;
    border: solid $primary-darken-3;
    background: $surface-darken-1;
    padding: 0 1;
}
"""

# ─── State constants ─────────────────────────────────────────────────────────

class _S:
    IDLE        = "idle"
    BUILDING    = "building"
    FLASHING    = "flashing"
    BUILD_FLASH = "build+flash"


# ─── App ─────────────────────────────────────────────────────────────────────

class MposTool(App[None]):
    """MicroPythonOS Build & Flash Tool."""

    TITLE = "MicroPythonOS Build & Flash Tool"
    CSS   = _CSS

    BINDINGS = [
        Binding("b",      "build",       "Build"),
        Binding("f",      "flash",       "Flash"),
        Binding("r",      "refresh",     "Refresh"),
        Binding("ctrl+l", "clear_log",   "Clear log"),
        Binding("q",      "quit",        "Quit"),
    ]

    _state: reactive[str] = reactive(_S.IDLE)

    # ── Compose ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Horizontal(id="layout"):
            # ── Left sidebar ─────────────────────────────────────────────
            with Vertical(id="sidebar"):
                yield Static("▶  BUILD", classes="section")
                yield Label("Target")
                yield Select(
                    BUILD_TARGETS,
                    value="spotpear",
                    id="build-target",
                )
                yield Button("▶  Build", id="btn-build", variant="primary")

                yield Rule()

                yield Static("⚡  FLASH", classes="section")
                yield Label("Serial port")
                with Horizontal(id="port-row"):
                    yield Select([], id="flash-port", prompt="— scanning… —")
                    yield Button("↺", id="btn-refresh", tooltip="Refresh ports & firmware")

                yield Label("Firmware")
                yield Select([], id="firmware-file", prompt="— no firmware found —")

                yield Label("Chip")
                yield Select(CHIP_TYPES, value="esp32s3", id="chip-type")

                yield Label("Flash size")
                yield Select(FLASH_SIZES, value="16MB", id="flash-size")

                yield Button("⚡  Flash", id="btn-flash", variant="warning")

                yield Rule()
                yield Button("▶⚡  Build + Flash", id="btn-build-flash")

            # ── Main panel ───────────────────────────────────────────────
            with Vertical(id="main-panel"):
                with Horizontal(id="statusbar"):
                    yield Static("●", id="status-dot")
                    yield Static("Idle — ready.", id="status-text")
                yield ProgressBar(total=100, id="progress", show_eta=False)
                yield RichLog(id="log", highlight=False, markup=False, wrap=True)

        yield Footer()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._refresh_ports_and_fw()
        self._info(f"Repo   : {REPO_ROOT}")
        self._info(f"OS     : {_OS}")
        if _OS == "Windows" and not _is_wsl_available():
            self._warn("Building requires WSL — not found.  Flash-only mode active.")
        self._info("Select a target, then press ▶ Build, ⚡ Flash, or ▶⚡ Build + Flash.")

    # ── Reactive watcher ─────────────────────────────────────────────────────

    def watch__state(self, state: str) -> None:
        busy = state != _S.IDLE
        for btn_id in ("btn-build", "btn-flash", "btn-build-flash", "btn-refresh"):
            self.query_one(f"#{btn_id}", Button).disabled = busy

        dot  = self.query_one("#status-dot",  Static)
        text = self.query_one("#status-text", Static)
        prog = self.query_one("#progress",    ProgressBar)

        if state == _S.IDLE:
            dot.styles.color    = "green"
            text.update("Idle — ready.")
            prog.styles.display = "none"
        elif state == _S.BUILDING:
            dot.styles.color    = "yellow"
            text.update("Building…")
            prog.styles.display = "none"
        elif state == _S.FLASHING:
            dot.styles.color    = "cyan"
            text.update("Flashing…")
            prog.styles.display = "block"
        elif state == _S.BUILD_FLASH:
            dot.styles.color    = "yellow"
            text.update("Building…  (flash follows)")
            prog.styles.display = "none"

    # ── Button dispatch ──────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "btn-build":       self.action_build()
            case "btn-flash":       self.action_flash()
            case "btn-build-flash": self.action_build_flash()
            case "btn-refresh":     self.action_refresh()

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_build(self) -> None:
        if self._state != _S.IDLE:
            return
        target = self.query_one("#build-target", Select).value
        if target is Select.BLANK:
            self._error("Select a build target first.")
            return
        self._state = _S.BUILDING
        self._worker_build(str(target))

    def action_flash(self) -> None:
        if self._state != _S.IDLE:
            return
        self._state = _S.FLASHING
        self._worker_flash()

    def action_build_flash(self) -> None:
        if self._state != _S.IDLE:
            return
        target = self.query_one("#build-target", Select).value
        if target is Select.BLANK:
            self._error("Select a build target first.")
            return
        self._state = _S.BUILD_FLASH
        self._worker_build_and_flash(str(target))

    def action_refresh(self) -> None:
        self._refresh_ports_and_fw()

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()

    # ── Workers (async, run in the LVGL event loop) ───────────────────────────

    @work(exclusive=True)
    async def _worker_build(self, target: str) -> None:
        self._preflight_crlf()
        t0 = time.monotonic()
        self._mpy_cross_missing = False
        rc = await self._run(build_command(target))
        if rc != 0 and self._mpy_cross_missing:
            self._warn(
                "mpy-cross was not found — normal on the very first build "
                "(make.py builds it during compilation). Retrying…"
            )
            self._mpy_cross_missing = False
            rc = await self._run(build_command(target))
        elapsed = time.monotonic() - t0
        if rc == 0:
            self._ok(f"Build finished in {elapsed:.0f}s.")
            self._refresh_ports_and_fw()
            if not find_firmware_files():
                self._error(
                    "Build exited 0 but no firmware file was found. "
                    "Check the log above for ESP-IDF setup errors "
                    "(e.g. '/usr/bin/env: bash\\r: No such file or directory'). "
                    "Run the build again — CRLF in ESP-IDF scripts has now been fixed."
                )
        else:
            self._error(f"Build failed (exit {rc}) after {elapsed:.0f}s.")
        self._state = _S.IDLE

    @work(exclusive=True)
    async def _worker_flash(self, prefer_target: str | None = None) -> None:
        rc = await self._flash_sequence(prefer_target)
        if rc == 0:
            self._ok("Flash complete — device is resetting…")
        else:
            self._error(f"Flash failed (exit {rc}).")
        self._state = _S.IDLE

    @work(exclusive=True)
    async def _worker_build_and_flash(self, target: str) -> None:
        # ── Phase 1: Build ────────────────────────────────────────────
        self._preflight_crlf()
        t0 = time.monotonic()
        self._mpy_cross_missing = False
        rc = await self._run(build_command(target))
        if rc != 0 and self._mpy_cross_missing:
            self._warn(
                "mpy-cross was not found — normal on the very first build. Retrying…"
            )
            self._mpy_cross_missing = False
            rc = await self._run(build_command(target))
        elapsed = time.monotonic() - t0
        if rc != 0:
            self._error(f"Build failed (exit {rc}) after {elapsed:.0f}s.")
            self._state = _S.IDLE
            return
        self._ok(f"Build finished in {elapsed:.0f}s — starting flash…")
        self._refresh_ports_and_fw()
        if not find_firmware_files():
            self._error(
                "Build exited 0 but no firmware file was found. "
                "Check the log above for ESP-IDF setup errors. "
                "Run the build again — CRLF in ESP-IDF scripts has now been fixed."
            )
            self._state = _S.IDLE
            return

        # ── Phase 2: Flash ────────────────────────────────────────────
        self._state = _S.FLASHING   # triggers watch__state → cyan dot + progress bar
        rc = await self._flash_sequence(prefer_target=target)
        if rc == 0:
            self._ok("Flash complete — device is resetting…")
        else:
            self._error(f"Flash failed (exit {rc}).")
        self._state = _S.IDLE

    # ── Core subprocess runner ────────────────────────────────────────────────

    async def _run(self, cmd: list[str] | None) -> int:
        """Execute *cmd*, stream stdout/stderr to the log, return exit code."""
        if cmd is None:
            self._error(
                "Building is not supported on this OS. "
                "Install WSL (Windows Subsystem for Linux) to enable it."
            )
            return 1

        self._step(f"$ {' '.join(cmd)}")
        log  = self.query_one("#log",      RichLog)
        prog = self.query_one("#progress", ProgressBar)

        # BASHOPTS=igncr makes bash (and every bash child process) silently
        # ignore \r characters in scripts.  This is essential when the repo
        # lives on a Windows NTFS filesystem (e.g. accessed via WSL at
        # /mnt/c/…) where git checks out shell scripts with CRLF line endings.
        env = {**os.environ, "BASHOPTS": "igncr"}

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=REPO_ROOT,
                env=env,
            )
        except FileNotFoundError as exc:
            self._error(f"Command not found: {exc}")
            return 127

        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n\r")
            log.write(_colorize(line))
            # Detect first-build mpy-cross absence so workers can auto-retry
            low = line.lower()
            if "mpy-cross" in low and ("not found" in low or "no such file" in low):
                self._mpy_cross_missing = True
            # Parse esptool flash progress  "Writing at 0x… (42 %)"
            m = re.search(r"\((\d+)\s*%\)", line)
            if m:
                prog.update(progress=int(m.group(1)))

        await proc.wait()
        return proc.returncode

    # ── Flash sequence ───────────────────────────────────────────────────────

    async def _flash_sequence(self, prefer_target: str | None = None) -> int:
        port  = self.query_one("#flash-port",    Select).value
        fw    = self.query_one("#firmware-file", Select).value
        chip  = self.query_one("#chip-type",     Select).value
        fsize = self.query_one("#flash-size",    Select).value

        if port is Select.BLANK:
            self._error("No serial port selected — connect the device and press ↺.")
            return 1

        if fw is Select.BLANK:
            fw = self._pick_firmware(prefer_target)
            if fw is None:
                self._error("No firmware file found. Build first or select manually.")
                return 1
            self._info(f"Auto-selected firmware: {Path(str(fw)).name}")

        fw_path = str(fw)
        # Merged images start at 0x0; a raw micropython.bin goes to 0x20000
        if fw_path.endswith("micropython.bin"):
            flash_addr = "0x20000"
            self._warn(
                "Flashing app binary only (0x20000). "
                "Bootloader + partition table must already be present on the device."
            )
        else:
            flash_addr = "0x0"

        esptool_py = find_esptool_python()
        cmd = [
            esptool_py, "-m", "esptool",
            "--chip",   str(chip),
            "--port",   str(port),
            "--before", "default_reset",
            "--after",  "hard_reset",
            "write_flash",
            "--flash_mode", "dio",
            "--flash_size", str(fsize),
            "--flash_freq", "80m",
            flash_addr, fw_path,
        ]

        self._step(f"Flashing {Path(fw_path).name}  →  {port}")
        self.query_one("#progress", ProgressBar).update(progress=0)
        return await self._run(cmd)

    def _pick_firmware(self, prefer_target: str | None) -> str | None:
        files = find_firmware_files()
        if not files:
            return None
        if prefer_target:
            tag = _TARGET_BIN_TAG.get(prefer_target, "")
            # Prefer merged binary matching the target
            for label, path in files:
                if "[merged]" in label and tag and tag.lower() in path.lower():
                    return path
            for label, path in files:
                if "[merged]" in label:
                    return path
        return files[0][1]

    # ── Pre-flight checks ─────────────────────────────────────────────────────

    def _preflight_crlf(self) -> None:
        """Fix CRLF line endings in scripts, patches, and ESP-IDF submodule before a build."""
        # Warn the user before the sweep in case it takes a moment on NTFS
        if _is_windows_fs(REPO_ROOT):
            canary = LVGL_DIR / "lib" / "esp-idf" / "install.sh"
            if canary.exists():
                try:
                    if b"\r" in canary.read_bytes():
                        self._warn(
                            "Windows filesystem detected — fixing CRLF in ESP-IDF scripts. "
                            "This only happens once and may take a few seconds…"
                        )
                except OSError:
                    pass
        n = _fix_crlf(REPO_ROOT)
        if n:
            self._warn(
                f"Fixed CRLF→LF in {n} file(s) (scripts, patches, ESP-IDF). "
                "(One-time correction for Windows checkout.)"
            )

    # ── UI helpers ───────────────────────────────────────────────────────────

    def _refresh_ports_and_fw(self) -> None:
        # Ports
        ports = find_serial_ports()
        port_sel = self.query_one("#flash-port", Select)
        if ports:
            port_sel.set_options(
                [(f"{dev}  [{desc}]" if desc != dev else dev, dev)
                 for dev, desc in ports]
            )
        else:
            port_sel.set_options([])
        self._info(f"Ports: {len(ports)} found.")

        # Firmware
        files = find_firmware_files()
        fw_sel = self.query_one("#firmware-file", Select)
        if files:
            fw_sel.set_options(files)
            fw_sel.value = files[0][1]
            self._info(f"Firmware: {len(files)} file(s) found.")
        else:
            fw_sel.set_options([])
            self._warn(
                "Firmware: 0 files found.  "
                f"Searched: {MERGED_BIN_DIR}  |  {ESP32_BUILD / 'build-*'}  |  {REPO_ROOT / '*.bin'}"
            )

    # ── Log helpers (safe to call from async workers or sync handlers) ────────

    def _info(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.query_one("#log", RichLog).write(Text(f"[{ts}] {msg}", style="dim"))

    def _step(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(Text(f"\n▶ {msg}\n", style="bold blue"))

    def _ok(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(Text(f"✓ {msg}", style="bold green"))

    def _warn(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(Text(f"⚠ {msg}", style="yellow"))

    def _error(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(Text(f"✗ {msg}", style="bold red"))


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="MicroPythonOS Build & Flash Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="print available serial ports and exit",
    )
    parser.add_argument(
        "--list-firmware",
        action="store_true",
        help="print found firmware files and exit",
    )
    args = parser.parse_args()

    if args.list_ports:
        ports = find_serial_ports()
        print("Serial ports:")
        for dev, desc in ports:
            print(f"  {dev:<22} {desc}")
        if not ports:
            print("  (none found)")
        return

    if args.list_firmware:
        files = find_firmware_files()
        print("Firmware files:")
        for label, path in files:
            print(f"  {label:<52} {path}")
        if not files:
            print("  (none found — run a build first)")
        return

    MposTool().run()


if __name__ == "__main__":
    main()
