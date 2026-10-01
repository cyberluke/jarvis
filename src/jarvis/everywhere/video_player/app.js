/* ══════════════════════════════════════════════════════════════════
   Toastovač Vision Shell — player core.

   Architecture:
     Stage   — video is ALWAYS the bottom visual layer (z0).
     Veil    — translucent dim during voice/thinking/responding (z10).
     Subtitles — custom VTT renderer (z20).
     Voice   — centered transient state text (z30).
     Idle    — ONLY before any video exists (z40).
     HUD     — transient chrome: topline, rail, seekbar (z50).
     Toast   — transient notification (z60).
     Spark   — the light line, the single motion language (z70).

   Rule: no UI state may create an opaque fullscreen surface while a
   video is mounted. Transient states only dim through the veil.

   State machine (body[data-state]):
     IDLE → LOADING → PLAYING ⇄ PAUSED
                      ↘ ERROR
     Voice overlays (non-owning): LISTENING / THINKING / SPEAKING /
     RESPONDING — video stays visible underneath.

   Playback logic preserved: ?tv=1 detection, transcoded mp4 path,
   muted-autoplay workaround, trusted-gesture unmute, voice command
   bridge, VTT subtitles.
   ══════════════════════════════════════════════════════════════════ */
"use strict";

const $ = (id) => document.getElementById(id);

async function postJSON(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return r.json();
}

function fmtTime(sec) {
  if (!isFinite(sec) || sec < 0) return "0:00";
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

/* ── Subtitle renderer: VTT cues + custom font/scale/color ──────── */
class SubtitleRenderer {
  constructor() {
    this.cues = [];
    this.mode = "original";            // original | translated | off
    this.style = { font: "system-ui, sans-serif", scale: 100, color: "#ffffff", bg: true };
    this._active = "";
    this._render();
  }

  parseVTT(text) {
    const cues = [];
    const ts = /(\d{1,2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})\.(\d{3})/;
    const toSec = (h, m, s, ms) => +h * 3600 + +m * 60 + +s + +ms / 1000;
    const lines = text.split(/\r?\n/);
    let i = 0;
    while (i < lines.length) {
      const m = lines[i].match(ts);
      if (m) {
        const start = toSec(m[1], m[2], m[3], m[4]);
        const end = toSec(m[5], m[6], m[7], m[8]);
        const body = [];
        i++;
        while (i < lines.length && lines[i].trim() && !ts.test(lines[i])) {
          body.push(lines[i].trim().replace(/<[^>]+>/g, ""));
          i++;
        }
        if (body.join(" ")) cues.push({ start, end, text: body.join(" ") });
        continue;
      }
      i++;
    }
    this.cues = cues;
  }

  update(t) {
    if (this.mode === "off") { this._active = ""; this._render(); return; }
    let active = "";
    for (const c of this.cues) {
      if (t >= c.start && t < c.end) { active = c.text; break; }
    }
    if (active !== this._active) { this._active = active; this._render(); }
  }

  setStyle(style) {
    Object.assign(this.style, style);
    this._render();
  }

  _render() {
    const el = $("subtitle-text");
    el.textContent = this._active;
    el.style.fontFamily = this.style.font;
    el.style.fontSize = `clamp(32px, ${3.2 * this.style.scale / 100}vw, ${58 * this.style.scale / 100}px)`;
    el.style.color = this.style.color;
    el.classList.toggle("has-bg", this.style.bg);
  }
}

/* ── Toastovač Shell: state, spark, hud, voice, modules ─────────── */
const Shell = {
  _ctx: null,
  modules: [],                // ToastovacModule[] — composable surfaces
  _mounted: new Set(),
  registerModule(mod) {
    this.modules.push(mod);
    if (this._ctx && !this._mounted.has(mod)) {
      this._mounted.add(mod);
      try { mod.mount(this._ctx); } catch (e) { console.error("module", mod.id, e); }
    }
  },

  /* state machine — body[data-state] drives CSS choreography */
  setState(state, opts = {}) {
    document.body.dataset.state = state;
    if (state === "LISTENING" || state === "THINKING" || state === "SPEAKING") {
      $("voice-layer").classList.remove("hidden");
      if (opts.text != null) $("voice-caption").textContent = opts.text;
      if (opts.status != null) $("voice-status").textContent = opts.status;
    } else if (state === "RESPONDING") {
      $("voice-layer").classList.remove("hidden");
      if (opts.text != null) $("voice-caption").textContent = opts.text;
      $("voice-status").textContent = opts.status || "";
      clearTimeout(this._respondTimer);
      this._respondTimer = setTimeout(() => {
        $("voice-layer").classList.add("hidden");
        if (this._videoPlaying()) this.setState("PLAYING");
        else this.setState("PAUSED");
      }, opts.hideAfter || 6000);
    } else {
      $("voice-layer").classList.add("hidden");
    }
    this.modules.forEach((m) => { try { m.onState && m.onState(state); } catch (e) { console.error(e); } });
  },

  _videoPlaying() {
    const v = $("video");
    return v && !v.paused && v.currentTime > 0;
  },

  /* the Spark: brief activity shimmer on any input */
  pulse() {
    const prev = document.body.dataset.state;
    if (["LISTENING", "THINKING", "SPEAKING", "RESPONDING"].includes(prev)) return;
    document.body.dataset.state = "ACTIVITY";
    clearTimeout(this._pulseTimer);
    this._pulseTimer = setTimeout(() => {
      // restore: keep IDLE/LOADING as-is, otherwise PLAYING/PAUSED by video
      const was = this._pulsePrev;
      document.body.dataset.state =
        was === "IDLE" || was === "LOADING" || was === "ERROR" ? was
        : (this._videoPlaying() ? "PLAYING" : "PAUSED");
    }, 1200);
    this._pulsePrev = prev;
  },

  /* HUD: transient chrome */
  _hudTimer: null,
  showHUD(ms = 4000) {
    $("hud").classList.remove("hidden");
    clearTimeout(this._hudTimer);
    this._hudTimer = setTimeout(() => this.hideHUD(), ms);
  },
  hideHUD() { $("hud").classList.add("hidden"); },

  toast(text, ms = 3000) {
    const t = $("toast");
    t.textContent = text;
    t.classList.remove("hidden");
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => t.classList.add("hidden"), ms);
  },

  companion(text, ms = 5000) {
    const c = $("companion");
    c.textContent = text;
    c.classList.remove("hidden");
    clearTimeout(this._companionTimer);
    this._companionTimer = setTimeout(() => c.classList.add("hidden"), ms);
  },

  /* adaptive rail: current context's surfaces */
  _syncRail() {
    this.modules.forEach((m) => {
      try { m.onRail && m.onRail($("rail")); } catch (e) { console.error(e); }
    });
  },

  setCtx(ctx) {
    this._ctx = ctx;
    this.modules.forEach((m) => {
      if (!this._mounted.has(m)) {
        this._mounted.add(m);
        try { m.mount(ctx); } catch (e) { console.error("module", m.id, e); }
      }
    });
  },
};

