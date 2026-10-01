//! Toastovač GPU frame engine — native zero-copy 4K HDR scene path.
//!
//! Pipeline (all GPU, no full-frame CPU readback):
//!   D3D11 (Intel) fullscreen PS -> RGBA16F HDR target
//!   -> compute shader -> P010 (BT.2020nc, limited, 10-bit) encoder surface
//!   -> oneVPL QSV HEVC Main10 -> Annex-B AU frames
//!
//! IPC:
//!   stdin   [u32 BE len][JSON]  scene state: {"t": <sec>, "hud": {k:v}}
//!   stdout  [u32 BE len][u8 kind][payload]  kind 1=AU, 2=metrics, 3=event
//!
//! readbackBytesPerFrame is always 0 except an explicit debug capture.

mod convert;
mod d3d;
mod mfx;
mod qsv;
mod scene;

use std::collections::VecDeque;
use std::io::{Read, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use scene::{SceneCB, SceneRenderer, FlipMode, H, W};

// ── CLI ─────────────────────────────────────────────────────────────────

struct Args {
    fps: u32,
    fps_den: u32,
    frames: u64, // 0 = run until stdin closes / forever
    out: Option<String>,
    gop: u32,
    pace: bool,
    bitrate_kbps: u32,
    max_kbps: u32,
    bufsize_kb: u32,
    width: u32,
    height: u32,
    flip: String,
}

fn parse_args() -> Args {
    let mut a = Args {
        fps: 30,
        fps_den: 1,
        frames: 0,
        out: None,
        gop: 30,
        pace: true,
        bitrate_kbps: 18000,
        max_kbps: 28000,
        bufsize_kb: 40000,
        width: W,
        height: H,
        flip: "auto".to_string(),
    };
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        let mut val = |d: &str| it.next().unwrap_or_else(|| d.to_string());
        match arg.as_str() {
            "--fps" => a.fps = val("30").parse().unwrap_or(30),
            // Fractional frame rate support (P2.4): --fps 60000 --fps-den 1001
            // = 59.94 fps, matching the box display refresh so the 60.0-vs-
            // 59.94 backlog cannot accumulate (the measured dominant limiter).
            "--fps-den" => a.fps_den = val("1").parse().unwrap_or(1),
            "--frames" => a.frames = val("0").parse().unwrap_or(0),
            "--out" => a.out = Some(val("")),
            "--gop" => a.gop = val("30").parse().unwrap_or(30),
            "--no-pace" => a.pace = false,
            "--bitrate" => a.bitrate_kbps = val("18000").parse().unwrap_or(18000),
            "--maxrate" => a.max_kbps = val("28000").parse().unwrap_or(28000),
            "--bufsize" => a.bufsize_kb = val("40000").parse().unwrap_or(40000),
            "--width" => a.width = val("3840").parse().unwrap_or(3840),
            "--height" => a.height = val("2160").parse().unwrap_or(2160),
            // Explicit orientation override: none | vertical | rotate180 | auto.
            // auto = env TOASTOVAC_FLIP -> display-profile default (see
            // scene::resolve_flip_mode). Local file output is never pre-flipped
            // unless explicitly requested.
            "--flip" => a.flip = val("auto"),
            _ => eprintln!("unknown arg {arg}"),
        }
    }
    a
}

// ── IPC state ───────────────────────────────────────────────────────────

#[derive(Deserialize, Clone, Default)]
struct StateMsg {
    t: Option<f32>,
    hud: Option<serde_json::Map<String, serde_json::Value>>,
}

#[derive(Serialize, Clone)]
struct MetricsMsg {
    kind: &'static str,
    produced_fps: f64,
    encoded_fps: f64,
    render_ms: f64,
    convert_ms: f64,
    encode_ms: f64,
    frame_ms: f64,
    deadline_misses: u64,
    queue_depth: usize,
    readback_bytes_per_frame: u32,
    frames: u64,
    aus: u64,
    render_ms_p95: f64,
    convert_ms_p95: f64,
    encode_ms_p95: f64,
}

// ── framed stdout ───────────────────────────────────────────────────────

const KIND_AU: u8 = 1;
const KIND_METRICS: u8 = 2;
const KIND_EVENT: u8 = 3;

