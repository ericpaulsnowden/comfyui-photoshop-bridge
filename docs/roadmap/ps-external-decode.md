# Roadmap — Photoshop-routed external-format decode

**Status:** planned (research done 2026-07-21; not yet built)
**Owner decisions (Eric, 2026-07-21):**
- Lives in a **new dedicated Tier-2 node**, *not* an extension of Load PSD. Load PSD stays ComfyUI-only (PSD/PSB/TIFF, instant, no plugin).
- **`.ai` first (flagship). `.dng` is a later, spike-gated milestone.**
- **No third-party Python decoders** (`pypdfium2`/`rawpy`) — the whole reason this roadmap exists. Photoshop itself does the decode.

## Why this exists

Eric asked to load `.ai` / `.dng` but does not want third-party Python libraries doing it. Photoshop can open both natively (`.ai` via its embedded-PDF engine; `.dng` via Adobe Camera Raw). So instead of bundling decoders, a Tier-2 node hands the file to the connected Photoshop plugin, Photoshop opens + rasterizes it, and the raster rides the existing edit-upload path back to the graph. This is the pack's ethos exactly: *"the PS plugin is a BETTER tier, never the only tier — except when a ComfyUI-only version is impossible/undesirable."* Same class of exception as the Run Action node.

## Market position (researched 2026-07-21)

- **`.ai` — genuinely novel + valuable.** No ComfyUI tool decodes any file by routing it through a live host app; every `.ai`-related tool uses `pypdfium2`/Ghostscript/PyMuPDF, or asks the user to pre-convert. The host-app-as-rasterizer pattern only exists in automation bridges (photoshop-mcp via ExtendScript; indesign-uxp-server), never as an image-decode codec feeding a node graph. **Value: fidelity** — matches exactly what Photoshop renders (fonts, effects, color management, live appearance), which a PDF-stream rasterizer cannot.
- **`.dng` — largely redundant, keep only for fidelity.** Raw loading in ComfyUI is *solved*: `comfyui-raw-image`, `ComfyUI-zveroboy-photo`, and especially **ComfyUI-Darkroom** (54-node suite: demosaic/WB/colorspace/film emulation) all cover `.dng` via `rawpy`/LibRaw. A PS-routed decode only differs by: (a) bit-for-bit match to Adobe's proprietary demosaic/color, (b) inheriting existing ACR edits/`.xmp` sidecars, (c) no third-party dep in *this* pack, (d) faster new-camera coverage. For "just get raw pixels in," it adds nothing. → **lower priority, spike-gated, framed as the PS-fidelity niche.**

## Architecture (the Run Action node is the template)

The closest existing pattern is `PhotoshopAction` (`cpsb/actions.py` + `photoshop_plugin/runAction.js`): a Tier-2-required node that creates a handoff, opens Photoshop through the shared seam, sends a **second** bounded cross-thread trigger message, blocks in `wait_for_edit`, and consumes via the shared tail. Reuse directly:

| Concern | Reuse |
| --- | --- |
| Tier-2 gate, no fallback | `routes.tier2_connected(...)` + `nodes._raise_interrupt()` (`cpsb/actions.py:235`) |
| Open in Photoshop | `nodes.PhotoshopBridge._open_in_photoshop` → `_send_tier2_open` (`cpsb/nodes.py:516,601`) |
| Point the handoff at the ORIGINAL file (not a managed PSD copy) | the `edit_in_place`/`original_path` mechanic already in `cpsb/routes.py` for `origin_kind:"load_psd"` (`_psd_path_for_handoff`, `ResolvedSource.original_path`) — applied unconditionally for the new origin kind |
| New trigger message | model on `routes.send_run_action` (`cpsb/routes.py:829`) + `_RUN_ACTION_SEND_TIMEOUT_SECONDS` bound |
| Block + consume | `manager.wait_for_edit(...)` then `nodes.PhotoshopBridge._load_edit_tensors(...)` (`cpsb/nodes.py:659`) |
| Plugin handler shape | `photoshop_plugin/runAction.js` (`waitForHandoffRecord` poll, `executeAsModal`, `deliverEdit`, `action_ok`/`action_error` ack) |
| Result upload | `handoffs.js` `deliverEdit` + `uploader.js` `uploadEdit` (flat PNG, `kind:"png"`) — unchanged |