/* ── Toastovač modules (composable surfaces) ───────────────────────
   interface ToastovacModule {
     id: string;
     mount(ctx): void;
     unmount?(): void;
     onCommand?(cmd): void;
     onState?(state): void;
     onRail?(railEl): void;   // append context buttons
   }
   ─────────────────────────────────────────────────────────────── */

/* Media module: owns the video element + transport commands */
const MediaModule = {
  id: "media",
  mount(ctx) {
    this.ctx = ctx;
    const v = ctx.video;
    v.addEventListener("play", () => {
      $("btn-play").textContent = "Pause";
      document.body.dataset.state = "PLAYING";
      $("idle").classList.add("hidden");
    });
    v.addEventListener("pause", () => {
      $("btn-play").textContent = "Play";
      document.body.dataset.state = "PAUSED";
    });
    v.addEventListener("timeupdate", () => {
      ctx.subtitles.update(v.currentTime);
      ctx.app._paintSeek();
    });
    v.addEventListener("progress", () => ctx.app._paintSeek());
    v.addEventListener("error", () => {
      const e = v.error;
      Shell.setState("ERROR");
      Shell.toast("Chyba přehrávání: " + (e ? e.code + " " + (e.message || "") : "unknown"), 6000);
    });
    v.addEventListener("playing", () => {
      $("idle").classList.add("hidden");
      document.body.dataset.state = "PLAYING";
    });
    // TV remotes often synthesize BOTH a keydown(OK) and a click per press;
    // debounce in the app so they don't cancel each other out.
    v.addEventListener("click", () => { ctx.app._togglePlay(); Shell.showHUD(); });

    $("btn-play").onclick = () => { ctx.app._togglePlay(); Shell.showHUD(); };
    $("btn-back").onclick = () => { v.currentTime = Math.max(0, v.currentTime - 10); Shell.showHUD(); };
    $("btn-fwd").onclick = () => { v.currentTime = Math.min(v.duration || 0, v.currentTime + 10); Shell.showHUD(); };
    $("btn-full").onclick = () => {
      document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
    };
    $("vol").oninput = (e) => { v.volume = e.target.value / 100; ctx.app._unmute(); };
    const seekbar = $("seekbar");
    const seekTo = (e) => {
      const rect = seekbar.getBoundingClientRect();
      const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
      if (v.duration) v.currentTime = frac * v.duration;
    };
    seekbar.addEventListener("click", seekTo);
    seekbar.addEventListener("touchstart", seekTo, { passive: true });
  },
  onCommand(c) {
    const v = this.ctx.video, p = c.payload || {};
    switch (c.cmd) {
      case "play": if (p.video_id) this.ctx.app.playVideo(p.video_id); break;
      case "pause": v.pause(); break;
      case "stop": v.pause(); v.currentTime = 0; break;
      case "resume": this.ctx.app._unmute(); v.play().catch(() => {}); break;
      case "seek": if (p.seconds != null) v.currentTime = Math.max(0, +p.seconds); break;
      case "volume": if (p.level != null) { v.volume = Math.min(1, Math.max(0, +p.level)); $("vol").value = v.volume * 100; this.ctx.app._unmute(); } break;
    }
  },
};

