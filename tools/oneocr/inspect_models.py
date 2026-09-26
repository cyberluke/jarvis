"""Inspect the extracted OneOCR ONNX bundle (maintainer tool).

Prints input/output names, shapes and element types for every ``.onnx``
file under ``src/jarvis/_vendor/oneocr/assets/models`` plus a listing of
the vocabulary files and the runtime manifest.

Usage:

    python tools/oneocr/inspect_models.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent.parent
ASSET_ROOT = REPO_ROOT / "src" / "jarvis" / "_vendor" / "oneocr" / "assets"
MODEL_ROOT = ASSET_ROOT / "models"


def main() -> int:
    try:
        import onnx
    except ImportError:
        print("onnx package required: pip install onnx", file=sys.stderr)
        return 1

    if not MODEL_ROOT.is_dir():
        print(f"missing model root: {MODEL_ROOT}", file=sys.stderr)
        return 1

    for path in sorted(MODEL_ROOT.rglob("*.onnx")):
        model = onnx.load(str(path))
        rel = path.relative_to(MODEL_ROOT).as_posix()
        print(f"== {rel} ({path.stat().st_size:,} bytes)")
        for inp in model.graph.input:
            dims = [d.dim_value or d.dim_param
                    for d in inp.type.tensor_type.shape.dim]
            print(f"   in  {inp.name:<28} {dims}")
        for out in model.graph.output:
            dims = [d.dim_value or d.dim_param
                    for d in out.type.tensor_type.shape.dim]
            print(f"   out {out.name:<28} {dims}")

    vocab_dir = MODEL_ROOT / "vocab"
    if vocab_dir.is_dir():
        print("== vocab files")
        for path in sorted(vocab_dir.glob("*.txt")):
            print(f"   {path.name:<28} {path.stat().st_size:>10,} bytes")

    manifest_path = ASSET_ROOT / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        print(f"== manifest schema={manifest.get('schema')} "
              f"engine={manifest.get('engine')} "
              f"assets={len(manifest.get('models', {}))}")
        upstream = manifest.get("upstream") or {}
        print(f"   upstream {upstream.get('repository')} "
              f"@ {upstream.get('commit')}")
    else:
        print("manifest.json not generated yet — run prepare_assets.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
