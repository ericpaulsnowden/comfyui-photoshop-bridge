# Roadmap — Layered images: ComfyUI's new LAYERS system × the Photoshop bridge

**Status:** DECIDED — building L1–L3 (Eric's decisions recorded 2026-08-09, below); L4 stays roadmap-only
**Ask (Eric, 2026-08-09):** ComfyUI shipped three new nodes around layered images ([PR #15317](https://github.com/Comfy-Org/ComfyUI/pull/15317)) — check the new build, look into the nodes, plan how they could benefit our Photoshop plugins.
**Verdict up front:** this is the biggest external gift this project has ever received. Core ComfyUI now has a first-class layered-image type — and **no PSD import**, and only a browser-download PSD export. Our pack owns the PSD/Photoshop seam. Two focused changes (Load PSD emits layers; Compose accepts layers) dissolve the README's oldest limitation — *"layers don't round-trip into the graph"* — and make this pack the Photoshop leg of ComfyUI's new layer system.

## What shipped in core (verified hands-on on the test rig, ComfyUI v0.31.1)

Three experimental nodes (category `image`, all `[BETA]`), shipped in **ComfyUI v0.31.0** (2026-08-08, with frontend 1.48.7; backend PR #15317 + frontend PR #14809, both by jtydhr88/Comfy-Org, merged 2026-08-07):

- **`ImageCompositor`** ("Create Layered Image") — consumes a layer stack + an editor recipe, composites with 26 blend modes, outputs IMAGE (alpha-capable) + MASK (1 = transparent). Output node with per-layer previews. The frontend adds a **full-screen "PSD-style layer editor"** (their words): layer list, WebGL2 canvas with a Figma-style gizmo, properties panel (position/rotation/flip/opacity/blend), undo/redo — and a **"Download PSD" button (ag-psd, browser-side)**.
- **`AddLayer`** ("Add Layer") — appends one layer (image + optional MASK + name/x/y/opacity/blend/rotation/size/z-index/flips) to a stack. Chain them to build documents node-by-node.
- **`LayersFromBoundingBoxes`** — adapts batch-plus-bboxes producers (detection nodes, ByteDance **Seedream 5.0 Layer Separation**, Qwen-Image-Layered-style models) into a stack.

**The load-bearing discovery: the `LAYERS` document type.** Socket type string `"LAYERS"` (`io.Layers`, `comfy_api/latest/_io.py`), carrying a plain dict **any custom node can construct or consume**:

```python
{"version": 1, "canvas": (w, h),          # optional
 "layers": [{
    "type": "raster",                      # only kind so far
    "image": tensor,                       # (B,H,W,3|4); batches expand to layers
    "mask": tensor,                        # optional; 1 = transparent (LoadImage convention)
    "name": str, "x": int, "y": int,       # position (±16384)
    "z_index": int,                        # stable-sorted stacking
    "opacity": float,                      # 0..1
    "blend_mode": str,                     # one of 26 (below); unknown ⇒ ValueError
    "visible": bool, "rotation": float,    # RADIANS clockwise
    "w": int, "h": int,                    # display size; absent = native
    "flip_h": bool, "flip_v": bool}]}
```

Caps: **50 flattened layers**, 16384px sides. There is a second, camelCase JSON shape (`COMPOSITOR`, the editor's saved recipe with content fingerprints) — we never need to write that one; the editor owns it.

**26 blend modes, GIMP semantics** — and the list is nearly Photoshop's own: normal, multiply, screen, overlay, darken, lighten, color-dodge, color-burn, hard-light, soft-light, difference, exclusion, linear-dodge, linear-burn, vivid-light, pin-light, linear-light, hard-mix, subtract, divide, grain-extract, grain-merge, hue, saturation, color, luminosity.

### Hands-on evidence (2026-08-09, rig at v0.31.1)

1. **Our pack loads unmodified on v0.31.1** — all 11 nodes register; nothing in the 133 commits since v0.28.0 touches our surfaces.
2. **End-to-end API run works:** EmptyImage ×2 → AddLayer → AddLayer(multiply @ 0.6, offset) → ImageCompositor composited correctly, returning per-layer previews, fingerprints, and editor bboxes. API quirk: the `compositor` widget must be passed explicitly (`null`) in raw `/prompt` JSON.
3. **The core spike — PSD → LAYERS is faithful:** authored a real 3-layer PSD with psd-tools (offsets, multiply @ 60%, one hidden layer), rebuilt it as a LAYERS document, ran it through the actual `nodes_compositor` code path. Names, positions, z-order, opacity, blend names, and visibility all carry over perfectly; the hidden layer is honored.
4. **…with one honest semantic gap:** the blended pixel diverged (core 170 vs Photoshop 101 on the multiply test). Core composites opacity/blends in **linear (or perceptual-per-mode) space, GIMP semantics**; Photoshop blends in sRGB. Anything at partial opacity or under a non-normal blend renders *similarly but not identically* to Photoshop. Known-and-pinned upstream behavior (golden fixtures, PR #15373), plus sub-pixel snapping. **Consequence for us:** the compositor is a *layout/arrangement* surface; the authoritative "what Photoshop shows" flatten remains Photoshop/psd-tools. Never promise pixel-parity.
5. **The editor needs the new frontend node system** — the widget renders "Compositor: Node 2.0 only" in the classic node mode. (Per-browser setting; check it first when it "doesn't work here.")
6. **psd-tools 1.17.4 (already our dependency) covers the reverse path**: `PixelLayer.frompil(parent, name=…, top=…, left=…)` plus settable `blend_mode`, `opacity`, `visible` — everything a LAYERS→PSD writer needs, no new dependencies.

### Ecosystem context (researched 2026-08-09)

- **No PSD import in core, and none announced.** Export is browser-download only (ag-psd; blend-mode fidelity untested by anyone). No docs pages exist yet for the three nodes; the changelog entry is the only official documentation. Zero user-filed bugs in the first two days.
- Core's positioning is *decompose-with-model, recompose-in-editor* (Seedream separation partner node outputs LAYERS directly; Qwen-Image-Layered is the same family).
- The incumbent third-party compositor (erosDiffusion, 634★) is currently broken on recent ComfyUI; PSD-loading custom packs exist (LayerStyle etc.) but none integrate a managed Photoshop round trip.
- Adjacent hazard noted upstream (#15374): autogrow slot-gap mask misalignment produced a real compositor incident — relevant if we ever adopt autogrow patterns with masks.

## Why this matters to us specifically

Our README's very first limitation has always been: *"Neither tier syncs individual Photoshop layers back into the ComfyUI graph — a ComfyUI image is a flat RGB tensor."* That premise just changed: the graph now has a layered currency, an editor for it, and model families that produce it. Every piece of our pack that touches layers gains a real target:

| Ours today | With LAYERS |
|---|---|
| Load PSD flattens to IMAGE+MASK | can also emit the PSD's **actual layers** into the graph |
| Compose builds PSDs only from flat image inputs | can write a **real layered PSD from any LAYERS stack** — including Seedream/Qwen model output and compositor-editor stacks |
| "Send to ComfyUI" sends topmost layer only | can send **every layer** as a stack |
| Annotate returns 4 flat views | could also return base+instructions as a 2-layer stack |

And the mapping is good: **24 of Photoshop's 27 blend modes map 1:1 by name** (dissolve → normal, darker-color → darken, lighter-color → lighten are the only lossy three, each with a sensible neighbor).

## The plan

### L1 — Load PSD emits LAYERS ("PSD in") — the opening move
Add a **`layers` (LAYERS) output** to Load PSD — appended at the END of the output tuple (outputs are positional; append-only is this repo's hard rule). Each PSD layer becomes a LayerItem: `topil()` → RGBA tensor, `left/top` → x/y, name, `opacity/255`, blend mode via the 24-map (fallbacks logged), `visible`, z from stack order; canvas from the PSD's size; the PSD's **layer masks** map to per-item `mask` (inverted to the 1=transparent convention). Non-raster content (adjustment layers, fill layers) is skipped-with-log; text layers and smart objects arrive rasterized (what `topil()` yields) — stated honestly in the tooltip.
- **Groups (decided):** a `flatten_groups` BOOLEAN checkbox, default **off** = a flat list of every leaf layer (descend into groups; group opacity/visibility composed onto leaves — preserves per-layer editability); **on** = one layer per top-level entry (groups composited to single layers). LAYERS has no group concept, so this is a real projection either way.
- Runs fully server-side (psd-tools) → stays in the plain **Handoffs** bucket: no Photoshop required.
- Cap honesty: >50 layers → **warning, not error** (decision 1 forbids breaking today's use cases — a 60-layer PSD flattens fine today and must keep loading; the 50 cap is core's *compositor* limit, enforced by core at consume time, so we log that Create Layered Image will reject the stack and move on).
- Spiked already (evidence #3) — this phase is de-risked.

### L2 — Compose Layers to PSD accepts LAYERS ("PSD out") — the flagship
Add an optional **`layers` (LAYERS) input** to Compose. When connected, the stack writes as a real layered PSD: per-layer name, position, opacity, **blend mode**, visibility (writer upgrade verified feasible on psd-tools 1.17.4; today's writer already does name/position/opacity). Rotation/flips/display-size are baked into pixels at write (PSD has no non-destructive transform we can emit) — logged when applied. Everything downstream already exists: managed handoff, open-in-Photoshop, block-until-save, gallery card. **One wire from Seedream separation / Qwen-Layered / a compositor stack to an editable, layered document open in Photoshop.** Nothing else in the ecosystem does this.

### L3 — The layer round trip + docs
L1+L2 compose into: PSD → LAYERS → (rearrange in ComfyUI's editor, or process per-layer in any workflow) → layered PSD → open in Photoshop → edit → save → back. Ship with: a bundled example (`examples/layered_roundtrip.json`: Load PSD → Create Layered Image → Compose(layers) → open), README rewrite of the limitation ("layers now flow both ways; blending semantics differ slightly from Photoshop — see honesty notes"), PROTOCOL §6 updates, node tooltips per the v0.5.63 standard, and the Node-2.0-required note for the editor.

### L4 — Later, demand-driven
- **Send to ComfyUI: "All layers (as stack)"** third picker option — retires the topmost-only limitation. Needs a chunked multi-layer upload (the manual_push pattern per layer) and a gallery card that Adds as a Load-PSD-with-layers node.
- **Annotate** gains a `layers` output (base + instructions).
- **Refine-per-layer**: refine pass fed by one chosen layer instead of the flat capture.
- Watch upstream: text layers, per-layer masks in the editor, sub-pixel resampling, and PSD *import* appearing in core (would change our positioning; today it is explicitly absent).

### Not doing
- Anything depending on pixel-parity between core compositing and Photoshop (evidence #4 forbids it).
- The COMPOSITOR recipe format (editor-owned; fingerprint-gated; camelCase) — we only ever speak LAYERS.
- Replacing our flat outputs — every existing output keeps working unchanged; LAYERS is additive.

## Compatibility & gating
- **Interop with core nodes needs ComfyUI ≥ 0.31.0** (bundled frontend ≥ 1.48.7; the editor additionally needs Node 2.0 mode on).
- **Our L1/L2 nodes work on OLDER ComfyUI too** — "LAYERS" is just a type string; our own output↔input still links on 0.28. Only the core nodes are absent there. No hard version gate needed; a startup log line ("core layer nodes not present on this ComfyUI — PSD⇄LAYERS still works pack-internally") is enough.
- All three core nodes are **experimental** — schema drift is possible. Our exposure is only the LayerItem dict keys; pin them in one `cpsb/layers.py` module with a version check on `doc["version"] == 1`, and cover with tests against `document_items()` when the rig has ≥0.31 (skip otherwise).
- Naming adjacency: core's "Add Layer" vs our "Photoshop Add Layer" (pushes INTO the PS document). Distinct ids and buckets; add a cross-referencing sentence to both tooltips.

## Decision points — DECIDED (Eric, 2026-08-09, verbatim)
1. **Extend existing nodes vs new nodes** — *"Extend existing nodes, but don't break older builds or current use cases."* → Load PSD gains an output, Compose gains an input; every existing output/widget/behavior stays byte-identical. Concretely: new outputs append at the END of the output tuple (outputs are positional), new widgets append LAST (widget values restore by position), defaults reproduce today's behavior exactly, and the pack keeps loading on pre-0.31 ComfyUI.
2. **Group handling** — *"Create an option to pick between these. Showing all layers in a flat list should be default, with a checkbox to flatten each group."* → `flatten_groups` BOOLEAN, default off (flat leaf-layer list), on = one layer per top-level group.
3. **Scope of L4** — *"Add all of these to the roadmap doc, but don't do them yet."* → every L4 item stays recorded below; none are built in this round.

## Sequencing & effort
L1 and L2 are each a contained, test-friendly change (the heavy machinery — psd-tools, the managed-PSD writer, handoffs — already exists and is validated). L3 is docs + one example. Recommended order: **L2 first** if forced to choose (it's the visible wow: model-separated layers landing in Photoshop as a real PSD), but L1+L2 together are what changes the story. Rig stays on v0.31.1 for development (rollback point: v0.28.0 @ 700821e1).
