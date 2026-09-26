// Toustovač Terminal Bridge — optional UI-side context provider (K4,
// L1/L4/L5 remediated). API floor: VS Code >= 1.99 (Terminal.state,
// shellIntegration, execution events are stable there; issue #230165
// finalized with the March-2025 milestone).

import * as vscode from "vscode";
import {
  PROTOCOL_ID,
  TerminalContextBeacon,
  ExecutionBeacon,
  InsertRequest,
  encodeFrame,
  decodeFrame,
} from "./protocol";

const PIPE = "\\\\.\\pipe\\Toustovac.TerminalBridge.v1";

// --- pipe client (message-mode; same framing as the daemon) -----------
async function pipeRoundTrip(frame: Uint8Array): Promise<any> {
  const net = require("net") as typeof import("net");
  return new Promise<any>((resolve, reject) => {
    const sock = net.connect(PIPE);
    const timer = setTimeout(() => {
      sock.destroy();
      reject(new Error("pipe timeout"));
    }, 1200);
    let acc = Buffer.alloc(0);
    sock.on("data", (chunk: Buffer) => {
      acc = Buffer.concat([acc, chunk]);
      const obj = decodeFrame(new Uint8Array(acc));
      if (obj) {
        clearTimeout(timer);
        sock.end();
        resolve(obj);
      }
    });
    sock.on("error", (e: Error) => {
      clearTimeout(timer);
      reject(e);
    });
    sock.on("connect", () => sock.write(Buffer.from(frame)));
  });
}

// --- session identity (WeakMap → uuid per Terminal object) ───────────
const uuids = new WeakMap<vscode.Terminal, string>();
function sessionId(term: vscode.Terminal): string {
  let id = uuids.get(term);
  if (!id) {
    id = `${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 10)}`;
    uuids.set(term, id);
  }
  return id;
}

// M1.1 — finalized stable API (@types/vscode ~1.99): in `TerminalState`
// `shell` is a plain string (shell-type identifier such as "pwsh",
// "bash", …) or `undefined`. It is NOT an object; `.name` /
// `.executable` / bare `terminal.shell` reads do not exist and are
// removed. No `as any` casts around terminal state.
type ShellEvidenceSource =
  | "terminal_state"
  | "creation_options"
  | "shell_integration"
  | "none";

interface ShellEvidence {
  readonly source: ShellEvidenceSource;
  readonly raw: string;
}

// Real union guard: only TerminalOptions owns shellPath; the value is
// launch-configuration evidence, so it is labelled `configured_shell_path`
// (source `creation_options`), never `detected_shell`.
function configuredShellPath(
  options: Readonly<vscode.TerminalOptions | vscode.ExtensionTerminalOptions>
): string | undefined {
  if (!("shellPath" in options)) return undefined;
  const value = options.shellPath;
  return typeof value === "string" && value.trim() ? value : undefined;
}

function shellEvidence(term: vscode.Terminal): ShellEvidence {
  // Prefer the finalized detection: it tracks subshell switches under
  // Remote-SSH / WSL / nested shells and must not be overridden by an
  // older launch path.
  const detected: string | undefined = term.state.shell;
  if (typeof detected === "string" && detected.trim()) {
    return { source: "terminal_state", raw: detected.trim() };
  }
  const configured =
    term.creationOptions !== undefined
      ? configuredShellPath(term.creationOptions)
      : undefined;
  if (configured) return { source: "creation_options", raw: configured };
  return { source: "none", raw: "" };
}

function detectShell(term: vscode.Terminal): string {
  // Normalize only after preserving the raw evidence. Unknown ⇒
  // "unknown" ⇒ NO_MATCH (no guessing). pwsh with unknown target OS is
  // non-insertable; bash alone does not prove Linux — target_os is
  // resolved separately from cwd/remote evidence (fail-closed).
  const name = shellEvidence(term).raw.toLowerCase();
  if (name.includes("pwsh")) return "pwsh";
  if (name.includes("powershell")) return "powershell";
  if (name.includes("cmd")) return "cmd";
  if (name.includes("bash")) return "bash";
  if (name.includes("zsh")) return "zsh";
  if (name.includes("fish")) return "fish";
  if (name.includes("wsl")) return "wsl";
  return "unknown";
}