/* Subtitles module */
const SubtitlesModule = {
  id: "subtitles",
  mount(ctx) {
    this.ctx = ctx;
    $("btn-subs").onclick = () => ctx.app._cycleSubs();
  },
  onCommand(c) {
    const p = c.payload || {};
    if (c.cmd === "subtitles") {
      this.ctx.subtitles.mode = p.mode || "original";
      this.ctx.app.loadSubtitles(this.ctx.subtitles.mode);
    }
    if (c.cmd === "style") this.ctx.subtitles.setStyle(p);
  },
};

/* Home module: future ambient scenes (weather/news/avatar/calendar) */
const HomeModule = {
  id: "home",
  mount(ctx) { this.ctx = ctx; },
  onCommand(c) {
    const p = c.payload || {};
    if (c.cmd === "notify") Shell.toast(p.text || "");
    if (c.cmd === "state" && p.state) {
      Shell.setState(String(p.state).toUpperCase(), { text: p.text, status: p.status, hideAfter: p.hideAfter });
    }
    if (c.cmd === "companion") Shell.companion(p.text || "", p.ms);
    if (c.cmd === "summary" && p.text) Shell.setState("RESPONDING", { text: p.text, status: "Shrnutí videa", hideAfter: 12000 });
    if (c.cmd === "comments" && Array.isArray(p.items)) {
      const top = p.items.slice(0, 5).map((t) => `${t.author}: ${t.text}`).join("\n");
      Shell.setState("RESPONDING", { text: top, status: "Top komentáře", hideAfter: 12000 });
    }
    if (c.cmd === "ask" && p.question) {
      Shell.setState("LISTENING", { text: p.question, status: "Otázka" });
    }
    if (c.cmd === "ask" && p.answer) {
      Shell.setState("RESPONDING", { text: p.answer, status: "Odpověď", hideAfter: 12000 });
    }
  },
};

