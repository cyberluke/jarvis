# Toaster Universe — Execution Contract

Working memory for the Ambient AGI Theatre. The 452k-line bible is the archive. This file is the live contract.

## Invariants

1. The simulation yields to human intent. Always.
2. Safety stack beats every later director: user intent → cursor/attention → activity → physics → character graph → social → comedy → narrative → LLM.
3. LLM never owns pixels, hit-testing, collision, focus, or raw motion. Structured `SceneIntent` only.
4. Main Toaster visual identity is the existing QPainter toaster. Extend it. Do not redesign it.
5. Mini Toasts are vector creatures, not pixel-art sprites.
6. User model is rhythm-only: typing/mouse/scroll/window cadence. No semantic surveillance of document content.
7. Rare events stay rare. Novelty budget + cooldowns are mandatory.
8. Pet overlay is click-through. Precision clicks, drags, selections, and scrolls win.
9. Feature flags and rollback exist for every disruptive knob.

## Unique product contracts

- Main Toaster soul: look-at-cursor, blink, breathe, hover spring, jelly, crumbs, heat shimmer, existing JarvisState face machine.
- Mini Toast ritual: wander → cold → approach → queue → jump slot → toast/overtoast → pop → cool / burnt / recover.
- Traits mutate slowly and never bypass safety.
- Population tiers: 1–3 intimate, 4–8 alive, 9–20 infestation, 20+ event-only swarm. Work mode caps at 3.
- Keyboard games ride natural typing. No exclusive gameplay mode.
- Doomscroll layer: observe → hearts → audience → chairs → popcorn → toaster joins → deadpan → cooldown.
- Appliance universe: microwave, air fryer, rice cooker, oven, espresso, mini fridge, secret chamber. Interiors may be larger than exteriors. Portals allowed when contained.
- Comedy: visual gag first, deadpan commitment, Prague roast pack, personal lore as local aliases.
- Observability: transitions, safety overrides, scene start, rare fires, frame/physics times.

## Roadmap

1. Phase 1 — Main Toaster soul + world host
2. Phase 2 — First Mini Toast + movement/safety arbiter
3. Phase 3 — Social simulation
4. Phase 4 — User signal / event layer
5. Phase 5 — Population / infestation + keyboard gameplay
6. Phase 6 — Appliance universe
7. Phase 7 — LLM Director + personal lore
8. Phase 8 — Rare-event living world

## Architecture decisions

- Runtime lives in `src/desktop_app/toaster_universe/`.
- Host: existing `FaceWindow` / `LowPolyFaceWidget` plus fullscreen click-through `WorldOverlay`.
- World clock is simulated (`world.clock`) so tests and live ticks share the same state machine.
- Keyboard/ecology/chaos propose states; `BehaviorEngine.enter` commits them so safety and graph rules still apply.
- Scene apply ignores toasts already in yield/safety states.
- Config keys: `toaster_universe_enabled`, bible camelCase aliases, and `toaster_universe_*` snake aliases.

## Implemented now

- Phase 1: soul look/jelly/crumbs/shimmer wired into existing toaster paint path; hover/click/voice events emit into the bus.
- Phase 2: Mini Toast entity, hop/wander, slot ritual, vector renderer, panic/strong/soft/reading/drag/selection/scroll yield, click-through overlay.
- Phase 3: social pairing, copy-dance, playful fight, crumb carry/eat. Safety states excluded.
- Phase 4: rhythm signals, content-free key probe, foreground app class, work-rect from OS, event bus with per-type debounce.
- Phase 5: population caps + decay, infestation/event swarm, keyboard modes as proposals, doomscroll phase machine.
- Phase 6: seven appliances, unlock/admit/release/heat, secret chamber / rice / microwave / air-fryer scene hooks, portal enter/return.
- Phase 7: SceneIntent schema, reject-unknown, lore catalog keys, comedy category routing including personal lore.
- Phase 8: 19 rare events, novelty budget, 20-minute global cooldown, 6-hour per-event cooldown, chaos modes that skip yielding toasts.
- Tests: `tests/test_toaster_universe.py`.
- Overlay attached from `desktop_app.app.JarvisSystemTray` when enabled.

## Remaining work

- Live LLM call path is payload-only (`pending_director_payload`). Ingest exists; outbound model call stays gated off.
- UIA caret/selection is best-effort. Baseline attention uses cursor/click/drag/scroll patches.
- Reduced-motion is still the toaster widget hint only.
- Multi-monitor topology is generic; this machine currently reports one Qt screen.

## Blockers

- None that stop the remaining runtime path. Bible filler (repeated numbered clauses) is discarded; unique names/invariants above are kept.
