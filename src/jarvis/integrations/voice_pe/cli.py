"""``jarvis voice-pe ...`` command helpers in the existing hand-rolled style.

The Jarvis CLI reads ``sys.argv`` directly (no argparse), so this module
exposes :func:`handle` returning the process exit code. Output uses an emoji
per line plus indentation for hierarchy, matching the rest of the CLI.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from . import config as pe_config
from .discovery import discover
from .models import VoicePEConfig, make_client

_ACCENT = (0.55, 0.0, 1.0)


def _flag(argv: list[str], name: str) -> Optional[str]:
    for index, item in enumerate(argv):
        if item == name and index + 1 < len(argv):
            return argv[index + 1]
        if item.startswith(f"{name}="):
            return item.split("=", 1)[1]
    return None


def _print_health(snapshot: dict) -> None:
    print("  🛰️ Device", flush=True)
    print(f"     🔗 {snapshot.get('device', '?')} (API {snapshot.get('api_version', '?')})", flush=True)
    print(f"     🟢 state: {snapshot.get('device_state', '?')} / session: {snapshot.get('session_state', '?')}", flush=True)
    print(f"     🎚️  audio queue: {snapshot.get('audio_queue_ms', 0)} ms", flush=True)
    features = snapshot.get("voice_features") or []
    print(f"     🧩 features: {', '.join(features) or 'none'}", flush=True)
    egress = snapshot.get("pcm_egress")
    if egress is not None:
        label = {0: "LAN WAV", 1: "Native API PCM"}.get(int(egress), "?")
        print(f"     🔊 TTS egress: {label}", flush=True)
    print(
        f"     👂 wake words: {'disabled' if snapshot.get('wake_words_disabled') else 'enabled'}",
        flush=True,
    )
    if snapshot.get("error"):
        print(f"     ⚠️  last error: {snapshot['error']}", flush=True)


def handle(argv: list[str], settings: Any, manager: Any = None) -> int:
    """Run one ``voice-pe`` subcommand. Returns a process exit code.

    ``manager`` is the live :class:`VoicePEManager` when the daemon bundled it;
    a standalone invocation passes a transient one in (see ``run_cli``).
    """
    cfg = manager.config if manager is not None else pe_config.from_settings(settings)

    if not argv:
        print("🎙️ Voice PE commands:", flush=True)
        for line in (
            "jarvis voice-pe discover",
            "jarvis voice-pe pair",
            "jarvis voice-pe list",
            "jarvis voice-pe status <device>",
            "jarvis voice-pe set-led <device> --rgb 8c00ff --brightness 0.66",
            "jarvis voice-pe announce <device> \"text\"",
            "jarvis voice-pe play <device> <url>",
            "jarvis voice-pe pause <device>",
            "jarvis voice-pe resume <device>",
            "jarvis voice-pe volume <device> 0.66",
            "jarvis voice-pe mute <device> on|off",
            "jarvis voice-pe media-state <device>",
            "jarvis voice-pe stop <device>",
            "jarvis voice-pe forget <device>",
            "jarvis voice-pe profile <device> <auto|desk|room|far_field|meeting>",
            "jarvis voice-pe audio-settings <device>",
            "jarvis voice-pe diag <device>",
            "jarvis voice-pe calibrate <device> [--profile NAME] [--timeout-s N]",
        ):
            print(f"  ▫️ {line}", flush=True)
        return 0

    command = argv[0]
    rest = argv[1:]

    if command == "discover":
        found = asyncio.run(discover(cfg))
        if not found:
            print("🔍 No Voice PE found on the network", flush=True)
            print("   ▫️ Check power, Wi-Fi and the mute switch position", flush=True)
            return 0
        print(f"🔍 Found {len(found)} Voice PE node(s)", flush=True)
        for entry in found:
            print(f"  🛰️ {entry.get('node_name') or entry.get('host')} ({entry.get('source')})", flush=True)
            print(f"     📡 {entry.get('host')}:{entry.get('port')}", flush=True)
            print(f"     🏷️  MAC {entry.get('mac_address')}", flush=True)
            print(f"     📦 {entry.get('project_name')} v{entry.get('project_version')}", flush=True)
        return 0

    if command == "pair":
        return _pair(cfg)

    if command == "list":
        devices = manager.devices if manager is not None else []
        if not devices:
            print("📋 No Voice PE devices attached", flush=True)
            return 0
        print(f"📋 {len(devices)} device(s)", flush=True)
        for device in devices:
            view = device.ui_view()
            connection = view["connection"]
            features = connection.get("voice_features") or []
            print(f"  🛰️ {view['config']['host']} - {device.identity.get('friendly_name', '')}", flush=True)
            print(
                f"     🎙️ {view['audio']['input_channel']} ch, "
                f"TTS {connection['session_state']}, "
                f"wake words {view['wake_words']}",
                flush=True,
            )
            print(
                f"     🟢 {connection['device_state']}, "
                f"🧩 {', '.join(features) or 'none'}",
                flush=True,
            )
            egress = {
                0: "LAN WAV",
                1: "Native API PCM",
            }.get(int(connection.get("pcm_egress", -1)), "?")
            print(f"     🔊 TTS egress: {egress}", flush=True)
        return 1 if any(
            device.health_snapshot().get("device_state") == "error"
            for device in devices
        ) else 0

    if command == "status":
        key = rest[0] if rest else ""
        device = manager.device(key) if manager is not None else None
        if device is None:
            print("🛰️ No attached device for that name", flush=True)
            return 1
        print(f"🛰️ {device.identity.get('node_name') or device._host}", flush=True)
        _print_health(device.health_snapshot())
        trail = device.media_delivery()
        if trail.get("key"):
            print(
                f"     📦 last WAV: {trail['key']} on :{trail.get('port', 0)} "
                f"status {trail.get('status', '-')}, "
                f"{trail.get('served_bytes', 0)} B in {trail.get('hits', 0)} hit(s)",
                flush=True,
            )
        return 0

    if command == "set-led":
        key = rest[0] if rest else ""
        rgb_text = _flag(rest, "--rgb") or ""
        brightness = _flag(rest, "--brightness")
        from .led import parse_hex_rgb

        rgb = parse_hex_rgb(rgb_text) or _ACCENT
        level = float(brightness) if brightness else cfg.led_brightness
        return _await(manager, manager.set_led(key, rgb, level)) if manager else 1

    if command == "announce":
        key = rest[0] if rest else ""
        text = rest[1] if len(rest) > 1 else ""
        return _await(manager, manager.announce(key, text)) if manager else 1

    if command == "play":
        key = rest[0] if rest else ""
        url = rest[1] if len(rest) > 1 else ""
        return _await(manager, manager.play(key, url)) if manager else 1

    if command == "pause":
        key = rest[0] if rest else ""
        return _await(manager, manager.pause_media(key)) if manager else 1

    if command == "resume":
        key = rest[0] if rest else ""
        return _await(manager, manager.resume_media(key)) if manager else 1

    if command == "volume":
        key = rest[0] if rest else ""
        try:
            level = float(rest[1]) if len(rest) > 1 else float(cfg.led_brightness)
        except (TypeError, ValueError):
            print("❓ volume expects a number between 0 and 1", flush=True)
            return 1
        return _await(manager, manager.set_volume(key, level)) if manager else 1

    if command == "mute":
        key = rest[0] if rest else ""
        wanted = str(rest[1] if len(rest) > 1 else "on").strip().lower()
        return (
            _await(manager, manager.set_muted(key, wanted in {"on", "1", "true"}))
            if manager
            else 1
        )

    if command == "media-state":
        key = rest[0] if rest else ""
        state = manager.media_state(key) if manager else {}
        if not state:
            print("🛰️ No attached device with a media player", flush=True)
            return 1
        print("🎵 Media player", flush=True)
        for name, value in state.items():
            print(f"   ▫️ {name}: {value}", flush=True)
        return 0

    if command == "stop":
        key = rest[0] if rest else ""
        return _await(manager, manager.stop_media(key)) if manager else 1

    if command == "forget":
        key = rest[0] if rest else ""
        mac = _mac_for(cfg, key)
        if not mac:
            print("🗑️ Nothing stored for that device", flush=True)
            return 0
        pe_config.forget_device(mac)
        print(f"🗑️ Forgot {mac} (metadata and local secret removed)", flush=True)
        print("   ▫️ The device itself keeps its firmware and Noise key", flush=True)
        return 0

    if command == "profile":
        if manager is None:
            print("🛰️ voice-pe profile needs the running daemon", flush=True)
            return 1
        key = rest[0] if rest else ""
        wanted = rest[1] if len(rest) > 1 else ""
        if not wanted:
            # Read: show the resolved profile + effective settings.
            view = manager.audio_settings_view(key)
            if "error" in view:
                print(f"🛰️ No attached device: {view['error']}", flush=True)
                return 1
            print(
                f"🎛️ Profile: {view['profile']} "
                f"({view['device']['name'] or view['deviceId']})",
                flush=True,
            )
            for row in view["settings"].get("settings", []):
                state = "🚫 " + str(row.get("reason")) if row.get("state") == "unsupported" else "✅"
                print(
                    f"   {state} {row['label']}: {row['value']} {row.get('unit') or ''}"
                    f"  [{row['layer']}]",
                    flush=True,
                )
            return 0
        result = manager.set_profile(key, wanted)
        if not result.get("applied"):
            print(f"🛰️ profile switch failed: {result.get('error', '?')}", flush=True)
            return 1
        print(
            f"🎛️ Profile → {result['profile']} on {result['deviceId']} "
            f"(persisted={result.get('persisted', False)})",
            flush=True,
        )
        for reason in (result.get("unsupported") or {}).values():
            print(f"   🚫 unsupported: {reason}", flush=True)
        return 0

    if command == "audio-settings":
        if manager is None:
            print("🛰️ voice-pe audio-settings needs the running daemon", flush=True)
            return 1
        view = manager.audio_settings_view(rest[0] if rest else "")
        if "error" in view:
            print(f"🛰️ No attached device: {view['error']}", flush=True)
            return 1
        print(
            f"🎚️ {view['device']['name'] or view['deviceId']} "
            f"(profile {view['profile']})",
            flush=True,
        )
        for row in view["settings"].get("settings", []):
            state = "🚫 " + str(row.get("reason")) if row.get("state") == "unsupported" else "✅"
            print(
                f"   {state} {row['label']}: {row['value']} {row.get('unit') or ''}"
                f"  [{row['layer']}]",
                flush=True,
            )
        device = view.get("device_audio_settings") or {}
        for name, label in (
            ("noise_suppression_level", "Device noise suppression"),
            ("auto_gain", "Device auto gain"),
            ("volume_multiplier", "Device volume multiplier"),
        ):
            row = device.get(name) or {}
            print(
                f"   📡 {label}: {row.get('display', 'not reported')} "
                f"(device-controlled, read-only)",
                flush=True,
            )
        return 0

    if command == "diag":
        if manager is None:
            print("🛰️ voice-pe diag needs the running daemon", flush=True)
            return 1
        result = manager.diag(rest[0] if rest else "")
        if "error" in result:
            print(f"🛰️ No attached device: {result['error']}", flush=True)
            return 1
        print(f"📊 Diagnostics: {result['deviceId']} (profile {result['profile']})", flush=True)
        diag = result.get("diagnostics") or {}
        print("   🎙️ Input", flush=True)
        for name, label in (
            ("input_rms_db", "RMS"),
            ("input_peak_db", "peak"),
            ("noise_floor_db", "noise floor"),
            ("snr_db", "SNR"),
        ):
            value = diag.get(name)
            print(f"      ▫️ {label}: {value if value is not None else '-'} dB", flush=True)
        print(f"      ▫️ queue depth: {diag.get('queue_depth_ms')} ms", flush=True)
        print(f"      ▫️ dropped frames: {diag.get('dropped_chunks')}", flush=True)
        aec = diag.get("aec") or {}
        print(f"   🔄 AEC: {aec.get('aec_state', '-')} "
              f"(reference {aec.get('reference_active', '-')})", flush=True)
        normalizer = diag.get("normalizer") or {}
        if normalizer:
            print(
                f"   🎛️ Normalizer: enabled={normalizer.get('enabled')} "
                f"gain={normalizer.get('gain_db')} dB "
                f"floor={normalizer.get('noise_floor_db')} dB "
                f"clipping={normalizer.get('clipping_ratio')}",
                flush=True,
            )
        return 0

    if command == "calibrate":
        if manager is None:
            print("🛰️ voice-pe calibrate needs the running daemon", flush=True)
            return 1
        key = rest[0] if rest else ""
        profile = _flag(rest, "--profile") or None
        timeout = _flag(rest, "--timeout-s")
        try:
            timeout_s = float(timeout) if timeout else 120.0
        except (TypeError, ValueError):
            timeout_s = 120.0
        print("🎙️ Starting Voice PE auto-calibration (5 steps)", flush=True)
        result = _run_coro(
            manager,
            manager.calibrate(key, profile, timeout_s=timeout_s, window_s=5.0),
        )
        if not isinstance(result, dict) or "error" in result:
            reason = result.get("error") if isinstance(result, dict) else "?"
            print(f"🛰️ calibration failed: {reason}", flush=True)
            return 1
        measurements = result.get("measurements") or {}
        print("   📏 Measurements", flush=True)
        for name, label in (
            ("noise_floor_db", "noise floor"),
            ("speech_rms_db", "speech RMS"),
            ("snr_db", "SNR"),
            ("far_rms_db", "far-field RMS"),
            ("far_degradation_db", "far degradation"),
            ("false_vad_rate", "false-VAD rate"),
            ("whisper_avg_logprob", "Whisper avg_logprob"),
            ("echo_leakage_db", "echo leakage"),
        ):
            value = measurements.get(name)
            if value is not None:
                print(f"      ▫️ {label}: {value}", flush=True)
        print("   🎛️ Tuned settings", flush=True)
        for key_name, value in (result.get("tuned") or {}).items():
            print(f"      ▫️ {key_name}: {value}", flush=True)
        print(
            f"   💾 Saved for {result.get('mac', '?')} (profile "
            f"{result.get('profile', '?')})",
            flush=True,
        )
        return 0

    print(f"❓ Unknown voice-pe subcommand: {command}", flush=True)
    return 1


def _pair(cfg: VoicePEConfig) -> int:
    """Discover, provision the Noise key if needed, then store metadata.

    Metadata is stored for every matched node, plaintext ones included, so the
    next start can reconnect from the persisted address instead of scanning
    again; the integration flag is written as well, otherwise a paired unit
    would still leave the manager disabled. ``forget`` reverses both.
    """
    from .provisioning import provision_noise_key

    async def _run() -> int:
        found = await discover(cfg)
        if not found:
            print("🔗 No Voice PE to pair (check power and Wi-Fi)", flush=True)
            return 1
        entry = found[0]
        print(f"🔗 Pairing {entry.get('node_name') or entry.get('host')}", flush=True)
        mac = str(entry.get("mac_address") or "") or str(entry["host"])
        psk = entry.get("noise_psk") or pe_config.get_psk(cfg, entry.get("mac_address", ""))
        stored = False
        for attempt in range(2):
            client = make_client(
                entry["host"],
                int(entry["port"]),
                psk,
                device_name=entry.get("node_name") or None,
            )
            try:
                await client.connect(login=True, log_errors=True)
                info = await client.device_info()
                meta = {
                    "mac_address": str(getattr(info, "mac_address", "") or mac),
                    "node_name": str(getattr(info, "name", "") or entry.get("node_name", "")),
                    "friendly_name": str(getattr(info, "friendly_name", "") or ""),
                    "project_name": str(getattr(info, "project_name", "") or ""),
                    "project_version": str(getattr(info, "project_version", "") or ""),
                    "voice_feature_flags": int(
                        getattr(info, "voice_assistant_feature_flags", 0) or 0
                    ),
                    "addresses": [str(entry["host"])],
                    "port": int(entry["port"]),
                }
                if psk:
                    meta["noise_psk"] = str(psk)
                    print("  ✅ Existing key accepted, no new key generated", flush=True)
                elif getattr(info, "api_encryption_provisionable", False):
                    ok, encoded = await provision_noise_key(client, info)
                    if ok and encoded:
                        meta["noise_psk"] = encoded
                        psk = encoded
                        print("  🔑 Noise PSK installed and stored", flush=True)
                    else:
                        print("  ⚠️  Provisioning window closed, storing plaintext metadata", flush=True)
                else:
                    print("  ✅ Plaintext node (no Noise key on this firmware)", flush=True)
                # Same metadata path as the live device, so both stay in sync.
                stored = bool(pe_config.save_device_metadata(meta["mac_address"], meta))
                break
            except Exception as err:
                print(f"  ⚠️  Attempt {attempt + 1}: {err}", flush=True)
                psk = None
            finally:
                try:
                    await client.disconnect(True)
                except Exception:
                    pass
        if not stored:
            print("  ❌ Nothing stored - the device is not paired", flush=True)
            return 1
        enabled = pe_config.enable_integration(True)
        print(f"  💾 Stored metadata for {mac} (integration {'enabled' if enabled else 'NOT enabled'})", flush=True)
        return 0 if enabled else 1

    return asyncio.run(_run())


def _run_coro(manager, coro):
    """Run one async manager call and return its raw result (or ``None``).

    The daemon-bundled manager owns a loop thread; a transient manager runs
    the coroutine directly. Errors print and map to ``None``.
    """
    loop = getattr(manager, "_loop", None)
    try:
        if loop is not None:
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=180.0)
        return asyncio.run(coro)
    except Exception as err:
        print(f"⚠️  voice-pe: {err}", flush=True)
        return None


def _await(manager, coro) -> int:
    loop = getattr(manager, "_loop", None)
    if loop is None:
        return 1
    try:
        result = asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=20.0)
    except Exception as err:
        print(f"⚠️  voice-pe: {err}", flush=True)
        return 1
    if result is False:
        print("⚠️  Device did not acknowledge the command", flush=True)
        return 1
    print("✅ Command delivered", flush=True)
    return 0


def _mac_for(cfg: VoicePEConfig, key: str) -> str:
    needle = str(key or "").strip().lower()
    for mac, meta in cfg.devices.items():
        if not needle:
            return mac
        if needle in {mac.lower(), str(meta.get("node_name", "")).lower()}:
            return mac
        if needle == str(mac).replace(":", "").lower():
            return mac
    return ""
