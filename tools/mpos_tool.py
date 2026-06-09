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

# ─── Build targets / chip types / flash sizes ────────────────────────────────

BUILD_TARGETS: list[tuple[str, str]] = [
    ("ESP32-S3  SPIRAM-OCT  16 MB  [esp32s3]",    "esp32s3"),
    ("ESP32     SPIRAM      16 MB  [esp32]",       "esp32"),
    ("ESP32     No-PSRAM     4 MB  [esp32-small]", "esp32-small"),
    ("unPhone               [unphone]",            "unphone"),
    ("LilyGO T4             [lilygo_t4]",          "lilygo_t4"),
    ("Linux desktop         [unix]",               "unix"),
    ("macOS desktop         [macos]",              "macos"),
]

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
    if _OS in ("Linux", "Darwin"):
        return ["bash", str(BUILD_SCRIPT), target]
    if _is_wsl_available():
        wsl_script = _windows_to_wsl(BUILD_SCRIPT)
        wsl_root   = _windows_to_wsl(REPO_ROOT)
        return ["wsl.exe", "bash", "-c",
                f"cd '{wsl_root}' && bash '{wsl_script}' {target}"]
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
    """
    results: list[tuple[str, str]] = []

    if MERGED_BIN_DIR.is_dir():
        for p in sorted(MERGED_BIN_DIR.glob("*.bin")):
            results.append((f"[merged]  {p.name}", str(p)))

    if ESP32_BUILD.is_dir():
        for p in sorted(ESP32_BUILD.glob("build-*/micropython.bin")):
            results.append((f"[app]     {p.parent.name}", str(p)))

    for p in sorted(REPO_ROOT.glob("*.bin")):
        results.append((f"[root]    {p.name}", str(p)))

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
                yield Select(BUILD_TARGETS, value="esp32s3", id="build-target")
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
        t0 = time.monotonic()
        rc = await self._run(build_command(target))
        elapsed = time.monotonic() - t0
        if rc == 0:
            self._ok(f"Build finished in {elapsed:.0f}s.")
            self._refresh_ports_and_fw()
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
        t0 = time.monotonic()
        rc = await self._run(build_command(target))
        elapsed = time.monotonic() - t0
        if rc != 0:
            self._error(f"Build failed (exit {rc}) after {elapsed:.0f}s.")
            self._state = _S.IDLE
            return
        self._ok(f"Build finished in {elapsed:.0f}s — starting flash…")
        self._refresh_ports_and_fw()

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

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=REPO_ROOT,
            )
        except FileNotFoundError as exc:
            self._error(f"Command not found: {exc}")
            return 127

        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n\r")
            log.write(_colorize(line))
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
        else:
            fw_sel.set_options([])
        self._info(f"Firmware: {len(files)} file(s) found.")

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
