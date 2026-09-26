# OneOCR maintainer tools

Build-time tooling for the vendored OneOCR runtime under
`src/jarvis/_vendor/oneocr/`. These scripts are NOT imported by the
production runtime.

## Prepare assets (regenerate the model bundle)

```powershell
python tools/oneocr/prepare_assets.py
```

1. Discovers the locally installed `Microsoft.ScreenSketch` (Snipping
   Tool) via `Get-AppxPackage` and reads `InstallLocation`.
2. Stages `oneocr.dll`, `oneocr.onemodel`, `onnxruntime.dll` into
   `tools/oneocr/.work/`.
3. Runs the ONNX Runtime C API hook extraction (`extractor.py`, port of
   upstream): decrypts all 11 sub-models and reconstructs the 9 script
   vocabularies.
4. Writes the final assets directly into
   `src/jarvis/_vendor/oneocr/assets/models/` with the corrected script
   names (CJK, Cyrillic, Latin, Arabic, Devanagari, Greek, Thai, Hebrew,
   Tamil — no Bengali recognizer exists in this model generation).
5. Generates `src/jarvis/_vendor/oneocr/assets/manifest.json` with
   SHA-256 + size per asset.
6. Deletes the temporary Microsoft DLL/container copies and prints the
   final inventory.

No network access is required when the Snipping Tool is already
installed. The optional upstream network downloader (rg-adguard Store
API) is not used automatically.

## Inspect the bundle

```powershell
python tools/oneocr/inspect_models.py
```

Prints ONNX inputs/outputs per model, the vocabulary listing, and the
manifest summary.

## Files

| File | Role |
|------|------|
| `prepare_assets.py` | end-to-end preparation command |
| `extractor.py` | ONNX Runtime API hook decryption (upstream port) |
| `inspect_models.py` | bundle diagnostics |

Upstream provenance: `third_party/oneocr/UPSTREAM.md`.