/* ── Player app ─────────────────────────────────────────────────── */
class PlayerApp {
  constructor() {
    this.video = $("video");
    this.hls = null;
    this.subtitles = new SubtitleRenderer();
    this.videoId = new URLSearchParams(location.search).get("v") || "";
    const urlParams = new URLSearchParams(location.search);
    this.mode = urlParams.get("mode") || "auto";
    // TV detection: explicit ?tv=1 (launcher sets it), UA markers,
    // or missing VP9 decoder (TV WebViews can't decode VP9 video).
    const vp9ok = this.video.canPlayType('video/mp4; codecs="vp09.00.10.08"');
    this._tv = urlParams.get("tv") === "1"
      || /tvwebbrowser|android tv|tv bro|tcl browser/i.test(navigator.userAgent)
      || !vp9ok;
    if (this.mode === "auto" && this._tv) this.mode = "mp4";
    const urlT = urlParams.get("transcode");
    this.transcode = urlT != null ? urlT === "1" : this._tv;
    this.job = null;
    this.title = "";
    this.badge = "";
    this._autoMuted = false;
    this._mp4Mode = false;
    this._hideTimer = null;

    const ctx = {
      video: this.video,
      subtitles: this.subtitles,
      app: this,
      sendCommand: (cmd, payload) => postJSON("/video/command", { cmd, payload }),
    };
    Shell.setCtx(ctx);
    Shell.registerModule(MediaModule);
    Shell.registerModule(SubtitlesModule);
    Shell.registerModule(HomeModule);

    // Capture any trusted input the remote/browser may deliver (capture
    // phase so nothing can swallow it first).
    ["keydown", "keyup", "click", "pointerdown", "touchend"].forEach((t) =>
      document.addEventListener(t, () => this._noteEvent(t), true));

    // Remote / keyboard control (DPAD keycodes used by Android TV remotes).
    document.addEventListener("keydown", (e) => this._onKey(e));

    // Native shell (toastovac-tv APK) injects keys the WebView would
    // otherwise swallow (e.g. BACK). Same pipeline as a real keydown.
    document.addEventListener("toastovac-key", (e) =>
      this._onKey({ keyCode: e.detail, key: "", preventDefault() {} }));

    // Native shell command plane (persistent WS will feed this later).
    document.addEventListener("toastovac-command", (e) => {
      try { this._handleCommand(JSON.parse(e.detail)); }
      catch (err) { console.warn("bad native command", e.detail); }
    });

    // autohide chrome (but not on BACK — _onKey handles that)
    ["mousemove", "touchstart", "keydown"].forEach((ev) =>
      document.addEventListener(ev, (e) => {
        if (ev === "keydown" && [4, 8, 27].includes(e.keyCode)) return;
        Shell.showHUD();
      }));

    setInterval(() => this._pollCommands(), 1500);
    setInterval(() => this._reportState(), 3000);

    if (this.videoId) this.playVideo(this.videoId);
    else Shell.setState("IDLE");
  }

  /* ── remote keys ── */
  _onKey(e) {
    const k = e.keyCode || (e.key && e.key.length === 1 ? 0 : null);
    Shell.pulse();
    switch (k) {
      case 19: case 38:      // UP — show HUD
        e.preventDefault();
        Shell.showHUD();
        break;
      case 20: case 40:      // DOWN — cycle subtitles
        e.preventDefault();
        this._cycleSubs();
        break;
      case 21: case 37:      // LEFT — seek −10
        e.preventDefault();
        this.video.currentTime = Math.max(0, this.video.currentTime - 10);
        Shell.showHUD();
        break;
      case 22: case 39:      // RIGHT — seek +10
        e.preventDefault();
        this.video.currentTime = Math.min(this.video.duration || 0, this.video.currentTime + 10);
        Shell.showHUD();
        break;
      case 23: case 66: case 13:  // OK / ENTER — play/pause (or authorize)
        e.preventDefault();
        if (this._waitingTap) { this._tryPlay(true); this._waitingTap = false; }
        else this._togglePlay();
        Shell.showHUD();
        break;
      case 4: case 8: case 27:     // BACK / ESC — hide HUD
        e.preventDefault();
        Shell.hideHUD();
        break;
      case 85:             // MEDIA_PLAY_PAUSE
        e.preventDefault();
        this._togglePlay();
        break;
    }
  }

  // Debounced play/pause. While the video is still auto-muted (muted
  // autoplay workaround), the FIRST user gesture must only unlock sound
  // and keep playing — never pause. TV Bro eats the very first OK anyway
  // (cursor mode), so this makes the first click that actually reaches
  // the page behave sanely.
  _togglePlay() {
    const now = Date.now();
    if (now - (this._lastToggle || 0) < 400) return;
    this._lastToggle = now;
    if (this.video.muted && this._autoMuted) {
      // first gesture = unlock sound + play
      this._unmute();
      Shell.toast("Zvuk zapnut");
      if (this.video.paused) this._tryPlay(true);
      else setTimeout(() => { if (this.video.paused) this._tryPlay(true); }, 250);
      return;
    }
    this.video.paused ? this.video.play() : this.video.pause();
  }

  // Track whether the page ever receives trusted input (TV remotes often
  // swallow keys before they reach the WebView).
  _noteEvent(name) {
    this._lastEvent = name;
  }

