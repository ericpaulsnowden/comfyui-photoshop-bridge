# M0 Verification Spikes

Before locking in the M1 implementation, we're validating eight assumptions the whole design leans on. Each one is small, throwaway, and answerable in isolation — pick one, follow its procedure, fill in its Results section, and check it off below.

**Status as of 2026-07-20:** these M0 spikes have been overtaken by production. The pack shipped through **v0.5.30** — four nodes, the Tier-1 and Tier-2 round trips, and cross-machine (Mac Photoshop ↔ PC ComfyUI) editing — and Eric has personally validated the whole feature set on his own machines (50/50 manual-test items). Spikes 2, 6, 7 were proven live on Photoshop 27.8.0 (2026-07-17; see **LIVE RESULTS** at the bottom). Spikes **1, 3, 4, 5 are now confirmed in production** — the right-click menu, psd-tools PSD write fidelity, the save-detection watcher (Windows on Eric's PC + macOS in dev), and paste-back + auto-queue all ship and are validated by real use, not a throwaway spike. The one genuinely open item is **spike 8 (programmatic Maximize Compatibility)** — in progress as an auto-set-on-connect feature, pending live validation.

The rule: **M1 design isn't final until every spike below has a written yes/no/workaround answer.**

This project is pre-release — see the status note at the top of the [main README](../README.md). The design assumptions these spikes were written to de-risk are now settled by the shipped, user-validated v0.5.30 feature set; only spike 8 (Maximize Compatibility) is still being proven live.

## Prerequisites

- **ComfyUI**: a recent build of the current Vue-based frontend (the one exposing `registerSidebarTab`, `getNodeMenuItems`, and the Settings API — 2025 or newer).
- **Photoshop 2025 (v26) or later.**
- **UXP Developer Tool (UDT)** — required for spikes 2, 6, 7, and 8, which load a throwaway plugin directly into Photoshop without going through a `.ccx` install.
- For spikes touching the ComfyUI side (1, 3, 4, 5): a working ComfyUI dev install with this repo cloned into `custom_nodes/` — see [../CONTRIBUTING.md](../CONTRIBUTING.md).

## Checklist

- [x] 1. Image-widget right-click menu hook — **CONFIRMED IN PRODUCTION** (the right-click "Open in Photoshop" menu ships and is validated; PROTOCOL.md §8)
- [x] 2. UXP localhost WebSocket on target Photoshop versions — **PASS** (2026-07-17, PS 27.8.0; see LIVE RESULTS)
- [x] 3. PSD write fidelity via `psd-tools` `frompil` — **CONFIRMED IN PRODUCTION** (compose/load/annotate all write PSDs that round-trip; validated live)
- [x] 4. Watchdog event pattern for Photoshop saves — **CONFIRMED IN PRODUCTION** (Tier-1 save detection validated on Eric's Windows PC + macOS in dev)
- [x] 5. Clipspace-style paste-back + auto-queue — **CONFIRMED IN PRODUCTION** (edits paste back to the origin node; auto-queue-on-save validated, incl. annotate re-run mode)
- [x] 6. Silent PNG export via `batchPlay` — **PASS** (2026-07-17, PS 27.8.0; see LIVE RESULTS)
- [x] 7. Save-event listener + document identity — **PASS** (2026-07-17, PS 27.8.0; see LIVE RESULTS)
- [ ] 8. Programmatic Maximize Compatibility preference — **IN PROGRESS** (auto-set-on-connect feature; pending live validation on Eric's Photoshop)

---

## Spike 1 — Image-widget right-click menu hook

**Goal:** determine whether `getNodeMenuItems` (current API) or the legacy `getExtraMenuOptions` monkeypatch intercepts a right-click landing directly on an image thumbnail, versus a right-click on the node body generally.

**Gates:** whether "Open in Photoshop" can be a single menu item scoped precisely to the clicked image, versus a node-body-scoped item (the fallback — still fully functional, just less visually precise; see [PROTOCOL.md](PROTOCOL.md) §8 for how the menu item is offered either way).

**Procedure:**
1. Create a throwaway extension folder (e.g. `custom_nodes/_spike1_menu/`) with a minimal `__init__.py` and a JS file wired up via `WEB_DIRECTORY`.
2. In the JS file, register:
   ```js
   app.registerExtension({
     name: "spike1.menu",
     getNodeMenuItems(node) {
       console.log("getNodeMenuItems fired on", node.comfyClass);
       return [{ content: "Spike1: getNodeMenuItems", callback: () => alert("getNodeMenuItems fired") }];
     },
   });
   ```
3. Also patch the legacy hook — in `beforeRegisterNodeDef` (or `nodeCreated`), wrap `nodeType.prototype.getExtraMenuOptions` to log and append a second test item, "Spike1: getExtraMenuOptions".
4. Build a test workflow with a `LoadImage` node (with an image already loaded, so its thumbnail is visible), a `PreviewImage` node, and a `SaveImage` node with output images.
5. Right-click directly on the image thumbnail of the `LoadImage` node. Record which console log(s) fired and which test item(s) appear in the resulting menu.
6. Repeat step 5 on the `PreviewImage` and `SaveImage` nodes.
7. Repeat again for all three node types, but right-click the node's title bar / empty body area instead of the thumbnail, and record whether the result differs from steps 5-6.
8. Note the exact ComfyUI frontend version tested (visible in the About panel) — this is an area of recent upstream churn.

**Status:**
- [x] **CONFIRMED IN PRODUCTION** — the "Open in Photoshop" right-click menu shipped and is validated in live use (see `web/cpsb/menu.js`, PROTOCOL.md §8). A dedicated throwaway spike was never needed once the real menu landed.

### Results
_Date, frontend version tested, findings, and the decision (single image-scoped item vs. node-body-scoped fallback) go here._

---

## Spike 2 — UXP localhost WebSocket on target Photoshop versions

**Goal:** confirm a minimal UXP plugin with an sd-ppp-style manifest (`network.domains: ["http://127.0.0.1:8188", "ws://127.0.0.1:8188"]`) can open a WebSocket to a throwaway server without a Permission Denied error, on every supported Photoshop version.

**Gates:** Tier 2 viability at all (this is the single point of failure called out in the risk list) — plus, separately, whether unsigned `.ccx` installs require Developer Mode.

**Procedure:**
1. Scaffold a minimal UXP plugin: manifest v5, `requiredPermissions.network.domains` set to exactly `["http://127.0.0.1:8188", "ws://127.0.0.1:8188"]`, one panel entrypoint.
2. Load it via UXP Developer Tool → Add Plugin → point at the manifest → Load.
3. Stand up a throwaway aiohttp WebSocket route (a scratch ComfyUI custom node, or a bare script) on `127.0.0.1:8188` that accepts a connection and echoes any message it receives.
4. From the plugin panel's JS, open the WebSocket, send a test message once it's open, and log the echo.
5. Record: does the connection open without a Permission Denied error? Does it stay open for at least 5 minutes idle? Does it survive switching to a different panel and back (panel hide/show)?
6. Repeat on every Photoshop major version in the supported range (at minimum v26; add later versions as you have access).
7. Separately, read Adobe's Developer Distribution documentation (developer.adobe.com) and record whether it confirms unsigned `.ccx` plugins require Developer Mode for end users, or whether Developer Distribution signing avoids that requirement. Cite the URL and the date checked.

**Status:**
- [x] **PASS** — verified live 2026-07-17 on Photoshop 27.8.0 (macOS). See LIVE RESULTS at the bottom of this file.

### Results
**PASS, verified live 2026-07-17 (PS 27.8.0, macOS)** — full detail in the LIVE RESULTS section below. The WebSocket opens with no Permission Denied error and the Tier-2 plugin round-trips end to end. Still open within this spike's original scope: the per-Photoshop-version sweep beyond 27.8, narrowing `network.domains` from `"all"` to a scoped host for release, and the unsigned-`.ccx` / Developer Mode documentation check.

---

## Spike 3 — PSD write fidelity via `psd-tools` `frompil`

**Goal:** confirm that a flat PSD written via `psd_tools.PSDImage.frompil()` from a representative ComfyUI image opens cleanly in Photoshop, with matching colors, and behaves as documented on a plain save.

**Gates:** whether `psd-tools` is sufficient for the write path, or whether M1 needs an alternative PSD-writing approach.

**Procedure:**
1. Pick 3-5 representative test images: a typical photorealistic generation, an image with fine gradients, an image with an alpha channel (if applicable to your pipeline), and at least one large image (4096px+ on the long side).
2. For each, load as a PIL image and call `psd_tools.PSDImage.frompil(pil_image).save(path)`.
3. Open each resulting `.psd` in the target Photoshop version. Record: any "corrupt or damaged" warning on open; a visual/color comparison against the source PNG; confirmation it opens as a single flattened layer.
4. With the file open and unmodified, do a plain Cmd/Ctrl+S. Record whether it saves silently (if Maximize Compatibility is "Always") or prompts (if "Ask"), and that the file still opens correctly afterward.
5. Add one visible edit (a new layer or adjustment) to one test file, save with Cmd/Ctrl+S, and confirm it still opens correctly.
6. For any failure (corruption, visible color shift), note the exact failure and try one alternative write path (e.g. a different library, or hand-writing a minimal valid PSD) to see if it resolves the issue.

**Status:**
- [x] **CONFIRMED IN PRODUCTION** — psd-tools `frompil`/`create_pixel_layer` writes drive compose, load, and annotate; round trips are validated live (incl. the v0.5.28 CMYK fidelity fix).

### Results
_Per-test-image outcome, any color/corruption issues, and the sufficiency verdict on `psd-tools` go here._

---

## Spike 4 — Watchdog event pattern for Photoshop saves (macOS + Windows)

**Goal:** log the exact sequence and timing of filesystem events a `watchdog.Observer` sees while Photoshop repeatedly saves a PSD, on both macOS and Windows.

**Gates:** the real debounce window (replacing the current 800ms placeholder) and whether Photoshop's save is an in-place write (`on_modified`) or a temp-file-then-rename (`on_created`/`on_moved`) on each OS — the implementation needs to handle whichever pattern actually occurs.

**Procedure:**
1. Write a throwaway script that starts a `watchdog.Observer` on a test folder and logs every event (`on_created`, `on_modified`, `on_moved`, `on_deleted`) with a high-resolution timestamp and path.
2. Place a test PSD (from spike 3) in the folder and open it in Photoshop.
3. Run each of these, starting a fresh log each time: (a) plain Cmd/Ctrl+S on a flat PSD; (b) add a layer, then Cmd/Ctrl+S with Maximize Compatibility set to Always (no dialog); (c) same, but with Maximize Compatibility set to Ask, clicking through the dialog; (d) two saves within 2 seconds of each other; (e) a large PSD (4096px+) save, timed for how long events take to settle.
4. For each run, record the full event sequence and the elapsed time between the first event and the point the file's mtime stops changing.
5. Repeat the entire sequence on both macOS and Windows.
6. From the logs, determine: does Photoshop write in place or via temp-file-then-rename, per OS? What debounce window covers the slowest observed case with reasonable margin?

**Status:**
- [x] **CONFIRMED IN PRODUCTION** — the Tier-1 watchdog save-detection ships and is validated on Eric's Windows PC and in macOS dev; the lock-ordering fix in v0.5.26 hardened it further.

### Results
_Per-OS event pattern, measured timings, and the resulting debounce window go here._

---

## Spike 5 — Clipspace-style paste-back + auto-queue

**Goal:** confirm, hands-on, that setting a `LoadImage` widget's value and calling its callback correctly refreshes the node's displayed image with no side effects, and that `app.queuePrompt()` behaves as expected immediately afterward.

**Gates:** the exact paste-back call sequence the frontend uses; any surprise requirement (e.g. a `setDirtyCanvas` call, or extra state needed before `queuePrompt`) gets absorbed into M1 instead of discovered in production.

**Procedure:**
1. In a throwaway extension, get a reference to an existing `LoadImage` node (`app.graph.getNodeById(...)`).
2. Find its image widget, set `widget.value` to a different, already-uploaded filename (`"<filename> [input]"`), and call `widget.callback?.(widget.value)`.
3. Also set `app.nodeOutputs[node.id]` directly to point at the new image.
4. Visually confirm the node's thumbnail updates without any manual canvas interaction. If it doesn't, try adding a canvas redraw call (e.g. `app.graph.setDirtyCanvas(true, true)`) and note whether that was required.
5. Immediately after, call `app.queuePrompt()` and confirm the queued prompt uses the updated widget value (check prompt history / server logs for the new filename) rather than a stale cached one.
6. Repeat with the node embedded partway through a multi-node workflow; confirm only the changed node and its downstream re-execute, not the whole graph.
7. Note the ComfyUI frontend version tested.

**Status:**
- [x] **CONFIRMED IN PRODUCTION** — paste-back to the origin node and auto-queue-on-save both ship and are validated (including the annotate "Re-run on every save" loop in v0.5.30).

### Results
_Confirmed call sequence (including any extra calls needed) and queuePrompt behavior go here._

---

## Spike 6 — Silent PNG export via `batchPlay`

**Goal:** confirm that duplicating the active document, flattening the duplicate, and exporting via a `batchPlay` descriptor with `dialogOptions: "dontDisplay"` produces a correct PNG with zero dialogs, across a range of document sizes and modes, run at least 50 times.

**Gates:** the Tier 2 primary export path. If unreliable, the Imaging API's `getPixels()` is promoted from fallback to primary, and its known Grayscale/LAB alpha bug becomes a documented limitation rather than an edge case. Per the risk list, Tier 2 does not proceed past M4 planning with zero proven export paths.

**Procedure:**
1. In a UXP plugin (UXP Developer Tool), implement: duplicate the active document, `flatten()` the duplicate, then build a `batchPlay` export/save descriptor targeting a PNG path in the plugin's sandbox with `dialogOptions: "dontDisplay"`.
2. Build a test matrix: at least 3 document sizes (e.g. 512x512, 2048x2048, 4096x4096+) crossed with at least 3 configurations (flat RGB, multi-layer RGB with adjustment layers, RGBA with transparency).
3. Run the export at least 50 times across the matrix (a test-panel button that loops N times is fine), logging: whether any dialog appeared (should be zero), export duration, and whether the resulting PNG opens correctly and matches the document.
4. Deliberately stress it: two exports back-to-back with no delay; export with unsaved changes present; export immediately following a Photoshop save event (the real sequence Tier 2 uses in production).
5. For any failure (dialog, corrupt file, thrown error), record the exact descriptor and error, then implement and test the Imaging API `getPixels()` fallback against the same matrix.
6. If the lean descriptor fails, re-test with the `saveStage` field the SuperPNG forum example carried (`saveStage: {_enum: 'saveStageType', _value: 'saveBegin'}`) before concluding batchPlay export is unviable.

**Status:**
- [x] **PASS** — verified live 2026-07-17 on Photoshop 27.8.0 (macOS). See LIVE RESULTS at the bottom of this file.

### Results
**PASS, verified live 2026-07-17 (PS 27.8.0, macOS)** — full detail in the LIVE RESULTS section below. The flatten-duplicate → `batchPlay` saveAs-PNG (`dialogOptions: "dontDisplay"`) → upload pipeline produced a pixel-accurate PNG with zero dialogs; the Imaging API `getPixels()` fallback was not needed. Still open within this spike's original scope: the full size × mode matrix and the 50×-run reliability count.

---

## Spike 7 — Save-event listener + document identity

**Goal:** confirm `action.addNotificationListener(['save'], ...)` fires on a plain Cmd/Ctrl+S, and that the event descriptor reliably identifies which document was saved — including with multiple documents open, only some of which are ours.

**Gates:** the entire Tier 2 save-matching design. If the descriptor doesn't reliably carry document identity, the fallback is reading `app.activeDocument` at event time, and this spike also needs to test whether that fallback is race-safe.

**Procedure:**
1. In a UXP plugin, register `action.addNotificationListener(['save'], (event, descriptor) => console.log(JSON.stringify(descriptor)))`. Note the signature: Adobe's current reference documents `addNotificationListener(events: string[], callback)` — the older object-array form (`[{event: 'save'}]`) appears in community examples and should not be used.
2. Open one test document, do a plain Cmd/Ctrl+S, and record the full descriptor — specifically whether it includes a document ID, file path, or title usable to match against a document we opened ourselves.
3. Open three documents at once: at least one opened via our own `app.open()` call (so we know its expected document ID), and at least one unrelated pre-existing document. Save each individually and confirm the descriptor for each event correctly identifies which document was saved.
4. Try Save As on one document; record whether the event still fires as `save` (or differently), and whether the descriptor's identity fields reflect the new path.
5. If the descriptor's identity is missing or unreliable in any case, implement the `app.activeDocument`-at-event-time fallback, then test its race-safety: trigger saves on two different documents in rapid succession (switching the active document immediately after triggering the first save) and check whether the callback ever reports the wrong document.

**Status:**
- [x] **PASS** — verified live 2026-07-17 on Photoshop 27.8.0 (macOS). See LIVE RESULTS at the bottom of this file.

### Results
**PASS, verified live 2026-07-17 (PS 27.8.0, macOS)** — full detail in the LIVE RESULTS section below. The `action` save listener fired on a plain Cmd+S, matched the tracked document, and triggered the export with no spurious fires. Still open within this spike's original scope: the multi-document identity test (three docs open at once) and the Save As / `app.activeDocument`-fallback race-safety checks.

---

## Spike 8 — Programmatic Maximize Compatibility preference

**Goal:** determine whether `batchPlay` can read and set Photoshop's Maximize PSD Compatibility preference to Always.

**Gates:** whether Tier 2 can honestly claim zero recurring save dialogs, and whether Tier 1's onboarding can offer a "fix it for me" action once the plugin is connected — versus keeping the manual preference nudge as a permanent requirement for both tiers.

**Procedure:**
1. In a UXP plugin, find the `batchPlay` descriptor that reads the current Maximize PSD Compatibility preference (start from Photoshop's ExtendScript reference for the equivalent preference property, then translate to a batchPlay `get`).
2. Set the preference to Always via a `batchPlay` `set` descriptor. Verify two ways: programmatically re-reading it, and manually checking Preferences → File Handling shows Always.
3. Starting from "Ask", set it to Always, then save a layered PSD and confirm no dialog appears.
4. Set it back to Ask and confirm the read-back reflects that too (round-trip both directions).
5. Confirm this doesn't require any permission beyond the plugin's existing manifest, and that the change persists across a Photoshop restart.

**Status:**
- [ ] **IN PROGRESS** — being implemented as an auto-set-Maximize-Compatibility-on-connect plugin feature; the batchPlay descriptor still needs live validation on Eric's Photoshop.

### Results
_Whether get/set worked, the exact descriptors used, and the final yes/no on automatic preference-setting go here._

---

## LIVE RESULTS (2026-07-17, Photoshop 27.8.0 on macOS, against scratch ComfyUI)

Driven directly by Claude via UXP Developer Tool + computer-use; not user-reported.

- **Spike 2 — UXP localhost WebSocket permission: PASS (was the #1 project risk).**
  Root fixes: (a) plugin files must sit at the plugin ROOT next to manifest.json, not
  in a src/ subfolder (UXP resolves the main document's relative sub-resources against
  the plugin root — nesting made panel.css/index.js 404, so the panel rendered bare and
  never even attempted a connection); (b) `requiredPermissions.network.domains` must be
  the STRING `"all"` — the array form fails to parse in current UXP, yielding exactly
  "Permission denied ... Manifest entry not found"; (c) connect to `ws://localhost:8188`,
  NOT `ws://127.0.0.1:8188` — UXP disallows raw-IP data transfers. FOLLOW-UP: try to
  narrow `"all"` to a scoped hostname allowance for the public release (least privilege).
- **Spike 6 — batchPlay silent PNG export ("historically fiddly"): PASS.** A plain
  Cmd+S on the opened handoff triggered the save listener; the flatten-duplicate →
  batchPlay saveAs-PNG (dialogOptions dontDisplay) → upload pipeline produced a
  pixel-accurate PNG. No dialog. fidelity="plugin".
- **Spike 7 — save-event listener + document identity: PASS.** action save listener
  fired on Cmd+S, matched the tracked document, ran the export. No spurious fires.
- **Full Tier-2 round trip PROVEN:** API open (tier 2) → WS open_handoff → PS opened the
  PSD → invert + Cmd+S → edit ingested, status "edited", pixels exactly the inverted
  image (center (25,55,195) blue, corner (215,165,75) tan). ps_version 27.8.0 exchanged
  via hello handshake; tier2_connected=true server-side.
