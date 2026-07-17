# comfyui-photoshop-bridge

Right-click any image in ComfyUI, choose **Open in Photoshop**, make your edit, hit Cmd/Ctrl+S — the result lands back on the same node automatically. It's the Lightroom "Edit in Photoshop" round trip, brought to ComfyUI: no exporting, no re-importing, no manual file juggling.

> **Project status: pre-release.** This repo is in active development. We're finishing M0 — verifying the riskiest technical assumptions ([docs/SPIKES.md](docs/SPIKES.md)) before locking in the M1 implementation. The Quick Start below describes the experience we're building toward; the Features table further down is the honest, current answer to "does this actually work yet."

## How it works

comfyui-photoshop-bridge ships as a single ComfyUI custom node pack with two tiers. You always use the same right-click workflow — the tier just changes what happens under the hood.

**Tier 1 — file hand-off (zero install beyond this node pack).** Choosing "Open in Photoshop" converts the image to a PSD inside a folder the node pack manages, then asks the OS to open it in Photoshop. A file-watcher monitors that exact file, and the moment you save, ComfyUI reads Photoshop's own saved composite back out of the PSD and updates the node that started the hand-off. No plugin, no account, no server to run — this tier is the floor the whole project stands on.

**Tier 2 — Photoshop plugin (optional, one-time install).** A small UXP panel keeps a persistent connection to ComfyUI's own server. Instead of watching the filesystem, it listens for Photoshop's native save event and pushes a flattened export back over HTTP the instant you save — no file-watch delay, and it works even when ComfyUI runs on a different machine than Photoshop, since the plugin fetches and returns images over the network instead of relying on a shared local path.

Neither tier syncs individual Photoshop layers back into the ComfyUI graph. This is a whole-image round trip, not a layer-level bridge.

<!-- demo.gif -->
*(Right-click an image → Open in Photoshop → edit → Cmd/Ctrl+S → the node updates. Demo GIF coming once M1 ships.)*

## Quick Start (Tier 1)

**Install**

- Via ComfyUI Manager: open Manager → **Install via Git URL** → paste this repository's URL. (Not yet listed in the searchable Registry — that's tracked as milestone M6.)
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

**One-time setup worth doing:** set Photoshop's Maximize PSD Compatibility preference to **Always** (Preferences → File Handling → Maximize PSD Compatibility). Why: without it, Photoshop pops a compatibility dialog on every single save of a layered file, since the hand-off file is a PSD — see [docs/INSTALL.md](docs/INSTALL.md) for the exact walkthrough.

## Upgrading to Tier 2

Installing the Photoshop plugin gets you:

- **Instant round trips** — no file-watch delay, since Photoshop's own save event triggers the export.
- **Remote ComfyUI support** — Tier 1 requires Photoshop and ComfyUI on the same machine; Tier 2 works over the network.
- **A live connection indicator** in the ComfyUI sidebar ("Photoshop: Connected").
- **Pixel-perfect exports** straight from Photoshop's own flattened render, rather than relying on the PSD's embedded composite.

Install is a one-time `.ccx` double-click: download (or build from `photoshop_plugin/`) the `.ccx` file, double-click it, and Creative Cloud shows its install dialog — click Install. The plugin auto-connects the next time Photoshop launches. Full walkthrough, plus a command-line alternative for scripted installs, is in [docs/INSTALL.md](docs/INSTALL.md).

Honest caveat: Tier 2 still opens a PSD (an untitled document would force a where-to-save dialog on every save, which is worse), so the same one-time Maximize Compatibility preference above still applies for now. We're investigating letting the plugin set that preference automatically at connect time ([docs/SPIKES.md](docs/SPIKES.md), spike 8) — until that's confirmed, do the one-time nudge either way.

<details>
<summary><strong>Why PSD? (click to expand)</strong></summary>

The short version: PSD is the only format that lets a layered document save in place with a plain Cmd/Ctrl+S, with no recurring per-save dialog.

- Since 2021, plain Cmd/Ctrl+S on a document **with any layer** is restricted by Photoshop to PSD, PSB, or TIFF — PNG and JPEG drop off the Save/Save As format list the instant a layer exists.
- A flat-PNG hand-off would work right up until you add your first adjustment layer, then silently stop round-tripping. Since "edit freely" is the whole point, we can't assume the image stays flat.
- TIFF supports the same plain-Cmd+S, layers-allowed behavior, but pops a "TIFF Options" dialog on every single save — a longstanding, unresolved complaint.
- PSD supports plain Cmd+S, in place, with layers, and no recurring dialog. The only friction is the Maximize PSD Compatibility preference (see Quick Start above), which is a one-time setting, not a per-save one.

This is also why Lightroom's "Edit in Photoshop" — the feature this project is modeled on — hands off PSD/TIFF derivatives rather than flat images.

</details>

## Features

Nothing below is implemented yet — we're in M0, verifying technical assumptions ([docs/SPIKES.md](docs/SPIKES.md)) before M1 begins. This table will move rows from Planned to Working as milestones land.

