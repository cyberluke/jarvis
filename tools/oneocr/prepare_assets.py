"""OneOCR asset preparation for the Jarvis workspace (maintainer tool).

Flow (no network required when the Snipping Tool is already installed):

  1. Discover the installed ``Microsoft.ScreenSketch`` package and read its
     ``InstallLocation``.
  2. Locate ``oneocr.dll`` / ``oneocr.onemodel`` / ``onnxruntime.dll`` and
     stage them into ``tools/oneocr/.work/``.
  3. Run the vendored extraction (ONNX Runtime C API hook) to decrypt all
     11 sub-models and reconstruct the 9 vocabularies.
  4. Normalize names with the corrected script mapping and write directly
     into ``src/jarvis/_vendor/oneocr/assets/models/``.
  5. Generate the SHA-256 runtime manifest.
  6. Delete the temporary Microsoft DLL/container copies.
  7. Print the final asset inventory.

Usage:

    python tools/oneocr/prepare_assets.py

This is a build/maintainer operation; it is NOT part of normal Jarvis
startup. The production runtime only reads the final ONNX models,
vocabularies and ``manifest.json``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent.parent
WORK_DIR = TOOLS_DIR / ".work"
ASSET_ROOT = REPO_ROOT / "src" / "jarvis" / "_vendor" / "oneocr" / "assets"
MODEL_ROOT = ASSET_ROOT / "models"

UPSTREAM_COMMIT = "75cc12666503425ffd6ea0cb052c0bcaaff21058"
TARGET_FILES = ("oneocr.dll", "oneocr.onemodel", "onnxruntime.dll")

# Corrected vocab-size -> script-name mapping (no Bengali recognizer in
# this model generation).
NAME_MAP = {
    32632: "cjk",
    548: "cyrillic",
    415: "latin",
    221: "arabic",
    237: "devanagari",
    244: "greek",
    199: "thai",
    201: "hebrew",
    179: "tamil",
}


def _discover_screensketch() -> tuple[str, str]:
    """Return ``(install_location, package_version)`` of ScreenSketch."""
    for shell in ("pwsh", "powershell"):
        try:
            proc = subprocess.run(
                [shell, "-NoProfile", "-Command",
                 "(Get-AppxPackage Microsoft.ScreenSketch | "
                 "Select-Object -First 1 | "
                 "ForEach-Object { $_.InstallLocation + \"|\" + "
                 "$_.Version })"],
                capture_output=True, text=True, timeout=30,
            )
        except Exception:
            continue
        out = (proc.stdout or "").strip()
        if proc.returncode == 0 and out and "|" in out:
            location, version = out.split("|", 1)
            return location.strip(), version.strip()
    raise RuntimeError(
        "Microsoft.ScreenSketch not found via Get-AppxPackage; "
        "install the Windows Snipping Tool first")


def _stage_files(install_location: str) -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    found: dict[str, Path] = {}
    for path in Path(install_location).rglob("*"):
        if path.is_file() and path.name in TARGET_FILES \
                and path.name not in found:
            found[path.name] = path
        if len(found) == len(TARGET_FILES):
            break
    missing = [name for name in TARGET_FILES if name not in found]
    if missing:
        raise RuntimeError(f"missing in {install_location}: {missing}")
    for name in TARGET_FILES:
        (WORK_DIR / name).write_bytes(found[name].read_bytes())
        print(f"staged {name} ({found[name].stat().st_size:,} bytes)")


def _run_extractor() -> None:
    """Run the extraction in a child process.

    The hook loads ``oneocr.dll`` / ``onnxruntime.dll`` into its own
    address space; once the child exits, the staged DLL copies are no
    longer mapped, so the parent can delete ``.work`` deterministically.
    """
    code = (
        "import sys; sys.path.insert(0, %r); import extractor; "
        "extractor.decrypt_and_extract(%r, %r)"
        % (str(TOOLS_DIR), str(WORK_DIR), str(MODEL_ROOT))
    )
    proc = subprocess.run([sys.executable, "-c", code])
    if proc.returncode != 0:
        raise RuntimeError(f"extraction failed: exit {proc.returncode}")


def _normalize_vocab_names() -> None:
    """Rename any ``vocab_v<N>.txt`` to the corrected script names."""
    vocab_dir = MODEL_ROOT / "vocab"
    if not vocab_dir.is_dir():
        return
    for path in sorted(vocab_dir.glob("vocab_v*.txt")):
        try:
            size = int(path.stem.removeprefix("vocab_v"))
        except ValueError:
            continue
        name = NAME_MAP.get(size)
        if name:
            target = vocab_dir / f"vocab_{name}.txt"
            if target.exists():
                target.unlink()
            path.rename(target)
            print(f"normalized {path.name} -> {target.name}")


def _write_manifest(package_version: str) -> dict:
    models: dict[str, dict] = {}
    for path in sorted(MODEL_ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(MODEL_ROOT).as_posix()
        # Extraction intermediates are not production runtime assets.
        if rel.startswith(("vocab_buffers/", "raw_decrypted/")):
            continue
        data = path.read_bytes()
        models[rel] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
    manifest = {
        "schema": 1,
        "engine": "oneocr",
        "upstream": {
            "repository": "bropines/oneocr-onnx-python",
            "commit": UPSTREAM_COMMIT,
        },
        "source": {
            "product": "Microsoft.ScreenSketch",
            "package_version": package_version,
        },
        "models": models,
    }
    (ASSET_ROOT / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def _cleanup_work() -> None:
    """Delete the staged DLL/container copies and the intermediates."""
    if not WORK_DIR.is_dir():
        return
    for path in sorted(WORK_DIR.rglob("*"), reverse=True):
        try:
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        except OSError as exc:
            print(f"  warning: could not delete {path.name}: {exc}")
    try:
        WORK_DIR.rmdir()
    except OSError:
        pass


def main() -> int:
    location, version = _discover_screensketch()
    print(f"Microsoft.ScreenSketch {version} at {location}")
    _stage_files(location)
    _run_extractor()
    _normalize_vocab_names()
    manifest = _write_manifest(version)
    _cleanup_work()

    print("\nFinal asset inventory:")
    for rel, meta in manifest["models"].items():
        print(f"  {rel:<48} {meta['size']:>12,} bytes  "
              f"sha256={meta['sha256'][:16]}…")
    print(f"\n{len(manifest['models'])} runtime assets under {ASSET_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
