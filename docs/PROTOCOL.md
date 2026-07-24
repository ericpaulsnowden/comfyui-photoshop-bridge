# cpsb Protocol & Interface Contract

This document is the single source of truth for every interface between the three
components of comfyui-photoshop-bridge: the Python backend (registered on ComfyUI's
PromptServer), the ComfyUI frontend extension (`web/js/`), and the Photoshop UXP
plugin (`photoshop_plugin/`). If an implementation and this document disagree, the
implementation is wrong or this document must be amended first — never drift silently.

Referenced design rationale lives in `/PLAN.md` (repo parent) — section numbers cited as (§N).

---

## 1. Identifiers & filesystem layout

- `handoff_id`: 8-char lowercase hex, generated with `uuid.uuid4().hex[:8]`, unique per
  handoff. Treated as an unguessable capability token (§3 security): routes that read or
  mutate a specific handoff require it and return 404 for unknown/inactive ids.
- Managed folder, one per handoff, under ComfyUI's input directory. The parent folder
  name is the `managed_folder_name` setting (default `"photoshop"`; §2 settings). It is
  written to `meta.json` per handoff (`managed_dir`) and included in the subfolder of
  every emitted image reference, so the **frontend never hardcodes it** — it derives the
  subfolder entirely from server events and `/cpsb/*` responses. `<managed>` below stands
  for that configured name:

```
input/<managed>/<handoff_id>/
    <derived>.psd     # the managed PSD handed to Photoshop (Tier 1 opens this path directly)
    meta.json         # authoritative handoff state (schema below)
    orig_thumb.png    # thumbnail of the ORIGINAL image, max 256px long side (gallery before/after)
    edit_001.png ...  # ingested edits, in arrival order (edit_%03d.png)
```

**Managed-copy filename is DERIVED, not literal (v0.5.26; previously always `source.psd`).**
Recorded per handoff as `HandoffMeta.psd_filename` at creation: the ORIGIN filename's stem
(sanitized: `[A-Za-z0-9 _.-]` kept, everything else `-`, dash runs collapsed, trimmed,
60-char cap; extension split MANUALLY — `Path.stem`'s multi-dot behavior differs between
Python 3.10 and 3.14 and this name is persisted + matched by the watcher, so it must be
identical on every interpreter; leading dots stripped so a dotfile origin can never yield a
hidden managed file) + `.psd`. Eric-Headshot.jpg → `Eric-Headshot.psd` — so Photoshop's
document TITLE and every filename dropdown can tell handoffs apart (user request
2026-07-19). Degenerate/empty stems fall back to `source.psd`, and a meta.json missing the
field (every pre-v0.5.26 handoff) reads back as `source.psd` — old handoffs keep working
untouched. Collisions are impossible by construction (one directory per handoff). ALL code
resolves the path through `HandoffManager.psd_path(meta)` — never join `"source.psd"` by
hand. The remote (Tier 2) plugin now saves its sandbox copy under a per-handoff SUBFOLDER
(`handoffs/<handoffId>/<basename of psd_path>`, falling back to `<handoffId>.psd`), so two
handoffs deriving the same name never collide in the shared sandbox; no wire change — the
`open_handoff` message's existing `psd_path` field already carries the name.

**Lock-ordering invariant in the watcher (deadlock found+fixed v0.5.26):** never call
`observer.schedule`/`unschedule` while holding the watcher's own lock. macOS fsevents'
`schedule` blocks coordinating with the observer's dispatch thread, and that thread runs
`notice()` which takes the watcher lock — holding it across `schedule` deadlocks whenever a
real save event dispatches concurrently (latent since edit_in_place shipped; the per-handoff
filename lookup widened the window enough to hit reliably). Bookkeeping happens under the
lock (a `None` reservation keeps concurrent callers idempotent); observer calls happen
outside it.

