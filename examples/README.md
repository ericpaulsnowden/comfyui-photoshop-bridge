# Example workflows

## `annotate-qwen-image-edit.json` — Annotate for Edit → Qwen-Image-Edit

ComfyUI's native **Qwen-Image-Edit** template with this pack's **Annotate for
Edit** (`PhotoshopAnnotate`) node spliced into the image path, so you mark up
the image in Photoshop *before* Qwen edits it.

### Flow

```
Load Image ──▶ Annotate for Edit ──▶ Qwen-Image-Edit (subgraph) ──▶ Save Image
                    │  (annotated: image with a red box drawn at your region)
                    └──▶ (also feeds the template's ImageScaleToTotalPixels node)
```

1. Pick an image in **Load Image**.
2. **Queue** the workflow. **Annotate for Edit** opens that image in Photoshop
   and pauses the run there (`annotate_mode = Open in Photoshop (mask from
   edits)`).
3. In Photoshop, **paint a box / region** over what you want to change — any
   tool, any color — then **save** (Cmd/Ctrl+S).
4. The run resumes. The node derives a mask from the pixel diff, composites a
   red box at that region (`box_composite = true`), and passes the annotated
   image on to Qwen-Image-Edit → Save Image.

### The prompt

The instruction you describe is **not auto-wired** into the Qwen prompt: the
template surfaces its prompt as a *promoted subgraph widget*, not an exposed
input socket, so type your edit instruction directly into the **`prompt`**
field on the Qwen-Image-Edit node. (The Annotate node also outputs the typed
`instruction` as a STRING if you want to route it elsewhere.)

### How to open it

- **Workflow menu → Open** and pick this `.json`, **or**
- **drag the `.json` onto the ComfyUI canvas**.

### Requirements

- **Qwen-Image-Edit model files** — the template's own **For Local User** note
  (kept in the workflow) lists every `text_encoders` / `diffusion_models` /
  `loras` / `vae` file and where to put it.
- **This node pack** (`comfyui-photoshop-bridge`) installed as a ComfyUI custom
  node, so `PhotoshopAnnotate` resolves.
- **The Photoshop plugin is optional.** The node opens Photoshop through the
  bridge's tier-selecting seam and also works via the file-watch tier, so a
  plain Photoshop install is enough to round-trip the image.