fn write_msg(out: &mut dyn Write, kind: u8, payload: &[u8]) -> std::io::Result<()> {
    let len = (payload.len() as u32).to_be_bytes();
    out.write_all(&len)?;
    out.write_all(&[kind])?;
    out.write_all(payload)?;
    out.flush()
}

fn emit_event(out: &mut dyn Write, name: &str, payload: serde_json::Value) {
    let msg = serde_json::json!({ "event": name, "data": payload });
    let _ = write_msg(out, KIND_EVENT, msg.to_string().as_bytes());
}

// ── stdin reader thread ─────────────────────────────────────────────────

fn spawn_stdin_reader(state: Arc<Mutex<Option<StateMsg>>>, eof: Arc<AtomicBool>) {
    std::thread::spawn(move || {
        let mut stdin = std::io::stdin().lock();
        let mut buf = [0u8; 4];
        loop {
            if stdin.read_exact(&mut buf).is_err() {
                eof.store(true, Ordering::SeqCst);
                return;
            }
            let len = u32::from_be_bytes(buf) as usize;
            if len == 0 || len > 1 << 20 {
                continue;
            }
            let mut body = vec![0u8; len];
            if stdin.read_exact(&mut body).is_err() {
                eof.store(true, Ordering::SeqCst);
                return;
            }
            if let Ok(msg) = serde_json::from_slice::<StateMsg>(&body) {
                if let Ok(mut g) = state.lock() {
                    *g = Some(msg);
                }
            }
        }
    });
}

// ── clock ───────────────────────────────────────────────────────────────

fn clock_string() -> String {
    let t = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64;
    let secs = t % 60;
    let mins = (t / 60) % 60;
    let hours = (t / 3600) % 24;
    format!("{hours:02}:{mins:02}:{secs:02}")
}

fn hud_line(live: u64, fps: u32, pq: &str) -> String {
    format!("live={live}  fps={fps}  pq={pq}")
}

fn p95(samples: &VecDeque<f64>) -> f64 {
    if samples.is_empty() {
        return 0.0;
    }
    let mut v: Vec<f64> = samples.iter().copied().collect();
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = v.len();
    v[(n as f64 * 0.95).floor() as usize]
}

// ── main ────────────────────────────────────────────────────────────────

