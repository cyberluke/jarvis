"""yt-dlp wrapper — download YouTube videos (cookies/Premium) and serve as HLS.

Pipeline for the "own player" path:
  1. Resolve formats with the user's YouTube cookies (yt_cookies.txt).
  2. Pick the best video+audio pair (prefer 4K / HDR VP9 / AV1, fall back to
     H.264), download and merge to a single MP4 via yt-dlp.
  3. Remux (stream copy — zero re-encode, no GPU) to fMP4 HLS segments so the
     TV browser can play with full seeking. Optional QSV/libx264 transcode
     when the TV cannot decode the source codec.
  4. Transcript (original subtitles) via yt-dlp ``--write-subs``.

Everything lands in ``%LOCALAPPDATA%/VIVERRA/Toastovac/videos/<video_id>/``
(outside the repo), so credentials never touch the codebase.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

_VIDEO_ROOT = (
    Path(os.environ.get("LOCALAPPDATA") or str(Path.home()))
    / "VIVERRA" / "Toastovac" / "videos"
)


def cookies_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home()))
    return base / "VIVERRA" / "Toastovac" / "yt_cookies.txt"


def config_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home()))
    return base / "VIVERRA" / "Toastovac" / "video_config.json"


def load_config() -> Dict[str, str]:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def youtube_api_key(cfg: Any = None) -> str:
    """API key from config object, env, or the local video_config.json."""
    key = ""
    if cfg is not None:
        key = str(getattr(cfg, "youtube_api_key", "") or "")
    if not key:
        key = os.environ.get("YOUTUBE_API_KEY", "")
    if not key:
        key = load_config().get("youtube_api_key", "")
    return key.strip()


@dataclass
class VideoAsset:
    """A downloaded + remuxed video ready to serve."""

    video_id: str
    title: str = ""
    uploader: str = ""
    duration_sec: float = 0.0
    height: int = 0
    fps: int = 0
    dynamic_range: str = ""
    vcodec: str = ""
    acodec: str = ""
    thumbnail_url: str = ""
    playlist_rel: str = ""        # relative URL path of the HLS playlist
    thumb_rel: str = ""
    mp4_rel: str = ""             # progressive MP4 fallback (no MSE)
    local_dir: Optional[Path] = None
    transcode: bool = False
    error: str = ""
    ready: bool = False


class VideoJob:
    """A background download/remux job with progress state."""

    def __init__(self, video_id: str, transcode: bool = False) -> None:
        self.video_id = video_id
        self.transcode = transcode
        self.state = "idle"          # idle|resolving|downloading|remuxing|ready|error
        self.progress: float = 0.0   # 0..1
        self.message = ""
        self.started_at = time.time()
        self.asset: Optional[VideoAsset] = None
        self.lock = threading.Lock()
        self._cancel = threading.Event()

    def set(self, state: str, message: str = "", progress: float = 0.0) -> None:
        with self.lock:
            self.state = state
            self.message = message
            self.progress = progress

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "video_id": self.video_id,
                "state": self.state,
                "progress": round(self.progress, 3),
                "message": self.message,
                "transcode": self.transcode,
                "asset": (
                    {
                        "title": self.asset.title,
                        "uploader": self.asset.uploader,
                        "duration_sec": self.asset.duration_sec,
                        "height": self.asset.height,
                        "fps": self.asset.fps,
                        "dynamic_range": self.asset.dynamic_range,
                        "vcodec": self.asset.vcodec,
                        "playlist": self.asset.playlist_rel,
                        "mp4": self.asset.mp4_rel,
                        "thumbnail": self.asset.thumb_rel,
                        "ready": self.asset.ready,
                        "error": self.asset.error,
                    }
                    if self.asset else None
                ),
            }


def _is_hdr(f: Dict[str, Any]) -> bool:
    """True when a format carries HDR.

    yt-dlp's ``dynamic_range`` field is populated unreliably (it can come
    back empty/"SDR" for the same HDR format across resolves), so fall back
    to the format note and the codec profile: vp09.02 / av01 *M.10* are
    10-bit (Profile 2) streams, which YouTube only emits for HDR content.
    """
    dr = (f.get("dynamic_range") or "").upper()
    if "HDR" in dr:
        return True
    note = (f.get("format_note") or "").upper()
    if "HDR" in note:
        return True
    vcodec = (f.get("vcodec") or "").lower()
    if vcodec.startswith("vp09.02") or vcodec.startswith("vp9.2"):
        return True
    if vcodec.startswith("av01") and ".10." in vcodec:
        return True
    return False


def _hdr_label(f: Dict[str, Any]) -> str:
    dr = (f.get("dynamic_range") or "").upper()
    if dr in ("HDR10", "HDR10+", "HLG", "HDR"):
        return dr
    return "HDR" if _is_hdr(f) else "SDR"


def _pick_formats(info: Dict[str, Any], max_height: int = 2160) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Pick best video + audio pair.

    Preference: HDR > SDR, tallest <= max_height, VP9/AV1 over H.264.
    The height cap is applied to the SHORT side (min(w,h)): for landscape
    that is the height (2160 ≈ 4K), but for portrait Shorts it is the width,
    so vertical tiers like 1296x2304 are NOT discarded by a landscape cap.
    """
    formats = info.get("formats") or []
    videos = [f for f in formats if (f.get("vcodec") or "") not in ("none", "") and f.get("acodec") in ("none", "")]
    audios = [f for f in formats if (f.get("acodec") or "") != "none" and f.get("vcodec") in ("none", "")]

    def sort_key(f: Dict[str, Any]) -> Tuple[int, int, int, int]:
        hdr = 1 if _is_hdr(f) else 0
        codec = f.get("vcodec") or ""
        vp9 = 2 if codec.startswith("vp9") else (1 if codec.startswith("av01") else 0)
        height = int(f.get("height") or 0)
        return (hdr, vp9, height, int(f.get("fps") or 0))

    def audio_key(f: Dict[str, Any]) -> Tuple[int, int]:
        codec = f.get("acodec") or ""
        opus = 2 if codec.startswith("opus") else (1 if codec.startswith("mp4a") else 0)
        return (opus, int(f.get("abr") or 0))

    viable = []
    for f in videos:
        h = int(f.get("height") or 0)
        w = int(f.get("width") or 0)
        if h <= 0 or min(h, w or h) > max_height:
            continue
        viable.append(f)
    video = max(viable, key=sort_key) if viable else None
    audio = max(audios, key=audio_key) if audios else None
    return video, audio


