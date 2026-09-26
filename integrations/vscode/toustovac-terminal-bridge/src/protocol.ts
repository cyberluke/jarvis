// Length-prefixed UTF-8 JSON framing shared with the Toustovač daemon
// (toustovac-terminal-bridge/2). 4-byte big-endian length + JSON.

export const PROTOCOL_ID = "toustovac-terminal-bridge/2";
// M2.4 framing contract (identical semantics to protocol.py): the
// 4-byte big-endian prefix encodes the PAYLOAD length only; a full
// frame is FRAME_HEADER_BYTES + payload (max 65_540 bytes).
export const FRAME_HEADER_BYTES = 4;
export const MAX_PAYLOAD_BYTES = 65_536;
export const MAX_FRAME_BYTES = FRAME_HEADER_BYTES + MAX_PAYLOAD_BYTES; // 65_540
export const MAX_MESSAGE_BYTES = MAX_PAYLOAD_BYTES; // legacy alias = payload

export interface TerminalContextBeacon {
  protocol: typeof PROTOCOL_ID;
  kind: "terminal_context";
  session_id: string;
  pid?: number;
  parent_pid?: number;
  process_created_ns?: number;
  shell?: string;
  shell_version?: string;
  ps_edition?: string;
  cwd?: string;
  hostname?: string;
  wt_session?: string;
  term_program?: string;
  target_os?: string;
  transport?: string;
  remote_kind?: string;         // vscode.env.remoteName ("ssh-remote")
  remote_authority?: string;    // cwd URI / workspace URI authority
  remote_host_fingerprint?: string;
  shell_integration_ready?: boolean;
  timestamp_ns: number;
}

export interface ExecutionBeacon {
  protocol: typeof PROTOCOL_ID;
  kind: "execution_record";
  session_id: string;
  command?: string;
  exit_code?: number;
  cwd?: string;
  output_tail?: string;
  timestamp_ns: number;
}

export interface InsertRequest {
  protocol: typeof PROTOCOL_ID;
  kind: "insert_request";
  message_id: string;
  extension_instance_nonce: string;
  session_id: string;
  foreground_hwnd: number;
  turn_epoch: number;
  proposal_hash: string;
  expires_at_monotonic: number;
  command: string;
  auto_execute: false;
  timestamp_ns: number;
}

export function encodeFrame(obj: object): Uint8Array {
  const json = JSON.stringify(obj);
  const payload = new TextEncoder().encode(json);
  if (payload.byteLength > MAX_PAYLOAD_BYTES) {
    throw new Error("message too large");
  }
  const frame = new Uint8Array(FRAME_HEADER_BYTES + payload.byteLength);
  const view = new DataView(frame.buffer);
  view.setUint32(0, payload.byteLength, false); // big-endian payload length
  frame.set(payload, FRAME_HEADER_BYTES);
  return frame;
}

export function decodeFrame(
  buf: Uint8Array
): (TerminalContextBeacon | ExecutionBeacon | InsertRequest) | null {
  if (buf.byteLength < FRAME_HEADER_BYTES) {
    return null;
  }
  const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  const n = view.getUint32(0, false);
  // M2.4: prefix = payload length only; exact-frame bounds, no extras.
  if (n <= 0 || n > MAX_PAYLOAD_BYTES) {
    return null;
  }
  const total = FRAME_HEADER_BYTES + n;
  if (buf.byteLength < total || buf.byteLength > total) {
    return null;
  }
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(
      buf.subarray(FRAME_HEADER_BYTES, total)
    );
    const obj = JSON.parse(text);
    return typeof obj === "object" && obj !== null
      ? (obj as TerminalContextBeacon | ExecutionBeacon | InsertRequest)
      : null;
  } catch {
    return null;
  }
}