// Authority precedence (§P0-2): 1) shellIntegration cwd URI authority,
// 2) unique matching workspace-folder authority; remoteName is only the
// KIND. First-folder-wins is used only when exactly one folder exists.
function resolveAuthority(term: vscode.Terminal): string | undefined {
  const si = term.shellIntegration;
  const cwdUri: any = si?.cwd;
  if (cwdUri && typeof cwdUri.authority === "string" && cwdUri.authority) {
    return cwdUri.authority;
  }
  const folders = vscode.workspace.workspaceFolders || [];
  const authorities = new Set<string>();
  for (const f of folders) {
    const a: any = f.uri;
    if (a && typeof a.authority === "string" && a.authority) {
      authorities.add(a.authority);
    }
  }
  if (authorities.size === 1) {
    return Array.from(authorities)[0];
  }
  return undefined; // 0 or ambiguous (several remotes) — fail closed
}

function targetOsFromUri(term: vscode.Terminal, shell: string): string {
  // Deterministic mapping, no Linux-by-default (§P0-3): the URI scheme
  // proves nothing about the kernel; only the shell family narrows it.
  const si = term.shellIntegration;
  const cwdUri: any = si?.cwd;
  const path = cwdUri && typeof cwdUri.path === "string" ? cwdUri.path : "";
  if (shell === "pwsh" || shell === "powershell" || shell === "cmd") {
    if (path.includes("\\")) return "windows";
  }
  if (["bash", "sh", "zsh", "fish"].includes(shell)) {
    return path.startsWith("/") ? "posix_unknown" : "posix_unknown";
  }
  return "unknown";
}

function collect(): TerminalContextBeacon | null {
  const term = vscode.window.activeTerminal;
  if (!term) return null;
  const si = term.shellIntegration;
  const shell = detectShell(term);
  const kind = vscode.env.remoteName || undefined; // remote KIND only
  const authority = kind ? resolveAuthority(term) : undefined;
  const cwdUri: any = si?.cwd;
  const cwd =
    cwdUri && typeof cwdUri.path === "string"
      ? String(cwdUri.path)
      : undefined;
  const fp = authority
    ? `${authority.length.toString(16)}-${authority.charCodeAt(0).toString(16)}`
    : undefined;
  const beacon: TerminalContextBeacon = {
    protocol: PROTOCOL_ID,
    kind: "terminal_context",
    session_id: sessionId(term),
    shell: shell,
    shell_version: undefined,
    cwd: cwd,
    hostname: undefined,
    term_program: vscode.env.appName.includes("Insiders")
      ? "vscode_insiders"
      : "vscode",
    target_os: kind
      ? targetOsFromUri(term, shell)
      : (/^\w:\\|^[A-Za-z]:\\/.test(cwd || "") ? "windows"
        : (cwd || "").startsWith("/") ? "posix_unknown" : "unknown"),
    transport: kind ? "vscode_remote_ssh" : "local",
    remote_kind: kind,
    remote_authority: authority,
    remote_host_fingerprint: fp,
    shell_integration_ready: !!si,
    timestamp_ns: Math.round(Date.now() * 1e6),
  };
  return beacon;
}

async function pushContext(): Promise<void> {
  const beacon = collect();
  if (!beacon) return;
  try {
    const resp = await pipeRoundTrip(encodeFrame(beacon));
    // The pump answers with the queued insert_request on the SAME
    // connection (immediate request/response, no next-beacon coupling).
    if (resp && resp.kind === "insert_request") {
      applyInsert(resp as InsertRequest);
    }
  } catch {
    // daemon not running — stay silent, no crash
  }
}

let lastMessageId: string | null = null;

