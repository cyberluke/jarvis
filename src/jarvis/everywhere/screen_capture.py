"""Screen capture and streaming with Intel QSV hardware encoding.

Uses Windows Desktop Duplication API (DXGI) for screen capture and
Intel Quick Sync Video (QSV) for hardware-accelerated H.264/H.265 encoding.
Streams to Cast devices via HTTP Live Streaming (HLS) or direct HTTP.

Requirements:
- Intel CPU with Quick Sync (6th gen Core or newer) or Intel Arc GPU
- FFmpeg with QSV support (ffmpeg -hwaccels should list qsv)
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..debug import debug_log

_LOG = logging.getLogger(__name__)


class CaptureSource(Enum):
    """Screen capture sources."""
    FULL_SCREEN = "full_screen"
    WINDOW = "window"
    REGION = "region"
    BROWSER_TAB = "browser_tab"


class EncoderType(Enum):
    """Hardware encoder types."""
    INTEL_QSV_H264 = "h264_qsv"
    INTEL_QSV_H265 = "hevc_qsv"
    INTEL_QSV_AV1 = "av1_qsv"
    SOFTWARE_H264 = "libx264"
    SOFTWARE_H265 = "libx265"


@dataclass
class CaptureConfig:
    """Screen capture configuration."""
    source: CaptureSource = CaptureSource.FULL_SCREEN
    monitor_index: int = 0
    window_title: Optional[str] = None
    region: Optional[Tuple[int, int, int, int]] = None  # x, y, width, height
    framerate: int = 30
    width: int = 1920
    height: int = 1080
    bitrate_kbps: int = 8000
    encoder: EncoderType = EncoderType.INTEL_QSV_H264
    preset: str = "veryfast"  # QSV presets: veryfast, faster, fast, medium, slow


@dataclass
class StreamConfig:
    """Streaming output configuration."""
    protocol: str = "hls"  # hls, http, rtsp
    port: int = 4322
    segment_duration: int = 2  # HLS segment duration in seconds
    playlist_size: int = 5  # Number of segments in HLS playlist


class ScreenCaptureService:
    """Captures screen and streams with hardware encoding."""
    
    def __init__(self, cfg: Any = None):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._running = False
        self._process: Optional[subprocess.Popen] = None
        self._capture_config = CaptureConfig()
        self._stream_config = StreamConfig()
        self._output_dir: Optional[Path] = None
        self._ffmpeg_path = "ffmpeg"
        
        # Check capabilities
        self._qsv_available = self._check_qsv()
        self._dd_available = self._check_desktop_duplication()
    
    def _check_qsv(self) -> bool:
        """Check if Intel QSV is available."""
        try:
            result = subprocess.run(
                [self._ffmpeg_path, "-hwaccels"],
                capture_output=True, text=True, timeout=5
            )
            return "qsv" in result.stdout.lower()
        except Exception:
            return False
    
    def _check_desktop_duplication(self) -> bool:
        """Check if Windows Desktop Duplication is available."""
        try:
            # Check for DXGI
            dxgi = ctypes.windll.dxgi
            return dxgi is not None
        except Exception:
            return False
    
    @property
    def capabilities(self) -> Dict[str, bool]:
        """Get available capabilities."""
        return {
            "qsv": self._qsv_available,
            "desktop_duplication": self._dd_available,
            "h264_qsv": self._qsv_available,
            "hevc_qsv": self._qsv_available,
            "av1_qsv": self._qsv_available,  # Requires Arc or 11th gen+
        }
    
    def set_config(self, capture: CaptureConfig, stream: StreamConfig) -> None:
        """Set capture and streaming configuration."""
        with self._lock:
            self._capture_config = capture
            self._stream_config = stream
    
    def start(self, output_dir: Optional[Path] = None) -> bool:
        """Start screen capture and streaming."""
        with self._lock:
            if self._running:
                return True
            
            if not self._qsv_available:
                debug_log("screen capture: QSV not available", "cast")
                return False
            
            self._output_dir = output_dir or Path("stream_output")
            self._output_dir.mkdir(exist_ok=True)
            
            # Build FFmpeg command
            cmd = self._build_ffmpeg_command()
            if not cmd:
                return False
            
            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                self._running = True
                
                # Start output monitor thread
                threading.Thread(
                    target=self._monitor_output,
                    name="screen-capture-monitor",
                    daemon=True
                ).start()
                
                debug_log(f"screen capture: started on port {self._stream_config.port}", "cast")
                return True
                
            except Exception as exc:
                debug_log(f"screen capture: start failed: {exc}", "cast")
                return False
    
    def stop(self) -> None:
        """Stop capture and streaming."""
        with self._lock:
            self._running = False
            if self._process:
                try:
                    self._process.terminate()
                    self._process.wait(timeout=5)
                except Exception:
                    self._process.kill()
                self._process = None
            debug_log("screen capture: stopped", "cast")
    
    def _build_ffmpeg_command(self) -> Optional[List[str]]:
        """Build the FFmpeg command line."""
        cap = self._capture_config
        stream = self._stream_config
        
        cmd = [self._ffmpeg_path, "-y"]
        
        # Input source
        if cap.source == CaptureSource.FULL_SCREEN:
            # Use gdigrab for full screen (simpler than DXGI)
            cmd.extend([
                "-f", "gdigrab",
                "-framerate", str(cap.framerate),
                "-video_size", f"{cap.width}x{cap.height}",
                "-i", "desktop"
            ])
        elif cap.source == CaptureSource.WINDOW and cap.window_title:
            cmd.extend([
                "-f", "gdigrab",
                "-framerate", str(cap.framerate),
                "-i", f"title={cap.window_title}"
            ])
        elif cap.source == CaptureSource.REGION and cap.region:
            x, y, w, h = cap.region
            cmd.extend([
                "-f", "gdigrab",
                "-framerate", str(cap.framerate),
                "-offset_x", str(x),
                "-offset_y", str(y),
                "-video_size", f"{w}x{h}",
                "-i", "desktop"
            ])
        else:
            debug_log("screen capture: invalid source", "cast")
            return None
        
        # Hardware encoding
        if cap.encoder in (EncoderType.INTEL_QSV_H264, EncoderType.INTEL_QSV_H265, 
                          EncoderType.INTEL_QSV_AV1):
            cmd.extend([
                "-init_hw_device", "qsv=hw",
                "-filter_hw_device", "hw",
                "-c:v", cap.encoder.value,
                "-preset", cap.preset,
                "-b:v", f"{cap.bitrate_kbps}k",
                "-maxrate", f"{cap.bitrate_kbps}k",
                "-bufsize", f"{cap.bitrate_kbps * 2}k",
            ])
        else:
            # Software fallback
            cmd.extend([
                "-c:v", cap.encoder.value,
                "-preset", cap.preset,
                "-b:v", f"{cap.bitrate_kbps}k",
            ])
        
        # Audio (capture from system)
        cmd.extend([
            "-f", "dshow",
            "-i", "audio=virtual-audio-capturer",  # Requires virtual audio device
            "-c:a", "aac",
            "-b:a", "128k",
        ])
        
        # Output
        if stream.protocol == "hls":
            playlist_path = self._output_dir / "stream.m3u8"
            cmd.extend([
                "-f", "hls",
                "-hls_time", str(stream.segment_duration),
                "-hls_list_size", str(stream.playlist_size),
                "-hls_flags", "delete_segments",
                str(playlist_path)
            ])
        elif stream.protocol == "http":
            cmd.extend([
                "-f", "mpegts",
                "-listen", "1",
                f"http://0.0.0.0:{stream.port}/stream"
            ])
        
        return cmd
    
    def _monitor_output(self) -> None:
        """Monitor FFmpeg output for errors."""
        if not self._process:
            return
        
        try:
            for line in self._process.stderr:
                line = line.decode('utf-8', errors='replace').strip()
                if 'error' in line.lower():
                    debug_log(f"screen capture error: {line}", "cast")
        except Exception:
            pass
    
    def get_stream_url(self) -> Optional[str]:
        """Get the URL for the stream."""
        if not self._running:
            return None
        
        if self._stream_config.protocol == "hls":
            # Need to serve the HLS files via HTTP
            return f"http://{_lan_ip()}:{self._stream_config.port}/stream.m3u8"
        elif self._stream_config.protocol == "http":
            return f"http://{_lan_ip()}:{self._stream_config.port}/stream"
        
        return None
    
    @property
    def is_running(self) -> bool:
        return self._running


def _lan_ip() -> str:
    """Get the LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ── Module-level singleton ────────────────────────────────────────────
_service: Optional[ScreenCaptureService] = None


def get_service() -> ScreenCaptureService:
    """Get or create the screen capture service."""
    global _service
    if _service is None:
        _service = ScreenCaptureService()
    return _service
