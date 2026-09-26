"use strict";
// Length-prefixed UTF-8 JSON framing shared with the Toustovač daemon
// (toustovac-terminal-bridge/2). 4-byte big-endian length + JSON.
Object.defineProperty(exports, "__esModule", { value: true });
exports.MAX_MESSAGE_BYTES = exports.MAX_FRAME_BYTES = exports.MAX_PAYLOAD_BYTES = exports.FRAME_HEADER_BYTES = exports.PROTOCOL_ID = void 0;
exports.encodeFrame = encodeFrame;
exports.decodeFrame = decodeFrame;
exports.PROTOCOL_ID = "toustovac-terminal-bridge/2";
// M2.4 framing contract (identical semantics to protocol.py): the
// 4-byte big-endian prefix encodes the PAYLOAD length only; a full
// frame is FRAME_HEADER_BYTES + payload (max 65_540 bytes).
exports.FRAME_HEADER_BYTES = 4;
exports.MAX_PAYLOAD_BYTES = 65536;
exports.MAX_FRAME_BYTES = exports.FRAME_HEADER_BYTES + exports.MAX_PAYLOAD_BYTES; // 65_540
exports.MAX_MESSAGE_BYTES = exports.MAX_PAYLOAD_BYTES; // legacy alias = payload
function encodeFrame(obj) {
    const json = JSON.stringify(obj);
    const payload = new TextEncoder().encode(json);
    if (payload.byteLength > exports.MAX_PAYLOAD_BYTES) {
        throw new Error("message too large");
    }
    const frame = new Uint8Array(exports.FRAME_HEADER_BYTES + payload.byteLength);
    const view = new DataView(frame.buffer);
    view.setUint32(0, payload.byteLength, false); // big-endian payload length
    frame.set(payload, exports.FRAME_HEADER_BYTES);
    return frame;
}
function decodeFrame(buf) {
    if (buf.byteLength < exports.FRAME_HEADER_BYTES) {
        return null;
    }
    const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
    const n = view.getUint32(0, false);
    // M2.4: prefix = payload length only; exact-frame bounds, no extras.
    if (n <= 0 || n > exports.MAX_PAYLOAD_BYTES) {
        return null;
    }
    const total = exports.FRAME_HEADER_BYTES + n;
    if (buf.byteLength < total || buf.byteLength > total) {
        return null;
    }
    try {
        const text = new TextDecoder("utf-8", { fatal: true }).decode(buf.subarray(exports.FRAME_HEADER_BYTES, total));
        const obj = JSON.parse(text);
        return typeof obj === "object" && obj !== null
            ? obj
            : null;
    }
    catch {
        return null;
    }
}