fn main() {
    let args = parse_args();

    // Raise the Windows timer resolution to 1 ms. std::thread::sleep uses the
    // system timer (default granularity ~15.6 ms); at 59.94 fps the frame
    // interval is 16.68 ms, so a plain sleep overshoots by up to a whole tick
    // and frames CLUMP (measured: deadline_misses grew ~10/s, box saw bursts
    // -> visible blink/jump). timeBeginPeriod(1) makes sleeps precise to 1 ms;
    // the pacing loop then spin-waits the final sub-ms to hit the deadline.
    #[cfg(windows)]
    unsafe {
        let _ = windows::Win32::Media::timeBeginPeriod(1);
    }

    // pipe mode: AUs + events + metrics share framed stdout.
    // file mode: raw AUs to the file, events/metrics to stderr.
    let stdout = std::io::stdout();
    let stderr = std::io::stderr();
    let file_mode = args.out.is_some();
    let mut au_out: Box<dyn Write> = match &args.out {
        Some(path) => Box::new(std::fs::File::create(path).expect("cannot create --out file")),
        None => Box::new(stdout),
    };
    let mut msg_out: Box<dyn Write> = if file_mode {
        Box::new(stderr)
    } else {
        Box::new(std::io::stdout())
    };
    let mut write_au = |payload: &[u8]| -> std::io::Result<()> {
        if file_mode {
            au_out.write_all(payload)?;
            au_out.flush()
        } else {
            write_msg(&mut au_out, KIND_AU, payload)
        }
    };

    eprintln!("ENGINE start fps={}/{} frames={} gop={} out={:?} pace={}",
              args.fps, args.fps_den, args.frames, args.gop, args.out, args.pace);

    // 1. D3D11 on Intel
    let factory = match d3d::create_dxgi_factory() {
        Ok(f) => f,
        Err(e) => {
            emit_event(&mut msg_out, "ENGINE_FATAL", serde_json::json!({
                "reason": "dxgi_factory", "detail": e }));
            std::process::exit(2);
        }
    };
    let adapter = match d3d::pick_intel_adapter(&factory) {
        Ok(a) => a,
        Err(e) => {
            emit_event(&mut msg_out, "ENGINE_FATAL", serde_json::json!({
                "reason": "no_intel_adapter", "detail": e }));
            std::process::exit(2);
        }
    };
    let adapter_iface = match d3d::find_adapter(&factory, adapter.vendor, adapter.device) {
        Ok(a) => a,
        Err(e) => {
            emit_event(&mut msg_out, "ENGINE_FATAL",
                       serde_json::json!({ "reason": "intel_adapter_not_found", "detail": e }));
            std::process::exit(2);
        }
    };
    let dev = match d3d::create_device_on(&adapter_iface) {
        Ok(d) => d,
        Err(e) => {
            emit_event(&mut msg_out, "ENGINE_FATAL",
                       serde_json::json!({ "reason": "d3d11_device", "detail": e }));
            std::process::exit(2);
        }
    };
    emit_event(&mut msg_out, "ADAPTER", serde_json::json!({
        "name": adapter.name,
        "vendor": format!("0x{:04x}", adapter.vendor),
        "device": format!("0x{:04x}", adapter.device),
        "luid": format!("{:08x}{:08x}", adapter.luid_high, adapter.luid_low),
        "featureLevel": format!("0x{:x}", dev.feature_level),
    }));

    // 2. scene renderer
    let flip = scene::resolve_flip_mode(&args.flip, file_mode);
    emit_event(&mut msg_out, "DISPLAY_PROFILE", serde_json::json!({
        "target": if file_mode { "LOCAL_FILE" } else { "HOMATICS_AMLOGIC_VIDEO_PLANE" },
        "verticalPreFlip": flip == FlipMode::Vertical,
        "effectiveMode": flip.name(),
        "override": args.flip,
    }));
    let mut scene = match SceneRenderer::new(&dev.device, &dev.context, flip) {
        Ok(s) => s,
        Err(e) => {
            emit_event(&mut msg_out, "ENGINE_FATAL",
                       serde_json::json!({ "reason": "scene_renderer", "detail": e }));
            std::process::exit(2);
        }
    };

    // 3. converter
    let conv = match convert::Converter::new(&dev.device) {
        Ok(c) => c,
        Err(e) => {
            emit_event(&mut msg_out, "ENGINE_FATAL",
                       serde_json::json!({ "reason": "converter", "detail": e }));
            std::process::exit(2);
        }
    };

    // 4. QSV encoder
    let mut enc = match qsv::Encoder::new(
        &dev.device,
        &dev.adapter,
        &qsv::EncoderInit {
            fps: args.fps,
            fps_den: args.fps_den,
            bitrate_kbps: args.bitrate_kbps,
            max_kbps: args.max_kbps,
            bufsize_kb: args.bufsize_kb,
            gop: args.gop,
        },
        args.width,
        args.height,
    ) {
        Ok(e) => e,
        Err(e) => {
            emit_event(&mut msg_out, "ENGINE_FATAL",
                       serde_json::json!({ "reason": "qsv_encoder", "detail": e }));
            std::process::exit(2);
        }
    };

    // per-surface UAVs
    let mut uavs = Vec::with_capacity(enc.surfaces.len());
    for tex in &enc.textures {
        match convert::Converter::create_p010_uavs(&dev.device, tex) {
            Ok((y, uv)) => uavs.push(Some((y, uv))),
            Err(e) => {
                emit_event(&mut msg_out, "ENGINE_FATAL",
                           serde_json::json!({ "reason": "p010_uav", "detail": e }));
                std::process::exit(2);
            }
        }
    }
    emit_event(&mut msg_out, "ENCODER", serde_json::json!({
        "impl": enc.impl_name,
        "apiVersion": format!("{}.{}", enc.api_version.0, enc.api_version.1),
        "codec": "HEVC Main10",
        "level": enc.actual_level,
        "surfaces": enc.surfaces.len(),
        "renderFormat": "RGBA16F",
        "encoderInputFormat": "P010",
        "zeroCopy": true,
        "readbackBytesPerFrame": 0,
    }));

    // 5. stdin reader
    let state = Arc::new(Mutex::new(None::<StateMsg>));
    let eof = Arc::new(AtomicBool::new(false));
    if args.out.is_none() {
        spawn_stdin_reader(state.clone(), eof.clone());
    } else {
        eof.store(true, Ordering::SeqCst);
    }

    // 6. frame loop
    let mut rendered: u64 = 0;
    let mut encoded_au: u64 = 0;
    let mut deadline_misses: u64 = 0;
    let mut queue_depth: usize = 0;
    let mut render_ring: VecDeque<f64> = VecDeque::with_capacity(240);
    let mut convert_ring: VecDeque<f64> = VecDeque::with_capacity(240);
    let mut encode_ring: VecDeque<f64> = VecDeque::with_capacity(240);
    let mut frame_ring: VecDeque<f64> = VecDeque::with_capacity(240);
    let mut render_sum = 0.0;
    let mut convert_sum = 0.0;
    let mut encode_sum = 0.0;
    let mut frame_sum = 0.0;
    let mut last_metric = Instant::now();
    let mut pending: VecDeque<mfx::mfxSyncPoint> = VecDeque::with_capacity(8);

    let t0 = Instant::now();

    let run_frames = if args.frames == 0 { u64::MAX } else { args.frames };
    let mut i: u64 = 0;

    while i < run_frames {
        let now = Instant::now();
        if args.pace {
            // P5.1: exact rational pacing — deadline(n) = t0 + n * den / num
            // seconds, computed in integer nanoseconds (u128) so 60000/1001
            // never drifts toward 60.0 (16.683333 ms per frame, not 16.666667).
            let n = i as u128;
            let deadline_ns = (n * args.fps_den as u128 * 1_000_000_000) / args.fps as u128;
            let deadline = t0 + Duration::from_nanos(deadline_ns as u64);
            if now < deadline {
                // timeBeginPeriod(1) makes std::thread::sleep precise to ~1 ms
                // (vs 15.6 ms default), so sleeping to the deadline gives even
                // frame spacing WITHOUT burning CPU. A spin-wait here starves
                // the oneVPL encoder's CPU threads (measured: encodeMs 1.04 ->
                // 6 ms, deadline misses worse). Sleep only; 1 ms jitter at
                // 59.94 fps is invisible (1.6% of frame period).
                std::thread::sleep(deadline - now);
            }
            if Instant::now() > deadline + Duration::from_millis(2) {
                deadline_misses += 1;
            }
        }
        let f_start = Instant::now();

        // state from stdin (latest wins)
        let st = {
            let mut g = state.lock().unwrap();
            g.take().unwrap_or_default()
        };
        // scene time from the same rational rate (P5.2): t = i * den / num, f64
        // so a multi-hour session never drifts (f32 loses precision above
        // ~16.7M frames*den products).
        let t = st.t.unwrap_or((i as f64 * args.fps_den as f64 / args.fps as f64) as f32);
        let (live, fps, pq) = match st.hud {
            Some(m) => (
                m.get("live").and_then(|v| v.as_u64()).unwrap_or(i),
                m.get("fps").and_then(|v| v.as_u64()).unwrap_or(args.fps as u64),
                m.get("pq").and_then(|v| v.as_str()).unwrap_or("1").to_string(),
            ),
            None => (i, args.fps as u64, "1".to_string()),
        };
        let clock = clock_string();
        let hudline = hud_line(live, fps as u32, &pq);

        // render
        let r0 = Instant::now();
        if let Err(e) = scene.render(t, &clock, &hudline) {
            emit_event(&mut msg_out, "ENGINE_FATAL",
                       serde_json::json!({ "reason": "render", "detail": e }));
            std::process::exit(2);
        }
        let r1 = Instant::now();

        // convert into this frame's encoder surface
        let surface_idx = (i % enc.surfaces.len() as u64) as usize;
        let (y_uav, uv_uav) = uavs[surface_idx].as_ref().expect("uav");
        conv.convert(&dev.context, &scene.rt_srv, y_uav, uv_uav, args.width, args.height);
        let r2 = Instant::now();
// encode: submit; the shared bitstream holds one in-flight AU, so
        // drain the previous frame's sync before submitting the next.
        if pending.len() >= 1 {
            if let Some(sync) = pending.pop_front() {
                match enc.sync_and_get_au(sync, 5000) {
                    Ok(Some(au)) => {
                        let _ = write_au(&au);
                        encoded_au += 1;
                    }
                    Ok(None) => {}
                    Err(e) => {
                        emit_event(&mut msg_out, "ENGINE_FATAL",
                                   serde_json::json!({ "reason": "sync", "detail": e }));
                        std::process::exit(2);
                    }
                }
            }
        }
        let mut sync_slot: Option<mfx::mfxSyncPoint> = None;
        match enc.encode_surface(surface_idx, i, &mut sync_slot) {
            Ok(_) => {}
            Err(e) => {
                emit_event(&mut msg_out, "ENGINE_FATAL",
                           serde_json::json!({ "reason": "encode", "detail": e }));
                std::process::exit(2);
            }
        }
        if let Some(s) = sync_slot {
            pending.push_back(s);
        }
        let r3 = Instant::now();

        // metrics
        let rd = (r1 - r0).as_secs_f64() * 1000.0;
        let cd = (r2 - r1).as_secs_f64() * 1000.0;
        let ed = (r3 - r2).as_secs_f64() * 1000.0;
        let fd = (r3 - f_start).as_secs_f64() * 1000.0;
        render_ring.push_back(rd);
        convert_ring.push_back(cd);
        encode_ring.push_back(ed);
        frame_ring.push_back(fd);
        render_sum += rd;
        convert_sum += cd;
        encode_sum += ed;
        frame_sum += fd;
        while render_ring.len() > 240 {
            render_ring.pop_front();
        }
        while convert_ring.len() > 240 {
            convert_ring.pop_front();
        }
        while encode_ring.len() > 240 {
            encode_ring.pop_front();
        }
        while frame_ring.len() > 240 {
            frame_ring.pop_front();
        }
        queue_depth = pending.len();
        rendered += 1;
        i += 1;

        if last_metric.elapsed() >= Duration::from_secs(2) {
            last_metric = Instant::now();
            let wall = (Instant::now() - t0).as_secs_f64();
            let m = MetricsMsg {
                kind: "metrics",
                produced_fps: rendered as f64 / wall.max(0.001),
                encoded_fps: encoded_au as f64 / wall.max(0.001),
                render_ms: render_sum / rendered as f64,
                convert_ms: convert_sum / rendered as f64,
                encode_ms: encode_sum / rendered as f64,
                frame_ms: frame_sum / rendered as f64,
                deadline_misses,
                queue_depth,
                readback_bytes_per_frame: 0,
                frames: rendered,
                aus: encoded_au,
                render_ms_p95: p95(&render_ring),
                convert_ms_p95: p95(&convert_ring),
                encode_ms_p95: p95(&encode_ring),
            };
            let _ = write_msg(&mut msg_out, KIND_METRICS, serde_json::to_string(&m).unwrap().as_bytes());
        }

        // stop when stdin closed (parent gone) after at least 2 s — only in
        // unlimited mode; an explicit --frames limit is authoritative (the
        // file-mode EOF early-exit previously capped --out encodes at 61).
        if args.frames == 0 && eof.load(Ordering::SeqCst) && rendered > 60 {
            break;
        }
    }

    // flush encoder
    match enc.flush() {
        Ok(au) => {
            if !au.is_empty() {
                let _ = write_au(&au);
                encoded_au += 1;
            }
        }
        Err(e) => eprintln!("FLUSH_FAIL {e}"),
    }

    let wall = (Instant::now() - t0).as_secs_f64();
    let final_m = MetricsMsg {
        kind: "final",
        produced_fps: rendered as f64 / wall.max(0.001),
        encoded_fps: encoded_au as f64 / wall.max(0.001),
        render_ms: render_sum / rendered.max(1) as f64,
        convert_ms: convert_sum / rendered.max(1) as f64,
        encode_ms: encode_sum / rendered.max(1) as f64,
        frame_ms: frame_sum / rendered.max(1) as f64,
        deadline_misses,
        queue_depth: 0,
        readback_bytes_per_frame: 0,
        frames: rendered,
        aus: encoded_au,
        render_ms_p95: p95(&render_ring),
        convert_ms_p95: p95(&convert_ring),
        encode_ms_p95: p95(&encode_ring),
    };
    let _ = write_msg(&mut msg_out, KIND_METRICS, serde_json::to_string(&final_m).unwrap().as_bytes());
    eprintln!("ENGINE end frames={rendered} aus={encoded_au} wall={wall:.2}s");
}