**Own-write suppression is PATH-keyed, not handoff-id-keyed (v0.5.37).** The watcher
cannot tell who wrote a file from a raw filesystem event alone, so every write this
package makes to a path that could be under watch calls
`HandoffManager.record_own_write(path)` right after the write completes (after an
atomic write's `os.replace`, never before); `CpsbWatcher._settle` checks
`HandoffManager.is_own_write(path, size, mtime_ns)` before ever reading a settled
file. The registry is keyed by the file's own resolved path, bounded to 128 entries
(oldest-inserted evicted; re-recording refreshes a path's position), **not** by
handoff id — a handoff-id-keyed predecessor could only suppress a write against that
same handoff's own managed copy, which missed the case where one node writes into a
path a *different* handoff is watching (§6c: Compose's `existing_psd_path` landing on
a Load PSD `edit_in_place` original — an infinite re-run loop under "Re-run on every
save"). `note_source_written(handoff_id)` still exists as a convenience wrapper over
`record_own_write` for the common "just created this handoff, wrote its managed copy"
case; it is not a separate mechanism.

- There is **no separate session manifest file**: on server start, the backend rebuilds
  its in-memory state by scanning `input/<managed>/*/meta.json` (the meta files are the
  source of truth; this supersedes PLAN §3's `session_manifest.json` — one file fewer,
  same restart guarantee). Changing `managed_folder_name` takes effect at next server
  start and applies to new handoffs; handoffs already living under the previous name stay
  where they are (their `meta.managed_dir` records which folder they belong to).
- Handoffs with status `edited`, `cancelled`, `discarded`, `superseded`, or `error` older
  than `cleanup_days` (default 14) are purged (folder deleted) at server start.
  `pending`/`editing` handoffs are never auto-purged.

### meta.json schema

```json
{
  "handoff_id": "a1b2c3d4",
  "origin_node_id": "17",
  "origin_kind": "load_image" | "terminal_output" | "bridge_node" | "load_psd" | "manual_send",
  "workflow_name": "my-workflow",
  "source_hash": "<sha256 hex of the original image's normalized PNG encoding>",
  "source": {"filename": "ComfyUI_00042_.png", "subfolder": "", "type": "output"},
  "psd_filename": "ComfyUI_00042_.psd",
  "managed_dir": "cpsb",              // managed_folder_name at creation; null on legacy metas
  "edit_in_place": false,             // §6b Edit-original (load_psd only)
  "original_path": null,              // absolute path of the user's file when edit_in_place
  "trigger_policy": "Re-run workflow" | "Update only (don't re-run)" | "Ignore (do nothing)",
  "wants_layered_psd": false,         // §6d remote Tier-2 layered annotate
  "plugin_doc_open": null,            // true/false per the plugin's opened/document_closed; null = unknown
  "created_ts": 1752680000.0,
  "updated_ts": 1752680123.4,
  "status": "pending" | "editing" | "edited" | "cancelled" | "discarded" | "superseded" | "error",
  "error": null,
  "edits": [
    {"filename": "edit_001.png", "ts": 1752680123.4,
     "fidelity": "composite" | "recomposite" | "plugin",
     "sibling_output": {"filename": "ComfyUI_00042_ps1.png", "subfolder": ""}
    }
  ]
}
```

Status transitions:
`pending` (created) → `editing` (open confirmed: Tier 1 OS-launch succeeded, or plugin sent
`opened`) → `edited` (≥1 edit ingested; stays `edited` as further saves append to `edits`).
`cancelled` (user cancel), `discarded` (gallery discard), `superseded` (replaced by a
"Start Fresh Edit"), `error` (open/ingest failure, with `error` string) are terminal.
"Closed" is **not** a status — the frontend derives it (`editing` AND
`plugin_doc_open === false`, gallery overhaul 2026-07-22, replacing an earlier "editing
and `updated_ts` older than 1h" client-only guess). `plugin_doc_open` (bool | None) is a
side-channel fact, not a status: `true` on the plugin's `opened` reply, `false` on its
`document_closed` message (§3), `None`/absent when no Tier-2 plugin has ever reported
either way (Tier-1-only, or none connected this session) — `None` still means "unknown,"
never "closed." Scope: this only detects the document WINDOW closing; a Save-As to a
different path leaves the document open (so `plugin_doc_open` stays `true`) while
silently orphaning the watch — see README's Limitations.

`fidelity` records how the edit's pixels were produced: `composite` = embedded
Maximize-Compatibility composite (Tier 1 best case), `recomposite` = psd-tools re-render
fallback (Tier 1, user declined Maximize Compatibility — imperfect fidelity),
`plugin` = final pixels delivered as-is via `/cpsb/upload` — both the UXP plugin's own
flattened PNG export (Tier 2) and a manual gallery drag-drop import map here, since
neither is derived by this backend.

Additional field semantics:
- `source_hash`: sha256 of the original image's normalized PNG bytes, written at
  creation. The bridge node compares it against its current input to decide whether an
  existing handoff still corresponds to the same image (§6); missing on legacy metas →
  treated as matching.
- Cleanup age is measured on `updated_ts` (last activity), not `created_ts`.
- Active-handoff lookup (`mode:"new"` 409 check, "Edit Original" targeting) is scoped by
  `workflow_name` when both the request's and the candidate's names are non-empty; an
  empty name on either side is a wildcard (unsaved workflows, bridge handoffs).

---

## 2. HTTP routes

All routes are registered on `PromptServer.instance.routes` (same port as ComfyUI, no
extra server). JSON errors use `{"error": "<human-readable message>"}` with the status
codes below.

### POST `/cpsb/open`
Create a handoff and open it in Photoshop. Body (JSON):

```json
{
  "filename": "ComfyUI_00042_.png",
  "subfolder": "",
  "type": "output",              // "input" | "output" | "temp" — same triple as /view
  "origin_node_id": "17",         // graph node id as string
  "origin_kind": "load_image",    // "load_image" | "terminal_output" | "bridge_node" | "load_psd" | "manual_send"
                                  // (manual_send bodies are only ever the gallery RE-opening a pushed
                                  //  card, mode:"original" — a push itself is created by §3's
                                  //  manual_push websocket message, never by this route)
  "workflow_name": "my-workflow", // optional, for the gallery
  "mode": "new"                   // "new" | "original" | "fresh"
}
```

- `mode:"new"`: no active handoff expected for this node. If one exists (status
  `pending`/`editing`/`edited`), respond **409** with
  `{"error": ..., "existing_handoff_id": "..."}` — the frontend then shows
  "Edit Original / Start Fresh" and re-calls with the chosen mode.
- `client_remote_ok` (optional bool, default false): acknowledges the client-locality
  gate below. When the Tier 1 path would be used AND the requesting client is not on
  the server's machine AND this flag is absent, respond **428** with
  `{"error": ..., "reason": "client_remote", "server_name": "<platform.node()>"}` —
  the frontend shows a confirm ("Photoshop will open on <server_name>, not this
  computer") and re-sends with `client_remote_ok: true` if the user proceeds (choice
  remembered per-browser in localStorage; only the affirmative is persisted). A connected Tier 2 plugin in REMOTE mode bypasses this gate (the plugin is on a
  different machine than the server — almost certainly the one the user is at). A
  plugin in LOCAL mode does NOT bypass it: local mode means the plugin sits on the
  server's own machine, so for a remote browser the document would still open on the
  wrong screen — the same confirm applies.
- `mode:"original"`: re-open the existing handoff's `source.psd` (layers preserved).
  Requires `existing` handoff for the node; 404 otherwise.
- `mode:"fresh"`: mark the existing handoff `superseded`, then proceed as `new`.

Success **200**: `{"handoff_id": "...", "tier": 1 | 2, "status": "pending"}`.
Errors: **404** source image not found; **400** malformed body; **503** neither tier
available (Tier 1 gated off — headless/container/remote — and no plugin connected),
body includes `{"tier1_available": false, "tier2_connected": false}`.
The response is **200 with `status:"pending"` even if the launch attempt itself then
fails** — the contract's error codes cover pre-launch validation only; launch outcome
is conveyed asynchronously via `cpsb.status` (`editing` on success, `error` on failure).

**PSD-native sources (`origin_kind: "load_psd"`)**: the `{filename, subfolder, type}`
triple points at a `.psd`/`.psb` the user loaded (Load PSD node). Handoff creation
COPIES that file verbatim as `source.psd` — never `write_psd`/`frompil` — so the user
edits a file with their layers intact; the ORIGINAL stays untouched (non-destructive).
`source_hash` = sha256 of the raw PSD bytes (not a PNG encoding). The thumbnail comes
from the §4 flatten of the copy.

Tier selection: if a UXP plugin websocket is currently connected → Tier 2 (send
`open_handoff` over WS); else if Tier 1 available → OS-launch Photoshop with the PSD
path. Tier 1's watchdog stays armed in both tiers (§5 — redundant detector; first
ingest wins, duplicates are idempotent by file hash).

### POST `/cpsb/upload`
Deliver edited pixels (Tier 2 plugin, or manual gallery drag-drop import).
`multipart/form-data`: `handoff_id` (field), `image` (PNG file part),
`source` (field, `"plugin"` | `"manual"`).

Accepted only when handoff status is `pending`, `editing`, or `edited` — otherwise
**409**. Unknown id **404**. On success the backend ingests (see §4 of this doc),
responds **200** `{"ok": true, "filename": "edit_002.png", "subfolder": "cpsb/<id>",
"type": "input"}`. A duplicate upload (SHA256-identical to the latest edit — the
watchdog and plugin may both report one save) is idempotent: **200** with the existing
latest edit's filename, no new edit recorded.

### GET `/cpsb/file/{handoff_id}`
Returns `source.psd` bytes (`Content-Type: image/vnd.adobe.photoshop`). Used by the
plugin in remote mode. 404 for unknown id or non-active status.

### GET `/cpsb/fs/list`
Server-side directory listing backing the Compose node's `existing_psd_path` **Browse
dialog** (v0.5.27 as `/cpsb/browse`; renamed + reshaped to the shared cross-pack contract
in v0.5.33 — see `../../STANDARD-fs-browse.md`, which cpsb, comfyui-premiere-bridge, and
comfyui-epsnodes now all implement). A true OS file dialog is impossible for a server-side
node, so this is the idiomatic substitute.
Query param `dir`: omitted/empty → the pack default dir (ComfyUI input); the literal
`dir=ROOTS` → the roots listing (`dir`/`parent` null): "ComfyUI Input" + "Home" + /Volumes
(macOS) / drive letters (Windows). Otherwise `dir` must be an absolute existing directory
(400 on relative/missing/not-a-dir). Optional `ext` narrows the extension filter (default
`.psd`/`.psb`). Response: `{dir, parent (null at a filesystem root), sep, dirs:[{name}],
files:[{name,size,mtime}], truncated}` — **names only** (the client joins each with
`dir`+`sep`; ROOTS entries additionally carry an absolute `path`, since there's nothing to
join the sentinel against). Case-insensitively sorted, dirs then files; dotfiles and
stat-failing entries skipped; 500-entry cap → `truncated:true`. **Locality is an explicit
build-time flag** `FS_LIST_LOCAL_ONLY` (cpsb = `False`/ungated — the cross-machine
Mac-browser→PC-paths flow needs it, and a read-only listing can't launch anything on the
wrong machine, unlike the Tier-1 428 gate; premiere/epsnodes set it `True`/loopback-only).
The frontend dialog (`web/cpsb/browse.js`, header comment `fs-browse dialog v1 — synced
from STANDARD-fs-browse.md`) opens at `dir=ROOTS`, is titled "Browse ComfyUI machine",
offers a "New PSD here" input (append creates missing targets), and reshapes the response
via `web/cpsb/api.js`'s `browseDirectory()` (the pack's single network-boundary module).

### POST `/cpsb/cancel/{handoff_id}`
Marks the handoff `cancelled`, unblocks a waiting bridge node (which then raises
`InterruptProcessingException`), notifies the plugin (`handoff_cancelled`), and emits
`cpsb.status` with `cancelled` so the node badge and gallery clear. 200 `{"ok": true}`;
404 unknown. This is the authoritative way to clear a handoff stuck in `editing` — e.g.
the user opened Photoshop and closed the document without saving, which produces no file
event in Tier 1 and so would otherwise sit in `editing` indefinitely (a Tier-2 plugin now
surfaces this same case proactively and in real time via `document_closed` above — see
`plugin_doc_open` — but Cancel remains the manual escape hatch either way, always
available immediately, never gated on any timeout). The frontend surfaces it directly on
the node's "Editing in Photoshop…" badge (hover → cancel) and in the gallery ("Cancel" on
a pending/editing/closed card). Idempotent: cancelling an already-terminal handoff
returns 200 and is a no-op. Any edit that arrives after cancellation (a late save landing
on a cancelled handoff) is ignored by the ingest path (status not in `ACTIVE_STATUSES`).

### POST `/cpsb/discard/{handoff_id}`
Gallery "Remove" for any handoff: same as cancel but sets `discarded`. 200/404.

### GET `/cpsb/status`
```json
{
  "server_version": "0.2.0",
  "tier1_available": true,
  "tier1_reason": null,            // or "headless-server" | "no-photoshop" | ...
  "tier2_connected": false,
  "ps_version": null,               // e.g. "26.5" when plugin connected
  "handoffs": [ <meta.json objects, newest first, max 200> ]
}
```

### GET `/cpsb/thumb/{handoff_id}`
Returns `orig_thumb.png` bytes. (Edited-image thumbnails are fetched via ComfyUI's own
`/view?filename=edit_00N.png&subfolder=cpsb/<id>&type=input`.)

### GET `/cpsb/settings` / POST `/cpsb/settings`
Backend-persisted settings (stored at `<user_dir>/cpsb.json`), JSON object:
`{"photoshop_path": "", "debounce_ms": 800, "cleanup_days": 14, "sibling_outputs": true,
"managed_folder_name": "photoshop"}`.
POST merges partial updates and returns the full object. `managed_folder_name` is
sanitized to a single safe path segment (no separators, no `..`); an invalid value falls
back to the default. (Frontend-only preferences — auto-queue toggle — use ComfyUI's
settings API instead, id prefix `cpsb.`.)

### GET `/cpsb/ws`
WebSocket upgrade endpoint for the UXP plugin. Protocol in §3 of this doc.

---

## 3. Plugin websocket protocol

Text frames, one JSON object per frame, every message has `"type"`. Unknown types are
ignored (forward compatibility). The server allows **one** plugin connection; a new
connection replaces the old (old socket closed with code 4000).

Plugin → server:
- `{"type": "hello", "plugin_version": "0.1.0", "ps_version": "26.5", "uxp_version": "8.1"}`
  Server replies `hello_ack`. Sent once per connection, first message.
- `{"type": "ready", "local_mode": true}` — after the plugin probes whether
  `input_cpsb_path` from `hello_ack` exists on its local filesystem. `local_mode:true`
  → `open_handoff` uses the shared path and `POST /cpsb/upload` for edits; `false` →
  REMOTE mode, where BOTH the PSD download and the edit upload move onto this same
  websocket (`request_file` / `upload_edit` below) instead of HTTP. UXP's runtime blocks
  cleartext `http://` to a non-localhost host but not `ws://` (and `http://localhost` is
  separately exempt, which is why LOCAL mode's HTTP never needed this) — REMOTE mode's
  `fetch()` calls to a remote ComfyUI simply fail, so both directions ride the control
  websocket that is already open and already proven to work cross-machine.
- `{"type": "opened", "handoff_id": "...", "document_id": 123}` — document open
  succeeded; server sets status `editing` and records `plugin_doc_open: true`
  (`HandoffMeta.plugin_doc_open`, gallery overhaul 2026-07-22).
- `{"type": "open_failed", "handoff_id": "...", "error": "..."}` — server sets status
  `error`. If the plugin is in LOCAL mode (same machine as the server), the server falls
  back to a Tier 1 OS-open if available. For a REMOTE-mode plugin it does **not** fall
  back — a server-side launch would open Photoshop on the wrong machine (the server, not
  the machine the user is at) — leaving the handoff in `error` with the plugin's message
  so the real failure is visible.
- `{"type": "save_detected", "handoff_id": "..."}` — informational (UI badge); pixels
  follow via POST `/cpsb/upload` (LOCAL mode) or `upload_edit` (REMOTE mode).
- `{"type": "document_closed", "handoff_id": "..."}` — gallery overhaul (2026-07-22): the
  plugin's own periodic `app.documents` check (`photoshop_plugin/handoffs.js`'s
  `startDocumentCloseWatcher`/`pruneClosedHandoffs`, every 5s) detected that a tracked
  handoff's document closed. Delivery is queued-and-retried, not fire-and-forget: every
  prune path (the watcher AND the panel-render prune) stashes the closure in a
  `pendingCloseReports` set that the watcher flushes only while the connection is up —
  so a document closed during a server restart/network blip is still reported after
  reconnect (the server's handoff records persist across restarts via `meta.json`, so
  the late report lands; an unknown id is just a logged warning). Server records
  `plugin_doc_open: false` (`HandoffMeta.plugin_doc_open`) — never touches `status` — so
  the gallery can show a real "closed without saving" signal for a Tier-2 handoff
  (`web/cpsb/state.js`'s `getDisplayStatus`), replacing an old client-only "editing for
  over an hour" guess that had no plugin involvement at all.
- `{"type": "request_file", "handoff_id": "..."}` — REMOTE-mode PSD download (replaces a
  `fetch` of `GET /cpsb/file/{handoff_id}`, which UXP blocks for a non-localhost host).
  Server replies with one or more `file_chunk`s or a `file_error`, reading the exact same
  path and the exact same guard (`ACTIVE_STATUSES`) `GET /cpsb/file/{handoff_id}` uses.
- `{"type": "upload_edit", "handoff_id": "...", "seq": 0, "total": 3, "data_b64": "...",
  "fidelity": "plugin"}` — one chunk of a REMOTE-mode edit upload (replaces a `fetch`
  POST to `/cpsb/upload`, same UXP restriction). `fidelity` is sent on every chunk for
  simplicity (a self-contained frame, no reliance on remembering seq 0's value) but is
  only actually read once, when the upload completes. See "File transfer chunking"
  below for how chunks are produced/reassembled. Server replies `upload_ok` once every
  chunk `0..total-1` has arrived and the reassembled bytes decode and ingest cleanly, or
  `upload_error` otherwise.
- `{"type": "manual_push", "push_id": "...", "seq": 0, "total": 3, "data_b64": "...",
  "title": "Background (layer)"}` — "send a layer/document to ComfyUI" (2026-07-23), the
  reverse of every other round trip: the plugin initiates, with no ComfyUI node or
  existing handoff behind it at all. Chunking is identical to `upload_edit` (see below),
  but keyed by a PLUGIN-generated `push_id` instead of a `handoff_id` — none exists yet,
  since THIS transfer is what creates one. `title` is sent on every chunk (same
  self-contained-frame convention as `upload_edit`'s `fidelity`) but only read once, from
  the first chunk; it becomes the new handoff's `source.filename`, which the gallery
  shows as the card's title (`web/cpsb/gallery.js`) since a pushed card has no workflow.
  Once reassembled, the server creates a fresh handoff (`origin_kind: "manual_send"`,
  `origin_node_id` synthesized as `ps-push:<push_id>` — unique, never matches a real
  ComfyUI node id, so Reveal simply never shows for these cards, same as any other
  node-less handoff), writes a normal flat managed PSD copy, and ingests the SAME pushed
  image as the handoff's first edit — it arrives already `edited`, ready for **Add**.
  Replies `manual_push_ok` (`{"push_id", "handoff_id"}`, the new id) or
  `manual_push_error` (`{"push_id", "error"}`) — an invalid/malformed transfer never
  creates a handoff at all.
- `{"type": "live_frame", "seq": 12, "data_b64": "<jpeg>", "doc_title": "sketch.psd"}` —
  realtime drawing (docs/roadmap/realtime-drawing.md M1): one save-free snapshot of the
  Live-Mode document (JPEG, captured via `imaging.getPixels` downscaled to 768px long
  side + `encodeImageData`), sent after every detected canvas change (a ~300ms poll of
  the document's history-state id — `photoshop_plugin/liveMode.js`). NOT chunked (frames
  are far below the 8 MiB websocket cap), NOT acked (fire-and-forget keep-latest: a bad
  or dropped frame is simply replaced by the next stroke's), and NEVER a handoff — the
  server holds exactly ONE frame in memory per connection (`PluginConnection.live_jpeg`),
  bumps a per-connection counter for the `cpsb.live` payload (the plugin's `seq` is
  ignored; the counter is display/logging only — the node's cache key is a content hash
  of the frame bytes, §6f), and emits `cpsb.live` (§5). `PhotoshopLiveCanvas` (§6f)
  serves the newest frame; invalid base64/non-JPEG payloads are dropped with a log line,
  never an error reply and never a socket teardown.
- `{"type": "pong"}`

Server → plugin:
- `{"type": "hello_ack", "server_version": "0.1.0", "input_cpsb_path": "/abs/path/to/input/cpsb"}`
- `{"type": "open_handoff", "handoff_id": "...", "psd_path": "/abs/.../source.psd", "file_url": "/cpsb/file/<id>"}`
  Plugin opens `psd_path` directly in LOCAL mode. In REMOTE mode the plugin downloads the
  PSD via `request_file`/`file_chunk` over this websocket (above) rather than fetching
  `file_url` — `file_url` is still included in this message for backward compatibility
  and manual/diagnostic use (`GET /cpsb/file/{handoff_id}` itself is unchanged and still
  works, e.g. from a browser on the server's own LAN) but the plugin no longer calls it.
  Plugin records document↔handoff mapping either way (path-keyed local, documentID-keyed
  remote) and replies `opened`/`open_failed`.
- `{"type": "file_chunk", "handoff_id": "...", "seq": 0, "total": 3, "data_b64": "..."}` —
  one chunk of a `request_file` response. See "File transfer chunking" below.
- `{"type": "file_error", "handoff_id": "...", "error": "..."}` — `request_file` failed:
  unknown/inactive `handoff_id`, or the file is missing/unreadable server-side. Sent
  instead of any `file_chunk`, never after one.
- `{"type": "upload_ok", "handoff_id": "..."}` — a REMOTE-mode `upload_edit` was fully
  reassembled and ingested (or was an idempotent duplicate of the latest edit — same
  "no error" treatment `POST /cpsb/upload` gives a duplicate).
- `{"type": "upload_error", "handoff_id": "...", "error": "...", "reason": "unknown_handoff" |
  "inactive" | "invalid_image" | "malformed"}` — a REMOTE-mode `upload_edit` failed.
  `reason` mirrors `POST /cpsb/upload`'s HTTP status codes so a client can decide whether
  retrying makes sense: `unknown_handoff`/`inactive` are the 404/409 equivalents (retrying
  identical bytes can never change the outcome); `invalid_image`/`malformed` are left
  retryable, matching the HTTP path's own behavior of still retrying after a 400.
- `{"type": "handoff_cancelled", "handoff_id": "..."}` — stop tracking that document.
- `{"type": "result_frame", "data_b64": "<jpeg>", "doc_title": "sketch.psd"}` — realtime
  drawing M3: one rendered live-loop result, sent by the `PhotoshopLivePreview` node
  (§6f) after each render for the plugin's "ComfyUI Preview" panel
  (`photoshop_plugin/previewPanel.js`, the second panel entrypoint). Fire-and-forget
  keep-latest, mirroring `live_frame` in the other direction: no ack, each frame replaces
  the last, and a frame arriving while the preview panel has never been opened is simply
  remembered and shown on first mount.
- `{"type": "ping"}` — every 30s; plugin must `pong` within 15s or the server closes
  the socket (plugin reconnects with backoff).

**File transfer chunking (`request_file`/`file_chunk`, `upload_edit`, and
`manual_push`):** all three use the identical scheme. The sender base64-encodes the
ENTIRE file ONCE,
then slices that single string into fixed-size pieces (~700,000 characters, comfortably
inside a "~512KB–1MB base64 chunk" target) sent as successive frames carrying the same
correlation id (`handoff_id` for `request_file`/`file_chunk` and `upload_edit`; `push_id`
for `manual_push` — no handoff exists yet there, see its own bullet above), an increasing
`seq` starting at 0, and a constant `total` (chunk count).
The receiver's job is only "concatenate every chunk's `data_b64` in `seq` order, then
base64-decode ONCE at the end" — chunks are never independently decodable, which avoids
any per-chunk base64 padding/alignment subtlety (padding only ever appears at the very
end of the full encoded string). A zero-byte file still produces exactly one chunk
(`total: 1`, `data_b64: ""`), never a degenerate empty stream. The server's plugin
websocket is opened with a generous inbound `max_msg_size` (8 MiB) as a safety margin —
chunking already keeps every real frame well under 1MB on its own.

**Server address (plugin-side, user-configurable):** the plugin targets a single
`host:port` base (default `localhost:8188`), from which it derives both the WebSocket
URL (`ws://<base>/cpsb/ws`) and the HTTP origin (`http://<base>`, used only for LOCAL
mode's `POST /cpsb/upload` — REMOTE mode no longer makes any HTTP call at all, having
moved both `GET /cpsb/file/*` and `POST /cpsb/upload` onto the websocket above). It is
editable from the panel's Advanced section ("ComfyUI server"); the value is persisted
plugin-side under the `localStorage` key `cpsb.serverBase` (falling back to
in-session-only if localStorage is unavailable) and applying a new address triggers a
clean reconnect — the current socket is torn down, the backoff/attempt state reset, and
the hello/ready handshake re-run against the new URL. Default localhost use is
unchanged. Cross-machine use — Photoshop on one computer, ComfyUI on another — requires
(a) the plugin manifest's `network.domains` set to the catch-all `"all"` (arbitrary
user-entered hosts cannot be enumerated ahead of time, so the localhost-only allowlist is
widened; a least-privilege allowlist is a possible follow-up for a locked-down release)
— needed for the `ws://` connection to an arbitrary host, which is the only network
capability REMOTE mode now depends on — and (b) the ComfyUI server reachable over the
network, i.e. started with `--listen`.

- **Auto-set Maximize PSD Compatibility on connect** (v0.5.31, `photoshop_plugin/prefs.js`;
  addresses SPIKES.md spike 8). On the first successful connect of a session, the plugin
  reads `app.preferences.fileHandling.maximizeCompatibility` (UXP's typed preferences API,
  PS ≥ 24.0) and, if it isn't already `constants.MaximizeCompatibility.ALWAYS`, sets it to
  ALWAYS inside a `core.executeAsModal` — retiring the documented one-time manual step (a
  layered PSD saved without it pops a compatibility dialog every save, and the Tier-1
  watcher reads the Maximize-Compatibility composite, so it matters for fidelity too).
  Fire-and-forget (never awaited, never blocks the handshake); once-per-session and
  idempotent (only writes on a real difference); wrapped in try/catch so any failure logs a
  warning and connect proceeds. Gated by a default-ON Advanced-section toggle persisted at
  `localStorage` key `cpsb.autoMaxCompat` (only a stored `"0"` disables it). NOT a raw
  batchPlay guess — the typed setter with a documented enum value; worst-case failure is a
  caught exception, never corrupted unrelated prefs. Live-validation on real Photoshop is
  the remaining open part of spike 8.

- **"Send to ComfyUI"** (`photoshop_plugin/manualSend.js`, manifest.json's `sendToComfyUI`
  command, top-level Plugins menu — owner ask 2026-07-23: "there should be a way to send a
  layer or send a file to ComfyUI"). The reverse of every other round trip in this doc: the
  PLUGIN initiates, with no ComfyUI node or existing handoff behind it at all, and no Tier-1
  equivalent (Tier 2 required — there is nothing for this to fall back to). No native
  Photoshop layer/document context-menu entry exists to hook (confirmed absent by Adobe
  staff on the Creative Cloud Developer Forums), so the top-level Plugins menu is the
  correct trigger surface. Firing the command shows a native `<dialog>`/`uxpShowModal()`
  picker (confirmed working from a bare command context with no panel open) offering
  **Active Layer** or **Whole Document**:
  - Whole document reuses `exporter.js`'s existing `runExport()` VERBATIM (duplicate +
    flatten + export + close, already spike-6-proven) — no new export code for this branch.
  - Active layer isolates the topmost of the current selection (`Document.activeLayers[0]`)
    into a fresh same-size document (`app.createDocument` + `Layer.duplicate(destDoc)`),
    trims it to its own non-transparent bounds (`Document.trim(constants.TrimType.
    TRANSPARENT, ...)`), then exports via the SAME batchPlay/Imaging pair as the whole-
    document path (`exporter.js`'s `exportFlattenedDocument`, factored out of `runExport`
    for this reuse). **Known, honest, unverified risk needing a live-Photoshop check** (the
    same spike-gated posture every other genuinely new UXP surface in this pack takes):
    `Layer.duplicate(destDoc)`'s pixel-offset/position behavior when `destDoc` is a FRESH
    document is undocumented — Adobe's own reference example only covers duplicating into
    an ALREADY-OPEN document. Worst case is a cosmetically-shifted-but-still-valid export
    (Trim still produces a correct crop of whatever pixels actually land), never corruption.
    **Also known: only the topmost of a multi-layer selection is sent, deliberately no
    auto-merge** — `Layer.merge()`'s documented multi-selection behavior is reported broken
    on the Creative Cloud Developer Forums, with no confirmed batchPlay alternative.

  Either way, the exported PNG bytes go over a new chunked `manual_push` websocket message
  (§3 above) — always over the websocket regardless of LOCAL/REMOTE mode, since (unlike an
  edit for an existing handoff) there is no pre-arranged shared-filesystem path to write
  onto. The server creates a fresh handoff (`origin_kind: "manual_send"`, a synthesized
  `origin_node_id` that never matches a real graph node), writes a normal flat managed PSD
  copy, and ingests the same pushed image as its first edit — it arrives already `edited`,
  ready for the gallery's **Add** action. The card's title is the pushed layer/document
  name (`source.filename`, shown by `web/cpsb/gallery.js` in place of the usual workflow
  name, which a pushed card has none of).

---

## 4. Ingest pipeline (backend, both tiers)

One function, `ingest_edit(handoff_id, pixels_or_path, fidelity)`, is the convergence
point (PLAN §3 `mark_edited`): Tier 1 watchdog-settled PSD reads and Tier 2 uploads both
end here. It:
1. Writes `edit_%03d.png` into the handoff folder (which lives under `input/`, so the
   result is addressable as `subfolder="cpsb/<id>", type="input"` by LoadImage widgets
   and `/view`).
2. Skips ingestion if the new edit's SHA256 equals the previous edit's (idempotency —
   the watchdog and plugin may both fire for one save).
3. If `origin_kind == "terminal_output"` and the source was in `output/` and setting
   `sibling_outputs` is on: also writes `<origname>_ps<N>.png` next to the original
   output file and records it as `sibling_output` in the edit entry.
4. Updates `meta.json` (status `edited`, appends to `edits`).
5. Unblocks a waiting bridge node for this handoff, if any.
6. Emits `cpsb.updated` (below).

Tier 1 PSD read: try the embedded Maximize-Compatibility composite first
(fidelity `composite`); if absent, re-composite via psd-tools (fidelity `recomposite`).
16-bit / non-RGB modes are converted to RGB8 (PLAN §4); alpha is preserved when present.
A read that still fails after the retry budget is **non-terminal**: the watcher logs and
leaves the handoff `editing` so the next save retries ingestion — it must never move a
handoff to `error` (terminal statuses would silently drop all subsequent saves).

**Mask channel extraction: REMOVED (owner's call, 2026-07-17)** — field testing showed
transparency-based masking covers the need; extra-channel extraction was dropped for
simplicity. MASK outputs derive from image alpha only (see §6/§6b). Git history and
research/research-annotate-node.md retain the design if ever revisited.

---

## 5. Frontend events (server → ComfyUI frontend via send_sync)

- `"cpsb.updated"` — an edit arrived:
  ```json
  {"handoff_id": "...", "origin_node_id": "17", "origin_kind": "load_image",
   "filename": "edit_002.png", "subfolder": "cpsb/a1b2c3d4", "type": "input",
   "fidelity": "plugin",
   "sibling_output": null }
  ```
- `"cpsb.status"` — handoff lifecycle changed (badge/gallery refresh):
  `{"handoff_id": "...", "origin_node_id": "17", "status": "editing", "plugin_doc_open": null}`
  `plugin_doc_open` mirrors the meta field (§1) at emit time. Load-bearing for the node
  badge: a `status: "editing"` event carrying `plugin_doc_open: false` is a
  document-CLOSED report (`document_closed` → `set_plugin_doc_open`), which clears the
  badge rather than (re)creating it. NB the ingest path emits ONLY `cpsb.updated` — an
  edit landing never produces a `cpsb.status`, so `badges.js` subscribes to both (the
  `cpsb.updated` subscription is what clears a Tier-1 badge when the edit arrives).
- `"cpsb.tier2"` — plugin connection state changed:
  `{"connected": true, "ps_version": "26.5"}`
- `"cpsb.live"` — a new live-drawing frame landed (realtime drawing M1):
  `{"seq": 12, "doc_title": "sketch.psd"}`. `seq` is a per-connection frame counter for
  display/logging only (`PhotoshopLiveCanvas.IS_CHANGED`'s cache key is a content hash
  of the frame, §6f). The M2 frontend live loop queues a coalesced re-run on this event;
  consumers never fetch the frame itself — only the node reads the in-memory slot, at
  execute time.

**Universal cancel (product-owner requirement 2026-07-17):** ANY node that shows the
"Editing in Photoshop…" badge (badges.js) MUST expose a working cancel ✕, regardless of
node type or whether the node has an image preview — imageless nodes (Edit in Photoshop,
Annotate for Edit, Compose Layers to PSD) included. A stuck editing badge with no cancel
is the worst failure mode, especially when Photoshop opened on a different (server)
machine the user can't reach. Cancel calls /cpsb/cancel (§2).

A blocking wait (`HandoffManager.wait_for_edit`) must ALSO honor two non-badge escape
hatches, so a node can never wedge until its timeout: (a) ComfyUI's OWN "Cancel current
run" — the wait polls `comfy.model_management.processing_interrupted()` and returns
promptly; and (b) the handoff transitioning to a terminal ERROR (e.g. the plugin's
`open_failed`) — the wait returns `WaitOutcome.ERROR` at once instead of spinning until
`timeout_seconds`. Both, like cancel/timeout, make the node raise
`InterruptProcessingException`.

Frontend paste-back behavior on `cpsb.updated` is specified in PLAN §3 (clipspace-style
widget update for `load_image`/`bridge_node`; cosmetic preview + toast with
"[Add as node]" for `terminal_output`). Auto-queue policy:
- `load_image` and `load_psd` origins: queue when the `cpsb.autoQueue` setting is on.
  (For `load_psd`, paste-back never rewrites the node's file widget — the edit is
  consumed by the node's own execute()/IS_CHANGED, §6b.)
- `bridge_node` origins: queue IFF the origin node's `mode` widget is
  `"Re-run on every save"` (§6) — the node-level mode overrides the global setting
  (an explicit per-node choice must not be silently disabled elsewhere). For the
  other two modes, NEVER: a blocking bridge node delivers the arriving edit
  downstream inside the run completing at that moment, so a re-queue would run the
  entire workflow (and its SaveImage nodes) again per save — the field-reported
  "one click saved multiple files" loop. This is safe by construction: blocking mode
  never auto-queues, and re-run mode never blocks, so no edit is ever both consumed
  in-run and re-queued.

---

## 6. Bridge node

- Class `PhotoshopBridge` (stable node id — saved workflows depend on it), display name
  "Edit in Photoshop", category `image/photoshop`.
- Inputs: `image` (IMAGE); `mode` (COMBO, exactly these strings — the frontend matches
  on them: `"Wait for first save"` (default) | `"Re-run on every save"` |
  `"Open only (don't wait)"`); `timeout_seconds` (INT, default 1800, min 10, max
  86400 — applies only to "Wait for first save"). Hidden: `unique_id` (UNIQUE_ID),
  `prompt` (PROMPT), `extra_pnginfo` (EXTRA_PNGINFO). (This replaces the earlier
  `wait_for_edit` BOOLEAN — pre-release breaking change, no migration shim.)
- Mode semantics:
  - "Wait for first save": block in execute() until the first save arrives, deliver it
    downstream in the same run, done. Later saves into the still-open document are
    still ingested (gallery stays current; next MANUAL queue consumes the latest) but
    never trigger a run.
  - "Re-run on every save": never blocks. First run opens Photoshop and passes the
    input through unchanged; each subsequent save auto-queues a re-run (frontend §5
    policy) which consumes the latest edit — live-iterate mode. No timeout involved.
  - "Open only (don't wait)": passthrough + fire-and-forget open; saves are ingested
    and consumed on the next manual queue only.
- Outputs: (IMAGE, MASK). MASK = `1 - alpha` of the edit image (LoadImage parity)
  when transparency is present, else an all-zero mask sized to the image.
- Execute (edit-consumption semantics): if an active handoff for this node already has
  an edit AND its `source_hash` matches the current input, that edit is returned
  immediately WITHOUT re-opening Photoshop — re-execution is the "consume the edit"
  path (a literal always-reopen reading would demand a new save on every re-queue, an
  open loop). If the `source_hash` differs (upstream re-generated), the old handoff is
  superseded and a fresh one is created and opened. Otherwise (no handoff, or no edit
  yet): saves the incoming image to a handoff (origin_kind `bridge_node`, keyed to
  `unique_id`), opens Photoshop (tier-selected), then if `wait_for_edit` blocks in a
  `while ... time.sleep(0.2)` poll on the shared pending-table until edited / cancelled
  / timeout (the latter two raise `InterruptProcessingException`; ComfyUI's own
  interruption notice is the user-facing signal). The wait must also detect an edit
  that landed between the open call and wait registration (edits-count snapshot, not
  waiter-flag alone). On timeout the handoff stays `editing` — a later save or re-queue
  resumes the same PSD with layers intact. If `wait_for_edit` is False: opens Photoshop
  (fire-and-forget) and passes the input through unchanged; a prior matching edit, when
  one exists, is returned as above.
- `IS_CHANGED`: SHA256 of the latest edit file for this node's active handoff (or a
  constant when none) — an arriving edit changes the hash and forces downstream
  re-execution on the next queue, matching LoadImage semantics.

---

### 6b. Load PSD node

- Class `PhotoshopLoadPSD` (unique id — plain "LoadPSD" collides with other packs),
  display name "Load PSD", category `image/photoshop`.
- Inputs: `psd` (COMBO of `.psd`/`.psb` files in the input directory, refreshed like
  LoadImage's combo) — the frontend adds a custom upload widget (accept `.psd,.psb`,
  hand-rolled input + POST to ComfyUI's own `/upload/image`; the stock IMAGEUPLOAD
  widget hardcodes png/jpeg/webp). Hidden: `unique_id`.
- Outputs: (IMAGE, MASK) — §4 read path (embedded composite → recomposite fallback,
  RGB8 normalize); MASK = `1 - alpha` of the flattened image, else zeros.
- Round trip: right-click → Open in Photoshop creates a `load_psd` handoff (§2 copy
  semantics). While an ACTIVE handoff for this node has a matching `source_hash` and
  edits, execute() returns the latest edit (and its mask) instead of re-flattening the
  original — the PhotoshopBridge consume pattern. `IS_CHANGED`: sha256 of the PSD
  file's raw bytes, combined with the latest edit hash when an active matching handoff
  exists.
- **Edit-original option**: the node carries a BOOLEAN widget `edit_original` (default
  False). Default (False) = the current copy-to-handoff behavior (non-destructive). When
  True, `/cpsb/open` for this node does NOT copy — the handoff's watched target IS the
  user's selected input PSD in place (`edit_in_place: true` + the source triple on the
  open body); Photoshop opens the real file, plain Cmd+S overwrites it, and the watcher
  must watch that exact file path (outside the managed folder), ingesting the user's own
  saved PSD directly. On such a handoff, `orig_thumb`/`source.psd`-copy are skipped; the
  edit is read from the original path. Terminal cleanup never deletes the user's file
  (only managed-folder artifacts are ever purged). This is the only path where a bridge
  handoff points at a file the user owns — guard every delete accordingly. Any *other*
  node's write that happens to land on this same original path (e.g. a Compose node's
  `existing_psd_path` pointed at it — §6c) is suppressed via the shared, path-keyed
  own-write registry (§1, v0.5.37) — suppression is not specific to the node that
  opened the original.
- **Save-trigger policy** (v0.5.21): the node carries a COMBO widget `on_save`, appended
  as the LAST required input (ComfyUI restores a saved workflow's widget values BY
  POSITION — see `LGraphNode`'s `widgets_values` zip — so appending is the only placement
  that leaves existing workflows untouched). Values, from `cpsb/load_psd.py`'s
  `OnSaveMode`:
  - `"Re-run workflow"` (default) — today's behavior exactly: ingest the edit and let the
    frontend re-queue the graph.
  - `"Update only (don't re-run)"` — ingest the edit so the NEXT manual run picks it up,
    but never auto-queue.
  - `"Ignore (do nothing)"` — do not ingest at all; saving in Photoshop does nothing.
    This is what makes "open a PSD, hide all but one layer, push that layer back, close
    without saving" workable without the graph firing on every save.
- The policy is read from the node's widget at OPEN time, sent on `/cpsb/open` as
  `trigger_policy` (validated there; 400 on an unknown value; omitted → server default),
  and PERSISTED on the handoff (`HandoffMeta.trigger_policy`; a meta.json written before
  this existed falls back to the default). Same contract as `edit_in_place`: changing the
  widget on an ALREADY-open handoff has no effect until the next open.
- **Enforced SERVER-SIDE**, via one shared gate `HandoffManager.should_ingest()`, consulted
  at all three ingest call sites — `POST /cpsb/upload`, the websocket `upload_edit` chunk
  handler, and `CpsbWatcher._ingest_settled`. The frontend cannot be the only guard: the
  plugin uploads with no browser tab open at all, and this must equally govern the Tier-1
  watcher save and BOTH of the plugin's manual Send paths, which all funnel through
  `deliverEdit` → `ingest_edit`. A suppressed upload returns a SUCCESS-shaped response
  (`{"ok": true, "ignored": true}` / `upload_ok`) and logs at INFO — the plugin did nothing
  wrong, and a user who forgot they set Ignore needs it diagnosable from the console.
- `maybeAutoQueue` (frontend) additionally refuses to queue for `Update only`/`Ignore`.
  Precedence: the global `cpsb.autoQueue` setting is checked FIRST, so per-node OFF wins
  over global ON, and global OFF still wins over per-node `Re-run workflow`.
- The policy string is duplicated across four layers that cannot import one another
  (`load_psd.OnSaveMode`, `handoff.TriggerPolicy`, `routes._VALID_TRIGGER_POLICIES`,
  `pasteback.js`'s constants). `tests/test_load_psd.py`'s drift guard asserts all four
  agree, reading the JS as text — rewording one in isolation would otherwise silently stop
  a policy being honored, with no type error anywhere.
- **Non-PSD formats** (`cpsb/raster_io.py`): the node's file combo and `VALIDATE_INPUTS`
  also accept `.tif`/`.tiff` (Pillow, no dependency). `execute()` dispatches `.psd`/`.psb`
  to `read_edited_psd` and TIFF to `raster_io.decode_to_rgb8` (returns one PIL image, alpha
  in the mode — the pack's convention; reuses `psd_io.normalize_to_rgb8` for
  16-bit/CMYK/grayscale, plus a 16-bit-grayscale scale fix Pillow doesn't do itself).
  `edit_original` stays PSD/TIFF-only (`raster_io.EDIT_IN_PLACE_CAPABLE_EXTENSIONS`).
  Illustrator `.ai` and camera raw/`.dng` are NOT decoded in-process (v0.5.40 removed the
  optional `pypdfium2`/`rawpy` path — the pack bundles no third-party decoders); Photoshop
  opens them natively, so they belong to a dedicated Tier-2 "Open via Photoshop" node — see
  `docs/roadmap/ps-external-decode.md`. NB: the `/cpsb/open` `_PSD_NATIVE_EXTENSIONS` gate
  is unchanged, so "Open in Photoshop" for a loaded `.tif` is still a follow-up.
- Frontend: the node type is allowlisted in `captureImageUploadType`'s detection (its
  hand-rolled widget bypasses the stock `image_upload` spec flag), and its context-menu
  origin_kind derives as `load_psd`.
- **CMYK loads correctly** (fixed v0.5.28 — user report: "a black square"): psd-tools
  1.17.4 already un-inverts Photoshop's on-disk CMYK convention in its own
  `pil_io.post_process` (verified in its source), so `normalize_to_rgb8` must NOT invert
  again — the old double inversion produced full-ink black. Both the node's IMAGE output
  and `/cpsb/psd_preview` share the one healed helper. Test-fixture trap, documented in
  `cpsb/psd_io.py`'s docstring: `PSDImage.frompil()`-built CMYK fixtures do NOT match
  Photoshop's on-disk bytes (no write-side inversion), so they validate the bug — real
  fixtures must pre-invert (`byte = 255*(1-ink)`). No-ICC conversion is naive/preview-
  grade by design; embedded profiles get psd-tools' own ICC transform.
- **Preview** (no Photoshop plugin required): the node shows a canvas preview of the
  selected PSD, like LoadImage does for a PNG. `GET /cpsb/psd_preview?filename=&subfolder=&type=`
  (defaults `subfolder=""`, `type="input"`; params mirror `/view` but default to the
  `input/` tree the combo draws from) flattens the PSD server-side via §4's read path
  (embedded composite → recomposite fallback — no plugin, no Photoshop) and caches the
  result content-addressed under `<temp>/cpsb/psdpreview_<sha256>.png`. Response is
  always 200: `{filename, subfolder:"cpsb", type:"temp"}` (addressable by ComfyUI's own
  `/view`) on success, or an all-null triple if flattening fails (logged, never a 500);
  400 for a missing/invalid `filename`/`type`/extension, 404 for a missing or
  traversal-escaping file. The frontend refreshes the preview (debounced, with a
  monotonic token guard against stale responses) whenever the combo value changes and
  on workflow load; failures degrade silently to no preview.

### 6c. Compose Layers to PSD node

- Class `PhotoshopComposePSD`, display name "Compose Layers to PSD", category
  `image/photoshop`.
- Frontend gives it AUTO-GROWING image inputs: `image_1`, `image_2`, … — connecting one
  reveals the next empty socket (pattern forked from rgthree's MIT implementation, with
  attribution comment). Backend accepts any number ≥ 1 via optional inputs. Each socket
  carries a ComfyUI IMAGE, which may be a multi-image BATCH (e.g. a VAE Decode emitting
  several images) — every image in every batch becomes its own layer (frames expanded in
  batch order within a socket, sockets in `image_1..image_N` order).
- **Renamable `image_N` INPUT SLOTS name the layers (v0.5.38, product owner verbatim: "you
  should be able to change the names of the input nodes by double clicking on them. The
  name of the node should become the layer names. Remove the separate layer name
  textbox.").** Double-clicking an `image_N` slot (connected or the trailing spare) opens
  a rename prompt (`LGraphCanvas.prompt`, falling back to `window.prompt`) exactly like
  `comfyui-epsnodes`' `EPSSwitcher` (FORMAT.md §6.4 "Renamable rows" — the same technique,
  clean-room reimplemented in `web/cpsb/compose.js`, not copied). Confirming sets that
  input's `.label` — display-only, `input.name` and every existing link are untouched, so
  a saved workflow's `image_7` link still restores onto a real `image_7` socket exactly as
  before. Every slot's current label is kept serialized into the hidden `layer_names`
  widget (below) as a JSON object; the backend (`_resolve_layer_names`) turns that into
  each layer's actual written name — a renamed slot's label is used VERBATIM when its own
  batch contributed one layer, suffixed per-frame (`"<label> 1"`, `"<label> 2"`, …) when it
  contributed more than one; an un-renamed slot keeps the original `"Layer <N>"` numbering.
  This REPLACES the former single `layer_name` STRING widget (a global base name for every
  layer alike) — removed outright, per the product owner's own ask.
- Widgets: `group_name` (STRING, default "ComfyUI Layers" — the group/folder the layers
  land in), a HIDDEN `layer_names` STRING (default `""` — never shown as a textbox; filled
  purely by the rename gesture above, one JSON object per node:
  `{"image_1": "Sky", "image_3": "Foreground"}`, absent key = that slot keeps the default
  numbered name; occupies the exact `required` position the removed `layer_name` widget
  used to, so every OTHER widget's saved position is unaffected), `mode` (COMBO — the SAME
  three strings as the Edit in Photoshop node's BridgeMode: "Wait for first save" (default)
  | "Re-run on every save" | "Don't open (composite only)"). (`mode` replaces the earlier
  `edit_after` BOOLEAN; the removed `filename_prefix` was dropped because it only named an
  intermediate file the user never sees: Photoshop opens the managed `source.psd` copy, not
  that file — both pre-release breaking changes.) `timeout_seconds`
  (INT, default 1800) applies to "Wait for first save".
  `max_layers` (INT, default 64, min 1, max 512) caps the total images turned into layers
  across all sockets, oldest-first; a larger batch is truncated (first N kept) with a
  logged warning — no silent drop.
- **Backward compatibility**: a workflow saved before v0.5.38 has no `layer_names` value,
  and — because ComfyUI restores saved widget values by POSITION — may deliver that older
  build's `layer_name` STRING value into this widget's slot instead (e.g. the literal text
  `Layer`). That is not valid JSON; `_parse_layer_names` degrades it to "no custom names"
  (logged once at WARNING, never raised), so the workflow loads and composes cleanly with
  the exact same default `"Layer <N>"` naming it always produced.
- Behavior: canvas = max width × max height across inputs; every image (across every
  socket's batch) becomes one pixel layer, CENTERED, never rescaled; `image_1`'s first
  frame is the BOTTOM layer, later frames/indices stack on top; all layers inside ONE
  group named `group_name`. Written via psd-tools
  (`PSDImage.new` → `create_pixel_layer` → `create_group`) to
  `input/<filename_prefix>_%05d.psd` (unique per execution).
- Channels: the PIL mode is matched to each frame's channel count, never forced —
  1ch→L→RGB, 3ch→RGB (the normal-VAE path), 4ch→**RGBA with alpha PRESERVED** (a
  4-channel IMAGE from a layer-decomposition model like Qwen Image Layered Control
  becomes a PSD layer carrying real per-pixel transparency; forcing it to RGB previously
  byte-misaligned it into tiled/noise garbage). The flatten is alpha-aware ("over"
  compositing) so a fully-opaque input reproduces the old opaque overwrite exactly.
  Outputs: (IMAGE flattened composite, MASK = the composite's accumulated alpha —
  transparent→1, covered→0 — else zeros, STRING = the written psd filename,
  input-relative — usable by Load PSD / addressable by /view).
- Mode semantics MIRROR the Edit in Photoshop node (§6) exactly, applied to the
  freshly-written LAYERED PSD (so the user composites/adjusts LAYERS in Photoshop, then
  the node outputs the SAVED result, flattened): "Wait for first save" BLOCKS execute()
  until the first save then continues the workflow with the edit; "Re-run on every save"
  never blocks (first run opens PS, passes the flat composite through; each save
  auto-queues a re-run consuming the latest edit); "Don't open (composite only)" is the
  old always-flat behavior (never opens PS). The handoff uses origin_kind `bridge_node`
  with a MANAGED COPY of the generated file (v1 scope decision, NOT `edit_in_place` —
  the managed copy is a byte-for-byte copy of the just-written composed PSD, made once
  at handoff-creation time; corrected v0.5.37, this paragraph previously claimed
  edit_in_place), so it shares the §6 bridge node's blocking-wait, consume, IS_CHANGED,
  and frontend auto-queue machinery verbatim (import from cpsb.nodes; do not
  duplicate). Default "Wait for first save" makes the useful edit-in-Photoshop flow the
  out-of-box behavior — a flat composite is only ever the output when the user has no
  edit yet or picked "Don't open".
- **Own-write suppression (v0.5.37).** Every real write this node makes to disk — the
  fresh auto-numbered file, and the `existing_psd_path` append target, whether newly
  created or pre-existing — is registered with the shared, path-keyed own-write
  registry (§1) right after the write lands (after `os.replace`, for the atomic
  append-mode write). Without this, pointing `existing_psd_path` at a file another
  handoff has open (e.g. a Load PSD node's `edit_in_place` original — §6b) would have
  the watcher misread this node's own write as that OTHER handoff's Photoshop save:
  under "Re-run on every save" that re-triggers the very node that just wrote it — an
  infinite loop; under "Wait for first save" it delivers the wrong pixels.
- Consume semantics: `IS_CHANGED` hashes the input images + params, folded with the
  latest-edit hash when an active matching handoff exists; execute() returns the latest
  edit (flattened) when the active handoff's `source_hash` matches the current inputs'
  hash — the §6/§6b consume pattern.
- **Outputs** (v0.5.25): `(image, mask, filename, layers)` — `layers` is APPENDED so saved
  workflows' links (stored by output slot index) keep their meaning. `image`/`mask` stay
  the single flattened composite (or the consumed edit); `layers` is an IMAGE **batch**,
  one canvas-sized frame per placed layer, frame order = layer order — wire it to a
  Preview node to see every layer individually (user report: "connect this node to a
  preview node ... it only shows one image", which was correct-but-unwanted behavior of
  the flat composite). Frames share the run's real canvas (fresh-build max-of-inputs, or
  the append target's fixed canvas), layer alpha flattened onto black. Batched `image_N`
  inputs expand to one frame per batch frame, mirroring the one-layer-per-frame PSD rule
  (v0.5.9). On the consume path and in "Wait for first save", `layers` remains this run's
  WRITTEN layers (identity matched, so the inputs are what was written) — the
  edited/saved result comes back through `image`/`mask`.
- **Finding the written file** (v0.5.22). "Don't open (composite only)" writes a real PSD
  but deliberately creates NO handoff — and every discoverability surface in this pack
  (gallery cards, badges, reveal/re-open, the right-click menu) is handoff-driven, so the
  file existed with nothing pointing at it ("how do I later find and open the file?").
  - A new informational event `cpsb.compose_written` (`COMPOSE_WRITTEN_EVENT`) is emitted
    right after the PSD is written, for ALL three modes, carrying the node id and the
    written filename. It is emitted via `context.send_event` ONLY — the same non-handoff
    transport `routes._emit_tier2` uses — and never touches `HandoffManager`, so "Don't
    open" keeps its zero-Photoshop-entanglement contract literally (asserted by test: no
    active handoff, no `meta.json`). It is NOT emitted on the consume path or the
    duplicate-append skip, since no write happens there.
  - The frontend shows a plain (disabled, non-clickable) text row `Written: <filename>
    (on ComfyUI machine)` plus a separate **Copy Path** button (v0.5.24; originally one
    click-to-copy button). The event payload carries `path` — the absolute, resolved
    server-side path — and Copy Path copies THAT, not the filename. The locality label
    stays: the path means the ComfyUI machine's filesystem. Still NO reveal-in-OS
    affordance (meaningless in remote mode). A localStorage record persisted before the
    `path` field existed disables the button (tooltip suggests a re-run) rather than
    silently copying the bare filename. The append-target widgets (`existing_psd`,
    `existing_psd_path`) are greyed out and click-blocked via `widget.disabled` while
    `append_to_existing` is false (cosmetic only — the backend contract is unchanged;
    mechanism verified against ComfyUI_frontend's BaseWidget/`computedDisabled` draw path).
  - The right-click "Open in Photoshop" gate (previously `node.imgs?.length`) now also
    accepts a Compose node with a recorded written filename, routed through the SAME
    `/cpsb/open` `mode:"new"` path everything else uses — so it inherits the client-locality
    confirm and Tier-1/Tier-2 handling for free.
  - Persistence: the filename is mirrored into `localStorage` (keyed by workflow name +
    node id) and restored on `nodeCreated`. It survives a reload of the same
    browser/profile/workflow; it does NOT survive a different browser/profile, cleared or
    blocked storage, or opening the exported workflow JSON fresh. There is no server-side
    record to re-sync from — by design, since "Don't open" creates none.
  - Already-shipped alternative, no new machinery: Compose writes into ComfyUI's input
    folder, and `PhotoshopLoadPSD`'s `psd` combo lists `input/*.psd` — so pointing a Load
    PSD node at the composed file works today. (Exception: an `existing_psd_path` override
    can point outside `input/`, where it won't appear in that combo.)
- **Append target is browse-only** (v0.5.29 — user simplification request: "Remove the
  append_to_existing checkbox and always make that on. Also remove the existing_psd
  selector. Just have the browse capability."). The `append_to_existing` BOOLEAN and
  `existing_psd` COMBO are REMOVED; behavior is driven purely by `existing_psd_path`
  (STRING + the Browse… dialog): EMPTY (default) → the classic fresh auto-numbered
  `compose_%05d.psd` per run; NON-EMPTY → append into that path (create if missing).
  Identity/IS_CHANGED fold the stripped path (empty string = fresh mode), so switching
  empty↔path supersedes the active handoff exactly as toggling used to. The path is used
  VERBATIM (suffix-validated only) — the deliberate power-user trust model the override
  branch always had; Browse is how ordinary users produce it. **Widget-position breaking
  change**: ComfyUI restores saved widget values by POSITION, so a pre-v0.5.29 workflow
  that configured append needs its compose widgets re-checked once after loading (repo
  precedent: the v0.5.12 filename_prefix removal — deliberate, no shim, pre-1.0). Final
  required order: group_name, layer_names, mode, timeout_seconds, max_layers,
  existing_psd_path (v0.5.38 renamed the `layer_name` slot to the hidden `layer_names` —
  see the rename bullet above — without disturbing this order).
- **Append into an existing document** (v0.5.20). Widgets, all appended at the END of
  `required` (ComfyUI matches saved widget values BY POSITION, so anywhere else silently
  shifts every existing workflow's values): `append_to_existing` (BOOLEAN, default False),
  `existing_psd` (COMBO over `.psd`/`.psb` in the input dir), `existing_psd_path` (STRING
  override, used verbatim when non-empty). Purpose: accumulate many runs into ONE
  reviewable document instead of a slew of separate files.
  - There is no OS file picker available to a ComfyUI node — nodes execute SERVER-SIDE.
    The input-dir COMBO is the idiomatic mechanism (what `PhotoshopLoadPSD` and core
    `LoadImage` already do) and the only one that works when ComfyUI and Photoshop are on
    different machines. `existing_psd_path` is a path on the **ComfyUI** machine.
  - Target resolution mirrors `load_psd.py`'s `_resolve_psd_path`, rejecting traversal
    identically. A target that does not exist yet is CREATED fresh (first-run convenience,
    not an error).
  - **Atomic write** (`_atomic_save`): `PSDImage.save()` opens its destination `"wb"`
    immediately, truncating the existing file before writing a byte — a mid-save failure
    would leave the user's accumulated document unopenable. Writes to a `mkstemp` temp
    file in the SAME directory (guaranteeing one filesystem, so the final step is a true
    atomic rename) and `os.replace()`s only after `save()` fully returns; on any exception
    the temp file is removed and the target is byte-for-byte untouched.
  - Guards: a non-RGB target is REFUSED naming its actual mode (psd-tools would silently
    desaturate/convert instead of erroring); a canvas-size mismatch WARNS naming both
    sizes and proceeds (psd-tools has no canvas-resize API, so a larger image is clipped).
  - Run grouping: each append lands in `"<group_name> <N>"`, N = 1 + the highest existing
    same-prefixed numbered group in the target, so runs stay navigable.
  - **Duplicate-append avoidance**: `append_to_existing` and the resolved target are folded
    into BOTH hashes (identity → switching targets supersedes a stale handoff; inputs →
    IS_CHANGED forces re-execution). Within one identity the real append happens AT MOST
    ONCE: if an active handoff for the node already matches the current identity (e.g. a
    re-queue of "Wait for first save" before any save), the append is SKIPPED — outputs are
    still computed from the same centering math, but nothing further is written.
  - Appending changes only WHERE this run's layers are written. IMAGE/MASK outputs stay
    this run's own flattened composite, never the whole accumulated document, and `mode`
    dispatch is unchanged.
- **Handoff identity is mode-FREE** (fixed v0.5.18 — this node previously had neither
  reuse nor supersede, which is what made "Wait for first save" hang forever and spawned
  a document per run). Two distinct hashes now:
  - `_compute_identity_hash` (images + `group_name` + `layer_names`) is what a handoff's
    `source_hash` records and what reuse/supersede is keyed on. `layer_names` is hashed as
    its raw serialized JSON (v0.5.38, replacing the removed `layer_name` STRING in this
    hash) — a rename therefore also supersedes/re-executes exactly like a `group_name` edit
    always has. Deliberately excludes `mode` and `filename_prefix`, matching
    `compute_source_hash`'s pixels-only contract.
    Folding `mode` in was the bug: flipping the widget with identical pixels changed the
    recorded identity, so the already-open handoff could never match again — it was
    stranded as a live but unreachable Photoshop document while a second one was created
    underneath it. The user then saved document A while execute() waited on document B,
    and blocked until timeout.
  - `_compute_inputs_hash` (identity + `filename_prefix` + `mode`) stays mode-sensitive
    and is `IS_CHANGED`'s job ONLY.
- Reuse/supersede now mirrors §6's bridge node exactly: an active `bridge_node` handoff
  for this node is REUSED (its `source.psd` is never rewritten — the user's in-progress
  layers live in it); it is superseded only when the identity hash genuinely differs, and
  also when switching to "Don't open (composite only)". A reused handoff in a NON-BLOCKING
  mode does NOT relaunch Photoshop (same rule as §6): "Re-run on every save" re-executes on
  every save, so reopening would steal focus and re-issue a Tier-1 OS open each time.
- `ingest_edit` logs an edit arriving for an inactive/superseded handoff at WARNING (was
  INFO), naming the handoff id, node id and status — this class of bug is otherwise
  invisible from the ComfyUI console.

### 6d. Annotate for Edit node

- Class `PhotoshopAnnotate`, display name "Annotate for Edit", category
  `image/photoshop`.
- Inputs: `image` (IMAGE); `instruction` (STRING, multiline, default ""); optional
  `mask` (MASK). Widgets: `mode` (COMBO, renamed from `annotate_mode` in v0.5.30 —
  breaking, see below: "Pass through" (default) | "Wait for first save" |
  "Re-run on every save"); `box_composite` (BOOLEAN, default False). Hidden: `unique_id`.
  The last two `mode` values ALIAS `BridgeMode.WAIT_FIRST_SAVE`/`.RERUN_EVERY_SAVE`
  (constant reuse, not duplicated strings — a `tests/test_annotate.py` drift guard asserts
  equality) so the node speaks the same vocabulary as Edit-in-Photoshop/Compose (user
  request 2026-07-19). Widget renamed AND its option strings changed = a workflow saved
  before v0.5.30 needs the Annotate node's mode re-selected once (pre-1.0, no shim).
- Outputs: (IMAGE, MASK, STRING instruction, IMAGE annotated). In PS mode the IMAGE is
  the SAVED composite EXCLUDING the `Instructions` layer (so any edit the user made to
  the base image BAKES IN); pass-through mode passes the input image through unchanged.
  STRING is the instruction verbatim.
- The four outputs cover the three views of an annotated edit, so nothing needs a fifth
  socket (product-owner question, 2026-07-18: "there are three slots — should they map
  to that?"):
  - **everything but the annotation** → `image` (clean, base edits baked in). Feed this
    plus `mask` to an inpainting/mask-driven model.
  - **just the annotation** → `mask`.
  - **image and annotation combined** → `annotated`. Feed this to a visual-prompt edit
    model (the "edit what I circled" convention) — it needs no mask input.
- `annotated` is the combined view, and `box_composite` selects the FORM the annotation
  takes in it (revised v0.5.19):
  - `True` → a 4px red unfilled rectangle at the final mask's bounding box, drawn on the
    CLEAN image. The tidy box REPLACES the raw strokes rather than adding to them: a
    marking blob plus a box around it is noisier for a box-prompt model than the box
    alone. This is the mark convention Kontext/Qwen-Image-Edit document responding to,
    and is what `examples/annotate-qwen-image-edit.json` wires into Qwen.
  - `False` → the full PS composite: the base image with the user's REAL painted strokes
    on top, in their real colors. Before v0.5.19 this branch returned the image
    completely unannotated, making the output indistinguishable from `image` and leaving
    no way to see what had been painted.
  - Pass-through (ComfyUI-only) mode has no Photoshop document and therefore no strokes,
    so `annotated` there stays the unchanged image — the original behavior for that tier.
  - Stroke COLOUR is deliberately NOT yet surfaced as a separate signal (e.g. red=remove,
    green=keep): `_layer_alpha_mask` keeps only alpha. Deferred until a real downstream
    consumer exists; it can be added additively at the tail of the output tuple.
- PS-mode markup uses a dedicated **`Instructions` LAYER** (product-owner redesign
  2026-07-17, replacing the old whole-image pixel diff + scipy morphology, all removed):
  on open the node writes `source.psd` LAYERED — the input image as a base layer + a
  fully-transparent top layer named exactly `Instructions`. The user draws on that layer
  to mark the region (any color) and may also edit the base image. On save the node
  reopens the saved layered `source.psd`:
  - **`Instructions` top-level layer found** → MASK = that layer's alpha (read via
    `layer.composite(viewport=psd.viewbox)` — `.composite()` rather than `.topil()` because
    it applies BOTH a layer's own alpha and any layer mask the user added in Photoshop, and
    the viewport re-expands Photoshop's trimmed layer bounds to the full canvas);
    IMAGE = composite of all OTHER layers (`layer_filter` excluding it by identity).
  - **Writing that transparent layer needs care** (fixed v0.5.16 after a user report of
    "a black layer with a black mask; drawing on it does nothing"): psd-tools decides where
    a layer's alpha goes from the PARENT document's `pil_mode` at `create_pixel_layer` time.
    For an `RGB` document it converts the RGBA source down to RGB — compositing a fully
    transparent source onto BLACK — and re-attaches the discarded alpha as an all-zero
    USER_LAYER_MASK, which Photoshop renders as an opaque black layer behind a mask that
    hides every brush stroke. The write therefore bumps `header.channels` 3→4 for the
    duration of that single call (so `pil_mode` reports `RGBA` and the alpha lands in the
    layer's own TRANSPARENCY_MASK channel `-1`, with no `-2` mask), then restores it before
    `save()` so the file stays a plain 3-channel RGB document with no stray alpha channel.
    Structure — not `composite()` — is what distinguishes the two shapes: `composite()`
    applies the mask and so reports a perfectly transparent layer under EITHER, which is
    exactly why the original test suite missed the bug.
  - **renamed/deleted** → treated as a plain image: IMAGE = full composite, MASK = None →
    falls through to the mask precedence below.
  - **REMOTE Tier 2 now preserves the Instructions layer** (v0.5.34 — was a documented
    limitation: the remote plugin used to upload a flat PNG, so the server-side layered PSD
    was never overwritten and the read above degraded to the empty-`Instructions` case).
    An annotate handoff carries `HandoffMeta.wants_layered_psd=True` (set in
    `_create_handoff`; the ONLY signal distinguishing it from a plain `bridge_node` handoff,
    which shares the origin_kind but writes a flat PSD), echoed to the plugin in
    `open_handoff`. On save in remote mode, a `wants_layered_psd` handoff re-reads the
    sandbox PSD Photoshop's own Cmd/Ctrl+S just wrote (no export/flatten) and uploads those
    BYTES over the existing chunked `upload_edit` WS transport tagged `kind:"psd"` (vs the
    default `kind:"png"`); the server's `_ingest_psd_upload` validates they parse, writes
    them to `manager.psd_path(meta)` — the exact path a local save would overwrite — and
    ingests via the same path the Tier-1 watcher uses, so the layered read above then runs
    identically to local mode. Malformed bytes error (`upload_error reason:invalid_image`)
    before touching disk. Non-annotate handoffs and local mode are byte-for-byte unchanged
    (flat PNG). NB: the actual UXP file-read-after-save, and real multi-MB PSD transfer
    timing, are only provable on Eric's two machines.
- MASK resolution precedence: (1) the PS-mode `Instructions`-layer mask above; (2) else
  the `mask` input socket (ComfyUI-only tier: MaskEditor or any mask source upstream);
  (3) else zeros.
- Mode behavior (v0.5.30 — two Photoshop modes, mirroring §6's bridge node so the user can
  iterate on a drawing identically in either):
  - **"Wait for first save"** (was the only PS mode): on execute with no consumable edit,
    write the LAYERED handoff (origin_kind `bridge_node`), open Photoshop, and BLOCK via
    `manager.wait_for_edit` until the user marks up and saves; then read the saved PSD
    (above). Cancel/timeout/error interrupt via InterruptProcessingException (§8, incl.
    ComfyUI's native cancel).
  - **"Re-run on every save"** (new): the SAME open (factored into `_open_only`, shared with
    the blocking path) but NEVER waits — returns the input + resolved mask immediately. The
    user keeps the Instructions doc open; each save auto-re-queues via `pasteback.js`'s
    `maybeAutoQueue` (gated on this exact mode string, generic over any `bridge_node` node),
    and the re-queue CONSUMES the new mask without relaunching Photoshop. Open failure does
    NOT interrupt in this mode (matches the bridge node's non-blocking modes; surfaced via
    the handoff's `error` status + logs). This is the iterate-on-the-drawing loop.
  - Both Photoshop modes CONSUME an already-arrived edit on re-queue without reopening (so
    downstream iteration doesn't reopen PS every run); once the doc is closed, re-entry is
    the **Re-open in Photoshop button** below, NOT a re-queue.
  - IS_CHANGED returns early only for "Pass through"; both PS modes fold the latest-edit
    hash so an auto-queued re-run actually re-executes with the new mask.
- **Re-open button** (v0.5.30, `web/cpsb/annotate.js`): the Annotate node has no context menu
  (no `node.imgs`; excluded from §8's allowlist), so a `button`-type widget "Re-open in
  Photoshop" (serialize:false) is added on `nodeCreated`. It looks up the node's active
  handoff (`state.getActiveHandoffForNode`) and reopens it through the shared
  `open.openInteractive` with `mode:"original"` — the one path that reopens an EXISTING
  handoff's `psd_path` with no rewrite, so the Instructions layer + painted strokes survive
  (identical to the gallery card's Re-open, now surfaced on the node). No active handoff →
  an info toast, not an error. The open logs a `cpsb annotate:` trail so a "didn't open"
  report is diagnosable.

### 6e. Run Photoshop Action node (v0.5.35)

- Class `PhotoshopAction`, display "Run Photoshop Action", category `image/photoshop`
  (`cpsb/actions.py`; registered as the pack's 5th node). Inputs: `image` (IMAGE); widgets
  `action_name` (STRING) + `action_set` (STRING, default "") — plain strings, NOT a dropdown,
  because UXP cannot enumerate Actions at node-def time; `timeout_seconds` (INT, default
  1800); hidden `unique_id`. Outputs `(IMAGE, MASK)`.
- **Tier-2-plugin-REQUIRED** — the pack's ethos exception (there is no ComfyUI-only or Tier-1
  way to run a saved Photoshop Action). `execute()` calls `routes.tier2_connected` BEFORE
  creating any handoff; with no plugin it raises `InterruptProcessingException` with a clear
  message (install/connect the plugin), never a silent no-op or a Tier-1 fallback that opens
  Photoshop with nothing to run.
- Flow: mirrors the bridge/annotate blocking-consume pattern — create handoff, open via the
  shared `PhotoshopBridge._open_in_photoshop` seam, then `routes.send_run_action` sends the
  new WS message and the node BLOCKS in `wait_for_edit` until the plugin plays the Action,
  exports, and uploads (reusing the existing `deliverEdit` export+upload). Re-queue consumes.
- **Play mechanism** (`photoshop_plugin/runAction.js`): UXP's TYPED action API, not a raw
  batchPlay guess — `app.actionTree` (ActionSet[]) → find the set by name → find the action
  by name → `action.play()`, inside `core.executeAsModal` with `app.activeDocument` set to the
  handoff doc. Confirmed against Adobe's `@adobe-uxp-types/photoshop` declarations + live forum
  usage. **Known risk (the spike):** an Action whose steps include an interactive dialog can
  FREEZE inside `executeAsModal` (PS 23.1+) with no client-side recovery — the node's
  `timeout_seconds` is the only backstop (stops the workflow, can't un-stick Photoshop). Use
  unattended Actions. The actual play + the open/run_action ordering race need live validation.
- **New WS messages**: `run_action` (server→plugin) `{handoff_id, action_name, action_set}`;
  `action_ok` (plugin→server, informational); `action_error` (plugin→server)
  `{handoff_id, error}` → server `manager.mark_error()` (same transition as `open_failed`),
  unblocking the waiter with `WaitOutcome.ERROR` rather than spinning to timeout. The plugin's
  existing generic dispatch forwards these; `connection.js` needed no change.

### 6f. Photoshop Live Canvas node (realtime drawing M1)

`PhotoshopLiveCanvas` (`cpsb/live.py`; docs/roadmap/realtime-drawing.md) serves the newest
save-free canvas snapshot the plugin's Live Mode streams (`live_frame`, §3) as
`(IMAGE, MASK)`. **Tier-2-required** like the Action node, same ethos exception — there is
no Tier-1 equivalent of save-free capture; it interrupts with an actionable log when no
plugin is connected or no frame has streamed yet.

- **No handoffs, no disk, no gallery** — the roadmap's ephemerality commitment. The node's
  entire server-side state is `PluginConnection`'s one keep-latest slot
  (`routes.get_live_frame`), which dies with the connection. None of the
  create/supersede/`wait_for_edit` machinery applies.
- **`IS_CHANGED` = a content hash of the newest frame's bytes** (`"no-frame"` when none)
  — the whole backpressure story on the graph side: a re-queue with no new frame is
  served from ComfyUI's cache, near-free, so the M2 live loop (or a mashed Queue button)
  can fire liberally. A hash, deliberately NOT the frame counter: the counter restarts
  with each plugin connection, so a counter key would alias across reconnects and serve
  the OLD session's cached render for a new drawing (review-caught 2026-07-24).
  Identical bytes hashing identically is the correct cache hit — same canvas, same
  render.
- **`auto_queue` widget (`On`/`Off`)** is read CLIENT-side only (`web/cpsb/live.js`, M2 —
  the same frontend-reads-the-widget gating as the bridge node's `mode` in `pasteback.js`):
  `On` auto-queues a coalesced re-run per arriving frame; `Off` still streams, the next
  manual queue picks up the newest.
- **MASK is always zeros**: the wire format is JPEG (UXP's `encodeImageData` is
  documented JPEG-only), which cannot carry alpha; the output exists for wiring parity.
- Plugin control surface: the panel's **LIVE MODE** section (`Start Live`/`Stop Live`,
  status line) — one session at a time, watching the document that was active when
  started; auto-stops when that document closes. Capture cadence: ~300ms history-id poll,
  768px long-side `targetSize`, `dispose()` after every capture (`liveMode.js`'s doc
  comment carries the research/spike provenance — the history-id poll's stroke-promptness
  is spike S-A, owner-verified via the checklist).
- **`PhotoshopLivePreview`** (M3, same module): the loop's feedback surface. An OUTPUT
  node (IMAGE in, no return sockets) that JPEG-encodes each render (quality 85) and
  pushes it as `result_frame` (§3) to the plugin's **"ComfyUI Preview" panel** — a second
  manifest panel entrypoint (documented multi-panel; one shared JS context, so it reuses
  the same connection singleton), dockable beside the canvas. Deliberately NOT
  Tier-2-gated with an interrupt: it runs at the END of a render, so a dropped plugin is
  a logged no-op — killing a finished render over a missing preview surface would be
  worse than the missing preview (the CANVAS node already gates the pipeline's start).
  Multi-panel mount caveats (show-fires-once, node-carrying shape variance) are handled
  in `previewPanel.js`/`index.js` per the community-verified example; the img-refresh
  rate is roadmap spike S-C, owner-verified via the checklist.

## 7. Photoshop discovery & launch (Tier 1, backend)

Order: settings `photoshop_path` override → platform discovery → error.
- macOS: `open -b com.adobe.Photoshop <psd>`; on failure enumerate installed apps via
  `mdfind "kMDItemCFBundleIdentifier == 'com.adobe.Photoshop'"`, prefer the highest
  year/version, `open -a <app path> <psd>`.
- Windows: `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Photoshop.exe`,
  else enumerate `HKLM\SOFTWARE\Adobe\Photoshop\<ver>` `ApplicationPath`, newest first;
  launch `Popen([exe, psd])`. `os.startfile(psd)` only as a last resort (association may
  not be Photoshop — surface a warning in the response so the frontend can toast it).
- Tier 1 gating (`tier1_available:false`, with `tier1_reason` ∈ `"headless-server"` |
  `"docker"` | `"wsl"` | `null`): Linux without `DISPLAY`/`WAYLAND_DISPLAY`;
  `/.dockerenv` present; WSL (`microsoft` in `platform.release().lower()`).
  "No Photoshop installed" is NOT a gating reason — it is only discoverable at launch
  time and surfaces through the launch-failure path (§2 note). The frontend must NOT
  hard-disable Tier 1 based on `window.location.hostname` — a non-localhost hostname
  (e.g. ComfyUI under `--listen`, browsed via a LAN address on the same machine) does
  not imply the client is elsewhere; hostname cannot distinguish the two cases at all.
  Launch calls are blocking subprocess work and MUST run off the event loop
  (`asyncio.to_thread`) when invoked from route/websocket handlers.

**Client locality (the authority on "is the browser on the server's machine"):** the
server decides, deterministically, per request: the requester is local iff the HTTP
request's peer address is an address this machine owns — tested by attempting to bind
a throwaway socket to `request.remote` (bind succeeds only for locally-owned
addresses; handles loopback AND the same-machine-via-LAN-address case that hostname
checks get wrong). If a forwarding header (`X-Forwarded-For`/`X-Real-IP`) is present,
the peer address is a proxy's, so the client is treated as remote/unknown (fails
safe into the §2 confirm flow). Non-local clients don't lose Tier 1 — they get the
§2 `428 client_remote` confirm ("Photoshop will open on <server_name>") with an
explicit, per-browser-remembered opt-in, because launching on the server's screen is
useless to someone sitting elsewhere but legitimate for VNC/dual-screen setups.

---

## 8. Frontend ↔ backend conventions

- All frontend calls go through `api.fetchApi("/cpsb/...")` (ComfyUI's wrapper — it
  handles the api prefix and client id).
- The context-menu integration registers via `getNodeMenuItems` when available,
  falling back to a `getExtraMenuOptions` monkeypatch on older frontends (spike §8-1
  will confirm image-region behavior; menu items appear regardless via the node menu).
- Menu items offered on any node whose `node.imgs` is non-empty: "Open in Photoshop"
  (no active handoff), or "Edit Original in Photoshop" + "Start Fresh in Photoshop"
  (active handoff exists — tracked client-side from `cpsb.status` events + initial
  `/cpsb/status` fetch).
- **Source-identity gate on the submenu** (v0.5.23 — user report: unexpected nodes showed
  Edit Original/Start Fresh, and clicking it "opened a different psd"). The client-side
  lookup matches by node id + workflow name only, and the workflow check wildcards when
  EITHER name is empty (unsaved workflow) — so a stale-but-ACTIVE handoff from an earlier
  session or another workflow could latch onto whatever node now holds that id, and
  `mode:"original"` reopens the STALE handoff's stored PSD unconditionally. menu.js now
  additionally requires the handoff's recorded `source` to match the node's CURRENT
  content before offering the submenu: `load_psd` → source.filename equals the node's psd
  combo selection; `load_image`/`terminal_output` → the source triple matches ANY of
  `node.imgs` (batches are real); `bridge_node` → no gate (its source.psd is
  generated/managed — node identity is the correct key and staleness is handled by
  server-side supersede); unknown origin_kind or missing source → fail OPEN, so a
  version-skewed server never silently kills the menu. On rejection the node gets the
  plain "Open in Photoshop", which opens a NEW handoff for what the node actually shows.
  In-flight edits are unaffected: pasteback keys strictly by handoff_id, never by this
  menu decision. Residual known gap: a stale bridge_node handoff cross-matching via the
  empty-workflow-name wildcard is still offered (no pixel identity is knowable client-side).
- Batch nodes (`node.imgs.length > 1`): open the **currently displayed** image
  (`node.imageIndex ?? 0`); an "Open all N in Photoshop" item appears for N ≤ 8.
- Frontend settings (ComfyUI settings API, ids): `cpsb.autoQueue` (bool, default true),
  `cpsb.showUpgradeBanner` (bool, default true).
- **Live loop** (`web/cpsb/live.js`, realtime drawing M2): each `cpsb.live` event queues at
  most ONE coalesced `queuePrompt(0)` — armed only while the current graph contains an
  ACTIVE (non-muted/bypassed) `PhotoshopLiveCanvas` node with `auto_queue` = "On",
  searched recursively through subgraphs (widget read client-side per event, the same
  gating convention as `pasteback.js`'s bridge-mode check). Single-slot backpressure
  (common case; a narrowly-raced extra queue between `queuePrompt` resolving and the
  next status event is absorbed by IS_CHANGED caching): while ComfyUI is busy (tracked
  via its own `status` event's `exec_info.queue_remaining`) new frames set a `trailing`
  flag instead of stacking queues; one run fires when the queue drains and picks up
  whatever frame is newest by then — intermediate frames are deliberately never
  rendered. Deliberately event-driven, not Auto-Queue "Instant" (which still works, via
  IS_CHANGED caching, but busy-loops).
- **Gallery** (v0.5.36; overhauled 2026-07-22): each card leads with ONE larger
  thumbnail — the latest edit, or the original when no edit exists yet — with the
  original layered underneath for comparison. Grid is the gallery's ONLY layout (the
  former List alternative and its header toggle, `cpsb.galleryGridLayout`, were
  removed); a single header-level "Hold to compare" button (Pointer Events; mouse and
  touch) reveals every visible card's original at once while held, replacing a prior
  per-thumbnail hold gesture. Cards whose status is `cancelled`, `discarded`, or
  `superseded` are hidden from the list entirely (alongside `error`, which stays
  visible — diagnostic, not routine bookkeeping); an `editing` card whose Tier-2 plugin
  has reported the document closed (`document_closed` above, `plugin_doc_open`) shows
  as "Closed without saving" instead of guessing from elapsed time. Card action labels
  are **Reveal** (center the origin node in the graph), **Open** (Re-open/Open-fresh-
  copy, mutually exclusive per card), **Add** (add the latest edit as a Load Image
  node), **Cancel**, and **Remove**. The corresponding per-node canvas badge
  (`badges.js`) no longer flashes its own "Edited" checkmark on completion — the
  gallery's chip is the only completion signal now, decluttering the node canvas.

---

## 9. Versioning

Backend, frontend JS, and plugin each carry a semver string; `hello`/`hello_ack`
exchange them. During 0.x, a minor-version mismatch between plugin and backend logs a
console warning and shows a gallery banner ("Photoshop panel v0.1.0 ≠ server v0.2.0 —
update the plugin") but does not refuse the connection.