| Feature | Milestone | Status |
|---|---|---|
| Right-click "Open in Photoshop" (Tier 1 file hand-off) | M1 | Planned |
| Automatic paste-back into `LoadImage`-class nodes | M1 | Planned |
| Terminal-output handling (SaveImage/PreviewImage), Edit Original vs. Start Fresh, onboarding modal, Save-As detection | M2 | Planned |
| ComfyUI sidebar gallery ("Photoshop Edits") | M3 | Planned |
| Photoshop Bridge node (blocking wait-for-edit, cancel, timeout) | M3 | Planned |
| Tier 2 Photoshop plugin (instant save events, remote ComfyUI support) | M4 | Planned |
| Photoshop-side panel gallery ("Generated in ComfyUI" / "Sent back") | M5 | Planned |
| ComfyUI Registry listing, signed `.ccx` installer | M6 | Planned |

## Limitations

- **Remote ComfyUI needs Tier 2.** Tier 1 opens a local file and watches the local filesystem, so Photoshop and ComfyUI must run on the same machine. If your ComfyUI runs elsewhere, install the Tier 2 plugin.
- **Tier 2's MVP only reaches ComfyUI's default port (8188).** The plugin's network permissions are fixed at install time, so a ComfyUI server on a non-default port is invisible to it. This is a documented limitation, not a silent failure — the connection indicator just stays disconnected.
- **16-bit and non-RGB images are converted to RGB8.** ComfyUI's image tensors are 8-bit RGB internally, so CMYK, Grayscale, Lab, or 16-bit sources get converted on the way back in, with a toast telling you it happened. Full-fidelity high-bit-depth or color-managed round-tripping is not a goal of this project.
- **Save-As to a different file or format breaks the automatic link.** The watcher only watches the exact managed hand-off path. If you Save As elsewhere, nothing comes back automatically — the sidebar gallery marks that hand-off "Stale," and accepts drag-and-drop of any image as a manual import to recover it.

## Troubleshooting

**Photoshop asks about Maximize Compatibility on every save.**
Set Preferences → File Handling → Maximize PSD Compatibility to Always (see Quick Start / [docs/INSTALL.md](docs/INSTALL.md)). Left on "Ask" (Photoshop's default), it prompts on every save of a layered file.

**My edit never comes back into ComfyUI.**
Most likely you Save-As'd to a different file or location, which breaks the automatic link (see Limitations) — check the sidebar gallery for a "Stale" chip and use drag-and-drop import to recover it manually. Also confirm you actually saved (closing without saving fires no event), and give it a couple of seconds to settle.

**"Open in Photoshop" is missing or disabled.**
Your ComfyUI server is probably remote or headless — Tier 1 needs a local Photoshop on the same machine with a GUI session. Install the Tier 2 plugin, which works over the network.

**Photoshop won't launch, or the wrong version opens.**
Either Photoshop isn't installed, or multiple versions are and the discovery logic picked one you didn't expect. Set an explicit Photoshop executable path in this node pack's settings.

**The Tier 2 status indicator says "Not connected."**
Confirm Photoshop was relaunched after installing the plugin, and that ComfyUI is running on the default port 8188 (Tier 2's MVP can't reach non-default ports — see Limitations). If both check out, see the known localhost-permission issue tracked in [docs/SPIKES.md](docs/SPIKES.md) (spike 2).

## Architecture

Everything lives inside ComfyUI's own server process — there's no second server, no extra port, no CORS surface to fight.

```
Right-click image
      |
      v
ComfyUI backend (on ComfyUI's own PromptServer)
      |   writes a handoff PSD under input/cpsb/<id>/
      |
      +-- Tier 1: OS opens Photoshop; a watchdog observer
      |           watches source.psd for the save
      |
      +-- Tier 2: WebSocket "open_handoff" to the Photoshop
                  plugin (local file path, or fetched over
                  HTTP if ComfyUI and Photoshop aren't on
                  the same machine)

           User edits, hits Cmd/Ctrl+S

      +-- Tier 1: watchdog reads the Maximize-Compatibility
      |           composite back out of the saved PSD
      |
      +-- Tier 2: plugin exports a flattened PNG on
                  Photoshop's native `save` event, POSTs
                  it to /cpsb/upload
      |
      v
Node updates in ComfyUI; sidebar gallery gets an entry
```

Both tiers converge on the same backend ingest step, so the rest of ComfyUI (caching, re-queueing, the sidebar gallery) never needs to know which tier delivered an edit. For the exact routes, WebSocket messages, and file formats, see [docs/PROTOCOL.md](docs/PROTOCOL.md) — it's the binding interface contract between the backend, the frontend, and the Photoshop plugin.

## Documentation

- **[docs/INSTALL.md](docs/INSTALL.md)** — detailed install steps for both tiers, uninstalling, and the Maximize Compatibility walkthrough.
- **[docs/SPIKES.md](docs/SPIKES.md)** — the M0 verification checklist we're working through right now.
- **[docs/PROTOCOL.md](docs/PROTOCOL.md)** — the interface contract (routes, WebSocket protocol, file schemas). Start here if you're building against this project or contributing code.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — development setup, code style, and PR expectations.

## License

MIT — see [LICENSE](LICENSE).
