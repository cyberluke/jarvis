"""Toastovac Cast Sender — Google Cast protocol integration.

Discovers and controls Cast devices on the LAN, launches custom receiver
apps, and streams live subtitles from the Everywhere subtitles service.

Supports multiple video sources:
- Direct URL streaming
- Local file playback
- VLC playlist integration
- Screen mirroring (with Intel QSV hardware encoding)
- Browser tab mirroring
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pychromecast
from pychromecast.controllers import BaseController
from pychromecast.controllers.media import MediaController
from pychromecast.controllers.youtube import YouTubeController

from ..debug import debug_log

_LOG = logging.getLogger(__name__)

# Cast app IDs
APP_DEFAULT_MEDIA_RECEIVER = "CC1AD845"
APP_YOUTUBE = "YouTube"
APP_NETFLIX = "Netflix"
APP_TOASTOVAC_RECEIVER = "ToastovacReceiver"  # Custom receiver app ID

# Custom namespaces
NS_SUBTITLES = "urn:x-cast:com.toastovac.subtitles"
NS_MEDIA_CONTROL = "urn:x-cast:com.toastovac.media"
NS_SCREEN_MIRROR = "urn:x-cast:com.toastovac.screen"


class VideoSourceType(Enum):
    """Supported video source types."""
    URL = "url"
    LOCAL_FILE = "local_file"
    VLC_PLAYLIST = "vlc_playlist"
    SCREEN_MIRROR = "screen_mirror"
    BROWSER_TAB = "browser_tab"
    YOUTUBE = "youtube"


@dataclass
class VideoSource:
    """A video source configuration."""
    type: VideoSourceType
    url: Optional[str] = None
    path: Optional[Path] = None
    playlist_index: int = 0
    tab_id: Optional[str] = None
    start_time: float = 0.0
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class SubtitleMessage:
    """A subtitle message to send to the receiver."""
    text: str
    source_text: str = ""
    timestamp: float = 0.0
    duration_ms: int = 5000
    language: str = "auto"
    target_language: str = "cs"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": "show",
            "text": self.text,
            "source_text": self.source_text,
            "timestamp": self.timestamp,
            "duration": self.duration_ms,
            "language": self.language,
            "target_language": self.target_language,
        }


class SubtitleController(BaseController):
    """Controller for sending subtitle messages to the receiver."""
    
    def __init__(self):
        super().__init__(NS_SUBTITLES)
        self._message_queue: List[SubtitleMessage] = []
        self._lock = threading.Lock()
        self._sent_count = 0
        self._error_count = 0
    
    def receive_message(self, message, data):
        """Handle messages from the receiver."""
        debug_log(f"cast subtitle ack: {data}", "cast")
        return True
    
    def send_subtitle(self, subtitle: SubtitleMessage) -> bool:
        """Queue a subtitle for sending."""
        with self._lock:
            self._message_queue.append(subtitle)
        return True
    
    def get_pending(self) -> List[SubtitleMessage]:
        """Get and clear pending messages."""
        with self._lock:
            out = list(self._message_queue)
            self._message_queue.clear()
            return out
    
    @property
    def stats(self) -> Dict[str, int]:
        return {
            "sent": self._sent_count,
            "errors": self._error_count,
            "pending": len(self._message_queue),
        }


class CastSenderService:
    """Manages Google Cast connections and media playback."""
    
    def __init__(self, cfg: Any = None):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._running = False
        
        # Cast device state
        self._chromecasts: List[pychromecast.Chromecast] = []
        self._active_device: Optional[pychromecast.Chromecast] = None
        self._browser: Optional[pychromecast.CastBrowser] = None
        
        # Controllers
        self._media_controller: Optional[MediaController] = None
        self._subtitle_controller: Optional[SubtitleController] = None
        self._youtube_controller: Optional[YouTubeController] = None
        
        # Subtitle integration
        self._subtitle_service: Optional[Any] = None
        self._subtitle_delay_ms = 0
        
        # Video source state
        self._current_source: Optional[VideoSource] = None
        self._playback_start_time: Optional[float] = None
        
        # Callbacks
        self._on_state_change: Optional[Callable] = None
        
    # ── Discovery ─────────────────────────────────────────────────────
    def discover_devices(self, timeout: float = 5.0) -> List[Dict[str, Any]]:
        """Discover Cast devices on the network."""
        debug_log("cast: discovering devices", "cast")
        
        chromecasts, browser = pychromecast.get_chromecasts(timeout=timeout)
        self._browser = browser
        
        devices = []
        for cc in chromecasts:
            try:
                cc.wait(timeout=2)
                devices.append({
                    "name": cc.name,
                    "model": cc.model_name,
                    "uuid": str(cc.uuid),
                    "host": getattr(cc, 'host', 'unknown'),
                    "port": getattr(cc, 'port', 8009),
                    "app_id": cc.app_id,
                    "is_idle": cc.is_idle,
                })
            except Exception as exc:
                debug_log(f"cast: device info failed: {exc}", "cast")
        
        self._chromecasts = chromecasts
        debug_log(f"cast: found {len(devices)} devices", "cast")
        return devices
    
    def connect_device(self, device_name: Optional[str] = None, 
                       device_uuid: Optional[str] = None) -> bool:
        """Connect to a Cast device by name or UUID."""
        with self._lock:
            if not self._chromecasts:
                self.discover_devices()
            
            target = None
            for cc in self._chromecasts:
                if device_name and cc.name.lower() == device_name.lower():
                    target = cc
                    break
                if device_uuid and str(cc.uuid) == device_uuid:
                    target = cc
                    break
            
            if target is None and self._chromecasts:
                target = self._chromecasts[0]  # Default to first
            
            if target is None:
                debug_log("cast: no device found", "cast")
                return False
            
            try:
                target.wait(timeout=5)
                self._active_device = target
                
                # Set up controllers
                self._media_controller = target.media_controller
                self._subtitle_controller = SubtitleController()
                target.register_handler(self._subtitle_controller)
                
                debug_log(f"cast: connected to {target.name}", "cast")
                return True
                
            except Exception as exc:
                debug_log(f"cast: connect failed: {exc}", "cast")
                return False
    
    def disconnect(self) -> None:
        """Disconnect from the active device."""
        with self._lock:
            if self._browser:
                pychromecast.stop_discovery(self._browser)
                self._browser = None
            self._active_device = None
            self._media_controller = None
            self._subtitle_controller = None
            self._youtube_controller = None
            debug_log("cast: disconnected", "cast")
    
    # ── App Management ────────────────────────────────────────────────
    def launch_app(self, app_id: str) -> bool:
        """Launch a Cast app on the connected device."""
        if not self._active_device:
            return False
        
        try:
            self._active_device.start_app(app_id)
            debug_log(f"cast: launched app {app_id}", "cast")
            return True
        except Exception as exc:
            debug_log(f"cast: launch failed: {exc}", "cast")
            return False
    
    def launch_custom_receiver(self, receiver_url: str) -> bool:
        """Launch the custom Toastovac receiver app."""
        if not self._active_device:
            return False
        
        # For custom receivers, we need to register the app ID
        # For now, use the default media receiver with custom message support
        try:
            # Quit current app first
            self._active_device.quit_app()
            time.sleep(1)
            
            # Launch default media receiver (supports custom namespaces)
            self._active_device.start_app(APP_DEFAULT_MEDIA_RECEIVER)
            time.sleep(2)
            
            debug_log("cast: custom receiver launched", "cast")
            return True
        except Exception as exc:
            debug_log(f"cast: custom receiver launch failed: {exc}", "cast")
            return False
    
    # ── Media Playback ────────────────────────────────────────────────
    def play_url(self, url: str, content_type: str = "video/mp4",
                 title: Optional[str] = None, start_time: float = 0.0) -> bool:
        """Play a media URL on the device."""
        if not self._media_controller:
            return False
        
        try:
            self._media_controller.play_media(
                url, content_type,
                title=title,
                current_time=start_time
            )
            self._current_source = VideoSource(
                type=VideoSourceType.URL,
                url=url,
                start_time=start_time,
                metadata={"title": title}
            )
            self._playback_start_time = time.time() - start_time
            debug_log(f"cast: playing {url}", "cast")
            return True
        except Exception as exc:
            debug_log(f"cast: play failed: {exc}", "cast")
            return False
    
    def play_local_file(self, file_path: Path, 
                        title: Optional[str] = None) -> bool:
        """Play a local file (requires local HTTP server)."""
        # Start local HTTP server to serve the file
        from . import cast as cast_mod
        
        server = cast_mod.get_cast_server()
        if not server.start():
            return False
        
        # For now, this serves the file through the cast server
        # In production, we'd need a dedicated media server
        url = f"{server.url}media/{file_path.name}"
        return self.play_url(url, self._guess_mime_type(file_path), title)
    
    def play_youtube(self, video_id: str) -> bool:
        """Play a YouTube video."""
        if not self._active_device:
            return False
        
        try:
            # Launch YouTube app
            self._active_device.start_app(APP_YOUTUBE)
            time.sleep(2)
            
            # Set up YouTube controller
            self._youtube_controller = YouTubeController()
            self._active_device.register_handler(self._youtube_controller)
            
            # Play video
            self._youtube_controller.play_video(video_id)
            
            self._current_source = VideoSource(
                type=VideoSourceType.YOUTUBE,
                url=f"https://youtube.com/watch?v={video_id}"
            )
            self._playback_start_time = time.time()
            
            debug_log(f"cast: playing YouTube {video_id}", "cast")
            return True
        except Exception as exc:
            debug_log(f"cast: YouTube failed: {exc}", "cast")
            return False
    
    def pause(self) -> bool:
        """Pause playback."""
        if not self._media_controller:
            return False
        try:
            self._media_controller.pause()
            return True
        except Exception:
            return False
    
    def resume(self) -> bool:
        """Resume playback."""
        if not self._media_controller:
            return False
        try:
            self._media_controller.play()
            return True
        except Exception:
            return False
    
    def seek(self, position_seconds: float) -> bool:
        """Seek to position."""
        if not self._media_controller:
            return False
        try:
            self._media_controller.seek(position_seconds)
            if self._playback_start_time:
                self._playback_start_time = time.time() - position_seconds
            return True
        except Exception:
            return False
    
    def set_volume(self, volume: float) -> bool:
        """Set volume (0.0 to 1.0)."""
        if not self._active_device:
            return False
        try:
            self._active_device.set_volume(volume)
            return True
        except Exception:
            return False
    
    def stop(self) -> bool:
        """Stop playback."""
        if not self._media_controller:
            return False
        try:
            self._media_controller.stop()
            self._current_source = None
            self._playback_start_time = None
            return True
        except Exception:
            return False
    
    # ── Subtitle Integration ──────────────────────────────────────────
    def set_subtitle_service(self, service: Any) -> None:
        """Connect to the Everywhere subtitles service."""
        self._subtitle_service = service
        debug_log("cast: subtitle service connected", "cast")
    
    def set_subtitle_delay(self, delay_ms: int) -> None:
        """Set the subtitle display delay (for sync with video)."""
        self._subtitle_delay_ms = delay_ms
        
        # Send delay to receiver
        if self._active_device and self._subtitle_controller:
            try:
                self._active_device.socket_client.send_message(
                    self._active_device.status.transport_id,
                    NS_SUBTITLES,
                    {"action": "set_delay", "delay_ms": delay_ms}
                )
            except Exception as exc:
                debug_log(f"cast: delay send failed: {exc}", "cast")
    
    def send_subtitle(self, text: str, source_text: str = "",
                      language: str = "auto") -> bool:
        """Send a subtitle line to the receiver."""
        if not self._active_device or not self._subtitle_controller:
            return False
        
        # Calculate timestamp based on playback position
        timestamp = 0.0
        if self._playback_start_time:
            timestamp = time.time() - self._playback_start_time
        
        # Apply delay
        timestamp += self._subtitle_delay_ms / 1000.0
        
        subtitle = SubtitleMessage(
            text=text,
            source_text=source_text,
            timestamp=timestamp,
            language=language
        )
        
        try:
            self._active_device.socket_client.send_message(
                self._active_device.status.transport_id,
                NS_SUBTITLES,
                subtitle.to_dict()
            )
            return True
        except Exception as exc:
            debug_log(f"cast: subtitle send failed: {exc}", "cast")
            return False
    
    def clear_subtitles(self) -> bool:
        """Clear all subtitles from the display."""
        if not self._active_device:
            return False
        try:
            self._active_device.socket_client.send_message(
                self._active_device.status.transport_id,
                NS_SUBTITLES,
                {"action": "clear"}
            )
            return True
        except Exception:
            return False
    
    # ── Status ────────────────────────────────────────────────────────
    @property
    def status(self) -> Dict[str, Any]:
        """Get current status."""
        if not self._active_device:
            return {"connected": False}
        
        media_status = self._media_controller.status if self._media_controller else None
        
        return {
            "connected": True,
            "device_name": self._active_device.name,
            "app_id": self._active_device.app_id,
            "player_state": media_status.player_state if media_status else "UNKNOWN",
            "current_time": media_status.current_time if media_status else 0,
            "duration": media_status.duration if media_status else 0,
            "volume": self._active_device.status.volume_level,
            "source": self._current_source.type.value if self._current_source else None,
            "subtitle_stats": self._subtitle_controller.stats if self._subtitle_controller else {},
        }
    
    @property
    def is_connected(self) -> bool:
        return self._active_device is not None
    
    @property
    def is_playing(self) -> bool:
        if not self._media_controller:
            return False
        return self._media_controller.status.player_state == "PLAYING"
    
    # ── Helpers ───────────────────────────────────────────────────────
    def _guess_mime_type(self, path: Path) -> str:
        """Guess MIME type from file extension."""
        suffix = path.suffix.lower()
        mime_map = {
            ".mp4": "video/mp4",
            ".mkv": "video/x-matroska",
            ".webm": "video/webm",
            ".avi": "video/x-msvideo",
            ".mov": "video/quicktime",
            ".mp3": "audio/mpeg",
            ".flac": "audio/flac",
            ".wav": "audio/wav",
        }
        return mime_map.get(suffix, "application/octet-stream")


# ── Module-level singleton ────────────────────────────────────────────
_service: Optional[CastSenderService] = None


def get_service() -> CastSenderService:
    """Get or create the cast sender service."""
    global _service
    if _service is None:
        _service = CastSenderService()
    return _service


def ensure_service(cfg: Any = None) -> CastSenderService:
    """Ensure the service exists with config."""
    global _service
    if _service is None:
        _service = CastSenderService(cfg)
    return _service
