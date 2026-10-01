# Voice PE Full Audio Pipeline and Auto-Calibration

Complete the Voice PE audio configuration: one typed settings model shared
by UI, runtime, calibration and diagnostics; per-device profiles; a
speech-aware post normalizer; a five-step auto-calibration wizard; and live
diagnostics. No decorative controls: every visible setting is wired to a
layer that consumes it.

## Phase 0 audit (recorded findings)

| Area | Finding |
|---|---|
| Integration | Direct Native API client (`aioesphomeapi`), stock firmware, no Home Assistant. Product mode `Stock Voice PE / push-to-talk + continued conversation` |
| aioesphomeapi API used | `subscribe_voice_assistant(start/stop/audio/announcement_finished)`, `set/get_voice_assistant_configuration` (wake words only), `send_voice_assistant_audio` (TTS PCM). No setter RPC exists for per-session audio DSP settings |
| Pipeline start payload | `handle_pipeline_start(conversation_id, flags, audio_settings, wake_word_phrase)` — `audio_settings` (the device's `VoiceAssistantAudioSettings`) arrives on every run and is **currently ignored** |
| Device firmware config | `active_wake_words` read/write with read-back (the only configurable firmware side), `led_ring` sync. On-device DSP settings are YAML-owned |
| Hidden transported fields | `noise_suppression_level` (enum none/low/medium/high), `auto_gain` (dBFS, -1 = disabled), `volume_multiplier` (float) ride in `audio_settings` but are never read. Device-authoritative: shown read-only, never silently dropped |
| AEC control exposed | `voice_pe_dsp_mode` (`host_raw_aec` default / `device_enhanced` / `shadow_compare`), `voice_pe_aec_acquire_max_ms`, jitter targets; host AEC3 lane + `reference_active`/`aec_state` telemetry |
| Stream pre/post device DSP | Device delivers raw channel blocks; channel 0 = XMOS-enhanced, channel 1 = raw. Host lane cleans host-side, post-device, pre-VAD. The delivered signal (post-lock, post-AEC) is what the listener and the WebAudio bridge receive |
| Input PCM format | PCM16LE, 16 kHz, mono, 512 samples/block (32 ms) |
| Silero VAD | Not used on the Voice PE path. Endpointing = WebRTC VAD (`vad_aggressiveness` 0-3, `vad_frame_ms` 20, `vad_pre_roll_ms` 240) + trailing-silence bookkeeping + `whisper_post_roll_ms`; faster-whisper `vad_filter=False` (a second Silero pass would only re-cut the clip). Silero appears only in the V271 PWA side of the WebAudio bridge |
| Whisper / faster-whisper | `whisper_language` (cs+vi pair-transcribe), `whisper_min_avg_logprob` (-0.7 acceptance gate), `whisper_no_speech_threshold` (0.5 hallucination gate), `whisper_min_audio_duration`, `whisper_min_word_length`, `whisper_post_roll_ms`. Beam size is backend default (not configurable; not exposed). All gates are read from the shared settings at decode time |
| Acoustic gate / language / confidence / post-roll | Hallucination gate (`no_speech_prob >= threshold`), avg_logprob gate (never softens), intent judge, per-utterance frame bookkeeping (`voiced/total/trailing_silence/post_roll_frames`), `last_segment` record (text, avg_logprob, no_speech_prob, frame stats) — the diagnostics and calibration read from these |

## Settings ownership

One typed model, `VoicePEAudioSettings` (`integrations/voice_pe/audio_settings.py`),
is the single source for every host-side knob. Each field carries metadata:

```text
key, label, unit, min/max, default, source_layer, restart_required, profile_scope
```

Layers: `device` (read-only, reported by the satellite), `host` (normalizer,
AEC mode), `listener` (WebRTC VAD), `whisper` (decode gates). A field is
visible only when its layer consumes it; anything else is marked
`unsupported` with the reason, never silently dropped.

## Profiles

`AUTO`, `DESK`, `ROOM`, `FAR_FIELD`, `MEETING`. A profile is a preset of
overrides over the base config for the profile-scoped fields:

| Field | AUTO | DESK | ROOM | FAR_FIELD | MEETING |
|---|---|---|---|---|---|
| `vad_aggressiveness` | base | 2 | 1 | 0 | 0 |
| `vad_pre_roll_ms` | base | 240 | 320 | 400 | 480 |
| `whisper_post_roll_ms` | base | 400 | 600 | 900 | 1400 |
| `whisper_min_avg_logprob` | base | base | base | base | -1.1 |
| `whisper_no_speech_threshold` | base | base | base | base | 0.6 |
| `normalizer_enabled` | base | on | on | on | on |
| `normalizer_target_db` | base | -26 | -28 | -32 | -30 |
| `normalizer_max_gain_db` | base | 6 | 9 | 12 | 9 |
| `normalizer_attack_db_per_s` | base | 3 | 2.5 | 2 | 2 |
| `normalizer_limiter_db` | base | -1 | -1 | -1.5 | -1.5 |

`AUTO` inherits the base config unchanged. Resolution order: per-device
override (device metadata `profile`, set via
`jarvis voice-pe profile <device> <name>`) > `voice_pe_profile` > `AUTO`.

## Voice Assistant vs Meeting behaviour

One active profile at a time, applied at the layer that consumes it:

- the **normalizer** lives per device (per `AudioIngress`), so it always
  follows the device's active profile;
- **listener/whisper knobs** are global (one shared listener); the manager
  applies the active profile's values to the live settings when the profile
  changes (same mechanism as the wake-word setting write), so the voice
  assistant runs with the configured profile and a coach (meeting scribe)
  session switches the device to `MEETING` and back on stop;
- the **device-reported** DSP fields are read-only and identical for both.

No compromise preset is forced on both modes.

## Speech-aware post normalizer

`SpeechAwareNormalizer` (`integrations/voice_pe/normalizer.py`) sits on the
delivered signal, applied in the ingress pump after the single
bytes->float32 conversion and before the listener queue:

```text
silence -> do not learn gain (noise floor EMA continues, gain frozen)
speech  -> estimate level (EMA of speech RMS)
           slowly adjust gain toward target (attack/decay per second)
           clamp maximum gain
           soft limiter at the configured ceiling
```

Guarantees: gain only rises while speech is present; max gain and
attack rate are bounded; the limiter caps peaks at `limiter_db`; clipping
is measured post-limiter. Never blind permanent gain. Metrics: current
gain, noise floor, speech level, last RMS/peak, clipping ratio, speech/
silence frame counts.

## Auto-calibration wizard

`jarvis voice-pe calibrate <device> [--profile NAME] [--timeout-s N]`.
Semi-automated: each step instructs the user (press the centre button /
speak / keep quiet), measures from the live delivered signal through a
temporary ingress sink, and reads Whisper confidence from the listener's
`last_segment`.

1. **Silence baseline** — noise floor (EMA RMS), peak noise, false-positive
   VAD rate (voiced frames / total while silent), dropped-frame anomalies.
2. **Normal speaking position** — speech RMS/peak, SNR over the noise
   floor, VAD probability, clipping %, Whisper `avg_logprob` and
   `no_speech_prob` of the last transcript.
3. **Far-field voice** — same metrics at distance; degradation = dB drop,
   voiced-ratio drop, confidence drop.
4. **Echo/AEC check** — play a known sample on the device
   (`announce(... start_conversation=False)`) while the mic streams;
   echo leakage = mic RMS during playback minus noise floor; plus the host
   AEC lane's `aec_state`/`reference_active` from the ingress telemetry.
   When the device cannot play (no speaker path), the step reports
   `unavailable` with the reason.
5. **Auto tune** — conservative targets: gain covers the far-field
   degradation plus headroom, clamped; VAD aggressiveness rises only when
   false-VAD is high and falls when far-field speech is missed; post-roll
   grows with far-field voiced-ratio loss; `whisper_min_avg_logprob` is
   relaxed only to the observed confidence minus headroom; the normalizer
   is enabled when far-field level sits below the target.

Tuned settings are stored as per-device calibration overrides
(`voice_pe_calibrations[mac]`) merged over the active profile, and the
calibration evidence (levels, SNR, false-VAD rate, confidence, clipping,
echo leakage, chosen settings, timestamp) is persisted with them.

## Diagnostics panel

`jarvis voice-pe diag <device>` and the device health snapshot expose live:

```text
input RMS / peak / clipping %        (frame stats of the delivered signal)
noise floor / estimated SNR          (normalizer + frame stats)
VAD probability / state              (voiced ratio of the last utterance)
Whisper confidence / avg_logprob     (listener last_segment)
no_speech_probability                (listener last_segment)
dropped audio frames / buffer depth  (ingress counters)
echo leakage indicator               (AEC lane state/reference + echo check)
active profile / effective gains     (profile resolution + normalizer gain)
device audio settings                (reported noise suppression / auto gain / volume multiplier)
```

## Runtime safety

- Bounded maximum gain, bounded gain-change rate, limiter ceiling, bounded
  ingress queues (existing `voice_pe_audio_queue_ms` drop-oldest) and the
  bridge tap budget.
- On device reconnect (`set_stream`/generation change) the active profile
  is re-applied and revalidated.
- A backend rejecting a setting (e.g. no WebRTC VAD, no native lane, no
  speaker for the echo step) surfaces as `unsupported` with the reason.

## Acceptance mapping

| # | Criterion | Evidence |
|---|---|---|
| 1 | Report documents real supported settings | this spec + `audio_settings.SETTING_METADATA` |
| 2 | Every visible setting has a runtime effect | layer column: host/listener/whisper all consumed; device fields read-only |
| 3 | Calibration completes 5 steps | wizard measurements; echo step honest when unavailable |
| 4 | Calibration persists per device/profile | `voice_pe_calibrations` + device metadata profile |
| 5 | Desk and Meeting differ | profile tables + `set_meeting_mode` switching |
| 6 | Live diagnostics | `diag`/`health_snapshot` fields |
| 7 | Far-field stability | wizard far-field degradation measured; tune covers it |
| 8 | Profile survives restart/reconnect | persisted config + re-apply on reconnect |
| 9 | Unsupported controls explicit | `unsupported`/`not reported` marks, never silent drops |
| 10 | No silent fallback | device settings read-only; echo step reports unavailable |

## Observability

`debug_log` lines carry `component=voice_pe` with `profile`,
`meeting_mode`, `normalizer_gain_db`, `aec_state`, `echo_leakage_db`.
`health_snapshot()` folds the normalizer metrics, device audio settings,
active profile and AEC indicator into one diagnostics payload.