**The one genuinely new piece:** the plugin must NOT use plain `app.open()` for `.ai`/`.dng` — the DOM path pops the Import-PDF / Camera-Raw dialog and freezes headless automation. It must use the low-level `batchPlay` `open` descriptors below, inside `executeAsModal`, with a watchdog timeout. Also, a foreign-file open fires **no save event**, so a new `open_external`/`rasterize` trigger message tells the plugin "open + flatten + deliver now."

### `.ai` open descriptor (community-verified headless path)
`batchPlay` `open` with the file wrapped in a nested `PDFGenericFormat` descriptor — every rasterization field fully specified (crop/box, resolution, mode, depth, antiAliasing, width, height, constrainProportions, `suppressWarnings:true`, selection+pageNumber), `_options:{dialogOptions:'silent'}`, wrapped in `core.executeAsModal`. An omitted field is what forces the interactive dialog. Requires the `.ai` to have been saved with "Create PDF Compatible File" (Illustrator default). File target via `fs.createSessionToken(entry)`, not a raw path string.
*(citation: community.adobe.com/questions-712/photoshop-script-import-pdf-application-open-without-dialog-1128527)*

### `.dng` open descriptor (M3)
Either legacy `app.open(entry, CameraRAWOpenOptions, asSmartObject=false)` (pins `bitsPerChannel`, `colorSpace`, `resolution`, `settings:CAMERA`) or `batchPlay` `open` with `overrideOpen:true` + `as:{_obj:"Adobe Camera Raw", settings:"camera"}` + `_options:{dialogOptions:'silent'}`, in `executeAsModal`. Result is **16-bit by default** → force 8-bit + pinned colorspace for pipeline determinism. Descriptor keys are ScriptListener-reverse-engineered — validate per Photoshop version with Alchemist.
*(citations: theiviaxx.github.io/photoshop-docs/Photoshop/CameraRAWOpenOptions.html; forums.creativeclouddeveloper.com/t/…/8291)*

---

## Milestones (each independently useful)

