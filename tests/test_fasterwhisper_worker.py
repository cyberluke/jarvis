"""Tests for the isolated faster-whisper STT worker (model load ladder).

The device/compute fallback ladder, corrupted-cache recovery, rate-limit
retry and warmup moved from ``jarvis.listening.listener`` into
``jarvis.listening.fasterwhisper_worker`` when STT was isolated into a
child process (its own GIL). These tests pin that moved behaviour.
"""

import io
import shutil
from unittest.mock import MagicMock, patch

import pytest

from jarvis.listening import fasterwhisper_worker as fw


def _load(model_name="medium", device="auto", compute="int8",
          threads=4, cache_dir=None):
    return fw._load_model(model_name, device, compute, threads, cache_dir)


class TestWhisperComputeTypeFallback:
    """Compute-type fallback ladder inside the worker's model loader."""

    def test_successful_load_with_int8(self):
        mock_model = MagicMock()
        with patch("faster_whisper.WhisperModel",
                   return_value=mock_model) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                model, device, compute, kwargs = _load(compute="int8")
                mock_class.assert_called_once()
                assert mock_class.call_args[1]["device"] == "auto"
                assert mock_class.call_args[1]["compute_type"] == "int8"
                assert model is mock_model
                assert compute == "int8"

    def test_fallback_from_int8_to_float16(self):
        mock_model = MagicMock()

        def side_effect(model_name, device, compute_type, **kwargs):
            if compute_type == "int8":
                raise RuntimeError(
                    "Requested int8 compute type, but the target device or "
                    "backend do not support efficient int8 computation.")
            return mock_model

        with patch("faster_whisper.WhisperModel", side_effect=side_effect) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                model, _dev, compute, _kw = _load(compute="int8")
                assert mock_class.call_count == 2
                calls = mock_class.call_args_list
                assert calls[0][1]["device"] == "auto"
                assert calls[0][1]["compute_type"] == "int8"
                assert calls[1][1]["device"] == "auto"
                assert calls[1][1]["compute_type"] == "float16"
                assert model is mock_model
                assert compute == "float16"

    def test_fallback_from_int8_to_float32(self):
        mock_model = MagicMock()

        def side_effect(model_name, device, compute_type, **kwargs):
            if compute_type in ("int8", "float16"):
                raise RuntimeError(
                    f"Requested {compute_type} compute type, but not supported.")
            return mock_model

        with patch("faster_whisper.WhisperModel", side_effect=side_effect) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                model, _dev, compute, _kw = _load(compute="int8")
                assert mock_class.call_count == 3
                calls = mock_class.call_args_list
                assert calls[0][1]["compute_type"] == "int8"
                assert calls[1][1]["compute_type"] == "float16"
                assert calls[2][1]["device"] == "auto"
                assert calls[2][1]["compute_type"] == "float32"
                assert model is mock_model

    def test_no_fallback_for_non_compute_type_errors(self):
        with patch("faster_whisper.WhisperModel") as mock_class:
            mock_class.side_effect = RuntimeError("Model not found: invalid_model")
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                with pytest.raises(RuntimeError):
                    _load()
                mock_class.assert_called_once()
                assert mock_class.call_args[1]["device"] == "auto"
                assert mock_class.call_args[1]["compute_type"] == "int8"

    def test_all_fallbacks_fail(self):
        def side_effect(model_name, device, compute_type, **kwargs):
            raise RuntimeError(
                f"Requested {compute_type} compute type, but not supported.")

        with patch("faster_whisper.WhisperModel", side_effect=side_effect) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                with pytest.raises(RuntimeError):
                    _load()
                # 3 compute types x 2 devices (auto + cpu fallback)
                assert mock_class.call_count == 6

    def test_float16_config_skips_float16_in_fallback_list(self):
        mock_model = MagicMock()

        def side_effect(model_name, device, compute_type, **kwargs):
            if compute_type == "float16":
                raise RuntimeError(
                    "Requested float16 compute type, but not supported.")
            return mock_model

        with patch("faster_whisper.WhisperModel", side_effect=side_effect) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                model, _dev, compute, _kw = _load(compute="float16")
                calls = mock_class.call_args_list
                assert calls[0][1]["compute_type"] == "float16"
                assert calls[1][1]["device"] == "auto"
                assert calls[1][1]["compute_type"] == "float32"
                assert model is mock_model

    def test_float32_config_no_fallback_needed(self):
        def side_effect(model_name, device, compute_type, **kwargs):
            raise RuntimeError(
                "Requested float32 compute type, but not supported.")

        with patch("faster_whisper.WhisperModel", side_effect=side_effect) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                with pytest.raises(RuntimeError):
                    _load(compute="float32")
                assert mock_class.call_count == 2
                assert mock_class.call_args_list[0][1]["compute_type"] == "float32"
                assert mock_class.call_args_list[1][1]["device"] == "cpu"


