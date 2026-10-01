"""Kodi JSON-RPC client — play YouTube on the TV's Kodi with 4K/HDR.

This is the quality-correct path for YouTube playback on the TV:

- Kodi on the TV decodes video with the TV's hardware decoder (4K/HDR),
  the PC does zero video work.
- ``plugin.video.youtube`` (signed in with the user's Google account)
  plays any video ID — regular videos and Shorts — and picks high-tier
  formats (2160p60, VP9.2 HDR when the TV supports it).
- Live translated subtitles: the PC serves a rolling SRT over the LAN
  cast server (``cast.py`` → ``/subtitles.srt``) and this client keeps
  telling Kodi to re-read that URL so cues track the live translation
  stream. Kodi renders them as the subtitle track.

Setup on the TV (one time):
1. Install Kodi (Android TV).
2. Settings → Services → Control → enable "Allow remote control via HTTP",
   set port (default 8080) and a username/password.
3. Install ``plugin.video.youtube`` from the Kodi repo and sign in with
   the Google account (YouTube Premium).
4. Tell the PC the TV's IP / port / credentials.

CLI (once Kodi is reachable):

    python -m jarvis.everywhere.kodi_client play <video_id> [--subs http://<pc-ip>:8765/subtitles.srt]
    python -m jarvis.everywhere.kodi_client status
    python -m jarvis.everywhere.kodi_client pause|resume|stop|seek <sec>
    python -m jarvis.everywhere.kodi_client subs <srt-url> [--reload]

Env / flags: KODI_HOST, KODI_PORT, KODI_USER, KODI_PASS
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import requests

_LOG = logging.getLogger(__name__)

PLUGIN_PLAY_URL = "plugin://plugin.video.youtube/play/?video_id={video_id}"
# plugin.video.youtube quality setting; values include 1080p60, 1440p60,
# 2160p60, 4320p60, auto. 2160p60 is the 4K default (HDR variants are
# picked automatically when the TV supports them).
QUALITY_SETTING = "plugin.video.youtube.youtube.quality"
DEFAULT_QUALITY = "2160p60"
SUBTITLE_RELOAD_INTERVAL_SEC = 12.0


class KodiError(RuntimeError):
    """A JSON-RPC error returned by Kodi."""


class KodiClient:
    """Thin JSON-RPC client for a Kodi instance on the LAN."""

    def __init__(
        self,
        host: str,
        port: int = 8080,
        username: str = "",
        password: str = "",
        timeout: float = 8.0,
    ) -> None:
        self.host = host
        self.port = port
        self.auth: Optional[tuple] = (username, password) if username else None
        self.timeout = timeout
        self._url = f"http://{host}:{port}/jsonrpc"
        self._rid = 0
        self._sub_reload_thread: Optional[threading.Thread] = None
        self._sub_reload_stop = threading.Event()

    # ── JSON-RPC plumbing ─────────────────────────────────────────────
    def _call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        self._rid += 1
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": self._rid, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            resp = requests.post(
                self._url, json=payload, auth=self.auth, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise KodiError(f"cannot reach Kodi at {self._url}: {exc}") from exc
        if resp.status_code not in (200, 400):
            raise KodiError(f"Kodi HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if "error" in data:
            raise KodiError(
                f"Kodi method {method} error: "
                f"{data['error'].get('message', data['error'])}"
            )
        return data.get("result")

    # ── Playback ──────────────────────────────────────────────────────
    def play_youtube(
        self,
        video_id: str,
        quality: str = DEFAULT_QUALITY,
        set_quality: bool = True,
    ) -> bool:
        """Play a YouTube video (regular or Short) via plugin.video.youtube."""
        if set_quality:
            try:
                self._call(
                    "Settings.SetSettingValue",
                    {"setting": QUALITY_SETTING, "value": quality},
                )
                _LOG.info("kodi: youtube quality -> %s", quality)
            except KodiError as exc:
                _LOG.warning("kodi: could not set quality (%s); continuing", exc)
        result = self._call(
            "Player.Open",
            {"item": {"file": PLUGIN_PLAY_URL.format(video_id=video_id)}},
        )
        _LOG.info("kodi: opened youtube video %s", video_id)
        return bool(result)

    def pause(self) -> bool:
        return self._player_command("Player.PlayPause", {"play": False})

    def resume(self) -> bool:
        return self._player_command("Player.PlayPause", {"play": True})

    def stop(self) -> bool:
        return self._player_command("Player.Stop")

    def seek(self, seconds: float) -> bool:
        return self._player_command(
            "Player.Seek", {"value": {"seconds": int(seconds)}}
        )

    def set_volume(self, volume: int) -> bool:
        """Volume in percent (0-100)."""
        return bool(
            self._call("Application.SetVolume", {"volume": max(0, min(100, int(volume)))})
        )

    def _player_command(self, method: str, params: Optional[Dict[str, Any]] = None) -> bool:
        pids = self._active_player_ids()
        if not pids:
            raise KodiError("no active player on Kodi")
        for pid in pids:
            merged = dict(params or {})
            merged.setdefault("playerid", pid)
            self._call(method, merged)
        return True

    # ── Status ────────────────────────────────────────────────────────
    def _active_player_ids(self) -> List[int]:
        active = self._call("Player.GetActivePlayers")
        if not isinstance(active, list):
            return []
        return [int(p["playerid"]) for p in active if isinstance(p, dict)]

    def status(self) -> Dict[str, Any]:
        pids = self._active_player_ids()
        if not pids:
            return {"playing": False, "active_players": []}
        pid = pids[0]
        props = self._call(
            "Player.GetProperties",
            {
                "playerid": pid,
                "properties": [
                    "time", "totaltime", "speed",
                    "subtitleenabled", "currentsubtitle", "currentaudiostream",
                ],
            },
        )
        item = self._call(
            "Player.GetItem",
            {"playerid": pid, "properties": ["file", "title", "type"]},
        )
        return {"playing": True, "playerid": pid, **props, "item": item}

    # ── Live subtitles ────────────────────────────────────────────────
    def set_subtitle_url(self, url: str, enable: bool = True) -> bool:
        """Point the active player's subtitle track at an external SRT URL.

        Kodi (18+) accepts a URL here and loads it as an external subtitle
        file. Calling this periodically re-reads the file, which is how the
        rolling SRT feed stays live.
        """
        pids = self._active_player_ids()
        if not pids:
            raise KodiError("no active player on Kodi")
        ok = True
        for pid in pids:
            result = self._call(
                "Player.SetSubtitle",
                {"playerid": pid, "subtitle": url, "enable": enable},
            )
            ok = ok and bool(result)
        _LOG.info("kodi: subtitle url -> %s (enable=%s)", url, enable)
        return ok

    def start_subtitle_stream(
        self, url: str, interval: float = SUBTITLE_RELOAD_INTERVAL_SEC
    ) -> None:
        """Background thread: keep Kodi re-reading the rolling SRT."""
        self.stop_subtitle_stream()
        self._sub_reload_stop.clear()

        def _loop() -> None:
            while not self._sub_reload_stop.is_set():
                try:
                    self.set_subtitle_url(url, enable=True)
                except Exception as exc:
                    _LOG.warning("kodi: subtitle reload failed: %s", exc)
                self._sub_reload_stop.wait(interval)

        self._sub_reload_thread = threading.Thread(
            target=_loop, name="kodi-subtitle-reload", daemon=True
        )
        self._sub_reload_thread.start()
        _LOG.info("kodi: subtitle stream started (%ss reload)", interval)

    def stop_subtitle_stream(self) -> None:
        self._sub_reload_stop.set()
        if self._sub_reload_thread is not None:
            self._sub_reload_thread.join(timeout=2)
            self._sub_reload_thread = None


def from_env() -> KodiClient:
    host = os.environ.get("KODI_HOST", "")
    if not host:
        raise KodiError("KODI_HOST not set (and no --host given)")
    return KodiClient(
        host=host,
        port=int(os.environ.get("KODI_PORT", "8080")),
        username=os.environ.get("KODI_USER", ""),
        password=os.environ.get("KODI_PASS", ""),
    )


def _say(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:  # cp1252 console
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kodi_client",
        description="Control Kodi on the TV over JSON-RPC (YouTube + live SRT).",
    )
    parser.add_argument("--host", default=os.environ.get("KODI_HOST", ""))
    parser.add_argument("--port", type=int, default=int(os.environ.get("KODI_PORT", "8080")))
    parser.add_argument("--user", default=os.environ.get("KODI_USER", ""))
    parser.add_argument("--pass", dest="password", default=os.environ.get("KODI_PASS", ""))
    sub = parser.add_subparsers(dest="command", required=True)

    p_play = sub.add_parser("play", help="play a YouTube video (or Short) by ID")
    p_play.add_argument("video_id")
    p_play.add_argument("--quality", default=DEFAULT_QUALITY)
    p_play.add_argument("--subs", default="", help="SRT URL to stream live subtitles from")

    p_subs = sub.add_parser("subs", help="point subtitles at an SRT URL")
    p_subs.add_argument("url")
    p_subs.add_argument("--reload", action="store_true", help="keep re-reading the URL")

    sub.add_parser("status", help="show playback status")
    sub.add_parser("pause", help="pause playback")
    sub.add_parser("resume", help="resume playback")
    sub.add_parser("stop", help="stop playback")
    p_seek = sub.add_parser("seek", help="seek to seconds")
    p_seek.add_argument("seconds", type=float)
    p_vol = sub.add_parser("volume", help="set volume 0-100")
    p_vol.add_argument("percent", type=int)

    args = parser.parse_args(argv)
    if not args.host:
        print("KODI_HOST is not set; pass --host <tv-ip> (and --user/--pass if Kodi requires them)")
        return 2

    client = KodiClient(args.host, args.port, args.user, args.password)
    try:
        if args.command == "play":
            client.play_youtube(args.video_id, quality=args.quality)
            print(f"▶ playing {args.video_id} on Kodi @ {args.host}")
            if args.subs:
                client.start_subtitle_stream(args.subs)
                print(f"ℹ subtitles streaming from {args.subs}")
        elif args.command == "subs":
            if args.reload:
                client.start_subtitle_stream(args.url)
            else:
                client.set_subtitle_url(args.url)
        elif args.command == "status":
            print(json.dumps(client.status(), indent=2, ensure_ascii=False))
        elif args.command == "pause":
            client.pause(); print("⏸ paused")
        elif args.command == "resume":
            client.resume(); print("▶ resumed")
        elif args.command == "stop":
            client.stop(); print("⏹ stopped")
        elif args.command == "seek":
            client.seek(args.seconds); print(f"⏩ seek {args.seconds}s")
        elif args.command == "volume":
            client.set_volume(args.percent); print(f"🔊 volume {args.percent}%")
    except KodiError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())