"""Build the app icon and install a self-elevating Desktop shortcut.

The shortcut runs pythonw.exe from the project's virtual environment, so no
console window ever appears, and carries the RunAs flag so double-clicking it
always starts Popup Stopper with administrator rights through a single UAC
prompt.

Re-run this any time to refresh the icon or the shortcut:
    .venv\\Scripts\\python.exe scripts\\install_shortcut.py
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = REPO_ROOT / "popupstopper" / "assets"
ICON_SVG = ASSETS_DIR / "icon.svg"
ICON_ICO = ASSETS_DIR / "icon.ico"
PYTHONW = REPO_ROOT / ".venv" / "Scripts" / "pythonw.exe"

ICON_SIZES = (256, 128, 64, 48, 40, 32, 24, 20, 16)


def build_icon() -> Path:
    """Render icon.svg into a multi-resolution .ico.

    Windows picks a different size for the tray, the taskbar and Explorer, so
    packing real renders at each size looks far sharper than shipping one big
    image and letting Windows downscale it.
    """
    from PySide6.QtCore import QBuffer, QSize, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    if not ICON_SVG.exists():
        raise FileNotFoundError(f"Missing source SVG: {ICON_SVG}")

    if QGuiApplication.instance() is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _ = QGuiApplication(sys.argv)

    renderer = QSvgRenderer(str(ICON_SVG))
    if not renderer.isValid():
        raise RuntimeError(f"Qt could not read {ICON_SVG}")

    images: list[tuple[int, bytes]] = []
    for size in ICON_SIZES:
        image = QImage(QSize(size, size), QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        renderer.render(painter)
        painter.end()

        # QBuffer manages its own QByteArray here; passing a temporary one in
        # crashes once Python garbage-collects it out from under Qt.
        buffer = QBuffer()
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise RuntimeError(f"Could not encode the {size}px icon")
        images.append((size, bytes(buffer.data())))
        buffer.close()

    ICON_ICO.parent.mkdir(parents=True, exist_ok=True)
    ICON_ICO.write_bytes(_pack_ico(images))
    print(f"  wrote {ICON_ICO}  ({ICON_ICO.stat().st_size} bytes, {len(images)} sizes)")
    return ICON_ICO


def _pack_ico(images: list[tuple[int, bytes]]) -> bytes:
    """Assemble PNG renders into an ICO container (PNG entries, Vista+)."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = bytearray()
    payload = bytearray()
    for size, data in images:
        dimension = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset
        )
        payload += data
        offset += len(data)
    return bytes(header + entries + payload)


def _desktop_dir() -> Path:
    profile = Path(os.environ.get("USERPROFILE", str(Path.home())))
    for candidate in (profile / "OneDrive" / "Desktop", profile / "Desktop"):
        if candidate.is_dir():
            return candidate
    fallback = profile / "Desktop"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _start_menu_dir() -> Path:
    appdata = Path(os.environ.get("APPDATA", ""))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _write_shortcut(path: Path, icon_path: Path, target: Path) -> None:
    import gc

    import pythoncom  # type: ignore[import-not-found]
    from win32com.client import Dispatch  # type: ignore[import-not-found]

    pythoncom.CoInitialize()
    try:
        shell = Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(str(path))
        shortcut.TargetPath = str(target)
        shortcut.Arguments = "-m popupstopper"
        shortcut.WorkingDirectory = str(REPO_ROOT)
        shortcut.IconLocation = f"{icon_path},0"
        shortcut.WindowStyle = 7  # minimised; pythonw has no console anyway
        shortcut.Description = "Popup Stopper - track and block Windows popups"
        shortcut.Save()
        del shortcut
        del shell
        gc.collect()
    finally:
        pythoncom.CoUninitialize()

    _set_runas_flag(path)


def _set_runas_flag(lnk_path: Path) -> None:
    """Set the RunAsUser bit so Windows elevates on double-click.

    Per MS-SHLLINK the LinkFlags DWORD sits at offset 0x14; RunAsUser is bit 13,
    which lands on bit 5 of the byte at 0x15 in little-endian order.
    """
    data = bytearray(lnk_path.read_bytes())
    if len(data) < 0x18:
        raise RuntimeError(f"{lnk_path} is too small to be a shortcut")
    data[0x15] |= 0x20
    lnk_path.write_bytes(bytes(data))


def install_shortcuts(icon_path: Path) -> list[Path]:
    if not PYTHONW.exists():
        raise FileNotFoundError(
            f"pythonw.exe not found at {PYTHONW}.\n"
            "Run .\\run.ps1 once to create the virtual environment, then try again."
        )

    written: list[Path] = []
    for folder in (_desktop_dir(), _start_menu_dir()):
        if not folder.is_dir():
            continue
        target = folder / "Popup Stopper.lnk"
        _write_shortcut(target, icon_path, PYTHONW)
        print(f"  wrote {target}")
        written.append(target)
    return written


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    print("Popup Stopper shortcut installer")
    print(f"  project:  {REPO_ROOT}")
    print(f"  pythonw:  {PYTHONW}  ({'found' if PYTHONW.exists() else 'MISSING'})")

    print("\n[1/2] Building the icon ...")
    icon = build_icon()

    print("\n[2/2] Writing shortcuts ...")
    shortcuts = install_shortcuts(icon)

    print("\nDone. Double-click this to start Popup Stopper:")
    for path in shortcuts:
        print(f"  {path}")
    print("\nThe shortcut asks for administrator rights so the app can trace")
    print("scheduled tasks and turn them on or off.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