class TestLargeV3TurboFallback:
    """large-v3-turbo runtime fallback when faster-whisper is too old."""

    def test_turbo_falls_back_to_large_v3_when_unsupported(self):
        mock_model = MagicMock()
        with patch("faster_whisper.WhisperModel",
                   return_value=mock_model) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                with patch(
                    "jarvis.listening.fasterwhisper_worker._is_turbo_supported",
                    return_value=False,
                ):
                    _load(model_name="large-v3-turbo")
                    mock_class.assert_called_once()
                    assert mock_class.call_args[0][0] == "large-v3"

    def test_turbo_kept_when_faster_whisper_supports_it(self):
        mock_model = MagicMock()
        with patch("faster_whisper.WhisperModel",
                   return_value=mock_model) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                with patch(
                    "jarvis.listening.fasterwhisper_worker._is_turbo_supported",
                    return_value=True,
                ):
                    _load(model_name="large-v3-turbo")
                    mock_class.assert_called_once()
                    assert mock_class.call_args[0][0] == "large-v3-turbo"


class TestCpuOptimisations:
    """CPU thread passing in the worker's model loader."""

    def test_cpu_threads_set_when_device_is_cpu(self):
        mock_model = MagicMock()
        mock_model.model.device = "cpu"
        with patch("faster_whisper.WhisperModel",
                   return_value=mock_model) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                with patch("jarvis.listening.fasterwhisper_worker.os.cpu_count",
                           return_value=8):
                    _load(device="cpu", threads=8)
                    assert mock_class.call_args[1]["cpu_threads"] == 8

    def test_cpu_threads_use_cpu_count_when_device_is_auto(self):
        mock_model = MagicMock()
        mock_model.model.device = "cpu"
        with patch("faster_whisper.WhisperModel",
                   return_value=mock_model) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                with patch("jarvis.listening.fasterwhisper_worker.os.cpu_count",
                           return_value=12):
                    _load(device="auto")
                    assert mock_class.call_args[1]["cpu_threads"] == 12


