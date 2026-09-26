"""Cast Manager — unified interface for all casting functionality.

Integrates:
- Google Cast protocol sender (cast_sender.py)
- LAN subtitle web-cast (cast.py)
- Screen capture and streaming (screen_capture.py)
- Video source management
- Subtitle synchronization
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..debug import debug_log
from . import cast as lan_cast
from .cast_sender import CastSenderService, VideoSource, VideoSourceType, SubtitleMessage
from .screen_capture import ScreenCaptureService, CaptureConfig, CaptureSource, StreamConfig, EncoderType

_LOG = logging.getLogger(__name__)


class CastMode(Enum):
    """Casting modes."""
    LAN_SUBTITLES = "lan_subtitles"      # Original: subtitles only to LAN browser
    MEDIA_URL = "media_url"              # Stream URL to Cast device
    LOCAL_FILE = "local_file"            # Stream local file
    SCREEN_MIRROR = "screen_mirror"      # Mirror PC screen
    BROWSER_TAB = "browser_tab"          # Mirror specific browser tab
    YOUTUBE = "youtube"                  # YouTube app on TV


@dataclass
class CastSession:
    """Active cast session info."""
    mode: CastMode
    device_name: str
    started_at: float
    video_source: Optional[VideoSource] = None
    subtitle_active: bool = False
    subtitle_delay_ms: int = 0


class CastManager:
    """Unified casting manager for all modes."""
    
    def __init__(self, cfg: Any = None):
        self._cfg = cfg
        self._lock = threading.Lock()
        
        # Services
        self._lan_cast = lan_cast.get_cast_server()
        self._cast_sender = CastSenderService(cfg)
        self._screen_capture = ScreenCaptureService(cfg)
        
        # State
        self._active_session: Optional[CastSession] = None
        self._subtitle_service: Optional[Any] = None
        self._subtitle_thread: Optional[threading.Thread] = None
        self._subtitle_running = False
        
        # Callbacks
        self._on_subtitle: Optional[Callable[[str, str], None]] = None
    
    # ── Discovery ──────────────────────────────────��──────────────────
    def discover_devices(self) -> List[Dict[str, Any]]:
        """Discover available Cast devices."""
        return self._cast_sender.discover_devices()
    
    def get_lan_cast_url(self) -> str:
        """Get the LAN subtitle cast URL."""
        return self._lan_cast.url
    
    # ── Session Management ────────────────────────────────────────────
    def start_session(self, mode: CastMode, 
                      device_name: Optional[str] = None,
                      **kwargs) -> bool:
        """Start a new cast session."""
        with self._lock:
            # Stop existing session
            self.stop_session()
            
            # Start based on mode
            success = False
            device = "LAN"
            
            if mode == CastMode.LAN_SUBTITLES:
                success = self._lan_cast.start()
                device = "LAN Browser"
                
            elif mode in (CastMode.MEDIA_URL, CastMode.LOCAL_FILE, 
                         CastMode.SCREEN_MIRROR, CastMode.BROWSER_TAB,
                         CastMode.YOUTUBE):
                success = self._cast_sender.connect_device(device_name)
                if success:
                    device = self._cast_sender.status.get("device_name", "Unknown")
                    
                    # Configure based on source type
                    if mode == CastMode.MEDIA_URL:
                        url = kwargs.get("url")
                        if url:
                            success = self._cast_sender.play_url(url)
                    
                    elif mode == CastMode.LOCAL_FILE:
                        path = kwargs.get("path")
                        if path:
                            success = self._cast_sender.play_local_file(Path(path))
                    
                    elif mode == CastMode.YOUTUBE:
                        video_id = kwargs.get("video_id")
                        if video_id:
                            success = self._cast_sender.play_youtube(video_id)
                    
                    elif mode in (CastMode.SCREEN_MIRROR, CastMode.BROWSER_TAB):
                        # Start screen capture
                        capture_cfg = CaptureConfig(
                            source=CaptureSource.BROWSER_TAB if mode == CastMode.BROWSER_TAB else CaptureSource.FULL_SCREEN,
                            window_title=kwargs.get("window_title"),
                            framerate=kwargs.get("framerate", 30),
                            bitrate_kbps=kwargs.get("bitrate", 8000)
                        )
                        stream_cfg = StreamConfig(port=4322)
                        self._screen_capture.set_config(capture_cfg, stream_cfg)
                        
                        if self._screen_capture.start():
                            stream_url = self._screen_capture.get_stream_url()
                            if stream_url:
                                success = self._cast_sender.play_url(stream_url, "application/x-mpegURL")
            
            if success:
                self._active_session = CastSession(
                    mode=mode,
                    device_name=device,
                    started_at=time.time()
                )
                debug_log(f"cast manager: session started mode={mode.value} device={device}", "cast")
            
            return success
    
    def stop_session(self) -> None:
        """Stop the active session."""
        with self._lock:
            # Stop subtitle forwarding
            self._subtitle_running = False
            if self._subtitle_thread:
                self._subtitle_thread.join(timeout=2)
                self._subtitle_thread = None
            
            # Stop screen capture
            self._screen_capture.stop()
            
            # Stop cast sender
            if self._active_session and self._active_session.mode != CastMode.LAN_SUBTITLES:
                self._cast_sender.stop()
                self._cast_sender.disconnect()
            
            # Stop LAN cast
            self._lan_cast.stop()
            
            self._active_session = None
            debug_log("cast manager: session stopped", "cast")
    
    # ── Subtitle Integration ──────────────────────────────────────────
    def set_subtitle_service(self, service: Any) -> None:
        """Connect to the subtitles service."""
        self._subtitle_service = service
        self._cast_sender.set_subtitle_service(service)
    
    def start_subtitles(self, delay_ms: int = 0) -> bool:
        """Start forwarding subtitles to the cast device."""
        if not self._active_session:
            return False
        
        self._subtitle_delay_ms = delay_ms
        self._cast_sender.set_subtitle_delay(delay_ms)
        
        # Start subtitle forwarding thread
        self._subtitle_running = True
        self._subtitle_thread = threading.Thread(
            target=self._subtitle_forward_loop,
            name="cast-subtitle-forward",
            daemon=True
        )
        self._subtitle_thread.start()
        
        if self._active_session:
            self._active_session.subtitle_active = True
            self._active_session.subtitle_delay_ms = delay_ms
        
        debug_log(f"cast manager: subtitles started delay={delay_ms}ms", "cast")
        return True
    
    def stop_subtitles(self) -> None:
        """Stop subtitle forwarding."""
        self._subtitle_running = False
        if self._active_session:
            self._active_session.subtitle_active = False
        debug_log("cast manager: subtitles stopped", "cast")
    
    def _subtitle_forward_loop(self) -> None:
        """Forward subtitles from the service to the cast device."""
        while self._subtitle_running:
            if self._subtitle_service:
                try:
                    # Get latest subtitle from service
                    service = self._subtitle_service
                    if hasattr(service, '_sub_lines') and service._sub_lines:
                        with service._sub_lock:
                            lines = list(service._sub_lines)
                            service._sub_lines.clear()
                        
                        for line in lines:
                            text = line.get("translated_text") or line.get("source_text", "")
                            source = line.get("source_text", "")
                            lang = line.get("source_language", "auto")
                            
                            if self._active_session:
                                if self._active_session.mode == CastMode.LAN_SUBTITLES:
                                    # LAN cast
                                    lan_cast.push_subtitle(text)
                                else:
                                    # Google Cast
                                    self._cast_sender.send_subtitle(text, source, lang)
                except Exception as exc:
                    debug_log(f"cast manager: subtitle forward error: {exc}", "cast")
            
            time.sleep(0.1)  # 100ms polling
    
    # ── Playback Control ───────────────────────────────────────���──────
    def pause(self) -> bool:
        """Pause playback."""
        return self._cast_sender.pause()
    
    def resume(self) -> bool:
        """Resume playback."""
        return self._cast_sender.resume()
    
    def seek(self, position: float) -> bool:
        """Seek to position."""
        return self._cast_sender.seek(position)
    
    def set_volume(self, volume: float) -> bool:
        """Set volume."""
        return self._cast_sender.set_volume(volume)
    
    # ── Status ────────────────────────────────────────────────────────
    @property
    def status(self) -> Dict[str, Any]:
        """Get current status."""
        session = self._active_session
        if not session:
            return {"active": False}
        
        result = {
            "active": True,
            "mode": session.mode.value,
            "device": session.device_name,
            "uptime": time.time() - session.started_at,
            "subtitles": {
                "active": session.subtitle_active,
                "delay_ms": session.subtitle_delay_ms,
            }
        }
        
        if session.mode != CastMode.LAN_SUBTITLES:
            result["cast_sender"] = self._cast_sender.status
        
        if session.mode in (CastMode.SCREEN_MIRROR, CastMode.BROWSER_TAB):
            result["screen_capture"] = {
                "running": self._screen_capture.is_running,
                "capabilities": self._screen_capture.capabilities,
            }
        
        return result


# ── Module-level singleton ────────────────────────────────────────────
_manager: Optional[CastManager] = None


def get_manager() -> CastManager:
    """Get or create the cast manager."""
    global _manager
    if _manager is None:
        _manager = CastManager()
    return _manager


def ensure_manager(cfg: Any = None) -> CastManager:
    """Ensure the manager exists with config."""
    global _manager
    if _manager is None:
        _manager = CastManager(cfg)
    return _manager
