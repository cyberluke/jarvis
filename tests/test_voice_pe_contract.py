"""Unit tests for the Voice PE public API contract (``voice-pe/v1``).

Behaviour-level, no network, no live device: a fake manager mirrors the
``VoicePEManager`` surface the contract touches and records the calls, in
the style of ``test_voice_pe.py``.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from jarvis.everywhere.voice_pe_contract import (
    ERROR_INVALID,
    ERROR_NO_DEVICE,
    ERROR_NO_MANAGER,
    VOICE_PE_CAPABILITIES,
    VoicePeContract,
    handle_voice_pe,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeMedia:
    def __init__(self):
        self.calls = []
        self.snapshot_data = {"state": "IDLE", "volume": 0.66, "muted": False}

    def snapshot(self):
        return dict(self.snapshot_data)

    def play_url(self, url):
        self.calls.append(("play_url", url))

    def stop(self):
        self.calls.append(("stop",))

    def pause(self):
        self.calls.append(("pause",))

    def resume(self):
        self.calls.append(("resume",))

    def set_volume(self, volume):
        self.calls.append(("set_volume", volume))

    def set_muted(self, muted):
        self.calls.append(("set_muted", muted))


class FakeCapabilities:
    def names(self):
        return ["voice_assistant", "api_audio", "speaker", "announce"]


class FakeDevice:
    def __init__(self, name, host, mac):
        self.identity = {
            "node_name": name,
            "friendly_name": name,
            "mac_address": mac,
            "model": "Voice Assistant",
        }
        self.config = SimpleNamespace(host=host, port=6053, room="kitchen")
        self.media = FakeMedia()
        self.capabilities = FakeCapabilities()

    def health_snapshot(self):
        return {
            "connected": True,
            "authenticated": True,
            "device": self.identity["node_name"],
            "device_state": "READY",
            "session_state": "IDLE",
            "voice_features": self.capabilities.names(),
        }


class FakeManager:
    """Mirrors the VoicePEManager surface the contract uses."""

    def __init__(self, devices=None):
        self.devices_list = list(devices or [])
        self.enabled = True
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, daemon=True
        )
        self._loop_thread.start()
        self.calls = []
        self.discovered = [
            {"host": "10.0.0.9", "port": 6053, "node_name": "Voice Assistant 9"}
        ]

    def stop(self):
        self._loop.call_soon_threadsafe(self._loop.stop)

    @property
    def devices(self):
        # Real VoicePEManager exposes ``devices`` as a property.
        return list(self.devices_list)

    def device(self, key):
        needle = str(key or "").strip().lower()
        if not needle:
            return self.devices_list[0] if self.devices_list else None
        for device in self.devices_list:
            candidates = {
                str(device.identity.get("node_name", "")).lower(),
                str(device.identity.get("friendly_name", "")).lower(),
                str(device.identity.get("mac_address", "")).lower(),
                str(device.identity.get("mac_address", "")).replace(":", "").lower(),
                str(device.config.host).lower(),
            }
            if needle in candidates:
                return device
        if len(self.devices_list) == 1:
            return self.devices_list[0]
        return None

    def health(self):
        return {
            "enabled": self.enabled,
            "devices": [d.health_snapshot() for d in self.devices_list],
            "metrics": self.metrics(),
        }

    def metrics(self):
        return {"device_count": len(self.devices_list)}

    async def aasync_discover(self):
        self.calls.append(("discover",))
        return self.discovered

    async def announce(self, key, text, *, start_conversation=True):
        self.calls.append(("announce", key, text, start_conversation))
        return bool(text)

    async def play(self, key, url):
        self.calls.append(("play", key, url))
        device = self.device(key)
        if device is not None:
            device.media.play_url(url)
        return device is not None

    async def stop_media(self, key):
        self.calls.append(("stop_media", key))
        return True

    async def pause_media(self, key):
        self.calls.append(("pause_media", key))
        return True

    async def resume_media(self, key):
        self.calls.append(("resume_media", key))
        return True

    async def set_volume(self, key, volume):
        self.calls.append(("set_volume", key, volume))
        return 0.0 <= float(volume) <= 1.0

    async def set_muted(self, key, muted):
        self.calls.append(("set_muted", key, muted))
        return True

    def media_state(self, key):
        device = self.device(key)
        if device is None:
            return {}
        return device.media.snapshot()

    async def set_led(self, key, rgb, brightness):
        self.calls.append(("set_led", key, rgb, brightness))
        return rgb is not None or brightness is not None

    def mirror_local(self, text):
        self.calls.append(("mirror_local", text))
        return len(self.devices_list)

    def restart(self):
        self.calls.append(("restart",))


@pytest.fixture()
def manager():
    device = FakeDevice("Voice Assistant 1234", "192.168.1.50", "aa:bb:cc:dd:ee:ff")
    fake = FakeManager(devices=[device])
    yield fake
    fake.stop()


@pytest.fixture()
def contract(manager):
    return VoicePeContract(manager)


@pytest.fixture()
def empty_contract():
    return VoicePeContract(None)


# ---------------------------------------------------------------------------
# Status / devices
# ---------------------------------------------------------------------------


def test_status_without_manager_reports_disabled(empty_contract):
    result = empty_contract.status()
    assert result["enabled"] is False
    assert result["devices"] == []


def test_status_renders_device_view(contract, manager):
    result = contract.status()
    assert result["enabled"] is True
    assert result["metrics"]["device_count"] == 1
    device = result["devices"][0]
    assert device["id"] == "aa:bb:cc:dd:ee:ff"
    assert device["name"] == "Voice Assistant 1234"
    assert device["host"] == "192.168.1.50"
    assert device["port"] == 6053
    assert "voice_assistant" in device["features"]
    assert device["connection"]["connected"] is True
    assert device["media"]["state"] == "IDLE"


def test_devices_endpoint(contract):
    result = contract.devices()
    assert len(result["devices"]) == 1
    assert result["devices"][0]["macAddress"] == "aa:bb:cc:dd:ee:ff"


def test_operation_without_manager_fails_cleanly(empty_contract):
    result = empty_contract.announce("", "hello")
    assert "error" in result
    assert result["error"]["code"] == ERROR_NO_MANAGER


# ---------------------------------------------------------------------------
# Device resolution
# ---------------------------------------------------------------------------


def test_announce_resolves_empty_key_to_single_device(contract, manager):
    result = contract.announce("", "Ahoj")
    assert result.get("ok") is True
    assert result.get("announced") is True
    assert ("announce", "aa:bb:cc:dd:ee:ff", "Ahoj", True) in manager.calls


def test_announce_resolves_by_host(contract, manager):
    result = contract.announce("192.168.1.50", "Ahoj")
    assert result.get("announced") is True


def test_announce_unknown_device(manager):
    # Two attached satellites disable the single-device fallback, so an
    # unmatched key must fail instead of landing on one of them.
    second = FakeDevice("Voice Assistant 5678", "10.0.0.8", "11:22:33:44:55:66")
    manager.devices_list.append(second)
    contract = VoicePeContract(manager)
    result = contract.announce("nope", "Ahoj")
    assert result["error"]["code"] == ERROR_NO_DEVICE


def test_announce_requires_text(contract):
    result = contract.announce("", "")
    assert result["error"]["code"] == ERROR_INVALID


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def test_play_validates_url(contract):
    result = contract.play("", "not-a-url")
    assert result["error"]["code"] == ERROR_INVALID
    result = contract.play("", "https://example.com/a.wav")
    assert result.get("ok") is True


def test_volume_validates_range(contract):
    assert "error" in contract.volume("", 1.5)
    assert "error" in contract.volume("", -0.1)
    result = contract.volume("", 0.5)
    assert result.get("ok") is True


def test_mute_and_media_state(contract, manager):
    result = contract.mute("", True)
    assert result.get("ok") is True
    assert ("set_muted", "aa:bb:cc:dd:ee:ff", True) in manager.calls
    state = contract.media_state("")
    assert state["media"]["volume"] == 0.66


def test_led(contract, manager):
    result = contract.led("", rgb=(0.55, 0.0, 1.0), brightness=0.66)
    assert result.get("led") is True
    assert ("set_led", "aa:bb:cc:dd:ee:ff", (0.55, 0.0, 1.0), 0.66) in manager.calls


def test_calibrate_uses_existing_operations(contract, manager):
    result = contract.calibrate("")
    assert result.get("ok") is True
    assert result.get("announced") is True
    assert result.get("led") is True
    assert any(call[0] == "announce" for call in manager.calls)
    assert any(call[0] == "set_led" for call in manager.calls)


def test_mirror(contract, manager):
    result = contract.mirror("Just a remark")
    assert result.get("satellites") == 1
    assert ("mirror_local", "Just a remark") in manager.calls


def test_discover(contract, manager):
    result = contract.discover()
    assert len(result["found"]) == 1
    assert result["found"][0]["node_name"] == "Voice Assistant 9"


def test_restart_is_proxied(contract, manager):
    result = contract.restart()
    assert result.get("restarted") is True
    assert ("restart",) in manager.calls
    result = handle_voice_pe("POST", "restart", {}, {}, contract)
    assert result.get("restarted") is True


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def test_router_dispatches_get_and_post(contract):
    result = handle_voice_pe("GET", "status", {}, {}, contract)
    assert result["enabled"] is True
    result = handle_voice_pe("POST", "announce", {"device": "", "text": "Ahoj"}, {}, contract)
    assert result.get("announced") is True
    result = handle_voice_pe("GET", "media-state", {}, {"device": [""]}, contract)
    assert "media" in result


def test_router_unknown_action_is_none(contract):
    assert handle_voice_pe("GET", "frobnicate", {}, {}, contract) is None
    assert handle_voice_pe("DELETE", "announce", {}, {}, contract) is None


def test_router_catches_handler_exceptions(contract):
    result = handle_voice_pe("POST", "volume", {"device": "", "volume": "x"}, {}, contract)
    assert result["error"]["code"] == ERROR_INVALID


def test_capability_list_is_stable():
    assert "voice-pe.status" in VOICE_PE_CAPABILITIES
    assert "voice-pe.announce" in VOICE_PE_CAPABILITIES
    assert "voice-pe.calibrate" in VOICE_PE_CAPABILITIES
    assert "voice-pe.mirror" in VOICE_PE_CAPABILITIES