class TestCorruptedWhisperCacheRecovery:
    """Corrupted model-cache detection and recovery in the worker."""

    def _error(self, snapshot_dir):
        return (f"Unable to open file 'model.bin' in model "
                f"'{snapshot_dir}'")

    def test_corrupted_cache_detected_and_recovered(self, tmp_path):
        snapshot_dir = (tmp_path / "models--Systran--faster-whisper-medium"
                        / "snapshots" / "abc123")
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "model.bin").write_bytes(b"corrupted")
        mock_model = MagicMock()
        call_count = 0

        def side_effect(model_name, device, compute_type, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError(self._error(snapshot_dir))
            return mock_model

        with patch("faster_whisper.WhisperModel",
                   side_effect=side_effect) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                model, _dev, _compute, _kw = _load()
                assert mock_class.call_count == 2
                assert model is mock_model
                assert not snapshot_dir.exists()

    def test_corrupted_cache_retry_also_fails(self, tmp_path):
        snapshot_dir = (tmp_path / "models--Systran--faster-whisper-medium"
                        / "snapshots" / "abc123")
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "model.bin").write_bytes(b"corrupted")

        def side_effect(model_name, device, compute_type, **kwargs):
            raise RuntimeError(self._error(snapshot_dir))

        with patch("faster_whisper.WhisperModel",
                   side_effect=side_effect) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                with pytest.raises(RuntimeError):
                    _load()
                # First config: corrupt raise + retry raise; the rest of the
                # ladder fails without the dir present -> fail closed.
                assert mock_class.call_count >= 2

    def test_corrupted_cache_parent_model_dir_deleted(self, tmp_path):
        model_dir = tmp_path / "models--Systran--faster-whisper-medium"
        snapshot_dir = model_dir / "snapshots" / "abc123"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "model.bin").write_bytes(b"corrupted")
        mock_model = MagicMock()
        call_count = 0

        def side_effect(model_name, device, compute_type, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError(self._error(snapshot_dir))
            return mock_model

        with patch("faster_whisper.WhisperModel",
                   side_effect=side_effect):
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                _load()
                assert not model_dir.exists()

    def test_unparseable_cache_path_falls_back(self):
        error_msg = "Unable to open file 'model.bin' somehow"
        mock_model = MagicMock()
        call_count = 0

        def side_effect(model_name, device, compute_type, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError(error_msg)
            return mock_model

        with patch("faster_whisper.WhisperModel",
                   side_effect=side_effect) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                model, _dev, _compute, _kw = _load()
                # No cache clear possible -> next fallback config is tried.
                assert mock_class.call_count == 2
                assert model is mock_model

    def test_rmtree_oserror_prevents_retry(self, tmp_path):
        snapshot_dir = (tmp_path / "models--Systran--faster-whisper-medium"
                        / "snapshots" / "abc123")
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "model.bin").write_bytes(b"corrupted")
        mock_model = MagicMock()
        call_count = 0

        def side_effect(model_name, device, compute_type, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError(self._error(snapshot_dir))
            return mock_model

        with patch("faster_whisper.WhisperModel",
                   side_effect=side_effect) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                with patch("shutil.rmtree",
                           side_effect=OSError("Permission denied")):
                    model, _dev, _compute, _kw = _load()
                    # rmtree failure -> fallback config is tried instead.
                    assert model is mock_model
                    assert mock_class.call_count == 2

    def test_no_models_ancestor_prevents_cache_clear(self, tmp_path):
        plain_dir = tmp_path / "some" / "random" / "path"
        plain_dir.mkdir(parents=True)
        (plain_dir / "model.bin").write_bytes(b"corrupted")
        mock_model = MagicMock()
        call_count = 0

        def side_effect(model_name, device, compute_type, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError(self._error(plain_dir))
            return mock_model

        with patch("faster_whisper.WhisperModel",
                   side_effect=side_effect) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                model, _dev, _compute, _kw = _load()
                assert model is mock_model
                assert mock_class.call_count == 2

    def test_corrupted_cache_retry_fails_then_fallback_succeeds(self, tmp_path):
        snapshot_dir = (tmp_path / "models--Systran--faster-whisper-medium"
                        / "snapshots" / "abc123")
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "model.bin").write_bytes(b"corrupted")
        mock_model = MagicMock()
        call_count = 0

        def side_effect(model_name, device, compute_type, **kwargs):
            nonlocal call_count
            call_count += 1
            # Config 1 (auto/int8): corrupt raise, retry also corrupt.
            # Config 2 (auto/float16): succeeds.
            if call_count <= 2:
                raise RuntimeError(self._error(snapshot_dir))
            return mock_model

        with patch("faster_whisper.WhisperModel",
                   side_effect=side_effect) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                model, _dev, compute, _kw = _load()
                assert mock_class.call_count == 3
                assert model is mock_model
                assert compute == "float16"
                assert not snapshot_dir.exists()


class _FakeHfHubHTTPError(RuntimeError):
    def __init__(self):
        super().__init__("HfHubHTTPError: 429 Too Many Requests")
        self.response = MagicMock()
        self.response.status_code = 429


class TestWhisperRateLimitRetry:
    """Retry logic when HuggingFace returns 429 Too Many Requests."""

    def test_429_retried_then_succeeds(self):
        mock_model = MagicMock()
        call_count = 0

        def side_effect(model_name, device, compute_type, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError(
                    "Got: HfHubHTTPError: 429 Too Many Requests for url: "
                    "https://huggingface.co/api/models/Systran/faster-whisper-medium")
            return mock_model

        with patch("faster_whisper.WhisperModel",
                   side_effect=side_effect) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                with patch("jarvis.listening.fasterwhisper_worker.time.sleep"):
                    model, _dev, _compute, _kw = _load()
                    assert mock_class.call_count == 2
                    assert model is mock_model

    def test_429_gives_up_after_max_retries(self):
        error_msg = ("429 Too Many Requests for url: "
                     "https://huggingface.co/api/models/Systran/faster-whisper-medium")

        def side_effect(model_name, device, compute_type, **kwargs):
            raise RuntimeError(error_msg)

        with patch("faster_whisper.WhisperModel",
                   side_effect=side_effect) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                with patch("jarvis.listening.fasterwhisper_worker.time.sleep"):
                    with pytest.raises(RuntimeError):
                        _load()
                    # 1 initial + 4 backoff retries, then give up.
                    assert mock_class.call_count == 5

    def test_hfhub_429_via_response_status_code_retried(self):
        mock_model = MagicMock()
        call_count = 0

        def side_effect(model_name, device, compute_type, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _FakeHfHubHTTPError()
            return mock_model

        with patch("faster_whisper.WhisperModel",
                   side_effect=side_effect) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                with patch("jarvis.listening.fasterwhisper_worker.time.sleep"):
                    model, _dev, _compute, _kw = _load()
                    assert mock_class.call_count == 2
                    assert model is mock_model

    def test_non_429_error_not_retried(self):
        def side_effect(model_name, device, compute_type, **kwargs):
            raise RuntimeError("Model not found: invalid_model")

        with patch("faster_whisper.WhisperModel",
                   side_effect=side_effect) as mock_class:
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                with pytest.raises(RuntimeError):
                    _load()
                mock_class.assert_called_once()


class TestWhisperWarmup:
    """The worker warms the decoder up before serving requests."""

    def test_warmup_runs_after_model_load(self):
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (iter([]), MagicMock())

        with patch("faster_whisper.WhisperModel",
                   return_value=mock_model):
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                mock_sys.stdin = io.StringIO('{"op": "shutdown"}\n')
                mock_sys.stdout = io.StringIO()
                rc = fw.worker_main()
                assert rc == 0
                # Warmup: non-silent noise, one second at 16 kHz, no forced
                # language (the config language is empty in the test env).
                args, kwargs = mock_model.transcribe.call_args
                audio = args[0]
                assert audio.shape[0] == 16000
                assert not (audio == 0).all(), \
                    "warmup should not use silent audio"
                assert kwargs.get("language") is None
                # A transcribe request and the ready stage were emitted.
                assert '"op": "transcribe"' not in mock_sys.stdout.getvalue()
                assert '"stage": "ready"' in mock_sys.stdout.getvalue()


class TestNvidiaDllPathSetup:
    """The CUDA probe must not regress to 'libraries missing' when the
    wheels are installed: DLL dirs are registered before ctypes scans."""

    def test_adds_pip_wheel_bin_dirs(self):
        pytest.importorskip("nvidia")
        saved_path = fw.os.environ.get("PATH", "")
        try:
            fw._setup_nvidia_dll_path()
            path = fw.os.environ.get("PATH", "")
            lowered = path.lower()
            assert any(
                "nvidia" in d and d.rstrip("\\").endswith("bin")
                for d in path.split(fw.os.pathsep)
            ), "nvidia/<pkg>/bin must be registered on PATH"
        finally:
            fw.os.environ["PATH"] = saved_path

    def test_scans_meipass_layout_when_frozen(self, tmp_path):
        """The PyInstaller bundle keeps the DLLs at _internal/nvidia/<pkg>/bin."""
        bin_dir = tmp_path / "nvidia" / "cublas" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "cublas64_12.dll").write_bytes(b"x")
        saved_path = fw.os.environ.get("PATH", "")
        try:
            with patch.object(fw.sys, "frozen", True, create=True):
                with patch.object(fw.sys, "_MEIPASS", str(tmp_path),
                                  create=True):
                    fw._setup_nvidia_dll_path()
            path = fw.os.environ.get("PATH", "")
            assert str(bin_dir).lower() in path.lower()
        finally:
            fw.os.environ["PATH"] = saved_path

    def test_probe_finds_bundled_cuda_dlls(self):
        """Integration on this host: the wheels are installed, so after the
        path setup the probe must report CUDA available (not 'missing')."""
        pytest.importorskip("nvidia")
        saved_path = fw.os.environ.get("PATH", "")
        try:
            available, missing = fw._probe_cuda_available()
            assert available, f"CUDA probe failed: missing={missing}"
        finally:
            fw.os.environ["PATH"] = saved_path


class TestWorkerRequestLoopRobustness:
    """One malformed line must never kill the worker's request loop."""

    def test_primitive_json_line_is_ignored(self):
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (iter([]), MagicMock())

        with patch("faster_whisper.WhisperModel",
                   return_value=mock_model):
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                # A JSON string primitive first (crashed with AttributeError
                # before the guard), then a clean shutdown.
                mock_sys.stdin = io.StringIO(
                    '"shutdown"\n{"op": "shutdown"}\n')
                mock_sys.stdout = io.StringIO()
                rc = fw.worker_main()
                assert rc == 0
                out = mock_sys.stdout.getvalue()
                assert '"stage": "ready"' in out
                assert '"ok": true' in out
                assert '"error"' not in out


def _silence_b64() -> str:
    """Base64 of 1600 float32 zero samples (any payload decodes fine)."""
    import base64

    import numpy as np

    arr = np.zeros(1600, dtype=np.float32)
    return base64.b64encode(arr.tobytes()).decode("ascii")


class TestLanguageSelectorResolution:
    """The worker must never feed a closed-set selector to ``transcribe``.

    ``whisper_language="cs+vi"`` is a selector, not an ISO-639-1 code: the
    listener resolves it into per-code forced passes before calling the
    worker, so the worker only ever sees a single code or ``None``. Passing
    the raw selector raises ``ValueError: 'cs+vi' is not a valid language
    code`` — the regression that crashed the warmup decode.
    """

    def test_single_code_passes_through(self):
        assert fw._resolve_forced_language("cs") == "cs"
        assert fw._resolve_forced_language("vi") == "vi"
        assert fw._resolve_forced_language("en") == "en"
        assert fw._resolve_forced_language("sk") == "sk"

    def test_selector_is_normalised_lowercase(self):
        assert fw._resolve_forced_language(" CS ") == "cs"

    def test_closed_set_selector_resolves_to_none(self):
        assert fw._resolve_forced_language("cs+vi") is None
        assert fw._resolve_forced_language("CS+VI") is None

    def test_legacy_auto_resolves_to_none(self):
        assert fw._resolve_forced_language("auto") is None

    def test_empty_or_none_resolves_to_none(self):
        assert fw._resolve_forced_language(None) is None
        assert fw._resolve_forced_language("") is None
        assert fw._resolve_forced_language("  ") is None

    def test_unknown_code_resolves_to_none(self):
        assert fw._resolve_forced_language("xx") is None

    def test_warmup_with_closed_set_selector_passes_none(self):
        """The regression: 'cs+vi' crashed the warmup decode."""
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (iter([]), MagicMock())

        with patch("faster_whisper.WhisperModel",
                   return_value=mock_model):
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                mock_sys.stdin = io.StringIO('{"op": "shutdown"}\n')
                mock_sys.stdout = io.StringIO()
                with patch.dict(
                    "os.environ",
                    {"JARVIS_STT_MODEL": "medium",
                     "JARVIS_STT_LANGUAGE": "cs+vi"},
                    clear=False,
                ):
                    rc = fw.worker_main()
                assert rc == 0
                args, kwargs = mock_model.transcribe.call_args
                assert kwargs.get("language") is None

    def test_warmup_with_single_code_forces_it(self):
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (iter([]), MagicMock())

        with patch("faster_whisper.WhisperModel",
                   return_value=mock_model):
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                mock_sys.stdin = io.StringIO('{"op": "shutdown"}\n')
                mock_sys.stdout = io.StringIO()
                with patch.dict(
                    "os.environ",
                    {"JARVIS_STT_MODEL": "medium",
                     "JARVIS_STT_LANGUAGE": "cs"},
                    clear=False,
                ):
                    rc = fw.worker_main()
                assert rc == 0
                args, kwargs = mock_model.transcribe.call_args
                assert kwargs.get("language") == "cs"

    def test_request_with_selector_never_crashes(self):
        """A stray 'cs+vi' in a request falls back to auto-detect."""
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (iter([]), MagicMock())

        with patch("faster_whisper.WhisperModel",
                   return_value=mock_model):
            with patch("jarvis.listening.fasterwhisper_worker.sys") as mock_sys:
                mock_sys.platform = "linux"
                mock_sys.stdin = io.StringIO(
                    '{"id": 1, "op": "transcribe", '
                    '"audio_b64": "' + _silence_b64() + '", '
                    '"language": "cs+vi"}\n'
                    '{"op": "shutdown"}\n')
                mock_sys.stdout = io.StringIO()
                rc = fw.worker_main()
                assert rc == 0
                out = mock_sys.stdout.getvalue()
                assert '"ok": true' in out
                assert '"error"' not in out
                calls = mock_model.transcribe.call_args_list
                assert calls[-1].kwargs.get("language") is None