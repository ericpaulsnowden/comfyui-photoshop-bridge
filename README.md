# comfyui-photoshop-bridge

Right-click any image in ComfyUI, choose **Open in Photoshop**, make your edit, hit Cmd/Ctrl+S — the result lands back on the same node automatically. It's the Lightroom "Edit in Photoshop" round trip, brought to ComfyUI: no exporting, no re-importing, no manual file juggling.

> **Project status: pre-1.0, actively developed.** The core round trip and the nodes below work today and are used day to day; expect rough edges and occasional breaking changes before 1.0. See the [releases/tags](https://github.com/ericpaulsnowden/comfyui-photoshop-bridge/tags) for the current version — the backend, frontend, and Photoshop plugin all report their version, and the ComfyUI sidebar shows an amber "update available" hint when they drift out of sync.

## How it works

comfyui-photoshop-bridge ships as a single ComfyUI custom node pack with two tiers. You use the same right-click workflow either way — the tier just changes what happens under the hood.

**Tier 1 — file hand-off (nothing to install beyond this node pack).** Choosing "Open in Photoshop" writes the image to a PSD in a folder the node pack manages, then asks the OS to open it in Photoshop. A file-watcher monitors that exact file, and the moment you save, ComfyUI reads Photoshop's own saved composite back out of the PSD and updates the node that started the hand-off. No plugin, no account, no server to run — this tier is the floor the whole project stands on.

**Tier 2 — Photoshop plugin (optional, one-time install).** A small UXP panel keeps a persistent connection to ComfyUI's own server. Instead of watching the filesystem, it listens for Photoshop's native save event and pushes a flattened export back over HTTP the instant you save — no file-watch delay, higher-fidelity pixels, and it works even when **ComfyUI runs on a different machine than Photoshop**, because the plugin fetches and returns images over the network. You point it at a ComfyUI address right in the panel.

Neither tier syncs individual Photoshop layers back into the ComfyUI graph — a ComfyUI image is a flat RGB tensor, so this is a whole-image round trip, not a layer-level bridge. (Your layers are preserved *in Photoshop* on the local/edit-in-place paths; they just don't flow into the graph as layers.)

## Nodes and features

Right-clicking an image and choosing **Open in Photoshop** is the core action, and it works on `LoadImage`-style nodes, generated previews, and saved outputs. On top of that, the pack adds five nodes (category `image/photoshop`):

- **Edit in Photoshop** — a node that opens its input in Photoshop and, in the default "Wait for first save" mode, *blocks* the workflow until you save, then continues with your edit. Also offers "Re-run on every save" and "Open only" modes, a timeout, and a cancel.
- **Load PSD** — start a workflow from a `.psd`/`.psb` — or a **`.tif`/`.tiff`** — in ComfyUI's input folder, with an on-node **preview** (rendered server-side, no Photoshop needed) and an optional "edit the original in place" mode. Outputs IMAGE + MASK. An **`on_save`** widget controls what a save in Photoshop actually does — *Re-run workflow* (default), *Update only* (take the edit, don't re-run), or *Ignore* (saving does nothing). Set it to Ignore when you want to open a PSD, shuffle layers, push one back and close, without the graph firing every time you hit save. It's enforced on the server, so it governs the plugin's **Send** button too, not just automatic saves.
- **Compose Layers to PSD** — stack multiple images into one layered, grouped PSD, then (by default) open it in Photoshop and block until you save. Outputs the flattened composite, the written PSD's filename, and a **`layers`** batch — one frame per layer — so a Preview node shows every layer individually instead of just the flat result. Leave the target empty and every run writes a fresh numbered PSD; **Browse…** to any PSD on the ComfyUI machine (or name a new one right in the dialog) and runs **accumulate into that single reviewable document**, each in its own numbered group. Writes are atomic, so a failed run can never truncate the document you've been collecting into — and safe to point at a file another node in this pack already has open (e.g. a Load PSD "edit original" target): a compose write into it is never mistaken for a Photoshop save on that other node's side. Whatever mode you use, the node shows **`Written: <filename>`** after a run with a **Copy Path** button (copies the full path on the ComfyUI machine) and offers **Open in Photoshop** on right-click — so a "Don't open (composite only)" run isn't a file you then have to go hunting for. You can also just point a **Load PSD** node at it: Compose writes into the same `input/` folder that node lists.
- **Annotate for Edit** — hand an image to Photoshop; it opens with an auto-created empty transparent **"Instructions"** layer. Just paint on that layer with any brush, any color, to mark a region; you can edit the base image too. Save, and you get back four outputs covering the three views of the result:
  - `image` — everything **but** your marks (your base edits baked in). Pair with `mask` for inpainting / mask-driven models.
  - `mask` — your marks alone.
  - `annotated` — image **and** marks combined, for visual-prompt edit models that take no mask ("edit what I circled"). The `box_composite` toggle picks the form: off = your real strokes, on = a tidy red box at their bounding box (what Kontext / Qwen-Image-Edit respond to).
  - `instruction` — your text, verbatim.

  Rename or delete the Instructions layer and it's just treated as a plain edited image. A **`mode`** widget matches the other nodes — *Wait for first save* (block until you save) or *Re-run on every save* (keep the doc open and re-run with your new mask each save, to iterate on the drawing), plus *Pass through*. A **Re-open in Photoshop** button on the node gets you back into your annotation — Instructions layer and strokes intact — after you've closed it.
- **Run Photoshop Action** — give it an image and the name of a **saved Photoshop Action** (plus its set), and it opens the image, plays that Action, and returns the processed result to your workflow — no manual step. This one **requires the Tier-2 plugin** (there's no way to trigger a Photoshop Action without it), and it says so clearly if the plugin isn't connected. Heads-up: an Action that pops an interactive dialog mid-run can stall Photoshop — use Actions that run start-to-finish unattended.

Handoffs opened in Photoshop are **named after the file they came from** — `Eric-Headshot.jpg` opens as `Eric-Headshot.psd`, not an anonymous `source.psd` — so document tabs and file dropdowns stay tellable-apart.

A **"Photoshop Edits" sidebar gallery** tracks every round trip for the workflow as a grid of cards: **Open** it again in Photoshop, **Add** it as a node, **Reveal** its origin node on the canvas, or **Remove** it from the list. Each card leads with the latest edit — hold the gallery's **"Hold to compare"** button to see every card's original at once. A card still `Editing` whose Tier-2 plugin has confirmed the document is closed shows "Closed without saving" instead of guessing from elapsed time. Any node that's waiting on Photoshop shows an "Editing in Photoshop…" badge with a working cancel.

<!-- demo.gif -->
*(Right-click an image → Open in Photoshop → edit → Cmd/Ctrl+S → the node updates. Demo GIF coming.)*

## Quick Start (Tier 1)

**Install**

- Via ComfyUI Manager: open Manager → **Install via Git URL** → paste this repository's URL. (Not yet in the searchable Registry — that's planned.)
- Or manually:
  ```bash
  cd ComfyUI/custom_nodes
  git clone https://github.com/ericpaulsnowden/comfyui-photoshop-bridge.git comfyui-photoshop-bridge
  pip install -r comfyui-photoshop-bridge/requirements.txt
  ```
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

The plugin **sets Maximize PSD Compatibility to Always for you** the first time it connects (v0.5.31) — so you can skip the manual Tier-1 step above. It only writes the preference if it isn't already Always, logs what it did in the panel, and never blocks connecting if it can't. You can turn this off in the panel's **Advanced** section if you'd rather manage the preference yourself.

### Editing across two machines

To edit on one computer while ComfyUI runs on another:

1. Start ComfyUI on the server machine with `--listen` (so it's reachable over the network) and note its address.
2. In the plugin panel, open **Advanced → ComfyUI server (host:port)**, enter the server's address (e.g. `192.168.1.50:8188` or a Tailscale address), and press **Apply / Connect**.
3. Open an image from ComfyUI — it opens in Photoshop on *your* machine, and a plain Cmd/Ctrl+S sends the edit back. The PSD download and the edit upload both ride the same WebSocket connection (chunked), so the whole round trip works over the network, not just the connection.

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

The round trip, the four nodes, the gallery, and cross-machine editing all work today. Still on the roadmap:

- **A packaged, signed plugin install** (`.ccx` / Adobe Exchange), so Tier 2 doesn't require developer mode + UDT.
- **A ComfyUI Registry listing** (so Manager can find it by search, not just Git URL).
- **A fuller Photoshop-side gallery** (the panel currently lists active hand-offs, not a browsable history).
- **Auto-setting Maximize PSD Compatibility** at connect time, to retire that one-time manual step.

## Limitations

- **Layers don't round-trip into the graph.** A ComfyUI image is flat RGB, so what returns to the node is always a flattened raster. Your layers survive in Photoshop (and on the edit-in-place path, in the file), but aren't exposed as layers in ComfyUI.
- **16-bit and non-RGB images are converted to RGB8.** CMYK, Grayscale, Lab, or 16-bit sources are converted on the way in — a plain, non-color-managed conversion (a CMYK PSD loads as recognizable RGB, not a colorimetric match). Full-fidelity high-bit-depth or color-managed round-tripping is out of scope. (Compose's *append-to-existing* is stricter: it refuses a non-RGB target outright rather than silently converting your artwork.)
- **`.tif`/`.tiff` load out of the box** in the Load PSD node (no extra dependency); no third-party image decoders are bundled. Illustrator `.ai` and camera raw/`.dng` open through Photoshop itself, via a dedicated Tier-2 "Open via Photoshop" node (see the roadmap).
- **Save-As to a different file or format breaks the automatic link.** The watcher only watches the exact managed hand-off path. If you Save As elsewhere, the document stays open in Photoshop — so it never shows as "Closed without saving" either — but no edit ever arrives at the card, which just sits at "Editing" with nothing to tell you why. Recover with drag-and-drop: drop the saved-elsewhere image onto that card in the sidebar gallery to import it manually.
- **Remote/headless ComfyUI needs Tier 2.** Tier 1 opens a local file and watches the local filesystem, so it needs Photoshop and ComfyUI on the same machine (with a GUI session). For remote ComfyUI, use the Tier 2 plugin and point it at the server's address.

## Troubleshooting

**Photoshop asks about Maximize Compatibility on every save.** Set Preferences → File Handling → Maximize PSD Compatibility to **Always** (see Quick Start / [docs/INSTALL.md](docs/INSTALL.md)).

**My edit never comes back into ComfyUI.** Most likely you Save-As'd to a different file or location, which breaks the automatic link (see Limitations) — the card just sits at "Editing" with no chip to flag it, so drag-and-drop the saved-elsewhere image onto that card in the sidebar gallery to import it manually. Also confirm you actually saved, and give it a second to settle.

**"Open in Photoshop" is missing or disabled.** Your ComfyUI server is probably remote or headless — Tier 1 needs a local Photoshop with a GUI session. Install the Tier 2 plugin, which works over the network.

**The Tier 2 panel says "Disconnected" or keeps retrying.** If ComfyUI is still starting up, "Waiting for ComfyUI — retrying…" is normal; it connects on its own. Otherwise, check the **Advanced → ComfyUI server** address is right and that ComfyUI is reachable (started with `--listen` for a remote server, firewall open). A red **"Action needed"** line means a plugin network-permission problem specifically.

**Two machines keep swapping the connection.** Update to the latest plugin — the displaced Photoshop now stands by instead of fighting. Use the panel's **Connect / Disconnect** button to pick the active machine.

**Photoshop won't launch, or the wrong version opens.** Either Photoshop isn't installed, or multiple versions are and discovery picked one you didn't expect. Set an explicit Photoshop executable path in this node pack's settings.

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

- **[docs/INSTALL.md](docs/INSTALL.md)** — detailed install steps and the Maximize Compatibility walkthrough.
- **[docs/PROTOCOL.md](docs/PROTOCOL.md)** — the interface contract (routes, WebSocket protocol, file schemas). Start here if you're building against this project or contributing code.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — development setup, code style, and PR expectations.

## License

MIT — see [LICENSE](LICENSE).