### M1 — Clean the accepted-format surface *(ships immediately; no plugin, no risk)*
**User value:** TIFF upload works from the Load PSD button (Eric's exact ask); no third-party deps anywhere; the format list is honest.
- Remove `pypdfium2`/`rawpy` from `cpsb/raster_io.py` (`AI_EXTENSIONS`, `RAW_EXTENSIONS`, `pypdfium2_available`, `rawpy_available`, `_decode_ai`, `_decode_raw`, `_AI_RENDER_SCALE`, `_optional_module`; collapse `available_extensions()`/`decode_to_rgb8()` to TIFF-only). Keep all TIFF code + `EDIT_IN_PLACE_CAPABLE_EXTENSIONS`.
- Remove the `.ai`/`.dng` tests (`tests/test_psd_io.py` `TestDecodeAi`/`TestDecodeRaw` + their fixture helpers; `tests/test_load_psd.py` the AI/raw listing+validate+dependency-error tests). Keep all TIFF tests.
- Prune `.ai`/`.dng` mentions from `requirements.txt`, `README.md`, `docs/PROTOCOL.md` §6b, and the `load_psd.py`/`raster_io.py` docstrings.
- **Load PSD upload widget** (`web/cpsb/loadpsd.js`): `ACCEPTED_EXTENSIONS` `['.psd','.psb']` → `['.psd','.psb','.tif','.tiff']` (feeds `ACCEPT_ATTR`); button label `'Choose PSD to upload'` → **`'Upload Photoshop File'`** (`:611`); add a plain-text caption under the button enumerating supported formats; reject toast `'Not a PSD/PSB file'` → wording covering PSD/PSB/TIFF (`:214-222`).
- **Release note:** `.ai`/`.dng` no longer load via Load PSD — that capability moves to the new node (M2/M3). A saved workflow pointing Load PSD at a `.ai`/`.dng` will stop resolving it.
- **Acceptance:** tests green on both interpreters; TIFF selectable via combo AND the upload button; button reads "Upload Photoshop File" with the format list beneath it.

### M2 — "Open via Photoshop" node for `.ai` *(flagship; Tier-2; validated live)*
**User value:** load an Illustrator `.ai` into ComfyUI with Photoshop's exact rendering, no third-party lib — a capability nothing else in the ComfyUI ecosystem has.
- New Tier-2 node (working name **`PhotoshopOpen` / "Open via Photoshop"**), `cpsb/open_external.py`, modeled on `cpsb/actions.py`. Inputs: a file picker (combo of `.ai` files in the input dir, hand-rolled upload widget like Load PSD, accept `.ai`), `timeout_seconds`. Output `(IMAGE, MASK)`.
- Server: gate on `tier2_connected` (interrupt if no plugin, clear log — no ComfyUI-only fallback by design); create a handoff pointing at the ORIGINAL `.ai` (reuse the `original_path` mechanic); open via the shared seam; send new `open_external` trigger (bounded send, mirror `_send_run_action`); block on `wait_for_edit`; consume via `_load_edit_tensors`.
- Plugin: new `photoshop_plugin/openExternal.js` (mirror `runAction.js`) — acquire the file (local path, or remote download via `connection.requestFile` like `openRemote`), open via the **`PDFGenericFormat` batchPlay descriptor** (NOT `app.open`), flatten, `deliverEdit`, ack `open_external_ok`/`open_external_error`. Wrap in `executeAsModal`; add a watchdog timeout so a dialog-freeze fails cleanly instead of hanging forever.
- Clear errors (not hangs): `.ai` without PDF-compatibility → actionable error; bad file → error; no plugin → interrupt.
- New WS messages folded into `docs/PROTOCOL.md` §3; new node section in §6.
- **Spike (Eric, live):** does a real `.ai` open headlessly via the descriptor without a dialog, and rasterize correctly (art with no background → real alpha for the MASK)? This is the load-bearing unknown; ships spike-gated exactly as the Run Action node did.
- **Acceptance:** automated tests for the server plumbing (handoff, gate, message, wait/consume) green on both interpreters; Eric confirms a real `.ai` loads matching Photoshop's render.

### M3 — `.dng` via Camera Raw *(later; Tier-2; higher-risk, spike-gated)*
**User value:** PS-exact raw decode that inherits your ACR edits, zero third-party dep. *(Flagged redundant with rawpy nodes for basic raw — pursue only for the PS-fidelity niche.)*
- Extend the M2 node to accept `.dng` (+ optionally `.cr2/.nef/.arw/…`), opening via the Camera Raw descriptor; force 8-bit + pinned colorspace + `settings:CAMERA` for determinism.
- **Gated on its own live freeze spike** — Camera Raw's dialog is the higher-risk, less-documented path; only proceed once M2 proves the two-message + executeAsModal pattern on the safer `.ai` format.
- **Acceptance:** Eric confirms a real `.dng` opens headlessly without freezing and the output matches Photoshop's Camera Raw default develop.

### M4 — Extensions *(parking lot; not scheduled)*
- Other PS-openable formats through the same node: `.eps`, multi-page `.pdf`, `.heic`, etc.
- `.dng` "as Smart Object" to carry/re-edit ACR settings; expose ACR-settings passthrough.
- Multi-artboard `.ai` page selection (UXP has no page-count introspection — needs a manifest or Illustrator-side query).
- Server-side thumbnail/preview for `.ai`/`.dng` before the round-trip (today none, since `raster_io` no longer decodes them — preview only appears after the node runs).

## Cross-cutting risks (from research; all need live confirmation)
1. **Dialog-freeze** (both formats): `executeAsModal` / `dialogOptions:'silent'` are not bulletproof — Adobe-acknowledged that some warnings survive `silent`. Mitigation: fully-specified descriptors + watchdog timeout + treat a stuck open as a hard failure (same recovery as a frozen Action). **This is why M2/M3 are spike-gated.**
2. **Two-message ordering race** (open_handoff then trigger): inherits `runAction.js`'s `RECORD_WAIT_TIMEOUT_MS` poll; a slower `.ai`/`.dng` open may need its own timing spike.
3. **`.ai` PDF-compatibility**: an `.ai` saved without it can't open via PS's PDF engine — must surface a clear error, never a hang.
4. **Capability regression**: workflows pointing Load PSD at `.ai`/`.dng` stop working after M1 (documented release note).
5. **No pre-run preview** for `.ai`/`.dng` (M4 item).

## Research provenance
Full findings + citations: workflow `ai-dng-ps-decode-research` (run `wf_8188f0fe-141`), 2026-07-21. Key sources: Adobe UXP Photoshop reference; community.adobe.com PDF-open-without-dialog thread; CameraRAWOpenOptions docs; ComfyUI-Darkroom / comfyui-raw-image / zveroboy-photo (raw prior art); codebase map of `cpsb/actions.py` + `runAction.js`.
