"""Toastovač Video Server — the "own player" path (Path 3).

Serves:
- the Apple-design player page (video_player/) to any LAN browser/TV
- HLS streams produced by YouTubeDownloader (yt-dlp + cookies → remux)
- YouTube Data API endpoints: comments / stats / reactions / captions
- original + LLM-translated subtitles as VTT for the overlay renderer
- a command bridge so Toastovač voice intents drive the player

Port 8766. LAN-only by design; the TV browser opens ``http://<pc-ip>:8766/``.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from ..debug import debug_log
from . import yt_api
from . import yt_download

_PORT = 8766
_PLAYER_ROOT = Path(__file__).resolve().parent / "video_player"
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".m3u8": "application/vnd.apple.mpegurl",
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".vtt": "text/vtt; charset=utf-8",
}

# Transcript cache: video_id -> cues list
_transcript_cache: Dict[str, List[Dict[str, Any]]] = {}
# Translated-subtitle cache: (video_id, lang) -> cues
_translated_cache: Dict[tuple, List[Dict[str, Any]]] = {}
# Player command queue (voice bridge): list of {"cmd": ..., "payload": {...}}
_player_commands: List[Dict[str, Any]] = []
_player_commands_lock = threading.Lock()
# Last ready asset so the player can reconnect after reload
_last_asset: Optional[yt_download.VideoAsset] = None
_last_asset_lock = threading.Lock()

_server: Optional["VideoServer"] = None


def push_player_command(cmd: str, payload: Optional[Dict[str, Any]] = None) -> None:
    """Append a command for the player page (used by the voice bridge)."""
    with _player_commands_lock:
        _player_commands.append({"cmd": cmd, "payload": payload or {}})
        if len(_player_commands) > 200:
            del _player_commands[:-200]


def drain_player_commands() -> List[Dict[str, Any]]:
    with _player_commands_lock:
        out = list(_player_commands)
        _player_commands.clear()
        return out


class _Handler(BaseHTTPRequestHandler):
    server_version = "ToastovacVideo/1"

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, rel: str) -> None:
        rel = rel.lstrip("/")
        if ".." in rel:
            self._text(400, b"bad path", "text/plain")
            return
        target = (_PLAYER_ROOT / rel).resolve()
        if not str(target).startswith(str(_PLAYER_ROOT.resolve())):
            self._text(403, b"forbidden", "text/plain")
            return
        if not target.is_file():
            self._text(404, b"not found", "text/plain")
            return
        data = target.read_bytes()
        ctype = _MIME.get(target.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _stream_file(self, rel: str) -> None:
        """Serve files from the videos root (HLS + thumbs) with Range."""
        video_id, _, name = rel.partition("/")
        if not name:
            self._text(404, b"missing file", "text/plain")
            return
        base = yt_download._VIDEO_ROOT / video_id
        target = (base / name).resolve()
        if not str(target).startswith(str(base.resolve())) or not target.is_file():
            self._text(404, b"not found", "text/plain")
            return
        data = target.read_bytes()
        ctype = _MIME.get(target.suffix.lower(), "application/octet-stream")
        range_header = self.headers.get("Range")
        if range_header:
            m = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if m:
                start = int(m.group(1) or 0)
                end = int(m.group(2) or len(data) - 1)
                end = min(end, len(data) - 1)
                chunk = data[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range",
                                 f"bytes {start}-{end}/{len(data)}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                self.wfile.write(chunk)
                return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _query(self) -> Dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _server(self) -> "VideoServer":
        return self.server.video_server  # type: ignore[attr-defined]

    # ── GET ───────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        q = self._query()
        srv = self._server()
        debug_log(f"VIDEO_REQ GET {self.path} UA={self.headers.get('User-Agent', '')[:140]}", "video")

        if path in ("/", "/index.html"):
            self._static("index.html")
            return
        if path == "/app.js":
            self._static("app.js")
            return
        if path == "/style.css":
            self._static("style.css")
            return
        if path.startswith("/vendor/"):
            self._static(path[1:])
            return

        if path == "/video/status":
            job = srv.active_job()
            with _last_asset_lock:
                last = _last_asset
            payload = {"job": job.snapshot() if job else None,
                       "last": srv._asset_payload(last) if last else None}
            self._json(200, payload)
            return

        if path.startswith("/video/stream/"):
            rel = path[len("/video/stream/"):]
            self._stream_file(rel)
            return
        if path.startswith("/video/thumb/"):
            self._stream_file(path[len("/video/thumb/"):])
            return

        video_id = q.get("id", "")
        if path == "/video/preview":
            try:
                self._json(200, srv.preview(video_id))
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return
        if path == "/video/comments":
            try:
                order = q.get("order", "relevance")
                self._json(200, {"comments": yt_api.comments(video_id, order=order)})
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return
        if path == "/video/stats":
            try:
                self._json(200, yt_api.top_reactions(video_id))
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return
        if path == "/video/summary":
            try:
                self._json(200, srv.summary(video_id))
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return
        if path == "/video/ask":
            question = q.get("q", "")
            try:
                self._json(200, srv.ask(video_id, question))
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return
        if path == "/video/subtitles":
            mode = q.get("mode", "original")
            lang = q.get("lang", "en")
            try:
                vtt = srv.subtitles_vtt(video_id, mode=mode, lang=lang)
                self._text(200, vtt.encode("utf-8"), "text/vtt; charset=utf-8")
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return

        if path == "/player/commands":
            self._json(200, {"commands": drain_player_commands()})
            return

        self._json(404, {"error": "unknown route"})

    # ── POST ──────────────────────────────────────────────────────────
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}") if length else {}
        except Exception:
            body = {}
        srv = self._server()

        if path == "/video/play":
            video_id = str(body.get("id") or body.get("video_id") or "")
            transcode = bool(body.get("transcode", False))
            max_height = int(body.get("max_height") or 2160)
            debug_log(f"VIDEO_POST /video/play id={video_id} "
                      f"transcode={transcode}", "video")
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", video_id or ""):
                self._json(400, {"error": "invalid video id"})
                return
            job = srv.play(video_id, transcode=transcode, max_height=max_height)
            self._json(200, {"job": job.snapshot()})
            return

        if path == "/video/command":
            cmd = str(body.get("cmd") or "")
            payload = body.get("payload") or {}
            if cmd:
                push_player_command(cmd, payload)
            self._json(200, {"ok": True})
            return

        if path == "/video/probe":
            debug_log(f"VIDEO_PROBE {json.dumps(body)[:220]}", "video")
            self._json(200, {"ok": True})
            return

        self._json(404, {"error": "unknown route"})

    def log_message(self, *args) -> None:  # quiet
        pass


class VideoServer:
    """LAN HTTP server: player + HLS + YouTube API + voice bridge."""

    def __init__(self, cfg: Any = None, port: int = _PORT) -> None:
        self.cfg = cfg
        self.port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._downloader = yt_download.get_downloader()
        self._job: Optional[yt_download.VideoJob] = None
        self._job_lock = threading.Lock()

    @property
    def url(self) -> str:
        from .cast import _lan_ip
        return f"http://{_lan_ip()}:{self.port}/"

    def start(self) -> bool:
        if self._httpd is not None:
            return True
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", self.port), _Handler)
            httpd.video_server = self  # type: ignore[attr-defined]
            self._httpd = httpd
        except OSError as exc:
            debug_log(f"video server: bind failed: {exc}", "everywhere")
            return False
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        name="video-server", daemon=True)
        self._thread.start()
        debug_log(f"video server: serving {self.url}", "everywhere")
        try:
            print(f"  🎬 Video player at {self.url} (open on the TV browser)",
                  flush=True)
        except UnicodeEncodeError:
            print(f"  Video player at {self.url} (open on the TV browser)",
                  flush=True)
        return True

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            self._httpd = None

    # ── jobs ──────────────────────────────────────────────────────────
    def play(self, video_id: str, transcode: bool = False,
             max_height: int = 2160) -> yt_download.VideoJob:
        with self._job_lock:
            if self._job is not None and self._job.video_id == video_id \
                    and self._job.state not in ("ready", "error"):
                return self._job
            self._job = self._downloader.start(video_id, transcode=transcode,
                                               max_height=max_height)
        return self._job

    def active_job(self) -> Optional[yt_download.VideoJob]:
        with self._job_lock:
            if self._job is not None and self._job.state in ("ready", "error"):
                with _last_asset_lock:
                    global _last_asset
                    if self._job.asset is not None:
                        _last_asset = self._job.asset
            return self._job

    def preview(self, video_id: str) -> Dict[str, Any]:
        return self._downloader.pick_preview(video_id)

    @staticmethod
    def _asset_payload(asset: yt_download.VideoAsset) -> Dict[str, Any]:
        return {
            "video_id": asset.video_id,
            "title": asset.title,
            "uploader": asset.uploader,
            "duration_sec": asset.duration_sec,
            "height": asset.height,
            "fps": asset.fps,
            "dynamic_range": asset.dynamic_range,
            "vcodec": asset.vcodec,
            "playlist": asset.playlist_rel,
            "thumbnail": asset.thumb_rel,
            "ready": asset.ready,
            "error": asset.error,
        }

    # ── transcript + subtitles ────────────────────────────────────────
    def _transcript(self, video_id: str) -> List[Dict[str, Any]]:
        if video_id not in _transcript_cache:
            _transcript_cache[video_id] = self._downloader.fetch_transcript(video_id)
        return _transcript_cache[video_id]

    def _llm(self, system: str, user: str) -> str:
        if self.cfg is None:
            return ""
        try:
            from ..reply.engine import chat_with_messages
            resp = chat_with_messages(
                self.cfg,
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                timeout_sec=120.0)
            if isinstance(resp, dict):
                msg = resp.get("message") or {}
                if isinstance(msg, dict):
                    return str(msg.get("content") or "")
        except Exception as exc:
            debug_log(f"video server: LLM call failed: {exc}", "everywhere")
        return ""

    def subtitles_vtt(self, video_id: str, mode: str = "original",
                      lang: str = "en") -> str:
        cues = self._transcript(video_id)
        if mode == "translated":
            cues = self._translate_cues(video_id, cues, lang)
        out = ["WEBVTT", ""]
        for i, cue in enumerate(cues, 1):
            out.append(f"{i}")
            out.append(f"{_fmt_vtt(cue['start'])} --> {_fmt_vtt(cue['end'])}")
            out.append(cue.get("text", ""))
            out.append("")
        return "\n".join(out)

    def _translate_cues(self, video_id: str, cues: List[Dict[str, Any]],
                        lang: str) -> List[Dict[str, Any]]:
        key = (video_id, lang)
        if key in _translated_cache:
            return _translated_cache[key]
        target_names = {"cs": "Czech", "en": "English", "de": "German",
                        "fr": "French", "es": "Spanish", "pl": "Polish",
                        "sk": "Slovak", "uk": "Ukrainian", "vi": "Vietnamese",
                        "ja": "Japanese", "ko": "Korean", "zh": "Chinese"}
        target = target_names.get(lang, lang)
        translated: List[Dict[str, Any]] = []
        batch: List[Dict[str, Any]] = []
        batch_size = 12
        for cue in cues:
            batch.append(cue)
            if len(batch) >= batch_size:
                translated.extend(self._translate_batch(batch, target))
                batch = []
        if batch:
            translated.extend(self._translate_batch(batch, target))
        _translated_cache[key] = translated
        return translated

    def _translate_batch(self, cues: List[Dict[str, Any]],
                         target: str) -> List[Dict[str, Any]]:
        system = ("You are a professional subtitle translator. Translate each "
                  f"line into {target}. Preserve the line count exactly. "
                  "Output ONLY the translated lines, one per line, in order, "
                  "with no numbering.")
        user = "\n".join(f"{i + 1}. {c['text']}" for i, c in enumerate(cues))
        text = self._llm(system, user).strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        out = []
        for i, cue in enumerate(cues):
            out.append({**cue, "text": lines[i] if i < len(lines) else cue["text"]})
        return out

    # ── analysis ──────────────────────────────────────────────────────
    def summary(self, video_id: str) -> Dict[str, Any]:
        cues = self._transcript(video_id)
        text = " ".join(c["text"] for c in cues)
        try:
            info = yt_api.video_info(video_id)
        except Exception:
            info = {}
        system = ("You are a sharp video analyst. Summarize the video from its "
                  "transcript: what it is about, key points, and whether it is "
                  "worth watching. Be concise (max 120 words), in the user's "
                  "language.")
        user = f"Title: {info.get('title', video_id)}\n\nTranscript:\n{text[:12000]}"
        result = self._llm(system, user)
        return {"video_id": video_id, "summary": result,
                "title": info.get("title", ""), "transcript_len": len(cues)}

    def ask(self, video_id: str, question: str) -> Dict[str, Any]:
        cues = self._transcript(video_id)
        text = " ".join(c["text"] for c in cues)
        try:
            top = yt_api.comments(video_id, max_results=10)
        except Exception:
            top = []
        comments_txt = "\n".join(
            f"- {c['author']}: {c['text']}" for c in top[:10])
        system = ("You are Toastovač's video companion. Answer the user's "
                  "question about the video using ONLY the transcript and "
                  "comments provided. Be honest when the transcript does not "
                  "cover something.")
        user = (f"Video transcript:\n{text[:14000]}\n\n"
                f"Top comments:\n{comments_txt}\n\n"
                f"Question: {question}")
        result = self._llm(system, user)
        return {"video_id": video_id, "question": question, "answer": result}


def _fmt_vtt(seconds: float) -> str:
    seconds = max(0.0, seconds)
    ms = int(round((seconds - int(seconds)) * 1000))
    s = int(seconds) % 60
    m = (int(seconds) // 60) % 60
    h = int(seconds) // 3600
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def get_video_server(cfg: Any = None) -> VideoServer:
    global _server
    if _server is None:
        _server = VideoServer(cfg)
    return _server


def start_video_server(cfg: Any = None) -> Optional[VideoServer]:
    """Start the video server (daemon integration, best-effort)."""
    srv = get_video_server(cfg)
    return srv if srv.start() else None


def stop_video_server() -> None:
    global _server
    if _server is not None:
        _server.stop()
        _server = None


if __name__ == "__main__":
    import sys
    srv = start_video_server(None)
    if srv is None:
        print("video server failed to start")
        sys.exit(1)
    print(f"video server: {srv.url}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop_video_server()