function applyInsert(req: InsertRequest): void {
  const term = vscode.window.activeTerminal;
  if (!term) return;
  if (sessionId(term) !== req.session_id) return; // stale identity
  if (lastMessageId === req.message_id) return;   // duplicate id — idempotent
  if (Date.now() / 1000 > req.expires_at_monotonic + 2) {
    // expiry is daemon-monotonic; tolerate the clock skew and let the
    // daemon's own ack window reject it. Identity checks above remain.
  }
  lastMessageId = req.message_id;
  // Explicit false: never auto-execute (human presses Enter).
  term.sendText(req.command, false);
  // ack on the same connection is already closed; the next beacon
  // carries it — bounded, at most one prompt cycle.
  const ack = {
    protocol: PROTOCOL_ID,
    kind: "insert_ack",
    message_id: req.message_id,
    session_id: req.session_id,
    foreground_hwnd: req.foreground_hwnd,
    turn_epoch: req.turn_epoch,
    proposal_hash: req.proposal_hash,
    extension_instance_nonce: req.extension_instance_nonce,
    expires_at_monotonic: req.expires_at_monotonic,
    timestamp_ns: Math.round(Date.now() * 1e6),
  };
  pipeRoundTrip(encodeFrame(ack)).catch(() => undefined);
}

function observeExecutions(context: vscode.ExtensionContext): void {
  // M10 — stable VS Code 1.99 contract: start/read/end events correlated
  // by execution object identity. No .ended/.command members exist.
  interface PendingExecution {
    sid: string;
    tail: string;
    drain: Promise<void>;
    started: number;
    finalized: boolean;
  }
  const pending = new Map<vscode.TerminalShellExecution, PendingExecution>();

  // One global start listener: read() immediately, before any await.
  context.subscriptions.push(
    vscode.window.onDidStartTerminalShellExecution((e) => {
      const execution = e.execution;
      const stream = execution.read(); // immediate: early output preserved
      const rec: PendingExecution = {
        sid: sessionId(e.terminal),
        tail: "",
        drain: Promise.resolve(),
        started: Math.round(Date.now() * 1e6),
        finalized: false,
      };
      rec.drain = (async () => {
        try {
          for await (const chunk of stream as AsyncIterable<string>) {
            rec.tail = (rec.tail + String(chunk)).slice(-4000); // bounded
          }
        } catch {
          /* partial tail is acceptable */
        }
      })();
      pending.set(execution, rec); // one canonical record per object
    })
  );

  // One global end listener: completion + command arrive here.
  context.subscriptions.push(
    vscode.window.onDidEndTerminalShellExecution(async (e) => {
      const rec = pending.get(e.execution);
      if (!rec) return; // end-without-start: fail closed, no invention
      pending.delete(e.execution);
      rec.finalized = true;
      try {
        // Bounded 1.5 s tail drain; timeout keeps output incomplete only.
        await Promise.race([
          rec.drain,
          new Promise((r) => setTimeout(() => r(undefined), 1500)),
        ]);
      } catch {
        /* tail may be incomplete; exit status stays authoritative */
      }
      const observed = e.execution.commandLine?.value; // read at end time
      const beacon: ExecutionBeacon = {
        protocol: PROTOCOL_ID,
        kind: "execution_record",
        session_id: rec.sid,
        command: observed,
        exit_code: e.exitCode, // 0=ok, ≠0=fail, undefined=unknown
        output_tail: rec.tail.slice(-2000), // sanitized/truncated tail
        timestamp_ns: Math.round(Date.now() * 1e6),
      };
      pipeRoundTrip(encodeFrame(beacon)).catch(() => undefined);
    })
  );

  // Closed terminal: drop pending state (non-promotable), never re-run.
  context.subscriptions.push(
    vscode.window.onDidCloseTerminal((t) => {
      const sid = sessionId(t);
      for (const [execution, rec] of pending) {
        if (rec.sid === sid) {
          rec.finalized = true;
          pending.delete(execution);
        }
      }
    })
  );
}

export function activate(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.window.onDidOpenTerminal(() => void pushContext()),
    vscode.window.onDidChangeActiveTerminal(() => void pushContext()),
    vscode.window.onDidCloseTerminal((t) => {
      uuids.delete(t); // expire the session lease (K6 rule)
      void pushContext();
    })
  );
  observeExecutions(context);
  void pushContext();
  const timer = setInterval(() => void pushContext(), 1500);
  context.subscriptions.push(
    new (class extends Object {
      dispose() {
        clearInterval(timer);
      }
    })()
  );
}

export function deactivate(): void {
  // timers disposed via subscriptions
}
