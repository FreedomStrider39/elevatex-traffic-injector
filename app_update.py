#!/usr/bin/env python3
"""
In-app update check and installer for ElevateX Traffic Injector.

Uses only the standard library: the GitHub releases API for the latest
version, the release asset's SHA-256 digest for verification, and a
rename-swap-restart sequence that works on Windows while the app is running.

Nothing here touches MSFS or SimConnect; it is called from the dashboard.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from typing import Callable, Optional

# Bump this for every release. The release tag on GitHub must match it.
CURRENT_VERSION = "1.10"

REPO = "FreedomStrider39/elevatex-traffic-injector"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASE_PAGE = f"https://github.com/{REPO}/releases/latest"
ASSET_NAME = "ElevateXTrafficInjector.zip"
EXE_NAME = "ElevateXTrafficInjector.exe"
USER_AGENT = f"ElevateXTrafficInjector/{CURRENT_VERSION}"

CHUNK = 64 * 1024


class UpdateError(RuntimeError):
    """A user-presentable update failure."""


# ---------------------------------------------------------------------------
# version handling
# ---------------------------------------------------------------------------
def parse_version(text: str) -> tuple:
    """'v1.10.2' -> (1, 10, 2). Non-numeric suffixes are ignored."""
    text = (text or "").strip().lstrip("vV")
    parts = []
    for piece in text.replace("-", ".").replace("_", ".").split("."):
        digits = ""
        for ch in piece:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            parts.append(int(digits))
        else:
            break
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


# ---------------------------------------------------------------------------
# GitHub release lookup
# ---------------------------------------------------------------------------
def _get_json(url: str, timeout: float = 12.0) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise UpdateError(
                "GitHub rate limit reached - try again in a few minutes.") from e
        raise UpdateError(f"GitHub returned HTTP {e.code}.") from e
    except urllib.error.URLError as e:
        raise UpdateError(f"Cannot reach GitHub: {e.reason}") from e
    except (ValueError, OSError) as e:
        raise UpdateError(f"Bad response from GitHub: {e}") from e


def check_latest(timeout: float = 12.0) -> dict:
    """Return the latest published release as a flat dict.

    Keys: version, tag, name, body, url, page, asset_url, asset_size,
          asset_sha256 (may be None), published.
    """
    data = _get_json(API_LATEST, timeout=timeout)
    tag = data.get("tag_name") or data.get("name") or ""
    assets = data.get("assets") or []
    asset = next((a for a in assets if a.get("name") == ASSET_NAME), None)
    if asset is None:
        asset = next((a for a in assets
                      if str(a.get("name", "")).lower().endswith(".zip")), None)

    digest = (asset or {}).get("digest") or ""
    sha = None
    if isinstance(digest, str) and digest.lower().startswith("sha256:"):
        sha = digest.split(":", 1)[1].strip().lower()

    return {
        "version": tag.lstrip("vV"),
        "tag": tag,
        "name": data.get("name") or tag,
        "body": data.get("body") or "",
        "url": data.get("html_url") or RELEASE_PAGE,
        "page": data.get("html_url") or RELEASE_PAGE,
        "asset_url": (asset or {}).get("browser_download_url"),
        "asset_size": int((asset or {}).get("size") or 0),
        "asset_sha256": sha,
        "published": data.get("published_at") or "",
    }


# ---------------------------------------------------------------------------
# download + verify
# ---------------------------------------------------------------------------
def download_update(asset_url: str, dest_dir: str,
                    expected_size: int = 0,
                    expected_sha256: Optional[str] = None,
                    progress: Optional[Callable[[int, int], None]] = None,
                    cancel: Optional[Callable[[], bool]] = None) -> str:
    """Download the release zip into ``dest_dir`` and verify it.

    ``progress(done_bytes, total_bytes)`` is called roughly twice per second.
    ``cancel()`` returning True aborts with UpdateError('cancelled').
    Returns the local zip path.
    """
    if not asset_url:
        raise UpdateError("This release has no downloadable asset.")

    os.makedirs(dest_dir, exist_ok=True)
    part_path = os.path.join(dest_dir, ASSET_NAME + ".part")
    final_path = os.path.join(dest_dir, ASSET_NAME)

    req = urllib.request.Request(asset_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length") or 0) or expected_size
            done = 0
            hasher = hashlib.sha256()
            with open(part_path, "wb") as fh:
                while True:
                    if cancel and cancel():
                        raise UpdateError("cancelled")
                    block = resp.read(CHUNK)
                    if not block:
                        break
                    fh.write(block)
                    hasher.update(block)
                    done += len(block)
                    if progress:
                        progress(done, total)
    except UpdateError:
        _silent_remove(part_path)
        raise
    except urllib.error.HTTPError as e:
        _silent_remove(part_path)
        raise UpdateError(f"Download failed (HTTP {e.code}).") from e
    except urllib.error.URLError as e:
        _silent_remove(part_path)
        raise UpdateError(f"Download failed: {e.reason}") from e
    except OSError as e:
        _silent_remove(part_path)
        raise UpdateError(f"Could not save the download: {e}") from e

    if expected_size and done != expected_size:
        _silent_remove(part_path)
        raise UpdateError(
            f"Download is incomplete ({done} of {expected_size} bytes).")
    if expected_sha256 and hasher.hexdigest().lower() != expected_sha256:
        _silent_remove(part_path)
        raise UpdateError("Download verification failed (checksum mismatch).")

    os.replace(part_path, final_path)
    return final_path


# ---------------------------------------------------------------------------
# unpack
# ---------------------------------------------------------------------------
def extract_exe(zip_path: str, dest_dir: str) -> str:
    """Extract ElevateXTrafficInjector.exe from the release zip."""
    os.makedirs(dest_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            member = next((n for n in zf.namelist()
                           if os.path.basename(n).lower() == EXE_NAME.lower()),
                          None)
            if member is None:
                raise UpdateError("The update package does not contain the app.")
            out_path = os.path.join(dest_dir, EXE_NAME + ".new")
            with zf.open(member) as src, open(out_path, "wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
            return out_path
    except zipfile.BadZipFile as e:
        raise UpdateError("The update package is corrupted.") from e


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------
def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> str:
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def prepare_new_exe(zip_path: str) -> str:
    """Extract the new exe next to the running one so the swap is atomic.

    Falls back to the temp directory when the app folder is read-only; in that
    case the caller should show the folder to the user instead of swapping.
    """
    try:
        return extract_exe(zip_path, app_dir())
    except (OSError, PermissionError):
        return extract_exe(zip_path, tempfile.mkdtemp(prefix="elevatex_upd_"))


def install_and_restart(new_exe: str) -> None:
    """Swap the running exe for ``new_exe`` and launch the new build.

    Windows allows renaming a running executable, so: current -> .old,
    new -> current, launch, and the caller exits the app. The .old file is
    deleted on the next start by cleanup_old().
    """
    if not is_frozen():
        raise UpdateError("In-app install only works in the packaged app.")

    exe = os.path.abspath(sys.executable)
    if os.path.dirname(os.path.abspath(new_exe)) != os.path.dirname(exe):
        # Cross-volume move: copy next to the exe first.
        local = os.path.join(os.path.dirname(exe), EXE_NAME + ".new")
        shutil.copy2(new_exe, local)
        new_exe = local

    old = exe + ".old"
    _silent_remove(old)
    try:
        os.replace(exe, old)      # rename the running exe (allowed on Windows)
    except OSError as e:
        raise UpdateError(
            f"Could not rename the running app ({e.strerror or e}). "
            "Close any other copy and try again, or install manually.") from e
    try:
        os.replace(new_exe, exe)
    except Exception as e:
        os.replace(old, exe)      # put things back if the swap failed
        raise UpdateError(
            "Could not place the new build. The old version has been kept.") from e

    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True)
    except Exception as e:
        raise UpdateError(f"Could not restart the app: {e}") from e


def cleanup_old() -> list[str]:
    """Remove leftovers from a previous in-app update.

    Returns the paths that could not be removed yet (Windows keeps the
    previous EXE locked while the old process is still shutting down, so the
    caller retries a few seconds later). Safe to call always.
    """
    if not is_frozen():
        return []
    base = os.path.dirname(os.path.abspath(sys.executable))
    stuck: list[str] = []
    for name in (EXE_NAME + ".old", EXE_NAME + ".new"):
        path = os.path.join(base, name)
        if not os.path.exists(path):
            continue
        try:
            os.remove(path)
        except OSError:
            stuck.append(path)
    return stuck


def save_zip_for_manual_install(zip_path: str) -> str:
    """Copy the downloaded zip to Downloads and return its path."""
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    if not os.path.isdir(downloads):
        downloads = tempfile.gettempdir()
    target = os.path.join(downloads, ASSET_NAME)
    shutil.copy2(zip_path, target)
    return target


def open_folder(path: str) -> None:
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _main() -> int:
    """Diagnostics: python app_update.py --check"""
    import argparse

    ap = argparse.ArgumentParser(description="ElevateX release check")
    ap.add_argument("--check", action="store_true",
                    help="query GitHub and report the latest version")
    ap.add_argument("--json", action="store_true",
                    help="print the raw release info as JSON")
    args = ap.parse_args()

    try:
        info = check_latest()
    except UpdateError as e:
        print(f"Update check failed: {e}")
        return 1

    newer = is_newer(info.get("version", ""), CURRENT_VERSION)
    if args.json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return 0

    print(f"Installed : v{CURRENT_VERSION}  ({'frozen' if is_frozen() else 'source'})")
    print(f"Latest    : v{info.get('version')}  ({info.get('tag')})")
    print(f"Asset     : {info.get('asset_url') or 'none'}")
    print(f"Size      : {info.get('asset_size', 0) / (1024 * 1024):.2f} MB")
    print(f"SHA-256   : {info.get('asset_sha256') or 'not published'}")
    print("Status    : " + ("update available" if newer else "up to date"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
