"""Extraction of the OneOCR ONNX bundle via the ONNX Runtime C API hooks.

Port of upstream ``oneocr/extractor.py`` at commit
75cc12666503425ffd6ea0cb052c0bcaaff21058 (MIT), with the corrected
classifier-index -> script name mapping. Maintainer tool only — the
production runtime never imports this module.

The trick (per upstream docs/WRITEUP.md): ``oneocr.dll`` decrypts each
sub-model of ``oneocr.onemodel`` and loads it through
``CreateSessionFromArray`` (slot #8 of the OrtApi table) or
``CreateSessionFromArrayWithPrepackedWeightsContainer`` (slot #151).
Both slots are patched in-process so the plaintext ONNX bytes can be
saved; ``Run`` (slot #9) is patched to drive the CTC/vocab feedback loop.
"""

from __future__ import annotations

import ctypes
import io
import struct  # noqa: F401  (kept for parity with upstream)
import sys
from ctypes import (POINTER, Structure, byref, c_char_p, c_float, c_int32,
                    c_int64, c_size_t, c_ubyte, c_void_p)
from pathlib import Path
from typing import Optional, Union

# Global hook state (kept alive against GC while native code runs).
global_references: dict = {}
hooked_callbacks: list = []
sessions: dict = {}
recognizer_sizes_to_v: dict = {}

target_script_id = 0
current_offset = 0
current_recognizer_v = None

orig_csfa_addr = None
orig_csfap_addr = None
orig_run_addr = None

get_tensor_mutable_data_fn = None
get_tensor_type_and_shape_fn = None
get_dimensions_count_fn = None
get_dimensions_fn = None
release_tensor_type_and_shape_info_fn = None

# Corrected vocab-size -> script-name mapping (no Bengali in this model
# generation; Greek=244, Thai=199, Hebrew=201, Tamil=179).
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


class OrtApiBase(Structure):
    pass


GetApiFuncType = ctypes.WINFUNCTYPE(c_void_p, c_int32)
GetVersionStringFuncType = ctypes.WINFUNCTYPE(c_char_p)

OrtApiBase._fields_ = [
    ("GetApi", GetApiFuncType),
    ("GetVersionString", GetVersionStringFuncType),
]

CreateSessionFromArrayType = ctypes.WINFUNCTYPE(
    c_void_p, c_void_p, c_void_p, c_size_t, c_void_p, c_void_p)
CreateSessionFromArrayPrepackedType = ctypes.WINFUNCTYPE(
    c_void_p, c_void_p, c_void_p, c_size_t, c_void_p, c_void_p, c_void_p)
RunType = ctypes.WINFUNCTYPE(
    c_void_p, c_void_p, c_void_p, POINTER(c_char_p), POINTER(c_void_p),
    c_size_t, POINTER(c_char_p), c_size_t, POINTER(c_void_p))


