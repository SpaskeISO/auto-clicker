#!/usr/bin/env python3
"""Image-based auto-clicker with portable Linux screenshot/click backends."""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

THRESHOLD = 0.9
POLL_INTERVAL = 0.15  # seconds between scans when nothing matched
POST_CLICK_SLEEP = (0.4, 0.8)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}


@dataclass(frozen=True)
class Match:
    left: int
    top: int
    width: int
    height: int


@dataclass
class Template:
    path: Path
    folder: str
    image: np.ndarray


_active_screenshot_backend: str | None = None
_active_click_backend: str | None = None
_screenshot_fn = None
_mss_instance = None


def get_application_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _run(cmd: list[str], timeout: float = 15.0) -> None:
    subprocess.run(
        cmd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )


def _load_bgr(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"Failed to read image: {path}")
    return image


def _capture_with_tool(cmd: list[str], output: Path) -> np.ndarray | None:
    try:
        _run(cmd)
    except (OSError, subprocess.SubprocessError):
        return None
    if not output.is_file() or output.stat().st_size == 0:
        return None
    try:
        return _load_bgr(output)
    except RuntimeError:
        return None


def _grab_mss() -> np.ndarray:
    global _mss_instance
    import mss

    if _mss_instance is None:
        _mss_instance = mss.mss()
    shot = _mss_instance.grab(_mss_instance.monitors[0])
    return np.ascontiguousarray(np.array(shot)[:, :, :3])


def _make_cli_grabber(name: str, build_cmd) -> Callable[[], np.ndarray]:
    tmp_dir = tempfile.mkdtemp(prefix="auto-clicker-")
    output = Path(tmp_dir) / "screenshot.png"

    def grab() -> np.ndarray:
        output.unlink(missing_ok=True)
        frame = _capture_with_tool(build_cmd(output), output)
        if frame is None:
            raise RuntimeError(f"{name} screenshot failed")
        return frame

    return grab


def _frame_looks_valid(frame: np.ndarray) -> bool:
    # Reject empty / all-black captures (common mss failure on Wayland).
    return frame is not None and frame.size > 0 and float(frame.std()) > 1.0


def _on_wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE") == "wayland" or "WAYLAND_DISPLAY" in os.environ


def _probe_backends() -> tuple[str, Callable[[], np.ndarray]]:
    """Pick the fastest working backend once, then reuse it."""
    candidates: list[tuple[str, Callable[[], np.ndarray]]] = []

    # Fast CLI tools before heavy GUI apps like spectacle
    if _which("grim"):
        candidates.append(("grim", _make_cli_grabber("grim", lambda p: ["grim", str(p)])))
    if _which("maim"):
        candidates.append(("maim", _make_cli_grabber("maim", lambda p: ["maim", str(p)])))
    if _which("scrot"):
        candidates.append(
            ("scrot", _make_cli_grabber("scrot", lambda p: ["scrot", "-z", "-o", str(p)]))
        )
    if _which("import"):
        candidates.append(
            (
                "import",
                _make_cli_grabber("import", lambda p: ["import", "-window", "root", str(p)]),
            )
        )
    if _which("spectacle"):
        candidates.append(
            (
                "spectacle",
                _make_cli_grabber(
                    "spectacle", lambda p: ["spectacle", "-b", "-n", "-o", str(p)]
                ),
            )
        )
    if _which("gnome-screenshot"):
        candidates.append(
            (
                "gnome-screenshot",
                _make_cli_grabber(
                    "gnome-screenshot", lambda p: ["gnome-screenshot", "-f", str(p)]
                ),
            )
        )

    # mss is fast but unreliable on pure Wayland — try last there, first on X11
    if _on_wayland():
        candidates.append(("mss", _grab_mss))
    else:
        candidates.insert(0, ("mss", _grab_mss))

    for name, fn in candidates:
        try:
            frame = fn()
            if _frame_looks_valid(frame):
                return name, fn
        except Exception:
            continue

    raise RuntimeError(
        "Unable to capture the screen. Install one of: grim, maim, scrot, "
        "ImageMagick (import), spectacle, gnome-screenshot, or use X11 with mss."
    )


def grab_screenshot() -> np.ndarray:
    global _active_screenshot_backend, _screenshot_fn
    if _screenshot_fn is None:
        name, fn = _probe_backends()
        _active_screenshot_backend = name
        _screenshot_fn = fn
        print(f"Using screenshot backend: {name}")
    return _screenshot_fn()