_ISO_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$")


def _iso_duration_to_sec(value: str) -> float:
    m = _ISO_DURATION_RE.match(value or "")
    if not m:
        return 0.0
    h, mi, s = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def _enrich_meta(video_id: str, out_dir: Path, meta: Dict[str, Any],
                 ffmpeg: str = "ffmpeg") -> Dict[str, Any]:
    """Fill missing metadata for cached assets: title/channel/duration/thumb
    via the Data API, height/fps/codec via ffprobe on the cached source.mp4.
    Persists back to meta.json when anything changed."""
    if not isinstance(meta, dict):
        meta = {}
    changed = False
    if not meta.get("title") or not meta.get("uploader"):
        try:
            from .yt_api import video_info
            info = video_info(video_id)
            if not meta.get("title"):
                meta["title"] = info.get("title") or video_id
            if not meta.get("uploader"):
                meta["uploader"] = info.get("channel") or ""
            if not meta.get("duration_sec"):
                meta["duration_sec"] = _iso_duration_to_sec(info.get("duration") or "")
            if not meta.get("thumbnail_url"):
                th = info.get("thumbnails") or {}
                for size in ("maxres", "standard", "high", "medium"):
                    if th.get(size, {}).get("url"):
                        meta["thumbnail_url"] = th[size]["url"]
                        break
            changed = True
        except Exception as exc:
            print(f"[video] meta enrich (API) failed for {video_id}: {exc}")
    if not meta.get("probed"):
        src = out_dir / "source.mp4"
        if src.is_file():
            probe = shutil.which("ffprobe") or str(Path(ffmpeg).with_name("ffprobe"))
            try:
                proc = subprocess.run(
                    [probe, "-v", "error", "-select_streams", "v:0",
                     "-show_entries",
                     "stream=width,height,r_frame_rate,codec_name,color_transfer:format=duration",
                     "-of", "json", str(src)],
                    capture_output=True, text=True, timeout=120)
                if proc.returncode == 0:
                    data = json.loads(proc.stdout or "{}")
                    st = (data.get("streams") or [{}])[0]
                    if st.get("height"):
                        meta["height"] = int(st["height"])
                    if st.get("codec_name"):
                        meta["vcodec"] = st["codec_name"]
                    fps = st.get("r_frame_rate") or ""
                    if "/" in fps:
                        num, den = fps.split("/", 1)
                        try:
                            meta["fps"] = round(float(num) / max(1.0, float(den)))
                        except Exception:
                            pass
                    if st.get("color_transfer") in ("smpte2084", "arib-std-b67"):
                        meta["dynamic_range"] = "HDR"
                    elif not meta.get("dynamic_range"):
                        meta["dynamic_range"] = "SDR"
                    dur = (data.get("format") or {}).get("duration")
                    if dur and not meta.get("duration_sec"):
                        try:
                            meta["duration_sec"] = float(dur)
                        except Exception:
                            pass
                    meta["probed"] = True
                    changed = True
            except Exception as exc:
                print(f"[video] meta enrich (probe) failed for {video_id}: {exc}")
    if changed:
        try:
            (out_dir / "meta.json").write_text(json.dumps({
                "title": meta.get("title", ""),
                "uploader": meta.get("uploader", ""),
                "duration_sec": meta.get("duration_sec", 0),
                "height": meta.get("height", 0),
                "fps": meta.get("fps", 0),
                "dynamic_range": meta.get("dynamic_range", ""),
                "vcodec": meta.get("vcodec", ""),
                "acodec": meta.get("acodec", ""),
                "thumbnail_url": meta.get("thumbnail_url", ""),
                "transcoded_codec": meta.get("transcoded_codec", ""),
                "probed": meta.get("probed", False),
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return meta


class YouTubeDownloader:
    """yt-dlp-backed downloader with cookies and HLS remuxing."""

    def __init__(self, cookies: Optional[Path] = None, ffmpeg: Optional[str] = None) -> None:
        self.cookies = cookies or cookies_path()
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg") or "ffmpeg"
        self._jobs: Dict[str, VideoJob] = {}
        self._lock = threading.Lock()

    # ── shared yt-dlp opts ────────────────────────────────────────────
    def _base_opts(self, quiet: bool = True) -> Dict[str, Any]:
        opts: Dict[str, Any] = {
            "quiet": quiet,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 20,
        }
        if self.cookies.is_file():
            opts["cookiefile"] = str(self.cookies)
        return opts

    # ── resolve ───────────────────────────────────────────────────────
    def resolve(self, video_id: str) -> Dict[str, Any]:
        import yt_dlp
        opts = self._base_opts()
        opts.update({"skip_download": True})
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(video_id, download=False)

    def pick_preview(self, video_id: str) -> Dict[str, Any]:
        """Resolve and return the chosen format summary (no download)."""
        info = self.resolve(video_id)
        video, audio = _pick_formats(info)
        available = [
            {
                "format_id": f.get("format_id"),
                "height": f.get("height"),
                "fps": f.get("fps"),
                "vcodec": f.get("vcodec"),
                "dynamic_range": f.get("dynamic_range", ""),
            }
            for f in (info.get("formats") or [])
            if (f.get("vcodec") or "") not in ("none", "") and f.get("acodec") in ("none", "")
        ]
        # show the highest formats first (dedupe by height+codec)
        seen = set()
        top = []
        for f in sorted(available, key=lambda x: (x["height"] or 0, x["fps"] or 0), reverse=True):
            key = (f["height"], f["vcodec"], f["dynamic_range"])
            if key in seen:
                continue
            seen.add(key)
            top.append(f)
            if len(top) >= 12:
                break
        return {
            "video_id": video_id,
            "title": info.get("title", ""),
            "uploader": info.get("uploader", ""),
            "duration_sec": float(info.get("duration") or 0),
            "thumbnail": info.get("thumbnail", ""),
            "chosen": {
                "video": (video or {}).get("format_id"),
                "vcodec": (video or {}).get("vcodec"),
                "height": (video or {}).get("height"),
                "fps": (video or {}).get("fps"),
                "dynamic_range": _hdr_label(video or {}),
                "audio": (audio or {}).get("format_id"),
                "acodec": (audio or {}).get("acodec"),
            },
            "available": top,
        }

    # ── download + remux ──────────────────────────────────────────────
    def start(self, video_id: str, transcode: bool = False,
              max_height: int = 2160) -> VideoJob:
        with self._lock:
            old = self._jobs.get(video_id)
            if old is not None:
                if old.state not in ("ready", "error"):
                    return old  # in-flight — cannot change transcode mid-flight
                if old.transcode == transcode:
                    return old  # finished with identical settings
                # Finished with a different transcode flag: re-run (the
                # cached fast path makes this instant).
                self._jobs.pop(video_id, None)
            job = VideoJob(video_id, transcode=transcode)
            self._jobs[video_id] = job
        thread = threading.Thread(
            target=self._run_job, args=(job, max_height), name=f"yt-dl-{video_id}",
            daemon=True)
        thread.start()
        return job

    def _run_job(self, job: VideoJob, max_height: int) -> None:
        import yt_dlp
        out_dir = _VIDEO_ROOT / job.video_id
        out_dir.mkdir(parents=True, exist_ok=True)
        # Fast path: already downloaded + remuxed on disk → serve from cache.
        cached_playlist = out_dir / "hls" / "index.m3u8"
        if cached_playlist.is_file() and (out_dir / "hls" / "init.mp4").is_file():
            job.set("resolving", "Found cached copy…", 0.05)
            meta_path = out_dir / "meta.json"
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
            else:
                meta = {}
            meta = _enrich_meta(job.video_id, out_dir, meta, self.ffmpeg)
            asset = VideoAsset(
                video_id=job.video_id,
                title=str(meta.get("title") or job.video_id),
                uploader=str(meta.get("uploader") or ""),
                duration_sec=float(meta.get("duration_sec") or 0),
                height=int(meta.get("height") or 0),
                fps=int(meta.get("fps") or 0),
                dynamic_range=_hdr_label({
                    "dynamic_range": meta.get("dynamic_range") or "",
                    "vcodec": meta.get("vcodec") or "",
                    "format_note": meta.get("format_note") or "",
                }),
                vcodec=str(meta.get("vcodec") or ""),
                acodec=str(meta.get("acodec") or ""),
                thumbnail_url=str(meta.get("thumbnail_url") or ""),
                playlist_rel=f"/video/stream/{job.video_id}/hls/index.m3u8",
                thumb_rel=f"/video/thumb/{job.video_id}/thumb.jpg",
                mp4_rel=f"/video/stream/{job.video_id}/source.mp4",
                local_dir=out_dir,
                ready=True,
            )
            self._ensure_transcode(job, out_dir, asset, meta)
            self._save_thumb(asset)
            job.asset = asset
            job.set("ready", "Ready (cached)", 1.0)
            try:
                (out_dir / "meta.json").write_text(json.dumps({
                    "title": meta.get("title", ""),
                    "uploader": meta.get("uploader", ""),
                    "duration_sec": meta.get("duration_sec", 0),
                    "height": meta.get("height", 0),
                    "fps": meta.get("fps", 0),
                    "dynamic_range": meta.get("dynamic_range", ""),
                    "vcodec": meta.get("vcodec", ""),
                    "acodec": meta.get("acodec", ""),
                    "thumbnail_url": meta.get("thumbnail_url", ""),
                    "transcoded_codec": meta.get("transcoded_codec", ""),
                    "probed": meta.get("probed", False),
                }, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            return
        try:
            job.set("resolving", "Resolving formats with your account…", 0.02)
            info = self.resolve(job.video_id)
            video, audio = _pick_formats(info, max_height=max_height)
            if video is None or audio is None:
                raise RuntimeError("no compatible video/audio formats found")
            height = int(video.get("height") or 0)
            hdr = _is_hdr(video)
            job.set("downloading", f"Downloading {height}p{' HDR' if hdr else ''} …", 0.1)

            formats = f"{video['format_id']}+{audio['format_id']}"
            merge = str(out_dir / "source.mp4")
            opts = self._base_opts(quiet=True)
            opts.update({
                "format": formats,
                "outtmpl": str(out_dir / "source.%(ext)s"),
                "merge_output_format": "mp4",
                "noprogress": True,
                "progress_hooks": [lambda d: self._progress(job, d)],
            })
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([job.video_id])

            if not Path(merge).is_file():
                candidates = list(out_dir.glob("source.*"))
                if not candidates:
                    raise RuntimeError("download produced no file")
                merge = str(candidates[0])

            job.set("remuxing", "Remuxing to HLS (stream copy)…", 0.92)
            self._remux(merge, out_dir)

            asset = VideoAsset(
                video_id=job.video_id,
                title=str(info.get("title") or job.video_id),
                uploader=str(info.get("uploader") or ""),
                duration_sec=float(info.get("duration") or 0),
                height=height,
                fps=int(video.get("fps") or 0),
                dynamic_range=_hdr_label(video),
                vcodec=str(video.get("vcodec") or ""),
                acodec=str(audio.get("acodec") or ""),
                thumbnail_url=str(info.get("thumbnail") or ""),
                playlist_rel=f"/video/stream/{job.video_id}/hls/index.m3u8",
                thumb_rel=f"/video/thumb/{job.video_id}/thumb.jpg",
                mp4_rel=f"/video/stream/{job.video_id}/source.mp4",
                local_dir=out_dir,
                transcode=job.transcode,
                ready=True,
            )
            meta = {
                "title": asset.title, "uploader": asset.uploader,
                "duration_sec": asset.duration_sec, "height": asset.height,
                "fps": asset.fps, "dynamic_range": asset.dynamic_range,
                "vcodec": asset.vcodec, "acodec": asset.acodec,
                "thumbnail_url": asset.thumbnail_url,
            }
            src_vcodec = asset.vcodec  # source codec survives the transcode
            self._ensure_transcode(job, out_dir, asset, meta)
            meta["vcodec"] = src_vcodec
            self._save_thumb(asset)
            job.asset = asset
            job.set("ready", "Ready", 1.0)
            # persist metadata so cache hits are instant
            try:
                (out_dir / "meta.json").write_text(json.dumps(
                    meta, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        except Exception as exc:
            job.asset = VideoAsset(video_id=job.video_id, error=str(exc))
            job.set("error", str(exc))

    def _progress(self, job: VideoJob, d: Dict[str, Any]) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                job.set("downloading", "Downloading…", 0.1 + 0.8 * min(1.0, done / total))

    def _remux(self, source: str, out_dir: Path, transcode: bool = False) -> None:
        hls_dir = out_dir / "hls"
        shutil.rmtree(hls_dir, ignore_errors=True)
        hls_dir.mkdir(parents=True, exist_ok=True)
        playlist = str(hls_dir / "index.m3u8")
        cmd = [self.ffmpeg, "-y", "-i", source, "-c", "copy",
               "-f", "hls", "-hls_time", "6", "-hls_list_size", "0",
               "-hls_playlist_type", "vod",
               "-hls_segment_type", "fmp4",
               "-hls_fmp4_init_filename", "init.mp4",
               "-hls_flags", "temp_file",
               playlist]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg remux failed: {(proc.stderr or '')[-400:]}")
        # ffmpeg 7.1.1 gyan builds reference init.mp4 in the playlist but never
        # write it — extract ftyp+moov from the source so hls.js can init MSE.
        init_bytes = _extract_init_mp4(Path(source).read_bytes())
        if init_bytes:
            (hls_dir / "init.mp4").write_bytes(init_bytes)
        else:
            raise RuntimeError("could not extract HLS init segment from source")
        # Faststart pass on the merged MP4: move moov to the front so the
        # progressive-MP4 fallback (native player, no MSE) seeks properly.
        fast = Path(source)
        tmp_fast = fast.with_suffix(".fast.mp4")
        proc = subprocess.run(
            [self.ffmpeg, "-y", "-i", str(fast), "-c", "copy",
             "-movflags", "+faststart", str(tmp_fast)],
            capture_output=True, text=True, timeout=1800)
        if proc.returncode == 0 and tmp_fast.is_file():
            fast.unlink(missing_ok=True)
            tmp_fast.rename(fast)

    def _ensure_transcode(self, job: VideoJob, out_dir: Path,
                          asset: VideoAsset, meta: Dict[str, Any]) -> None:
        """Point the MP4 asset at a TV-friendly transcode when required.

        The TV WebView cannot decode VP9/AV1 video tracks (audio-only or
        "media cannot be played"), so sources in those codecs are transcoded
        once to a single faststart MP4: HEVC Main10 with BT.2020/PQ signaling
        for HDR sources, H.264 otherwise (override with ``transcode_codec``
        in video_config.json). The result is cached in meta.json so later
        requests skip re-transcoding.
        """
        if not job.transcode:
            return
        src_codec = (asset.vcodec or "").split(".")[0].lower()
        if src_codec in ("h264", "avc", "hevc", "h265"):
            return  # already TV-friendly
        transcoded = out_dir / "transcoded.mp4"
        done = str(meta.get("transcoded_codec") or "")
        if done:
            asset.mp4_rel = f"/video/stream/{job.video_id}/transcoded.mp4"
            asset.vcodec = done
            return
        cfg = load_config()
        codec = str(cfg.get("transcode_codec") or "hevc").lower()
        if codec not in ("hevc", "h264"):
            codec = "hevc"
        job.set("transcoding", f"Transcoding for TV ({codec.upper()})…", 0.95)
        try:
            self._transcode_mp4(
                str(out_dir / "source.mp4"), transcoded,
                codec=codec, hdr=(asset.dynamic_range == "HDR"))
        except Exception as exc:
            job.set("error", f"transcode failed: {exc}")
            raise
        asset.mp4_rel = f"/video/stream/{job.video_id}/transcoded.mp4"
        asset.vcodec = codec
        meta["transcoded_codec"] = codec
        # meta["vcodec"] deliberately stays the SOURCE codec — it drives the
        # TV-friendliness check on the next request.

    def _transcode_mp4(self, source: str, dest: Path, codec: str = "hevc",
                       hdr: bool = False) -> None:
        """Transcode source → single faststart MP4 for the TV.

        Primary encoder: NVENC (RTX 4090, CUDA hwdecode). Falls back to QSV,
        then x265/x264. HDR sources become 10-bit BT.2020/PQ HEVC (hvc1 tag).
        Height is capped (``transcode_height`` in video_config.json, default
        1080) — TV WebView decode limits + bandwidth. The scale runs on CPU
        frames, so CUDA output stays in system memory (-hwaccel_output_format
        cuda is deliberately NOT used)."""
        dest = Path(dest)
        tmp = dest.with_name(dest.name + ".tmp")
        cap = int(str(load_config().get("transcode_height") or "1080"))
        scale = ["-vf", f"scale=-2:'min({cap},ih)'"] if cap > 0 else []
        if codec == "hevc":
            video = ["-hwaccel", "cuda",
                     "-c:v", "hevc_nvenc", "-preset", "p1", "-rc", "vbr",
                     "-cq", "26", "-tag:v", "hvc1", *scale]
            if hdr:
                video += ["-color_primaries", "bt2020", "-color_trc",
                          "smpte2084", "-colorspace", "bt2020nc"]
            fallbacks = [
                ["-c:v", "hevc_qsv", "-preset", "veryfast", "-global_quality", "24",
                 "-pix_fmt", "p010le", "-tag:v", "hvc1", *scale],
                ["-c:v", "libx265", "-preset", "fast", "-crf", "21",
                 "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1", *scale],
            ]
            if hdr:
                fallbacks[1] += self._x265_hdr_params(source)
        else:
            video = ["-hwaccel", "cuda",
                     "-c:v", "h264_nvenc", "-preset", "p1", "-rc", "vbr",
                     "-cq", "23", *scale]
            fallbacks = [
                ["-c:v", "h264_qsv", "-preset", "veryfast",
                 "-b:v", "12M", "-maxrate", "14M", "-bufsize", "20M", *scale],
                ["-c:v", "libx264", "-preset", "fast", "-crf", "20", *scale],
            ]
        cmd = [self.ffmpeg, "-y", "-i", source, *video,
               "-c:a", "aac", "-b:a", "192k",
               "-movflags", "+faststart", "-f", "mp4", str(tmp)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if proc.returncode != 0:
            for fb in fallbacks:
                tmp.unlink(missing_ok=True)
                cmd = [self.ffmpeg, "-y", "-i", source, *fb,
                       "-c:a", "aac", "-b:a", "192k",
                       "-movflags", "+faststart", "-f", "mp4", str(tmp)]
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=14400)
                if proc.returncode == 0:
                    break
        if proc.returncode != 0:
            raise RuntimeError(f"transcode failed: {(proc.stderr or '')[-400:]}")
        if tmp.is_file():
            tmp.rename(dest)

    def _x265_hdr_params(self, source: str) -> List[str]:
        """x265 master-display/cll params extracted from the source stream."""
        probe = shutil.which("ffprobe") or str(Path(self.ffmpeg).with_name("ffprobe"))
        try:
            proc = subprocess.run(
                [probe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream_side_data", "-of", "json", source],
                capture_output=True, text=True, timeout=60)
            data = json.loads(proc.stdout or "{}")
        except Exception:
            return []
        sd = ((data.get("streams") or [{}])[0].get("side_data_list")) or []

        def frac(v: Any, scale: float = 1.0, digits: int = 4) -> str:
            """Parse a 'num/den' fraction string (or number) → decimal."""
            try:
                if isinstance(v, str) and "/" in v:
                    n, d = v.split("/", 1)
                    return f"{float(n) / float(d) * scale:.{digits}f}"
                return f"{float(v) * scale:.{digits}f}"
            except Exception:
                return "0" if digits == 0 else "0." + "0" * digits

        md = cll = ""
        for item in sd:
            t = item.get("side_data_type", "")
            if t == "Mastering display metadata" and not md:
                def coord(key: str) -> str:
                    return (f"{frac(item.get(key + '_x'), 1, 4)},"
                            f"{frac(item.get(key + '_y'), 1, 4)}")
                md = (f"G({coord('green')})B({coord('blue')})R({coord('red')})"
                      f"WP({coord('white_point')})"
                      f"L({frac(item.get('max_luminance'), 10000, 0)},"
                      f"{frac(item.get('min_luminance'), 10000, 0)})")
            if t == "Content light level metadata":
                cll = f"{item.get('max_content', 0)},{item.get('max_average', 0)}"
        if not md:
            return []
        params = f"hdr10=1:master-display={md}"
        if cll:
            params += f":cll={cll}"
        return ["-x265-params", params]

    def _save_thumb(self, asset: VideoAsset) -> None:
        if not asset.thumbnail_url or asset.local_dir is None:
            return
        try:
            r = requests.get(asset.thumbnail_url, timeout=15)
            if r.status_code == 200:
                (asset.local_dir / "thumb.jpg").write_bytes(r.content)
        except Exception:
            pass

    # ── transcript ────────────────────────────────────────────────────
    def fetch_transcript(self, video_id: str, langs: Tuple[str, ...] = ("en", "cs")) -> List[Dict[str, Any]]:
        """Fetch original/auto subtitles as a cue list (start, end, text).

        Languages are tried in order; the first one that yields cues wins
        (mixing languages produces unusable transcripts for analysis).
        """
        import yt_dlp
        for lang in langs:
            tmp = _VIDEO_ROOT / video_id / "subs"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True, exist_ok=True)
            opts = self._base_opts(quiet=True)
            opts.update({
                "skip_download": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": [lang],
                "subtitlesformat": "vtt",
                "outtmpl": str(tmp / "%(id)s.%(ext)s"),
            })
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([video_id])
            except Exception:
                continue
            cues: List[Dict[str, Any]] = []
            for path in sorted(tmp.glob("*.vtt")):
                cues.extend(self._parse_vtt(path.read_text(encoding="utf-8", errors="replace"), lang))
            if cues:
                return cues
        return []

    @staticmethod
    def _parse_vtt(text: str, lang: str = "und") -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        timestamp = re.compile(
            r"(\d{1,2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})\.(\d{3})")

        def to_sec(h: str, m: str, s: str, ms: str) -> float:
            return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

        lines = text.splitlines()
        i = 0
        while i < len(lines):
            m = timestamp.search(lines[i])
            if m:
                start = to_sec(*m.groups()[:4])
                end = to_sec(*m.groups()[4:])
                cue_lines = []
                i += 1
                while i < len(lines) and lines[i].strip() and not timestamp.search(lines[i]):
                    cue_lines.append(lines[i].strip())
                    i += 1
                body = " ".join(cue_lines)
                # strip VTT inline styles like <c.xxx>
                body = re.sub(r"<[^>]+>", "", body)
                if body:
                    out.append({"start": round(start, 3), "end": round(end, 3),
                                "text": body, "lang": lang})
                continue
            i += 1
        return out


def _extract_init_mp4(data: bytes) -> bytes:
    """Extract the fMP4 init segment (ftyp [+free] + moov) from an MP4.

    Some ffmpeg builds emit ``#EXT-X-MAP:URI="init.mp4"`` without ever
    writing init.mp4; the HLS muxer still needs it. Box-walk the source to
    pull ftyp (and any pre-mdat boxes) plus moov, in file order.
    """
    def top_boxes(limit: int):
        boxes = []
        off = 0
        while off + 8 <= limit:
            size = int.from_bytes(data[off:off + 4], "big")
            typ = data[off + 4:off + 8].decode("latin1", "replace")
            if size < 8 or off + size > len(data):
                break
            boxes.append((typ, off, size))
            off += size
        return boxes

    pre = []
    moov = None
    for typ, off, size in top_boxes(len(data)):
        if typ == "moov":
            moov = (off, size)
            break
        if typ == "mdat":
            break
        pre.append((off, size))
    if moov is None:
        return b""
    parts = [data[o:o + s] for o, s in pre]
    moff, msize = moov
    parts.append(data[moff:moff + msize])
    return b"".join(parts)


_downloader: Optional[YouTubeDownloader] = None


def get_downloader() -> YouTubeDownloader:
    global _downloader
    if _downloader is None:
        _downloader = YouTubeDownloader()
    return _downloader