def decrypt_and_extract(bin_dir: Union[str, Path],
                        models_dir: Union[str, Path]) -> int:
    """Decrypt the OneOCR onemodel container and extract sub-models.

    Returns the number of vocabularies extracted. Raises on any failure.
    """
    global target_script_id, current_offset, current_recognizer_v
    global orig_csfa_addr, orig_csfap_addr, orig_run_addr
    global get_tensor_mutable_data_fn, get_tensor_type_and_shape_fn
    global get_dimensions_count_fn, get_dimensions_fn
    global release_tensor_type_and_shape_info_fn

    bin_dir = Path(bin_dir).absolute()
    models_dir = Path(models_dir).absolute()
    vocab_dir = models_dir / "vocab"
    # Intermediates stay inside the working directory, never in the
    # production assets root.
    buffers_dir = bin_dir / "vocab_buffers"
    raw_dir = bin_dir / "raw_decrypted"

    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer,
                                      encoding="utf-8", errors="replace")

    try:
        import onnx
    except ImportError as exc:  # tools-only dependency
        raise RuntimeError("maintainer tool requires 'onnx' installed "
                           "(pip install onnx)") from exc

    for d in (models_dir, vocab_dir, buffers_dir, raw_dir,
              models_dir / "detector", models_dir / "classifier",
              models_dir / "recognizers"):
        d.mkdir(parents=True, exist_ok=True)

    if not (bin_dir / "oneocr.dll").exists():
        raise FileNotFoundError(f"oneocr.dll not found in {bin_dir}")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetDllDirectoryW(str(bin_dir))

    lib_ort = ctypes.WinDLL(str(bin_dir / "onnxruntime.dll"))
    global_references["ort_handle"] = lib_ort._handle

    def save_raw_model(model_bytes: bytes, size: int) -> str:
        if size > 1024 * 1024:
            out_path = raw_dir / f"model_{size}.onnx"
            out_path.write_bytes(model_bytes)
            return f"model_{size}.onnx"
        out_path = raw_dir / f"vocab_{size}.bin"
        out_path.write_bytes(model_bytes)
        return f"vocab_{size}.bin"

    def hook_create_session_from_array(env, model_data, model_data_length,
                                       options, out):
        orig_func = CreateSessionFromArrayType(orig_csfa_addr)
        status = orig_func(env, model_data, model_data_length, options, out)
        if status is None or status == 0:
            try:
                session_ptr = ctypes.cast(out,
                                          POINTER(c_void_p)).contents.value
                if session_ptr:
                    model_bytes = ctypes.string_at(model_data,
                                                   model_data_length)
                    name = save_raw_model(model_bytes, model_data_length)
                    print(f"[DECRYPTED] session={session_ptr:#x} "
                          f"size={model_data_length:,} bytes -> {name}")
                    sessions[session_ptr] = {"size": model_data_length,
                                             "V": None}
            except Exception as exc:
                print("Error in hook_create_session_from_array:", exc)
        return status

    def hook_create_session_from_array_prepacked(env, model_data,
                                                 model_data_length, options,
                                                 prepacked, out):
        orig_func = CreateSessionFromArrayPrepackedType(orig_csfap_addr)
        status = orig_func(env, model_data, model_data_length, options,
                           prepacked, out)
        if status is None or status == 0:
            try:
                session_ptr = ctypes.cast(out,
                                          POINTER(c_void_p)).contents.value
                if session_ptr:
                    model_bytes = ctypes.string_at(model_data,
                                                   model_data_length)
                    name = save_raw_model(model_bytes, model_data_length)
                    print(f"[DECRYPTED] session={session_ptr:#x} "
                          f"size={model_data_length:,} bytes (prepacked) "
                          f"-> {name}")
                    sessions[session_ptr] = {"size": model_data_length,
                                             "V": None}
            except Exception as exc:
                print("Error in hook_create_session_from_array_prepacked:",
                      exc)
        return status

    def hook_run(session, run_options, input_names, inputs, input_len,
                 output_names, output_len, outputs):
        orig_func = RunType(orig_run_addr)
        try:
            status = orig_func(session, run_options, input_names, inputs,
                               input_len, output_names, output_len, outputs)
        except Exception as exc:
            print(f"[hook_run] orig_func crashed with: {exc}")
            raise

        if status is not None and status != 0:
            return status

        try:
            if not outputs or not ctypes.cast(outputs,
                                              ctypes.c_void_p).value:
                return status
            info = sessions.get(session, {"size": 0, "V": None})
            if not info["size"]:
                return status

            # A. Force the classifier to report the target script id.
            is_classifier = (info["size"] == 3456873 or output_len == 6)
            if is_classifier and output_len > 0:
                script_id_out_idx = -1
                for idx in range(output_len):
                    try:
                        name_bytes = output_names[idx]
                        if name_bytes:
                            name = name_bytes.decode("utf-8",
                                                     errors="ignore")
                            if name == "script_id_score":
                                script_id_out_idx = idx
                                break
                    except Exception:
                        pass
                if script_id_out_idx != -1 and outputs[script_id_out_idx]:
                    try:
                        out_tensor = outputs[script_id_out_idx]
                        data_ptr = c_void_p()
                        get_tensor_mutable_data_fn(out_tensor,
                                                   byref(data_ptr))
                        if data_ptr.value:
                            import numpy as np
                            arr = np.ctypeslib.as_array(
                                ctypes.cast(data_ptr, POINTER(c_float)),
                                shape=(1, 1, 10))
                            arr.fill(-999.0)
                            arr[0, 0, target_script_id] = 10.0
                    except Exception as exc:
                        print("Error patching classifier output:", exc)

            # B. Inject a sequence of vocab indices into the recognizer.
            is_recognizer = (output_len == 1 and info["size"] > 1024 * 1024)
            if is_recognizer and outputs[0]:
                global current_recognizer_v
                try:
                    out_tensor = outputs[0]
                    info_ptr = c_void_p()
                    get_tensor_type_and_shape_fn(out_tensor, byref(info_ptr))
                    if info_ptr.value:
                        num_dims = c_size_t()
                        get_dimensions_count_fn(info_ptr, byref(num_dims))
                        dims = (c_int64 * num_dims.value)()
                        get_dimensions_fn(info_ptr, dims, num_dims.value)
                        release_tensor_type_and_shape_info_fn(info_ptr)

                        if num_dims.value >= 3:
                            t_len, _, v_len = list(dims)[:3]
                            current_recognizer_v = v_len
                            info["V"] = v_len
                            recognizer_sizes_to_v[info["size"]] = v_len

                            data_ptr = c_void_p()
                            get_tensor_mutable_data_fn(out_tensor,
                                                       byref(data_ptr))
                            if data_ptr.value:
                                import numpy as np
                                arr = np.ctypeslib.as_array(
                                    ctypes.cast(data_ptr, POINTER(c_float)),
                                    shape=(t_len, 1, v_len))
                                arr.fill(-999.0)
                                limit = min(t_len - 2, 200)
                                for t in range(1, limit + 1):
                                    idx = current_offset + t
                                    if idx < v_len:
                                        arr[t, 0, idx] = 0.0
                except Exception as exc:
                    print("Error patching recognizer output:", exc)
        except Exception as exc:
            print("General error in hook_run:", exc)
        return status

    # Patch the OrtApi table slots in place.
    orig_ort_get_api_base = lib_ort.OrtGetApiBase
    orig_ort_get_api_base.restype = POINTER(OrtApiBase)
    orig_ort_get_api_base.argtypes = []

    orig_api_base = orig_ort_get_api_base().contents
    real_api_ptr = orig_api_base.GetApi(7)

    orig_csfa_addr = ctypes.cast(real_api_ptr + 8 * 8,
                                 POINTER(c_void_p)).contents.value
    orig_run_addr = ctypes.cast(real_api_ptr + 9 * 8,
                                POINTER(c_void_p)).contents.value
    orig_csfap_addr = ctypes.cast(real_api_ptr + 151 * 8,
                                  POINTER(c_void_p)).contents.value

    def get_api_fn(api_ptr, idx, argtypes, restype):
        fn_ptr = ctypes.cast(api_ptr + idx * 8,
                             POINTER(c_void_p)).contents.value
        return ctypes.WINFUNCTYPE(restype, *argtypes)(fn_ptr)

    get_tensor_mutable_data_fn = get_api_fn(
        real_api_ptr, 51, [c_void_p, POINTER(c_void_p)], c_void_p)
    get_tensor_type_and_shape_fn = get_api_fn(
        real_api_ptr, 65, [c_void_p, POINTER(c_void_p)], c_void_p)
    get_dimensions_count_fn = get_api_fn(
        real_api_ptr, 61, [c_void_p, POINTER(c_size_t)], c_void_p)
    get_dimensions_fn = get_api_fn(
        real_api_ptr, 62, [c_void_p, POINTER(c_int64), c_size_t], c_void_p)
    release_tensor_type_and_shape_info_fn = get_api_fn(
        real_api_ptr, 99, [c_void_p], None)

    kernel32.VirtualProtect.argtypes = [c_void_p, c_size_t, ctypes.c_ulong,
                                        POINTER(ctypes.c_ulong)]
    kernel32.VirtualProtect.restype = ctypes.c_int

    old_protect = ctypes.c_ulong()
    page_execute_readwrite = 0x40
    if not kernel32.VirtualProtect(real_api_ptr, 1600, page_execute_readwrite,
                                   ctypes.byref(old_protect)):
        raise RuntimeError("VirtualProtect on API table failed")

    cb_csfa = CreateSessionFromArrayType(hook_create_session_from_array)
    hooked_callbacks.append(cb_csfa)
    ctypes.cast(real_api_ptr + 8 * 8,
                POINTER(c_void_p))[0] = ctypes.cast(
                    cb_csfa, ctypes.c_void_p).value

    cb_run = RunType(hook_run)
    hooked_callbacks.append(cb_run)
    ctypes.cast(real_api_ptr + 9 * 8,
                POINTER(c_void_p))[0] = ctypes.cast(
                    cb_run, ctypes.c_void_p).value

    cb_csfap = CreateSessionFromArrayPrepackedType(
        hook_create_session_from_array_prepacked)
    hooked_callbacks.append(cb_csfap)
    ctypes.cast(real_api_ptr + 151 * 8,
                POINTER(c_void_p))[0] = ctypes.cast(
                    cb_csfap, ctypes.c_void_p).value

    kernel32.VirtualProtect(real_api_ptr, 1600, old_protect.value,
                            ctypes.byref(old_protect))
    print("ONNX Runtime API hooks patched in-place successfully.")

    # Load oneocr.dll and trigger decryption via CreateOcrPipeline.
    print("Loading oneocr.dll...")
    dll = ctypes.WinDLL(str(bin_dir / "oneocr.dll"))
    global_references["oneocr_handle"] = dll._handle

    dll.CreateOcrInitOptions.restype = c_int64
    dll.CreateOcrInitOptions.argtypes = [POINTER(c_int64)]
    dll.OcrInitOptionsSetUseModelDelayLoad.restype = c_int64
    dll.OcrInitOptionsSetUseModelDelayLoad.argtypes = [c_int64, ctypes.c_char]
    dll.CreateOcrPipeline.restype = c_int64
    dll.CreateOcrPipeline.argtypes = [c_char_p, c_char_p, c_int64,
                                      POINTER(c_int64)]
    dll.ReleaseOcrPipeline.restype = None
    dll.ReleaseOcrPipeline.argtypes = [c_int64]
    dll.ReleaseOcrInitOptions.restype = None
    dll.ReleaseOcrInitOptions.argtypes = [c_int64]

    dll.CreateOcrProcessOptions.restype = c_int64
    dll.CreateOcrProcessOptions.argtypes = [POINTER(c_int64)]
    dll.ReleaseOcrProcessOptions.restype = None
    dll.ReleaseOcrProcessOptions.argtypes = [c_int64]
    dll.ReleaseOcrResult.restype = None
    dll.ReleaseOcrResult.argtypes = [c_int64]

    dll.GetOcrLineCount.restype = c_int64
    dll.GetOcrLineCount.argtypes = [c_int64, POINTER(c_int64)]
    dll.GetOcrLine.restype = c_int64
    dll.GetOcrLine.argtypes = [c_int64, c_int64, POINTER(c_int64)]
    dll.GetOcrLineContent.restype = c_int64
    dll.GetOcrLineContent.argtypes = [c_int64, POINTER(c_char_p)]

    class ImageStructure(Structure):
        _fields_ = [
            ("type", c_int32),
            ("width", c_int32),
            ("height", c_int32),
            ("_reserved", c_int32),
            ("step_size", c_int64),
            ("data_ptr", POINTER(c_ubyte)),
        ]

    dll.RunOcrPipeline.restype = c_int64
    dll.RunOcrPipeline.argtypes = [c_int64, POINTER(ImageStructure), c_int64,
                                   POINTER(c_int64)]

    print("Initializing OCR pipeline (triggers model decryption)...")
    init_opts = c_int64()
    dll.CreateOcrInitOptions(byref(init_opts))
    dll.OcrInitOptionsSetUseModelDelayLoad(init_opts, 0)

    model_path = str(bin_dir / "oneocr.onemodel").encode()
    key = b"kj)TGtrK>f]b[Piow.gU+nC@s\"\"\"\"\"\"4"
    pipeline = c_int64()
    ret = dll.CreateOcrPipeline(model_path, key, init_opts, byref(pipeline))
    if ret != 0:
        raise RuntimeError(f"Failed to create pipeline: {ret:#x}")

    print("All ONNX models successfully decrypted and saved.")

    # Vocabulary extraction feedback loop.
    print("Starting vocabulary extraction loop...")
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (2540, 200), (255, 255, 255, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 60)
    except Exception:
        font = None
    draw.text((30, 60),
              "Это очень длинный текст для перехвата сессии распознавания",
              fill=(0, 0, 0, 255), font=font)

    b_ch, g_ch, r_ch, a_ch = img.split()
    bgra = Image.merge("RGBA", (b_ch, g_ch, r_ch, a_ch))
    raw_bytes = bgra.tobytes()
    arr = (c_ubyte * len(raw_bytes)).from_buffer_copy(raw_bytes)
    img_struct = ImageStructure(type=3, width=bgra.width, height=bgra.height,
                                _reserved=0, step_size=bgra.width * 4,
                                data_ptr=arr)

    proc_opts = c_int64()
    dll.CreateOcrProcessOptions(byref(proc_opts))

    extracted_vocabs = 0
    for script_id in range(10):
        target_script_id = script_id
        current_offset = 0
        captured_vocab: list[str] = []
        v_limit: Optional[int] = None

        print(f"Scanning Script ID {script_id}...")

        while True:
            current_recognizer_v = None
            result_h = c_int64()
            ret = dll.RunOcrPipeline(pipeline, byref(img_struct), proc_opts,
                                     byref(result_h))
            if ret != 0 or result_h.value == 0:
                break

            line_count = c_int64()
            dll.GetOcrLineCount(result_h, byref(line_count))
            decoded_text = ""
            if line_count.value > 0:
                line_h = c_int64()
                dll.GetOcrLine(result_h, 0, byref(line_h))
                text_ptr = c_char_p()
                dll.GetOcrLineContent(line_h, byref(text_ptr))
                if text_ptr.value:
                    decoded_text = text_ptr.value.decode("utf-8",
                                                         errors="replace")
            dll.ReleaseOcrResult(result_h)

            if current_recognizer_v is None:
                break

            v_limit = current_recognizer_v
            chunk_size = min(630 - 2, 200)

            while len(captured_vocab) < v_limit:
                captured_vocab.append("")

            for k, char in enumerate(decoded_text):
                idx = current_offset + 1 + k
                if idx < v_limit:
                    captured_vocab[idx] = char

            current_offset += chunk_size
            if current_offset >= v_limit - 1:
                extracted_vocabs += 1
                lang_name = NAME_MAP.get(v_limit, f"v{v_limit}")
                out_path = vocab_dir / f"vocab_{lang_name}.txt"
                with open(out_path, "w", encoding="utf-8") as handle:
                    for idx, char in enumerate(captured_vocab):
                        handle.write(f"{idx}: {char}\n")
                print(f"  -> Extracted vocab for {lang_name} "
                      f"({len(captured_vocab)} entries)")
                break

    dll.ReleaseOcrProcessOptions(proc_opts)
    dll.ReleaseOcrPipeline(pipeline)
    dll.ReleaseOcrInitOptions(init_opts)

    # Organize decrypted models into the final layout.
    def post_process_models() -> None:
        if not raw_dir.exists():
            return
        for item in sorted(raw_dir.iterdir()):
            if not item.is_file():
                continue
            if item.name.endswith(".bin"):
                dest = buffers_dir / item.name
                if dest.exists():
                    dest.unlink()
                item.rename(dest)
                print(f"Organized: {item.name} -> vocab_buffers/")
                continue
            if item.name.endswith(".onnx"):
                try:
                    model_bytes = item.read_bytes()
                    model = onnx.load_model_from_string(model_bytes)
                    output_names = [out.name for out in model.graph.output]

                    if any("fpn" in name for name in output_names):
                        dest = models_dir / "detector" / "text_detector.onnx"
                        if dest.exists():
                            dest.unlink()
                        item.rename(dest)
                        print("Organized: "
                              f"{item.name} -> detector/text_detector.onnx")
                    elif "script_id_score" in output_names:
                        dest = (models_dir / "classifier"
                                / "script_classifier.onnx")
                        if dest.exists():
                            dest.unlink()
                        item.rename(dest)
                        print("Organized: "
                              f"{item.name} -> classifier/script_classifier.onnx")
                    elif "logsoftmax" in output_names:
                        logsoftmax_out = next(
                            (out for out in model.graph.output
                             if out.name == "logsoftmax"), None)
                        v_size = 0
                        if logsoftmax_out is not None:
                            try:
                                v_size = (logsoftmax_out.type
                                          .tensor_type.shape
                                          .dim[-1].dim_value)
                            except Exception:
                                v_size = 0
                        if v_size == 0:
                            v_size = recognizer_sizes_to_v.get(
                                item.stat().st_size, 415)
                        lang_name = NAME_MAP.get(v_size, f"v{v_size}")
                        dest = (models_dir / "recognizers"
                                / f"recognizer_{lang_name}.onnx")
                        if dest.exists():
                            dest.unlink()
                        item.rename(dest)
                        print(f"Organized: {item.name} -> "
                              f"recognizers/recognizer_{lang_name}.onnx")
                    else:
                        dest = buffers_dir / item.name
                        if dest.exists():
                            dest.unlink()
                        item.rename(dest)
                        print(f"Organized: {item.name} -> vocab_buffers/")
                except Exception as exc:
                    print(f"Failed to post-process {item.name}: {exc}")
        try:
            raw_dir.rmdir()
        except Exception:
            pass

    post_process_models()
    print(f"SUCCESS: decrypted models + {extracted_vocabs} vocabularies "
          f"written under {models_dir}")
    return extracted_vocabs
