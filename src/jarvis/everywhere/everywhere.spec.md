# Everywhere Spec (OS-wide AI text interaction plane)

Canonical spec for `src/jarvis/everywhere/`. The native companion process is
`Toastovac.Everywhere.Host.exe` (see `native/Toastovac.Everywhere.Host/`).

## 1. One runtime, many surfaces

The existing Jarvis runtime (config, LLM router, terminal composer, voice
listener, interaction memory) stays canonical. Everywhere adds one capability
plane on top of it: a selection-driven action broker reached over a current-user
named pipe by the native Windows host. No Electron, no second LLM router, no
second terminal broker, no cloud dependency.

## 2. SelectionSnapshot (immutable)

Every interaction starts from a frozen `SelectionSnapshot`:

```text
snapshot_id, revision, captured_at
source_kind, provider_id
text, text_hash
hwnd, process_id, process_name, window_title
editable, replace_capability
selection_bounds[], cursor_position
semantic_context, provider_token
```

`source_kind` values: `vscode-editor`, `vscode-terminal`, `powershell`,
`windows-terminal`, `uia-text`, `ocr-region`.

OCR backends (`everywhere_ocr_backend`, explicit, no silent fallback):
`windows-ai` (Windows.AI.Text), `windows-media` (Windows.Media.Ocr),
`oneocr` (vendored OneOCR ONNX runtime under `src/jarvis/_vendor/oneocr`,
fully self-contained; provider selected via `everywhere_ocr_provider`:
`cpu` | `directml` | `cuda` | `openvino`).

Snapshots are never mutated. A changed selection produces a new snapshot with a
new revision; the previous in-flight transaction becomes `STALE`.

`text_hash` is the lowercase hex SHA-256 of the UTF-8 text. Logs carry hashes,
lengths and metadata only, never raw text by default.

## 3. Provider hierarchy (deterministic, not a fallback chain)

```text
vscode-editor / vscode-terminal  -> existing VSIX semantic provider tokens
powershell / windows-terminal    -> existing terminal composer / bridge tokens
uia-text                         -> Windows UI Automation (generic apps)
ocr-region                       -> explicit Alt+drag OCR capture
```

The provider chosen for a snapshot is fixed for that snapshot. If it fails the
transaction fails closed with an explicit typed reason; no silent switch to
another provider or OCR engine.

## 4. Context transactions

Each LLM operation is bound to
`(snapshot_id, revision, provider_id, text_hash, target identity, action_id,
request_id)`. Before any insertion the target is revalidated:

- generic UIA: same process, same HWND/control identity, same selected text and
  hash, still editable;
- VS Code: same document URI, same document version (or unchanged range), same
  original range text;
- terminal: same session, shell, local/remote identity, prompt generation /
  line revision.

Validation failure keeps the generated result, shows `Target changed`, offers
Copy, and does not insert. `STALE` and `CANCELLED` are final with respect to
automatic insertion.

State machine:

```text
IDLE -> SNAPSHOT_READY -> ACTION_PENDING -> STREAMING -> RESULT_READY
     -> APPLYING -> DONE
any active state -> CANCELLED
context invalidation -> STALE
```

## 5. Actions

| Action       | Result mode | Notes                                          |
|--------------|-------------|------------------------------------------------|
| rewrite      | replace     | one atomic replacement after revalidation      |
| proofread    | structured  | per-issue accept/reject, Fix All by confidence |
| alternatives | alternatives| 3..15 variants, one structured response        |
| explain      | panel       | streamed, follow-up bound to the snapshot      |
| translate    | panel       | tracked target language, explicit Insert       |
| prompt       | per prompt  | saved prompt library, user-selected model      |

Terminal surfaces additionally expose `fix_command`, `explain_command`,
`safer_variant`, `docker_help`, `devops_help` via the existing terminal
composer; insertion stays `sendText(command, false)` / the existing bridge,
never an automatic Enter.

## 6. Model profiles

Action-oriented profile names map onto the existing two-tier router
(`jarvis.llm.resolve_model`): `fast-edit` and `structured-edit` ride the FAST
tier, `reasoning` / `translation` / `creative-edit` ride the CHAT tier,
`user-selected` uses the saved prompt's own model. No new client, no silent
fallback between providers.

## 7. Failure codes

`NO_SELECTION`, `EMPTY_SELECTION`, `PASSWORD_FIELD`, `READ_ONLY_TARGET`,
`TARGET_GONE`, `TARGET_CHANGED`, `SELECTION_CHANGED`, `DOCUMENT_CHANGED`,
`REMOTE_CONTEXT_CHANGED`, `PROVIDER_UNAVAILABLE`, `HOTKEY_CONFLICT`,
`OCR_BACKEND_UNAVAILABLE`, `OCR_NO_TEXT`, `INPUT_BLOCKED_BY_UIPI`,
`REPLACE_UNSUPPORTED`, `MODEL_UNAVAILABLE`, `MODEL_CANCELLED`,
`PIPE_DISCONNECTED`. Every failure carries exactly one code.

## 8. Replace capabilities

`SEMANTIC_RANGE_EDIT` (VSIX), `VALUE_PATTERN_EDIT`, `UNICODE_INPUT_REPLACE`,
`CLIPBOARD_TRANSACTION_REPLACE`, `COPY_ONLY`. The host picks the mechanism the
target actually supports; OCR regions are `COPY_ONLY`. Password controls are
never harvested. Clipboard transaction mode restores the previous clipboard
only when nobody else wrote it meanwhile.

## 9. Pipe protocol

`\\.\pipe\toastovac-everywhere-v1`, message-mode, DACL restricted to the
current interactive logon SID (same construction as the terminal bridge).
Frames are 4-byte big-endian payload length + UTF-8 JSON, protocol id
`toustovac-everywhere/1`, request-id correlated duplex messages. No TCP.

Message kinds: `snapshot`, `action`, `action_result`, `apply`, `apply_result`,
`cancel`, `ping`, `pong`.

## 10. Observability

Structured debug events (`debug_log`, tag `everywhere`):
`everywhere.snapshot.created`, `everywhere.toolbar.shown`,
`everywhere.action.started`, `everywhere.action.first_token`,
`everywhere.action.cancelled`, `everywhere.action.completed`,
`everywhere.target.stale`, `everywhere.apply.started`,
`everywhere.apply.completed`, `everywhere.apply.failed`,
`everywhere.ocr.started`, `everywhere.ocr.completed`,
`everywhere.ocr.failed`. Dimensions are metadata only (provider, source_kind,
process_name, action, model_profile, ocr_backend, replace_capability,
durations, lengths, failure_reason).

## 11. Voice

Voice commands resolve the current snapshot through the same broker and run the
same action ids as the toolbar. No valid snapshot means an explicit short Czech
line; old selections are never guessed.

## 12. Persistence

Configuration lives in the normal `config.json`. The saved prompt library,
recent translation languages and the chosen OCR backend persist there too, so a
restart keeps prompts, shortcuts, recents and explicit backend choices.
