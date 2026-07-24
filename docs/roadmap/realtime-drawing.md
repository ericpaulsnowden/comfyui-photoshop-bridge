# Roadmap — Real-time drawing feedback (draw in Photoshop, live ComfyUI re-render)

**Status:** planned (research done 2026-07-24; NOT started — plan only, per Eric)
**Ask (Eric, 2026-07-24):** "do work in photoshop, especially drawing, and get realtime feedback."
**Verdict up front:** worth building, and cheaper than it looks — the transport/export half already exists in this pack; the genuinely new work is a capture loop, a scheduler, and a preview surface, all with proven designs to port.

## Why this is a different problem than everything shipped so far

Every existing round trip in this pack is **save-triggered**: `saveListener` fires on a real Cmd/Ctrl+S → duplicate + flatten + PNG export → upload → ingest → (optionally) auto-queue. That is seconds per iteration and demands a Save per iteration — structurally wrong for drawing. Real-time needs: capture **without saves**, frames that **never touch disk or handoffs** (ephemeral, keep-latest), and feedback that **never fights the user's brush**.

## What the research established (sources read, not summarized)

**Prior art — the two working implementations were read at source level:**
- **Krita AI Diffusion (live painting mode)** — the architecture to port. Capture is a **10Hz canvas-diff POLL** (no stroke event at all); an **adaptive scheduler** (`LiveScheduler`: zero debounce while generations are fast, +0.25s grace once the rolling average exceeds 1.5s, never withholds >3s); its **own ComfyUI API client** (own `client_id`, direct `POST /prompt`, own `/ws` listener — the browser's Auto-Queue is not involved); **single-slot keep-latest backpressure** (exactly one job in flight, resample the newest canvas on completion); result shown as a **non-destructive preview**, merged to a layer only on explicit apply.
- **comfyui-photoshop (NimaNzrii), decompiled UXP bundle** — proves the Photoshop-side capture trick: it does NOT use notification events; it **polls `activeDocument.activeHistoryState.id` every 300ms** (a cheap DOM read — no pixels move) and only captures when the id changes (≈ once per completed stroke), wrapping its own capture actions in `suspendHistory` so they don't self-trigger — the history-stack analogue of this pack's own-write suppression. Field reports (issue #19) call it fragile in practice, so treat it as proof of mechanism, not of hardening.
- **Every serious realtime integration** (Krita, comfyui-photoshop, ComfyStream/RealtimeNodes) bypasses the browser Auto-Queue in favor of direct `/prompt` resubmission. Eric's pasted reference's Auto-Queue-Instant method *works* but busy-loops; this pack's existing **event-driven `app.queuePrompt(0)`-per-arriving-edit** pattern is the better fit and burns zero idle GPU.

**UXP facts (Adobe-documented unless noted):**
- `imaging.getPixels({ targetSize })` **downscales at capture** using Photoshop's own resolution-pyramid cache — documented "dramatic performance improvements"; never move full-res pixels. `imageData.dispose()` in a `finally` is documented-mandatory (plugin memory warnings otherwise).
- `imaging.encodeImageData({ imageData, base64: true })` → **JPEG base64** is the documented (JPEG-only) encoder and is exactly what the shipped realtime plugin uses on the wire. Not the hand-rolled uncompressed PNG (~64MB frames) — JPEG frames at 768px are ~100-300KB.
- **`historyStateChanged` notifications are NOT trustworthy for per-stroke timing** — a forum report says such events fire only "after you click away," and both shipped precedents poll instead. The DOM history-id poll sidesteps this, but its promptness on stroke-completion is itself spike material.
- **Second panel entrypoint is documented multi-panel** (one plugin, several `{"type":"panel"}` entries, one shared JS context — the existing `connection.js` singleton is shared for free). Caveats from a working forum example: `show()` fires once at creation; use `getElementById`.
- `imaging.putPixels({ layerID, ... })` can write into one specific layer, and `hostControl.suspendHistory`/`resumeHistory` (documented exactly) coalesces a whole live session into ONE undo entry. **But `executeAsModal` behavior while the user is mid-brushstroke is documented NOWHERE** — all documented contention is plugin-vs-plugin; the likely symptom (inferred from "modal state blocks the UI thread") is a visible stroke stutter. No shipped plugin writes putPixels-per-second into a live layer (even Higgsfield's "live sync" delivers discrete layers per generation). → in-canvas feedback is a late, spike-gated milestone, never the v1.
- Panel `<img>` refresh at 2-10Hz: nothing documents a throttle, nothing confirms smoothness — small spike, with `<canvas>` as fallback.

**Models & honest latency (Eric's pasted settings adopted where right):**
- Sampler settings from the reference are correct and go in the bundled example workflow: **steps 1-4, CFG 1.0-2.0, `lcm`/`euler_ancestral`, `normal`/`sgm_uniform`, denoise ~0.45-0.60 as the "AI creativity" slider**.
- Model tiers (2026): **SD1.5-LCM** = lowest latency/VRAM (what Krita ships by default); **SDXL Lightning** = best deployable quality at 1024px (~0.3-0.5s/4-step on a 4090); **Flux-schnell/FLUX.2-klein** = quality upgrade in the same speed class. SDXL Turbo is fast but finicky for img2img strength.
- Realistic stroke→feedback budget on the PC (local): history-poll ≤300ms + capture 50-150ms + wire ~50ms + generation 0.3-0.9s + preview ~100ms ≈ **0.7-1.5s typical**. Krita achieves the same class. Multi-fps "video" streaming is a different problem (StreamDiffusionV2's headline numbers are 4×H100 video rigs — do not expect them on one consumer GPU).
- Remote (Mac PS → PC Comfy): JPEG frames over the existing chunked WS ≈ 1-3 Mbps at 2-3fps — LAN-fine; latency adds ~100-200ms.

## Architecture (the loop, end to end)

```
Photoshop (plugin, Live Mode ON for one document)
  └─ poll activeHistoryState.id every ~250-300ms      (cheap DOM read, no pixels)
       └─ id changed?  → getPixels(targetSize: 768) → encodeImageData(JPEG b64)
            └─ live_frame over the EXISTING cpsb websocket  (keep-latest, drop if one unsent)
cpsb server (in ComfyUI)
  └─ latest-frame slot per live session — in MEMORY, no handoff, no disk
       └─ emits cpsb.live event to the frontend
ComfyUI frontend (web/cpsb)
  └─ queuePrompt(0) per frame, COALESCED (≤1 queued while ≤1 runs — no busy loop)
ComfyUI graph (user's own workflow)
  └─ NEW node: Photoshop Live Canvas (IMAGE out; IS_CHANGED = frame content hash → only
     the changed subgraph re-runs) → user's LCM/Lightning img2img → …
       └─ NEW node: Photoshop Live Preview (IMAGE in; pushes result JPEG back over
          the plugin websocket)
Photoshop (plugin)
  └─ second docked panel "ComfyUI Preview" shows the latest result   (M3)
  └─ (much later, spike-gated) result into an in-document "AI Preview" layer (M5)
```

Design commitments:
- **Ephemeral frames.** Live frames never become handoffs, never hit disk, never enter the gallery. One in-memory latest-frame slot; a session, not a record. (The normal save-triggered path still works and still delivers full-res — a real Save during/after a live session behaves exactly as today.)
- **Keep-latest everywhere.** Plugin drops a frame if the previous one hasn't sent; server slot holds only the newest; node serves only the newest; frontend never stacks queues. Backpressure is the design, not an afterthought (straight from Krita's single-slot pattern).
- **Tier-2-required, like the Action node** — there is no Tier-1 (file-watch) equivalent of save-free capture. Same ethos exception, stated up front.
- **The browser tab drives execution in v1** (event-driven queue — it's already how this pack auto-queues, and the user is in the tab building the workflow anyway). A tab-free server-side `/prompt` driver is the M4 upgrade, not the prerequisite.

## Milestones (each independently useful)

### M0 — Live spikes (throwaway plugin build; ~an hour of Eric's Photoshop time)
The plan's load-bearing unknowns, none guessable from docs:
- **S-A (gates M1):** does `activeDocument.activeHistoryState.id` update promptly at stroke mouse-up under a 250ms poll, or only "after clicking away"? (Also log `historyStateChanged` timestamps alongside, to settle the event question for good.)
- **S-B (gates M1):** `getPixels(targetSize 512/768/1024)` + `encodeImageData` timing, called every ~300ms for 2+ minutes — measure ms/frame, memory with `dispose()`, and what happens when it fires **mid-brushstroke** (stall? queue? clean frame?).
- **S-C (gates M3):** panel `<img>` src-swap at 2-10Hz — smooth, or does it need `<canvas>`?
- **S-D (gates M5, deferred):** `putPixels` into a non-active layer mid-stroke — stutter/contention measurement. Not needed for M1-M4.

### M1 — Live capture + Photoshop Live Canvas node
Plugin: per-document **Live Mode** toggle (main panel); history-id poll → debounced capture → `live_frame` (new lightweight WS message; single frame, JPEG b64, keep-latest). Server: in-memory live session + latest-frame slot + `cpsb.live` event. Node: **Photoshop Live Canvas** (IMAGE; IS_CHANGED = a content hash of the frame — as built, NOT the plan's original frame-counter idea: the counter restarts per plugin connection and would alias across reconnects). PROTOCOL.md §3/§6 additions.
**Useful alone:** draw in PS, watch the result re-render in the ComfyUI tab on a second monitor — the full loop minus in-PS preview.

### M2 — The loop + the recipe
Frontend: `cpsb.live` → coalesced `queuePrompt(0)` (reuses the existing auto-queue seam; gated by a Live arm/disarm control so it never fires unexpectedly). Ship a **bundled example workflow** (Live Canvas → LCM/Lightning img2img with the reference settings → preview). Panel shows live status (frames sent, capture ms, last-frame age; Stop Live is the pause — a live fps ticker is M4 polish).

**Prompt + creativity controls (shipped in M2, added after Eric's live tests):** two nodes fed from controls **in the "ComfyUI Preview" panel, under the image** (they sit with the render they affect and survive collapsing the main panel):
- **Photoshop Live Prompt** (STRING out → a `CLIPTextEncode` `text` input): the PROMPT box. Streams `live_prompt`.
- **Photoshop Live Creativity** (FLOAT out → the KSampler `denoise` input): the Creativity slider (0..1), mapped onto a denoise band (default 0.40–0.85). Streams `live_creativity`. Denoise is the single most effective adherence knob at a fixed few-step count — low = hug the drawing, high = reinterpret.

Both re-render via the shared `requestQueue()` seam (`cpsb.liveprompt`/`cpsb.livecreativity`), and both are **NOT Tier-2-gated** — they fall back to their own node widgets so ComfyUI-only still works. Diagnosis that drove this: the "prompt/output barely changes" symptom is denoise + few-step-model, NOT wiring — the drawing's latent dominates at low denoise, weak low-CFG LCM adherence compounds it, and `lcm`/4-step on a non-few-step checkpoint (or a distilled checkpoint with an accelerator LoRA stacked on top) barely denoises. The on-canvas Note states the model requirement and the effective-work ≈ steps × denoise relationship.

### M3 — Feedback inside Photoshop
Second manifest panel **"ComfyUI Preview"** (documented multi-panel; shares the existing connection singleton). New **Photoshop Live Preview** output node: pushes its IMAGE input back over the plugin WS as a JPEG `result_frame`; the panel displays it (img or canvas per S-C). This is the headline UX: **draw on the canvas, AI result lives in a docked panel beside it.**

### M4 — Robustness & upgrades
- Adaptive scheduler (port Krita's `LiveScheduler` semantics: no debounce while fast, grace when slow).
- Tab-free driver: server-side direct `/prompt` resubmission with a frontend-captured graph (`graphToPrompt` snapshot on arm) — removes the browser from the loop.
- Remote-mode tuning (Mac PS → PC Comfy): frame-size cap, adaptive JPEG quality.
- ROI capture (`sourceBounds` + `targetSize`) for big canvases.

### M5 — In-canvas "AI Preview" layer (spike-gated on S-D; off by default)
`putPixels` into a dedicated pixel layer, whole session wrapped in ONE `suspendHistory`/`resumeHistory` pair (documented; collapses hundreds of writes into one undo step; auto-resume safety net on modal exit). Only if S-D shows acceptable brush contention — no shipped plugin has proven this, and the likely failure is stroke stutter. The M3 panel remains the default feedback surface regardless.

## Discussed but NOT yet implemented (backlog)
Raised in conversation with Eric while building the live loop; captured here so they aren't lost. Not scheduled into a milestone yet.
- **Creativity slider → more than denoise.** Today the slider drives only the KSampler `denoise` (via `PhotoshopLiveCreativity`). Eric asked for it to "modify the settings (at least denoise but possibly other settings)". Candidates to fold in behind the one slider or add adjacent controls: **steps** (INT out), **CFG** (FLOAT out — but risky on distilled models that require CFG≈1), and per-model presets. Cleanest path: extend `PhotoshopLiveCreativity` with optional extra outputs, or a companion `PhotoshopLiveSampler` node exposing steps/CFG.
- **Auto-render for prompt-only / creativity-only graphs.** The live loop arms only on an active `PhotoshopLiveCanvas` node. A graph with just a prompt/creativity node (e.g. live txt2img steering, no drawing) won't auto-render on a prompt/slider change — the panel copy is scoped honestly, but the real fix is to also arm on those nodes (needs an explicit auto-queue toggle so it's not always-on).
- **Configurable capture resolution.** Live capture is fixed at 768px long side (`liveMode.js` `LIVE_TARGET_SIZE`), which is SD1.5's home turf but below SDXL's native 1024 — SDXL/Lightning renders come out a touch soft. Expose a capture-size setting (512/768/1024) in the panel, or auto-pick from the connected model family.
- **Model / preset picker in the panel.** Pick a fast model + its correct sampler recipe (LCM vs Turbo vs Lightning want different sampler/CFG) from Photoshop, instead of hand-setting the KSampler. Would prevent the two traps Eric hit: a non-few-step checkpoint, and stacking an accelerator LoRA on an already-distilled checkpoint.
- **"Wrong fast-model setup" guardrail.** Detect/warn when the graph pairs `lcm`/tiny-steps with a checkpoint that has no distillation (blur/near-passthrough), or stacks an LCM LoRA on a Lightning/Turbo checkpoint (conflicting distillations).
- **Persist panel prompt + creativity across reloads** (prefs.js), like the server-address field — so a plugin reload doesn't drop the last prompt/slider position.
- **Negative-prompt control from the panel** (pairs with a CFG-preserving model like Hyper-SD-CFG, where the negative actually does something).

## Decision points that are Eric's (flagged, not decided)
1. **Where does v1 feedback live** — is M1's "watch the ComfyUI tab" acceptable to start, or is M3's in-PS panel the true MVP bar? (Plan assumes M1 ships first as a usable increment.)
2. **Default model recipe** for the bundled workflow: SD1.5-LCM (fastest, softest) vs SDXL Lightning (slower, 1024px, better) — depends on the PC's GPU and taste; both configs can ship.
3. **Does Live Mode suppress the save-listener for that document** while active, or coexist (a mid-session Cmd+S also fires the normal full-res delivery)? Plan default: coexist — a Save means "I want the real thing."

## Honest risks
1. **S-A is the keystone.** If the history-id poll only advances "on click-away" (like the notification reportedly does), capture degrades to Krita-style blind canvas polling — pixel-diff on cheap downscaled captures still works (Krita proves it at 10Hz), but costs more per tick. The plan survives; the mechanism changes. That's why S-A is spike #1.
- 2. **Mid-brush capture contention** (S-B): unverified anywhere. Precedent (shipped 300ms-poll plugin) suggests workable; measure before believing.
3. **Latency expectations:** 0.7-1.5s stroke-to-feedback, not 30fps video. Set the expectation in the panel UI itself (show the measured loop time).
4. **comfyui-photoshop's fragility reports** (its issue #19) are a warning about hardening, not mechanism — this pack's reconnect/backpressure discipline (connection.js) is the antidote, and it already exists.

## Research provenance
Workflow `realtime-drawing-research` run `wf_d3b05455-82f` (2026-07-24): Adobe UXP Imaging/executeAsModal/EntryPoints references (primary source); Creative Cloud Developer Forums (event-timing, multi-panel, modal-contention threads); **source-level reads** of Acly/krita-ai-diffusion (`LiveScheduler`, `ComfyClient`, single-slot `QueuedJob`) and NimaNzrii/comfyui-photoshop's decompiled UXP bundle (history-id poll + `suspendHistory`); StreamDiffusion benchmarks (with the V2-is-multi-GPU-video caveat); Auto-Photoshop-StableDiffusion-Plugin's realtime img2img implementation (capture + JPEG wire format + 2s debounce).