def find_on_screen(template: np.ndarray, screenshot: np.ndarray, threshold: float) -> Match | None:
    if template.shape[0] > screenshot.shape[0] or template.shape[1] > screenshot.shape[1]:
        return None

    result = cv2.matchTemplate(screenshot, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    if max_val < threshold:
        return None

    height, width = template.shape[:2]
    return Match(left=max_loc[0], top=max_loc[1], width=width, height=height)


_click_fn: Callable[[int, int], None] | None = None
_xlib_display = None


def _click_xlib(x: int, y: int) -> None:
    global _xlib_display
    from Xlib import X
    from Xlib.display import Display
    from Xlib.ext.xtest import fake_input

    if _xlib_display is None:
        _xlib_display = Display()
    d = _xlib_display
    d.screen().root.warp_pointer(x, y)
    d.sync()
    fake_input(d, X.ButtonPress, 1)
    d.sync()
    fake_input(d, X.ButtonRelease, 1)
    d.sync()


def _click_xdotool(x: int, y: int) -> None:
    # No --sync: that waits for the pointer and is very slow on Wayland/XWayland.
    _run(["xdotool", "mousemove", str(x), str(y), "click", "1"], timeout=2.0)


def _click_ydotool(x: int, y: int) -> None:
    _run(["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)], timeout=2.0)
    _run(["ydotool", "click", "0xC0"], timeout=2.0)


def _click_pynput(x: int, y: int) -> None:
    from pynput.mouse import Button, Controller

    mouse = Controller()
    mouse.position = (x, y)
    mouse.click(Button.left, 1)


def _click_pyautogui(x: int, y: int) -> None:
    import pyautogui

    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0
    pyautogui.moveTo(x, y, duration=0)
    pyautogui.click()


def _probe_click_backend() -> tuple[str, Callable[[int, int], None]]:
    # On Wayland, Xlib/XTest only talk to XWayland and usually do not move the real cursor.
    candidates: list[tuple[str, Callable[[int, int], None]]] = []
    if _which("ydotool"):
        candidates.append(("ydotool", _click_ydotool))
    if _which("xdotool"):
        candidates.append(("xdotool", _click_xdotool))
    if not _on_wayland():
        candidates.insert(0, ("xlib", _click_xlib))
    else:
        candidates.append(("xlib", _click_xlib))
    candidates.append(("pynput", _click_pynput))
    candidates.append(("pyautogui", _click_pyautogui))

    errors: list[str] = []
    for name, fn in candidates:
        try:
            if name == "xlib":
                from Xlib.display import Display  # noqa: F401
            elif name == "pynput":
                from pynput.mouse import Controller  # noqa: F401
            elif name == "pyautogui":
                import pyautogui  # noqa: F401
            return name, fn
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    detail = "; ".join(errors) if errors else "none"
    raise RuntimeError(f"Unable to click. Tried: {detail}")


def click_at(x: int, y: int) -> None:
    global _active_click_backend, _click_fn
    if _click_fn is None:
        name, fn = _probe_click_backend()
        _active_click_backend = name
        _click_fn = fn
        print(f"Using click backend: {name}")
    _click_fn(x, y)


def load_templates(pictures_dir: Path) -> list[Template]:
    templates: list[Template] = []
    folders = sorted(
        p for p in pictures_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    for folder in folders:
        for path in sorted(folder.iterdir()):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                templates.append(
                    Template(path=path, folder=folder.name, image=_load_bgr(path))
                )
    return templates


def auto_clicker() -> None:
    pictures_dir = get_application_path() / "pictures"
    if not pictures_dir.is_dir():
        raise FileNotFoundError(f"Pictures directory not found: {pictures_dir}")

    templates = load_templates(pictures_dir)
    if not templates:
        raise FileNotFoundError(f"No template images found in {pictures_dir}")

    print(f"Working directory: {Path.cwd()}")
    print(f"Loaded {len(templates)} template(s)")

    while True:
        try:
            screenshot = grab_screenshot()
        except RuntimeError as exc:
            print(f"Screenshot failed: {exc}")
            time.sleep(1.0)
            continue

        matched = False
        for template in templates:
            match = find_on_screen(template.image, screenshot, THRESHOLD)
            if match is None:
                continue

            click_x = min(match.left + 5, match.left + match.width - 1)
            click_y = min(match.top + 5, match.top + match.height - 1)
            click_at(click_x, click_y)
            print(f"Image found: {template.path.name} at ({click_x}, {click_y})")
            matched = True
            time.sleep(random.uniform(*POST_CLICK_SLEEP))
            break

        if not matched:
            time.sleep(POLL_INTERVAL)


def main() -> None:
    try:
        auto_clicker()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