  /* ── diagnostic beacon ── */
  _reportState() {
    const v = this.video;
    try {
      fetch("/video/probe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          t: Math.round(v.currentTime * 10) / 10,
          rs: v.readyState,
          ns: v.networkState,
          err: v.error ? v.error.code + "/" + (v.error.message || "") : null,
          paused: v.paused,
          muted: v.muted,
          vol: Math.round(v.volume * 100),
          w: v.videoWidth,
          h: v.videoHeight,
          src: (v.currentSrc || "").split("/").pop(),
          ev: this._lastEvent || "none",
          state: document.body.dataset.state,
          idle: $("idle").classList.contains("hidden") ? 0 : 1,
        }),
      }).catch(() => {});
    } catch (e) { /* ignore */ }
  }

  /* ── UI: subtitles ── */
  _cycleSubs() {
    const order = ["original", "translated", "off"];
    const next = order[(order.indexOf(this.subtitles.mode) + 1) % order.length];
    this.subtitles.mode = next;
    const label = next[0].toUpperCase() + next.slice(1);
    $("btn-subs").textContent = "Subs: " + label;
    Shell.toast("Titulky: " + label);
    if (next === "off") { this.subtitles.update(0); return; }
    if (this.videoId) this.loadSubtitles(next);
  }

  async loadSubtitles(mode) {
    const lang = new URLSearchParams(location.search).get("lang") || "cs";
    Shell.toast(mode === "translated" ? "Překládám titulky…" : "Načítám titulky…");
    try {
      const r = await fetch(`/video/subtitles?id=${encodeURIComponent(this.videoId)}&mode=${mode}&lang=${lang}`);
      const text = await r.text();
      if (r.ok && text.startsWith("WEBVTT")) {
        this.subtitles.parseVTT(text);
        this.subtitles.mode = mode;
        Shell.toast("Titulky: " + mode);
        $("btn-subs").textContent = "Subs: " + mode[0].toUpperCase() + mode.slice(1);
      } else {
        const err = JSON.parse(text).error || "failed";
        Shell.toast("Titulky selhaly: " + err);
      }
    } catch (e) { Shell.toast("Titulky selhaly: " + e.message); }
  }

  /* ── playback flow ── */
  async playVideo(videoId) {
    this.videoId = videoId;
    history.replaceState(null, "", `/?v=${videoId}`);
    Shell.setState("LOADING");
    $("idle").classList.remove("hidden");
    $("idle-status").textContent = "Připravuji…";
    try {
      await postJSON("/video/play", { id: videoId, transcode: this.transcode ? 1 : 0 });
      await this._waitReady(videoId);
    } catch (e) {
      Shell.setState("ERROR");
      $("idle-status").textContent = "Chyba: " + e.message;
      Shell.toast("Chyba: " + e.message, 8000);
    }
  }

  async _waitReady(videoId) {
    for (let i = 0; i < 600; i++) {
      const r = await fetch("/video/status");
      const data = await r.json();
      const job = data.job;
      if (!job) { await this._sleep(500); continue; }
      if (job.state === "error") {
        Shell.setState("ERROR");
        $("idle-status").textContent = "Chyba: " + (job.message || "stahování selhalo");
        return;
      }
      if (job.state === "ready" && job.asset && job.asset.playlist) {
        this.title = job.asset.title;
        const h = job.asset.height ? job.asset.height + "p" : "";
        this.badge = `${h}${job.asset.dynamic_range === "HDR" ? " HDR" : ""}${job.asset.fps >= 50 ? " " + job.asset.fps : ""}${job.asset.vcodec ? " · " + job.asset.vcodec.split(".")[0] : ""}`.trim();
        $("title").textContent = this.title;
        $("badge").textContent = this.badge;
        if (this.mode === "mp4" || this.mode === "native") {
          this._fallbackToMp4(job.asset.mp4, "mode=" + this.mode);
        } else {
          await this._attachHls(job.asset.playlist, job.asset.thumbnail, job.asset.mp4);
        }
        this.loadSubtitles(this.subtitles.mode === "off" ? "original" : this.subtitles.mode);
        return;
      }
      const pct = Math.round((job.progress || 0) * 100);
      $("idle-status").textContent = `${job.message || job.state}${pct ? " " + pct + "%" : ""}`;
      await this._sleep(500);
    }
    $("idle-status").textContent = "Vypršel čas na stahování.";
  }

  async _attachHls(playlist, thumb, mp4Url) {
    if (this.hls) { this.hls.destroy(); this.hls = null; }
    $("idle").classList.add("hidden");
    Shell.setState("LOADING");
    const src = playlist;
    if (window.Hls && Hls.isSupported()) {
      this.hls = new Hls({ maxBufferLength: 60, enableWorker: false });
      this.hls.loadSource(src);
      this.hls.attachMedia(this.video);
      this.hls.on(Hls.Events.MANIFEST_PARSED, () => { this._tryPlay(); });
      this.hls.on(Hls.Events.ERROR, (e, d) => {
        if (!d.fatal) return;
        if (mp4Url) { this._fallbackToMp4(mp4Url, d.details || d.type); return; }
        Shell.setState("ERROR");
        Shell.toast("Chyba HLS: " + (d.details || d.type));
      });
      clearTimeout(this._stallTimer);
      this._stallTimer = setTimeout(() => {
        if (this.video.paused && this.video.currentTime === 0 && mp4Url) {
          this._fallbackToMp4(mp4Url, "stall");
        }
      }, 10000);
    } else if (this.video.canPlayType("application/vnd.apple.mpegurl")) {
      this.video.src = src;
      this.video.play().catch(() => {});
    } else if (mp4Url) {
      Shell.toast("HLS nepodporováno, používám MP4");
      this.video.src = mp4Url;
      this.video.play().catch(() => this._idleForTap());
    } else {
      Shell.toast("Tento prohlížeč neumí HLS");
    }
    Shell.showHUD();
  }

  _fallbackToMp4(mp4Url, reason) {
    if (this._mp4Mode) return;
    this._mp4Mode = true;
    clearTimeout(this._stallTimer);
    if (this.hls) { this.hls.destroy(); this.hls = null; }
    if (reason && !reason.startsWith("mode=")) {
      Shell.toast("TV nezvládá HLS/VP9-MSE (" + reason + ") — používám MP4");
    }
    $("idle").classList.add("hidden");
    this.video.src = mp4Url;
    this._tryPlay();
  }

  // Muted autoplay is exempt from the WebView user-gesture policy, but TV
  // Bro PAUSES the video the moment we unmute without a real user gesture.
  // So playback starts muted and sound is enabled only on explicit action
  // (remote key that reaches the page, volume slider, voice volume command).
  _tryPlay(sound) {
    if (!sound) {
      this.video.muted = true;
      this._autoMuted = true;
    }
    const p = this.video.play();
    if (p && typeof p.then === "function") {
      p.then(() => {
        $("idle").classList.add("hidden");
        $("voice-layer").classList.add("hidden");
        document.body.dataset.state = "PLAYING";
        if (this._autoMuted && !this._hintShown) {
          this._hintShown = true;
          Shell.toast("Stiskni OK pro zvuk", 5000);
        }
      }).catch((e) => this._idleForTap(e));
    }
    return p;
  }

  _unmute() {
    this.video.muted = false;
    this._autoMuted = false;
  }

  // Bound handler used by _idleForTap: OK press = real user gesture, so play
  // with sound. Stays registered until playback actually starts.
  _tapStart = () => {
    this._waitingTap = false;
    this._tryPlay(true);
  };

  _idleForTap(err) {
    this._waitingTap = true;
    // calm centered prompt — voice layer is transparent, video visible
    $("voice-caption").textContent = "Stiskni OK pro přehrát";
    $("voice-status").textContent = err && err.name ? "(" + err.name + ")" : "";
    $("voice-layer").classList.remove("hidden");
    document.body.dataset.state = "PAUSED";
    document.removeEventListener("keydown", this._tapStart);
    document.removeEventListener("keyup", this._tapStart);
    document.removeEventListener("click", this._tapStart);
    document.addEventListener("keydown", this._tapStart);
    document.addEventListener("keyup", this._tapStart);
    document.addEventListener("click", this._tapStart);
  }

  /* ── voice command bridge ── */
  async _pollCommands() {
    try {
      const r = await fetch("/player/commands");
      const data = await r.json();
      for (const c of data.commands || []) this._handleCommand(c);
    } catch (e) { /* TV offline for a moment */ }
  }

  _handleCommand(c) {
    Shell.modules.forEach((m) => {
      try { m.onCommand && m.onCommand(c); } catch (e) { console.error("cmd", m.id, e); }
    });
  }

  /* ── misc ── */
  _paintSeek() {
    const v = this.video;
    const dur = v.duration || 0;
    const buffered = v.buffered.length ? v.buffered.end(v.buffered.length - 1) : 0;
    $("seek-fill").style.width = dur ? (v.currentTime / dur) * 100 + "%" : "0%";
    $("seek-buffer").style.width = dur ? (buffered / dur) * 100 + "%" : "0%";
    const thumb = $("seek-thumb");
    thumb.style.left = dur ? (v.currentTime / dur) * 100 + "%" : "0%";
    $("time").textContent = `${fmtTime(v.currentTime)} / ${fmtTime(dur)}`;
  }

  _sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
}

window.PlayerApp = PlayerApp;
new PlayerApp();