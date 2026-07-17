# Installing comfyui-photoshop-bridge

This page covers both tiers in detail. If you just want the short version, see the Quick Start in the [main README](../README.md).

> This project is pre-1.0 and actively developed (see the status note at the top of the [main README](../README.md)). Tier 1 and the Tier 2 plugin both work today; Tier 2 currently installs as a UXP developer plugin (a packaged one-click install is on the roadmap).

## Requirements

- **ComfyUI**: a reasonably current build of the Vue-based frontend (the one that provides `registerSidebarTab`, `getNodeMenuItems`, and the Settings API — anything from 2025 onward). No hard version pin exists yet; [docs/SPIKES.md](SPIKES.md) tracks the exact minimum once verified.
- **Photoshop**: 2025 (v26) or later. Earlier versions may work but are outside the supported/tested range.
- **Python dependencies** (installed automatically from `requirements.txt`): `watchdog` (filesystem watching for Tier 1), `psd-tools` (reading and writing PSD files), `Pillow` (image conversion). Nothing else.
- **Tier 2 only**: Adobe's free **UXP Developer Tool (UDT)**, used to load the plugin (today's developer-plugin install). A packaged `.ccx` install via the Creative Cloud desktop app is planned but not available yet.

## Tier 1 — file hand-off

### Install via ComfyUI Manager

1. Open ComfyUI Manager from the ComfyUI sidebar.
2. Choose **Install via Git URL**.
3. Paste this repository's URL.
4. Restart ComfyUI when prompted.

This project isn't listed in the searchable Registry yet (it's on the roadmap), so search-by-name won't find it until then — Install via Git URL works regardless.

### Install manually (git clone)

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/ericpaulsnowden/comfyui-photoshop-bridge.git comfyui-photoshop-bridge
pip install -r comfyui-photoshop-bridge/requirements.txt
```

Restart ComfyUI. No further configuration is required — Tier 1 works out of the box on any machine that has both ComfyUI and Photoshop installed locally (see the README's Limitations for the remote-ComfyUI caveat).

### Recommended one-time preference: Maximize PSD Compatibility

Every Tier 1 hand-off is a PSD file (see "Why PSD?" in the README for the full reasoning). Photoshop's **Maximize PSD Compatibility** preference controls whether saving a layered PSD pops a dialog asking you to confirm, and it defaults to "Ask."

**Menu path:**
- macOS: **Photoshop → Preferences → File Handling…**
- Windows: **Edit → Preferences → File Handling…**
- In the dialog, under **File Compatibility**, find **Maximize PSD and PSB File Compatibility** and change the dropdown from **Ask** to **Always**.

**Before:** every time you save a layered PSD, Photoshop interrupts you with a dialog asking whether to include a compatibility composite — you have to click through it on every single save.

**After:** Photoshop silently includes that composite on every save, with no dialog. This matters beyond convenience: that embedded composite is exactly what comfyui-photoshop-bridge reads back as your edit's pixels — Photoshop's own pixel-accurate render, not a re-interpretation of your layers. Skipping this step doesn't break the round trip: if you decline and save without a compatible composite, the backend falls back to re-compositing the PSD itself, with a warning that some effects may not render exactly as they appear in Photoshop.

## Tier 2 — Photoshop plugin

Tier 2 is optional. It adds instant (save-event) round trips, higher-fidelity pixel exports, and cross-machine editing (Photoshop and ComfyUI on different computers).

### Installing today (UXP developer plugin)

The plugin currently installs as an unpackaged UXP developer plugin loaded through Adobe's UXP Developer Tool. A packaged, signed one-click install is on the roadmap (see "Packaged install (planned)" below).

1. **Enable Developer Mode in Photoshop:** Preferences → Plugins → **Enable Developer Mode**, then restart Photoshop.
2. **Install the Adobe UXP Developer Tool (UDT)** — a free download from Adobe.
3. **Load the plugin in UDT:** click **Add Plugin**, select `photoshop_plugin/manifest.json` from your cloned copy of this repo, then use its **Load** action.
4. The **ComfyUI** panel appears under Photoshop's **Plugins** menu. It connects to `localhost:8188` automatically — no manual "connect" step for the local case. The panel's pill reads **Connected** when it's talking to ComfyUI.

**Editing across machines:** open the panel's **Advanced → ComfyUI server (host:port)**, enter the other machine's address (that ComfyUI must be started with `--listen`), and press **Apply / Connect**. ComfyUI keeps a single plugin slot, so if two machines run the plugin against the same server the latest to connect wins and the other **stands by** — use the panel's **Connect / Disconnect** button to pick the active machine.

**After updating:** `git pull`, then reload the plugin in UDT to pick up the new version. The panel's Advanced section shows the plugin and server versions so you can confirm they're in sync.

### Packaged install (planned)

Once the plugin is packaged and signed, install will be a one-click `.ccx`: download it from this repo's releases (or build it from `photoshop_plugin/`), double-click, and the Creative Cloud desktop app shows an install prompt — no Developer Mode needed. For scripted installs, Adobe's Unified Plugin Installer Agent (UPIA, ships with Creative Cloud ≥5.7) can install a `.ccx` non-interactively (`UnifiedPluginInstallerAgent --install comfyui-photoshop-bridge.ccx`, with `--list` / `--remove <id>` to manage it). **This path isn't available yet** — packaging/signing is tracked on the roadmap.

## Uninstalling

**Tier 1:**
- Via ComfyUI Manager: find comfyui-photoshop-bridge in your installed custom nodes list and choose Uninstall (or Disable), then restart ComfyUI.
- Manually: delete the `ComfyUI/custom_nodes/comfyui-photoshop-bridge` folder and restart ComfyUI.
- Optional cleanup: delete `ComfyUI/input/cpsb/` to remove hand-off history and thumbnails. This is safe — it only clears the gallery's history, not your workflows or generated images. (You can also leave it alone: entries older than the configured cleanup window are purged automatically on server start.)

**Tier 2 (developer-plugin install):**
- In the UXP Developer Tool, select the plugin and use its **Unload** / remove action; it won't load on the next Photoshop launch.
- (Once the packaged `.ccx` install ships, you'll also be able to remove it from the Creative Cloud desktop app or via UPIA `--remove <id>`.)
- Removing the plugin doesn't affect Tier 1 — the file hand-off workflow keeps working without it.
