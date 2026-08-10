# comfyui-photoshop-bridge

Right-click any image in ComfyUI, choose **Open in Photoshop**, make your edit, hit Cmd/Ctrl+S — the result lands back on the same node automatically. It's the Lightroom "Edit in Photoshop" round trip, brought to ComfyUI: no exporting, no re-importing, no manual file juggling.

> **Project status: pre-1.0, actively developed.** The core round trip and the nodes below work today and are used day to day; expect rough edges and occasional breaking changes before 1.0. See the [releases/tags](https://github.com/ericpaulsnowden/comfyui-photoshop-bridge/tags) for the current version — the backend, frontend, and Photoshop plugin all report their version, and the ComfyUI sidebar shows an amber "update available" hint when they drift out of sync.

**Want to see it working first?** Every workflow in [examples/](examples/) is annotated on the canvas and ready to load — seven files between them cover all eleven nodes, from a no-Photoshop-required PSD compose to the full realtime drawing loop. See [examples/README.md](examples/README.md) for which need Photoshop and which don't.

## How it works

comfyui-photoshop-bridge ships as a single ComfyUI custom node pack with two tiers. You use the same right-click workflow either way — the tier just changes what happens under the hood.

**Tier 1 — file hand-off (nothing to install beyond this node pack).** Choosing "Open in Photoshop" writes the image to a PSD in a folder the node pack manages, then asks the OS to open it in Photoshop. A file-watcher monitors that exact file, and the moment you save, ComfyUI reads Photoshop's own saved composite back out of the PSD and updates the node that started the hand-off. No plugin, no account, no server to run — this tier is the floor the whole project stands on.

**Tier 2 — Photoshop plugin (optional, one-time install).** A small UXP panel keeps a persistent connection to ComfyUI's own server. Instead of watching the filesystem, it listens for Photoshop's native save event and pushes a flattened export back over HTTP the instant you save — no file-watch delay, higher-fidelity pixels, and it works even when **ComfyUI runs on a different machine than Photoshop**, because the plugin fetches and returns images over the network. You point it at a ComfyUI address right in the panel.

The *edit* round trip itself is whole-image: what returns after a Photoshop save is the saved composite, as a flat image. But layers themselves now flow through the graph too — **Load PSD emits a file's actual layer stack, and Compose writes a layer stack back out as a real layered PSD** (names, positions, opacity, blend modes, visibility). See [Layered images](#layered-images-psd-layers-in-and-out-of-the-graph) below.

## Do I need the Photoshop plugin?

**Short answer: mostly no — but two nodes genuinely can't exist without it.**

This pack is one ComfyUI custom node pack with two tiers (see [How it works](#how-it-works) above). Tier 1 needs nothing beyond the node pack; Tier 2 is an optional Photoshop panel. Here is exactly what that changes, feature by feature:

| Photoshop plugin | Features |
|---|---|
| **Not needed at all** | **Load PSD** · **Compose Layers to PSD** · **Photoshop Live Prompt** · **Photoshop Live Creativity** · **Photoshop Refine Source** — these either never touch Photoshop, or fall back to their own node widgets. |
| **Optional — works without it** | **Open in Photoshop** (right-click) · **Edit in Photoshop** · **Annotate for Edit** · Load PSD's *edit in place* · Compose's *open in Photoshop* — all work via Tier 1 (the OS opens Photoshop, a file-watcher catches your save). The plugin makes them instant, higher-fidelity, and cross-machine. |
| **Required** | **Run Photoshop Action** · **Photoshop Live Canvas** — there is no Tier-1 way to play an Action, or to capture your canvas *without a save*. Both say so clearly instead of failing mysteriously. |
| **Required to be useful** | **Photoshop Live Preview** · **Photoshop Add Layer** · **Send to ComfyUI** · the whole **ComfyUI Preview panel** (prompt, creativity, Refine, Add as a layer) — these deliver *into* Photoshop, so without the plugin they run but have nowhere to land (a logged no-op, never a failed render). |

The rule behind that table: **anything that can work without the plugin, does.** The plugin exists to make things better, not to gate the basics — the only hard exceptions are the two things that are genuinely impossible otherwise.

Two different questions, so don't read one table for the other: this table is about **the plugin**, while the node-browser groups below are labelled for **Photoshop the application**. That's why nodes like *Photoshop Live Prompt* and *Photoshop Refine Source* appear as "not needed at all" here yet still sit in a group marked *(requires Photoshop)* — they need no plugin and fall back to their own widgets, but the loop they belong to is pointless without Photoshop open.

## The nodes

Right-clicking any image and choosing **Open in Photoshop** is the core action, and it needs no node at all — it works on `LoadImage`-style nodes, generated previews, and saved outputs. On top of that, the pack adds eleven nodes, organized under **Photoshop Bridge** in the node browser — and the grouping tells you what you need before you place a node:

| Node-browser group | What's in it |
|---|---|
| **Handoffs** | Runs entirely on the ComfyUI machine — **Photoshop not required at all**: Load PSD, Compose Layers to PSD. |
| **Handoffs (requires Photoshop)** | Needs Photoshop installed to do its job: Edit in Photoshop, Annotate for Edit, Run Photoshop Action. |
| **Live Rendering (requires Photoshop)** | The live drawing loop and refine pass — the six `Photoshop Live…` / refine nodes. The loop as a whole needs the plugin (Live Canvas captures your canvas); a few of its helper nodes fall back to their own widgets, as each node's own section notes. |

### Edit in Photoshop

> **Plugin: optional** — Tier 1 handles it; the plugin makes it instant and cross-machine.

Opens its input image in Photoshop and, in the default **Wait for first save** mode, *blocks* the workflow until you save — then continues with your edit as the node's output. Also offers **Re-run on every save** (keep iterating, each save re-runs the graph) and **Open only (don't wait)**, plus a timeout and a working cancel.

### Load PSD

> **Plugin: not needed** — the preview is rendered server-side, with no Photoshop involved.

Starts a workflow *from* a `.psd`/`.psb` — or a **`.tif`/`.tiff`** — sitting in ComfyUI's input folder. Shows an on-node **preview** and outputs IMAGE + MASK — plus a **`layers` (LAYERS) output**: the file's actual layer stack, every layer with its pixels, name, position, opacity, blend mode, and visibility, ready for ComfyUI 0.31+'s layer nodes or this pack's own Compose node (see [Layered images](#layered-images-psd-layers-in-and-out-of-the-graph)). A **`flatten_groups`** checkbox picks the group projection: off (default) lists every layer individually in one flat list, with group opacity/visibility applied to its layers; on collapses each top-level group into a single layer. An optional **edit the original in place** mode opens that very file in Photoshop and takes your saves back (that part uses Tier 1 or the plugin).

An **`on_save`** widget controls what a save in Photoshop actually does: *Re-run workflow* (default), *Update only* (take the edit, don't re-run), or *Ignore* (saving does nothing). Set it to Ignore when you want to open a PSD, shuffle layers, push one back and close, without the graph firing on every save. It's enforced on the server, so it governs the plugin's **Send** button too, not just automatic saves.

### Compose Layers to PSD

> **Plugin: not needed** to write the PSD; opening it in Photoshop uses Tier 1 or the plugin.

Stacks multiple images into one **layered, grouped PSD**, then (by default) opens it in Photoshop and blocks until you save. Outputs the flattened composite, the written PSD's filename, and a **`layers`** batch — one frame per layer — so a Preview node shows every layer individually instead of just the flat result.

It also accepts an optional **`layers` (LAYERS) input** — from Load PSD, ComfyUI 0.31+'s layer nodes, or any layer-splitting model — and writes that stack as **real PSD layers**: each layer's name, absolute position, opacity, blend mode, and visibility land in the document as live, editable properties (rotation, flips, and display size are baked into the pixels — PSD has no non-destructive transforms — with a log line per bake). Stack layers sit below any connected `image_N` inputs inside the same run group, and the `max_layers` cap counts the combined total.

Leave the target empty and every run writes a fresh numbered PSD; **Browse…** to any PSD on the ComfyUI machine (or name a new one right in the dialog) and runs **accumulate into that single reviewable document**, each in its own numbered group. Writes are atomic, so a failed run can never truncate the document you've been collecting into — and it's safe to point at a file another node in this pack already has open (e.g. a Load PSD "edit original" target): a compose write is never mistaken for a Photoshop save on that other node's side.

After a run the node shows **`Written: <filename>`** with a **Copy Path** button and offers **Open in Photoshop** on right-click — so even a "Don't open (composite only)" run isn't a file you have to go hunting for. Compose writes into the same `input/` folder **Load PSD** lists, so you can chain the two.

### Annotate for Edit

> **Plugin: optional** — and in this node's default mode, Photoshop isn't involved at all.

Pairs a typed **instruction** with a **mask** marking where it applies — the two inputs an editing model like Kontext or Qwen-Image-Edit expects — without you having to draw shapes or write text onto the pixels yourself.

In the default **Pass through** mode it never opens Photoshop: it reads the optional **`mask`** input socket (or hands back an empty mask when nothing is connected) and passes the image along unchanged, so any mask source already in your graph works.

Switch **`mode`** to **Wait for first save** or **Re-run on every save** to draw the mask in Photoshop instead. Either one hands the image over, and Photoshop opens it with an auto-created empty transparent **"Instructions"** layer. Paint on that layer with any brush, any color, to mark a region; you can edit the base image too. Save, and you get back four outputs covering the three useful views of the result:

- **`image`** — everything *but* your marks (your base edits baked in). Pair with `mask` for inpainting / mask-driven models.
- **`mask`** — your marks alone (in **Pass through**, whatever you connected to the `mask` input).
- **`annotated`** — image *and* marks combined, for visual-prompt edit models that take no mask ("edit what I circled"). The `box_composite` toggle picks the form: off = your real strokes, on = a tidy red box at their bounding box (what Kontext / Qwen-Image-Edit respond to).
- **`instruction`** — your text, verbatim.

Rename or delete the Instructions layer and it's treated as a plain edited image. The two Photoshop modes are the same ones **Edit in Photoshop** offers, so you iterate the same way in either node — but unlike every other node here, this one *defaults* to **Pass through**. A **Re-open in Photoshop** button gets you back into your annotation — Instructions layer and strokes intact — after you've closed it.

### Run Photoshop Action

> **Plugin: REQUIRED** — there is no way to trigger a Photoshop Action without it. The node says so clearly if the plugin isn't connected.

Give it an image and the name of a **saved Photoshop Action** (plus its set), and it opens the image, plays that Action, and returns the processed result to your workflow — no manual step. Heads-up: an Action that pops an interactive dialog mid-run can stall Photoshop, so use Actions that run start-to-finish unattended.

### Photoshop Live Canvas

> **Plugin: REQUIRED** — save-free capture is impossible without it.

The input side of **realtime drawing**. Toggle **Live Mode** in the plugin panel and this node serves a fresh snapshot of the canvas you're drawing on after **every stroke — no saving** (a lightweight change-detect poll plus a downscaled capture, ~sub-second). With its **`auto_queue`** widget On, every stroke queues a re-render automatically, coalesced so you always get your newest strokes and never a backlog.

A **CAPTURE SIZE** control in the main panel (512 / 768 / 1024, persisted) sets the live frames' long side — use 1024 with SDXL-class models for sharper results. Frames are ephemeral: never written to disk, never added to the gallery. The MASK output is always empty (the live stream is JPEG, which carries no alpha); derive masks downstream.

### Photoshop Live Prompt

> **Plugin: not needed** — falls back to its own text widget, so a ComfyUI-only setup works.

Outputs a `STRING` to wire into a `CLIPTextEncode`'s `text` input, so you can **type your prompt in Photoshop** instead of the graph. The **PROMPT** box lives in the ComfyUI Preview panel, right under the render it affects; edits re-render live. Prompts **persist per document** — reopen a file and its prompt comes back, and settings never leak between files.

### Photoshop Live Creativity

> **Plugin: not needed** — falls back to its own widgets.

Outputs a `FLOAT` for the KSampler's `denoise` input, driven by a **Low / Medium / High** control in the preview panel: low hugs your drawing, high reinterprets it. Because the same denoise behaves very differently at 512 vs 1024 depending on your model, the band those levels map onto is a **CREATIVITY RANGE** setting in the main panel (Subtle / Balanced / Bold) **remembered per capture size** — and the preview panel always names the exact denoise each level currently means.

### Photoshop Live Preview

> **Plugin: required to be useful** — it delivers *into* Photoshop. Without it the node runs and logs a no-op rather than failing your render.

The output side of the live loop: wire your rendered IMAGE here and each result appears in the **"ComfyUI Preview" panel docked beside your canvas in Photoshop**. Hover the render for **Add as a layer** (drops it into your document, scaled to the canvas) and **Refine** (below). Behind the scenes this node also keeps every render at **full quality** server-side, which is what the refine pass and Add-as-a-layer actually place — the panel only *displays* a small copy.

### Photoshop Refine Source

> **Plugin: not needed** — serves the last render even with no plugin connected.

The input side of the **refine pass**. Two IMAGE outputs, and your refine workflow wires whichever it wants: **`render`** (the last live render, at full quality — "refine exactly what I saw") and **`canvas`** (a full-resolution capture of your document, sent when you click Refine — a higher quality ceiling, though composition can drift). Either one missing falls back to the other, so every wiring shape works.

**Mute this node to disarm the refine branch** — ComfyUI skips its whole downstream chain, so an expensive refine never runs per-stroke. Clicking **Refine** in the panel un-mutes it for exactly one run, then re-mutes it.

### Photoshop Add Layer

> **Plugin: required to be useful** — it delivers *into* Photoshop; without it, a logged no-op.

Ends a chain by pushing the result **straight into your open Photoshop document as a layer**, scaled to the document bounds (pixels capped at 4096px on the long side). A **REFINED LAYER** control in the main panel picks whether repeated refines **Stack** new layers or **Replace** the previous one. (Not the same as ComfyUI 0.31+'s own **Add Layer** node, which appends a layer to a LAYERS stack *inside the graph* — this one delivers pixels to the live Photoshop document.)

## Layered images: PSD layers in and out of the graph

ComfyUI 0.31 introduced a first-class layered-image type — the **LAYERS** socket — with its own layer nodes and a full-screen layer editor. Core has **no PSD import** and only a browser-download export; this pack owns the Photoshop seam on both sides:

```
your .psd ─▶ Load PSD ─LAYERS▶ (rearrange / process / generate) ─LAYERS▶ Compose ─▶ layered .psd ─▶ Photoshop
```

- **Works with ComfyUI alone, on any version.** `Load PSD → Compose` round-trips layers with nothing else installed — LAYERS is just a socket type, so the pair links up even on pre-0.31 builds (a startup log line tells you when core's own layer nodes aren't available).
- **On ComfyUI 0.31+**, core's **Create Layered Image** node sits between the two: a PSD-style editor (move, rotate, reorder, per-layer blend and opacity) for the stack before it's written back. The editor itself needs the **Node 2.0** frontend mode — in classic mode its widget shows "Compositor: Node 2.0 only". Core's **Add Layer** builds stacks node-by-node (not to be confused with this pack's **Photoshop Add Layer**, which pushes pixels into the open Photoshop document). Layer-splitting model nodes that output LAYERS wire straight into Compose.
- **Try it:** [`examples/layered-roundtrip.json`](examples/layered-roundtrip.json) — annotated on the canvas, uses only bridge nodes so it loads anywhere.

**Honesty notes.** ComfyUI composites blends/opacity in linear (per-mode perceptual) space, Photoshop in sRGB: arrangement carries over exactly, but partial-opacity and non-normal-blend pixels render *close to*, not identical to, Photoshop — the written PSD in Photoshop is always the authoritative render, and this pack never promises pixel parity. 24 of Photoshop's 27 blend modes map 1:1 by name; dissolve, darker-color, and lighter-color fall back to their nearest neighbor (logged). Text and smart-object layers arrive rasterized; adjustment layers are skipped (logged). ComfyUI's compositor caps stacks at 50 layers — Load PSD warns past that but still emits the full stack (Compose has no such cap).

## Realtime drawing: draw in Photoshop, watch it re-render

The four `Live` nodes plus the refine pass make one loop: **draw → see an AI render in a panel beside your canvas → steer it with a prompt and a creativity dial → refine the good one at high quality → drop it into your document as a layer.** All of it without leaving Photoshop.

A ready-made workflow ships in [`examples/live_drawing_lcm.json`](examples/live_drawing_lcm.json) — drop it in, point the checkpoint at a fast model, and draw.

**Pick the right model — this is the one setup mistake that breaks everything.** The bundled settings (`lcm` sampler, 4 steps, CFG 2) only behave on a **few-step model**. A plain checkpoint (SD1.5 base, SDXL base, DreamShaper/Juggernaut with no LCM/Turbo/Lightning/Hyper in the name) barely denoises at 4 steps — which is exactly the "the output looks 99% like my drawing and the prompt does nothing" trap.

- **Zero-setup drop-in:** `DreamShaper8_LCM.safetensors` (HF `Lykon/dreamshaper-8-lcm`, SD1.5, ~2 GB) — swap it in and the bundled graph just works.
- **Best quality on 10 GB+ VRAM:** `sdxl_lightning_4step.safetensors` (HF `ByteDance/SDXL-Lightning`), then set the KSampler to `euler` / `sgm_uniform` / CFG 1.0 (keep 4 steps) and **don't** add an LCM LoRA — Lightning is already few-step, and stacking two accelerators fights itself.
- **Keep your own SD1.5 model:** add the LCM-LoRA (`latent-consistency/lcm-lora-sdv1-5`) through a `LoraLoaderModelOnly` node.
- **No download at all:** set the KSampler to `euler` / `normal`, ~20 steps, CFG ~7 — works with any model, but roughly 5× slower.

Expect about a second from stroke to re-render on a strong GPU: live iteration, not 30fps video. The seed is fixed on purpose, so only your drawing and your prompt change the output.

**The refine pass** turns a good live render into a finished one. Click **Refine** on the render (hover it in the preview panel) and the workflow's refine branch runs **once** at high quality — pausing live re-renders while it works — then delivers the result wherever you wired it: back to the preview pane (**Live Preview**), straight into your document as a layer (**Add Layer**), or both. Prefer to drive it yourself? Un-mute **Refine Source** and press Queue in ComfyUI, no plugin required.

## Beyond the nodes

**The "Photoshop Edits" sidebar gallery** tracks every round trip — across all your workflows, each card titled with its workflow's name — as a grid of cards: **Open** it again in Photoshop, **Add** it as a node, **Reveal** its origin node on the canvas, or **Remove** it from the list. Each card leads with the latest edit; press and hold any thumbnail to compare it against its original. A card still `Editing` whose plugin has confirmed the document is closed shows "Closed without saving" instead of guessing from elapsed time. Any node waiting on Photoshop shows an "Editing in Photoshop…" badge with a working cancel.

**Send TO ComfyUI, starting from Photoshop.** Everything above starts from a ComfyUI node; **Plugins ▸ ComfyUI for Photoshop ▸ Send to ComfyUI** (or the button in the panel) goes the other way — pick **Active Layer** or **Whole Document**, and it lands as a new, ready-to-use card in the sidebar gallery, with no workflow or node required to receive it. Click **Add** on that card to drop it into any workflow as a Load Image node. *Requires the plugin* — there's no Tier-1 equivalent, since nothing in ComfyUI initiates it. Only the topmost layer of a multi-layer selection is sent; merge manually first if you need more than one.

**Sensible file names.** Handoffs opened in Photoshop are **named after the file they came from** — `Eric-Headshot.jpg` opens as `Eric-Headshot.psd`, not an anonymous `source.psd` — so document tabs and file dropdowns stay tellable-apart.

<!-- demo.gif -->
*(Right-click an image → Open in Photoshop → edit → Cmd/Ctrl+S → the node updates. Demo GIF coming.)*

## Quick Start (Tier 1)

**Requirements:** ComfyUI with the current Vue-based frontend (anything from 2025 onward — the one with `registerSidebarTab` and the Settings API), and Photoshop 2025 (v26) or later for the round trip itself. See [docs/INSTALL.md](docs/INSTALL.md) for the full breakdown.

**Install**

- Via ComfyUI Manager: open Manager → **Install via Git URL** → paste this repository's URL. (Not yet in the searchable Registry — that's planned.)
- Or manually:
  ```bash
  cd ComfyUI/custom_nodes
  git clone https://github.com/ericpaulsnowden/comfyui-photoshop-bridge.git comfyui-photoshop-bridge
  pip install -r comfyui-photoshop-bridge/requirements.txt
  ```
  Run that `pip install` with the same Python ComfyUI itself uses — its venv/conda env, or `python_embeded\python.exe -m pip install -r ...` on the portable Windows build — not an unrelated virtual environment, or the node pack won't import when ComfyUI starts.
- Restart ComfyUI.

**Use it**

1. Right-click any image — a `LoadImage` node, a generated preview, a saved output — and choose **Open in Photoshop**.
2. Photoshop opens (or comes to the front) with the image loaded.
3. Edit it. Add layers, adjustments, whatever you need.
4. Hit Cmd/Ctrl+S.
5. The edit appears back on the originating node automatically.

**One-time setup worth doing:** set Photoshop's Maximize PSD Compatibility preference to **Always** (Preferences → File Handling → Maximize PSD Compatibility). Without it, Photoshop pops a compatibility dialog on every save of a layered file, since the hand-off file is a PSD — see [docs/INSTALL.md](docs/INSTALL.md).

## Tier 2 — installing the Photoshop plugin

Tier 2 is optional. Install it for instant (save-event) round trips, higher-fidelity pixels, and cross-machine editing.

Today the plugin installs as an **unpackaged developer plugin** (a packaged, one-click `.ccx` / Adobe Exchange install is on the roadmap — see below):

1. In Photoshop: **Preferences → Plugins → Enable Developer Mode**, then restart Photoshop.
2. Install Adobe's free **UXP Developer Tool (UDT)**.
3. In UDT: **Add Plugin** → select `photoshop_plugin/manifest.json` from your cloned copy of this repo, then **Load**.
4. The **ComfyUI** panel appears (Plugins menu). It auto-connects to `localhost:8188` by default; the pill shows **Connected** when it's talking to ComfyUI.

Once installed, a plain Cmd/Ctrl+S sends your edit back automatically — the panel's "Send" button (one per open document) is just a manual fallback for saves that don't fire a normal save event (e.g. Export As).

The plugin **sets Maximize PSD Compatibility to Always for you** the first time it connects — so you can skip the manual Tier-1 step above. It only writes the preference if it isn't already Always, logs what it did in the panel, and never blocks connecting if it can't. You can turn this off in the panel's **Advanced** section if you'd rather manage the preference yourself.

### Editing across two machines

To edit on one computer while ComfyUI runs on another:

1. Start ComfyUI on the server machine with `--listen` (so it's reachable over the network) and note its address.
2. In the plugin panel, open **Advanced → ComfyUI server (host:port)**, enter the server's address (e.g. `192.168.1.50:8188` or a Tailscale address), and press **Apply / Connect**.
3. Open an image from ComfyUI — it opens in Photoshop on *your* machine, and a plain Cmd/Ctrl+S sends the edit back. The PSD download and the edit upload both ride the same WebSocket connection (chunked), so the whole round trip works over the network, not just the connection.

This bridge's `/cpsb/*` routes live on ComfyUI's own server process, so `--listen` exposes them exactly as it exposes the rest of ComfyUI's API — no separate authentication layer — so only do this on a network you'd already trust with ComfyUI itself.

Only **one Photoshop holds the connection at a time** (ComfyUI keeps a single plugin slot). If you have Photoshop+plugin running on two machines pointed at the same ComfyUI, the most recent one to connect wins and the other **stands by** — no fighting. Use the panel's **Connect / Disconnect** button to choose which machine is active, or to bow out.

<details>
<summary><strong>Why PSD? (click to expand)</strong></summary>

The short version: PSD is the only format that lets a layered document save in place with a plain Cmd/Ctrl+S and no recurring per-save dialog.

- Since 2021, plain Cmd/Ctrl+S on a document **with any layer** is restricted by Photoshop to PSD, PSB, or TIFF — PNG and JPEG drop off the Save format list the instant a layer exists.
- A flat-PNG hand-off would work right up until you add your first adjustment layer, then silently stop round-tripping. Since "edit freely" is the whole point, we can't assume the image stays flat.
- TIFF supports the same plain-Cmd+S behavior, but pops a "TIFF Options" dialog on every save.
- PSD supports plain Cmd+S, in place, with layers, and no recurring dialog. The only friction is the one-time Maximize PSD Compatibility preference.

This is also why Lightroom's "Edit in Photoshop" — the feature this project is modeled on — hands off PSD/TIFF derivatives rather than flat images.

</details>

## What's not here yet

The round trip, the eleven nodes, the gallery, and cross-machine editing all work today. Still on the roadmap:

- **A packaged, signed plugin install** (`.ccx` / Adobe Exchange), so Tier 2 doesn't require developer mode + UDT.
- **A ComfyUI Registry listing** (so Manager can find it by search, not just Git URL).
- **A fuller Photoshop-side gallery** (the panel currently lists active hand-offs, not a browsable history).

## Limitations

- **The *edit* round trip returns a flat composite.** What comes back after a Photoshop save is the saved composite as a flat image — an edit isn't decomposed back into a layer stack. (Layers themselves *do* flow through the graph now — Load PSD's `layers` output and Compose's `layers` input, see [Layered images](#layered-images-psd-layers-in-and-out-of-the-graph) — with their own honesty notes: blending semantics differ slightly from Photoshop, text/smart-object layers arrive rasterized, adjustment layers are skipped.)
- **16-bit and non-RGB images are converted to RGB8.** CMYK, Grayscale, Lab, or 16-bit sources are converted on the way in — a plain, non-color-managed conversion (a CMYK PSD loads as recognizable RGB, not a colorimetric match). Full-fidelity high-bit-depth or color-managed round-tripping is out of scope. (Compose's *append-to-existing* is stricter: it refuses a non-RGB target outright rather than silently converting your artwork.)
- **`.tif`/`.tiff` load out of the box** in the Load PSD node (no extra dependency); no third-party image decoders are bundled. Illustrator `.ai` and camera raw/`.dng` open through Photoshop itself, via a dedicated Tier-2 "Open via Photoshop" node (see the roadmap).
- **Save-As to a different file or format breaks the automatic link.** The watcher only watches the exact managed hand-off path. If you Save As elsewhere, the document stays open in Photoshop — so it never shows as "Closed without saving" either — but no edit ever arrives at the card, which just sits at "Editing" with nothing to tell you why. Recover with drag-and-drop: drop the saved-elsewhere image onto that card in the sidebar gallery to import it manually.
- **Remote/headless ComfyUI needs Tier 2.** Tier 1 opens a local file and watches the local filesystem, so it needs Photoshop and ComfyUI on the same machine (with a GUI session). For remote ComfyUI, use the Tier 2 plugin and point it at the server's address.

## Troubleshooting

**ComfyUI logs `ModuleNotFoundError: No module named 'watchdog'` (or `psd_tools`, `PIL`, …) at startup.** The pack's dependencies aren't installed in the **same Python that runs ComfyUI**. Install them with that interpreter — from the pack's folder, `pip install -r requirements.txt` (portable Windows: `python_embeded\python.exe -m pip install -r ...`; a venv/conda ComfyUI: activate it first). If the log *also* shows a confusing `No module named 'cpsb'` right after, ignore that second one — it's a fallback import path failing; the first error is the real cause.

Note that a missing **`watchdog` alone does not stop the pack from loading**: every node, the gallery, and the whole Tier-2 plugin keep working, and only automatic **Tier-1** save detection is disabled — which is the right trade on a headless or remote ComfyUI (a Linux box or container), where Tier 1 can't work anyway because there's no local Photoshop to open the file in. The startup log says so explicitly.

**Photoshop asks about Maximize Compatibility on every save.** Set Preferences → File Handling → Maximize PSD Compatibility to **Always** (see Quick Start / [docs/INSTALL.md](docs/INSTALL.md)).

**My edit never comes back into ComfyUI.** Most likely you Save-As'd to a different file or location, which breaks the automatic link (see Limitations) — the card just sits at "Editing" with no chip to flag it, so drag-and-drop the saved-elsewhere image onto that card in the sidebar gallery to import it manually. Also confirm you actually saved, and give it a second to settle.

**"Open in Photoshop" is missing or disabled.** Your ComfyUI server is probably remote or headless — Tier 1 needs a local Photoshop with a GUI session. Install the Tier 2 plugin, which works over the network.

**The Tier 2 panel says "Disconnected" or keeps retrying.** If ComfyUI is still starting up, "Waiting for ComfyUI — retrying…" is normal; it connects on its own. Otherwise, check the **Advanced → ComfyUI server** address is right and that ComfyUI is reachable (started with `--listen` for a remote server, firewall open). A red **"Action needed"** line means a plugin network-permission problem specifically.

**Two machines keep swapping the connection.** Update to the latest plugin — the displaced Photoshop now stands by instead of fighting. Use the panel's **Connect / Disconnect** button to pick the active machine.

**Photoshop won't launch, or the wrong version opens.** Either Photoshop isn't installed, or multiple versions are and discovery picked one you didn't expect. Set an explicit Photoshop executable path via the pack's `photoshop_path` setting — there's no settings-panel row for it yet, so either send it to the running server (`curl -X POST http://<comfyui-host>:8188/cpsb/settings -d '{"photoshop_path": "<full path to Photoshop>"}'`, effective immediately), or add the same key to the `cpsb.json` file in ComfyUI's user directory (e.g. `user/default/cpsb.json`) and restart ComfyUI.

## Architecture

Everything lives inside ComfyUI's own server process — no second server, no extra port, no CORS surface to fight.

```
Right-click image
      |
      v
ComfyUI backend (on ComfyUI's own PromptServer)
      |   writes a handoff PSD under input/<managed>/<id>/
      |
      +-- Tier 1: OS opens Photoshop; a watchdog watches
      |           the handoff PSD for the save
      |
      +-- Tier 2: WebSocket "open_handoff" to the Photoshop
                  plugin (opens a local file path directly, or
                  streams the PSD over the same WebSocket when
                  ComfyUI and Photoshop are on different machines)

           User edits, hits Cmd/Ctrl+S

      +-- Tier 1: watchdog reads the Maximize-Compatibility
      |           composite back out of the saved PSD
      |
      +-- Tier 2: plugin exports a flattened PNG on Photoshop's
                  native `save` event and returns it — POSTed to
                  /cpsb/upload locally, or streamed back over the
                  WebSocket when cross-machine
      |
      v
Node updates in ComfyUI; sidebar gallery gets an entry
```

Both tiers converge on the same backend ingest step, so the rest of ComfyUI (caching, re-queueing, the sidebar gallery) never needs to know which tier delivered an edit. For the exact routes, WebSocket messages, and file formats, see [docs/PROTOCOL.md](docs/PROTOCOL.md) — the binding interface contract between the backend, the frontend, and the plugin.

## Documentation

- **[examples/README.md](examples/README.md)** — the example workflows index: which of the seven files need Photoshop, which need the plugin, and what each one teaches.
- **[docs/INSTALL.md](docs/INSTALL.md)** — detailed install steps and the Maximize Compatibility walkthrough.
- **[docs/PROTOCOL.md](docs/PROTOCOL.md)** — the interface contract (routes, WebSocket protocol, file schemas). Start here if you're building against this project or contributing code.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — development setup, code style, and PR expectations.

## License

MIT — see [LICENSE](LICENSE).
