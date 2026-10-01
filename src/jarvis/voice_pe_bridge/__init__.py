"""Voice PE WebAudio bridge: Voice PE -> framed PCM over loopback WebSocket.

Serves the V271 PWA / BrowserOS composer over ``v271-webaudio/1`` (see
``voice_pe_bridge.spec.md``). The satellite microphone signal is tapped off
the Voice PE ``AudioIngress`` and streamed as framed PCM16LE with sequence
numbers, timestamps, stream state and discontinuity markers.
"""

from .protocol import (
    PROTOCOL_NAME,
    AudioFrame,
    pack_frame,
    unpack_frame,
)
from .server import VoicePEBridgeServer
from .sink import VoicePEFrameTap, samples_to_pcm16

__all__ = [
    "PROTOCOL_NAME",
    "AudioFrame",
    "pack_frame",
    "unpack_frame",
    "VoicePEBridgeServer",
    "VoicePEFrameTap",
    "samples_to_pcm16",
]