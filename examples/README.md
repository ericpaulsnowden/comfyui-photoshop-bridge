# Example workflows

Seven workflows, simplest first. **Every one of them is annotated on the canvas** —
drag the `.json` in, read the yellow notes, and you shouldn't need this page at
all. This is here for picking which one to open.

**How to open any of them:** *Workflow → Open* and pick the `.json`, or just
drag the `.json` onto the ComfyUI canvas.

**All of them need this node pack installed** as a ComfyUI custom node, so the
`Photoshop*` node types resolve.

| # | Workflow | Photoshop? | Plugin? | Models? |
|---|----------|-----------|---------|---------|
| 1 | [`annotate-mask-passthrough.json`](#1-annotate-mask-passthroughjson) | no | no | none |
| 2 | [`compose-layers-to-psd.json`](#2-compose-layers-to-psdjson) | no | no | none |
| 3 | [`load-psd-start-a-workflow.json`](#3-load-psd-start-a-workflowjson) | only to send it back | no | none |
| 4 | [`edit-in-photoshop-roundtrip.json`](#4-edit-in-photoshop-roundtripjson) | **yes** | optional | none |
| 5 | [`run-photoshop-action.json`](#5-run-photoshop-actionjson) | **yes** | **required** | none |
| 6 | [`annotate-qwen-image-edit.json`](#6-annotate-qwen-image-editjson) | **yes** | optional | Qwen-Image-Edit (multi-GB) |
| 7 | [`live_drawing_lcm.json`](#7-live_drawing_lcmjson) | **yes** | **required** | a fast few-step checkpoint |

---

## 1. `annotate-mask-passthrough.json`

**The simplest thing in this pack, and the one that proves a point: Photoshop
is genuinely optional.** **Annotate for Edit**'s default mode — *Pass
through* — never opens Photoshop at all; it just relays whatever mask you
connect.

```
Load Image ──IMAGE──▶ Annotate for Edit ──image──▶ Preview Image
 Solid Mask ──MASK──▶      (Pass through)   └mask──▶ MaskToImage ──▶ Preview Image
```

- **Needs:** nothing. No Photoshop, no plugin, no model — `mode` ships at its
  default, **Pass through**, so the node never launches anything.
- **Expect:** a sub-second run and two previews — your image, untouched, and a
  solid gray square that's exactly the **Solid Mask** value you set.
- **Shows:** that the `mask` input is a normal optional socket ("any mask
  source already in your graph works," per the main README), and that Pass
  through relays it unchanged rather than drawing on it.
- **Then try:** swap **Solid Mask** for a real mask — from an inpainting node,
  a segmentation model, anything — and watch the second preview follow it
  exactly. Switch `mode` to *Wait for first save* and you're doing what
  `annotate-qwen-image-edit.json` does instead.

## 2. `compose-layers-to-psd.json`

**Start here if you don't have Photoshop open.** Three images — a flat blue
backdrop, `example.png`, and a half-size inverted copy of it — go into
**Compose Layers to PSD** and come back out as one layered `.psd` written to
`ComfyUI/input/compose_00001.psd`.

- **Needs:** nothing. No Photoshop, no plugin, no model. `mode` ships as
  *Don't open (composite only)*, so the node writes the file and returns the
  result without launching anything.
- **Expect:** a sub-second run and three previews — the flattened composite,
  the three layers one at a time, and the written filename as text.
- **Shows:** layer stacking (`image_1` is the bottom), that the canvas is sized
  to the largest input with smaller layers centred and never rescaled, naming a
  layer by double-clicking its `image_N` socket, and the difference between the
  `image` output (flattened) and the `layers` output (a batch, one per layer).
- **Then try:** switch `mode` to *Wait for first save* and queue again — now it
  opens the PSD in Photoshop and waits for you.

## 3. `load-psd-start-a-workflow.json`

The other direction: **Load PSD** reads a `.psd` / `.psb` / `.tif` / `.tiff`
out of `ComfyUI/input/`, flattens it, and hands it to the graph. A second
branch turns the MASK output into an image you can actually look at.

- **Needs:** a layered file. The `psd` widget ships **blank on purpose** —
  anything typed in there wouldn't exist on your machine. Use the **Upload
  Photoshop File** button on the node, drag a `.psd` onto it, or drop one into
  `ComfyUI/input/`. No PSD handy? Run workflow #2
  (`compose-layers-to-psd.json`) once; it writes you one.
- **Expect:** the flattened file in the top preview, and a mostly-black mask in
  the lower one (it marks transparency, so a file with a full background layer
  is correctly all black).
- **Shows:** `edit_original` (off = your original is never touched) and the
  three `on_save` behaviours, plus the right-click **Open in Photoshop** route
  back out.

## 4. `edit-in-photoshop-roundtrip.json`

**The pack's hello world.** Load an image → **Edit in Photoshop** → preview
what came back. The run pauses at the node, Photoshop opens the image, and the
moment you save it carries on with your edit.

- **Needs:** Photoshop. **The plugin is optional** — without it the node opens
  the file and watches for a save; with it the same round trip is instant.
- **Expect:** the workflow to sit and wait at the bridge node until you save,
  then finish. Erase part of the image before you save and the second preview
  lights up white where you erased.
- **Shows:** what each of the three `mode` values does (wait once / re-queue on
  every save / open and move on), and what the MASK output actually marks —
  transparency in your edit, not a selection.

## 5. `run-photoshop-action.json`

Load an image → **Run Photoshop Action** plays one of your saved Photoshop
Actions on it automatically → preview and save the result.

- **Needs:** Photoshop **and the panel plugin**. This is the one node in the
  pack with no plugin-free path — only the plugin can reach Photoshop's Actions
  panel.
- **Needs from you:** `action_name` and `action_set` ship blank. Type them
  exactly as they appear in Photoshop's Actions panel before you queue. And
  turn off every "show dialog" toggle inside the Action, or Photoshop stops on
  a modal and your run waits out `timeout_seconds`.
- **Expect:** nothing at all until those two fields are filled in.

## 6. `annotate-qwen-image-edit.json`

ComfyUI's native **Qwen-Image-Edit** template with this pack's **Annotate for
Edit** (`PhotoshopAnnotate`) spliced into the image path, so you mark up the
image *before* Qwen edits it.

```
Load Image ──▶ Annotate for Edit ──▶ Qwen-Image-Edit (subgraph) ──▶ Save Image
                    │  (annotated: image with a red box drawn at your region)
                    └──▶ (also feeds the template's ImageScaleToTotalPixels node)
```

1. Pick an image in **Load Image**.
2. **Queue.** **Annotate for Edit** opens that image in Photoshop and pauses the
   run there (this file ships `mode = Wait for first save`).
3. In Photoshop, **paint a box / region** on the auto-created "Instructions"
   layer — any tool, any colour — then **save** (Ctrl/Cmd+S).
4. The run resumes. The node derives a mask from that layer's painted pixels,
   composites a red box at that region (`box_composite = true`), and passes the
   annotated image on to Qwen-Image-Edit → Save Image.

**The prompt is not auto-wired.** The template surfaces its prompt as a
*promoted subgraph widget*, not an exposed input socket, so type your edit
instruction directly into the **`prompt`** field on the Qwen-Image-Edit node.
(The Annotate node also outputs the typed `instruction` as a STRING if you want
to route it somewhere else.)

- **Needs:** the **Qwen-Image-Edit model files** — the template's own *For Local
  User* note, kept in the workflow, lists every `text_encoders` /
  `diffusion_models` / `loras` / `vae` file and where to put it. Plus
  Photoshop; the plugin is optional.
- **Point Load Image at your own file.** It ships pointing at the ComfyUI
  template's sample image, which won't be in your `input/` folder.
- **Note:** `PhotoshopAnnotate`'s *default* mode is **Pass through**, which
  never opens Photoshop and reads a connected `mask` input instead. This file
  deliberately uses the Photoshop path.

## 7. `live_drawing_lcm.json`

**The realtime one.** You draw in Photoshop, ComfyUI re-renders continuously,
and the result streams back into the panel's preview pane. A second, muted
branch is the one-shot **Refine** pass (2× upscale + a low-denoise polish) that
the panel's REFINE button fires.

- **Needs:** Photoshop **and the panel plugin** (this is the only way to drive
  the live loop), plus a **fast few-step checkpoint**. A plain model will not
  work at 4 steps. The on-canvas *READ ME* note names two known-good options
  and what to change for each — read it before you queue.
- **Point the Checkpoint loader at your own model.** The value saved in the
  file won't exist on your machine.
- **Expect:** nothing until you hit **Start Live** in Photoshop's *ComfyUI*
  panel (Plugins → ComfyUI Bridge → ComfyUI). Prompt and Creativity are driven
  from the *ComfyUI Preview* panel, not from the graph — you never have to edit
  this workflow to change them.
