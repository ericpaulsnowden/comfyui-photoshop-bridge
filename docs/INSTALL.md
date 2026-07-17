# Installing comfyui-photoshop-bridge

This page covers both tiers in detail. If you just want the short version, see the Quick Start in the [main README](../README.md).

> This project is pre-release (see the status note at the top of the [main README](../README.md)). The steps below describe the target install flow; check the Features table in the README for what's actually working today.

## Requirements

- **ComfyUI**: a reasonably current build of the Vue-based frontend (the one that provides `registerSidebarTab`, `getNodeMenuItems`, and the Settings API — anything from 2025 onward). No hard version pin exists yet; [docs/SPIKES.md](SPIKES.md) tracks the exact minimum once verified.
- **Photoshop**: 2025 (v26) or later. Earlier versions may work but are outside the supported/tested range.
- **Python dependencies** (installed automatically from `requirements.txt`): `watchdog` (filesystem watching for Tier 1), `psd-tools` (reading and writing PSD files), `Pillow` (image conversion). Nothing else.
- **Tier 2 only**: the Adobe Creative Cloud desktop app (for the `.ccx` install flow), or the UPIA command-line tool if you're installing without Creative Cloud's UI.

## Tier 1 — file hand-off

### Install via ComfyUI Manager

1. Open ComfyUI Manager from the ComfyUI sidebar.
2. Choose **Install via Git URL**.
3. Paste this repository's URL.
4. Restart ComfyUI when prompted.

This project isn't listed in the searchable Registry yet (tracked as milestone M6), so search-by-name won't find it until then — Install via Git URL works regardless.

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

### What the install looks like

1. Download the `.ccx` file (from this repo's releases, once published) or build it from `photoshop_plugin/` per its own build instructions.
2. Double-click the `.ccx` file.
3. The Adobe Creative Cloud desktop app opens with an install prompt. Because this plugin isn't distributed through the Adobe Exchange marketplace, expect a warning that it's from a developer outside the marketplace — that's expected, not an error. Click **Install**.
4. Launch (or relaunch) Photoshop. The plugin connects to ComfyUI automatically — there's no manual "connect" step. Check the ComfyUI sidebar for a "Photoshop: Connected" indicator.

### Command-line alternative (UPIA)

For scripted or unattended installs, Adobe's Unified Plugin Installer Agent (UPIA) can install a `.ccx` non-interactively. It ships with Creative Cloud Desktop (≥5.7).

**Windows:**
```
"C:\Program Files\Common Files\Adobe\Adobe Desktop Common\RemoteComponents\UPI\UnifiedPluginInstallerAgent\UnifiedPluginInstallerAgent.exe" /install comfyui-photoshop-bridge.ccx
```

**macOS:**
```bash
./UnifiedPluginInstallerAgent --install ~/Downloads/comfyui-photoshop-bridge.ccx
```
The macOS binary's exact folder varies by Creative Cloud version. If it isn't where you expect, search for it first (for example, `mdfind -name UnifiedPluginInstallerAgent` in Terminal) and run the command from that directory.

Other useful flags: `--list` (show installed plugins), `--remove <plugin-id>` (uninstall — run `--list` first to get the id), `--version`, `--help`.

### Developer Mode: status unconfirmed

Whether an unsigned `.ccx` requires enabling Photoshop's Developer Mode is **unconfirmed** — this is open spike 2 in [docs/SPIKES.md](SPIKES.md). The plan is to sign the plugin through Adobe's Developer Distribution program so the normal install (above) never requires Developer Mode for end users. Until that's verified: if the double-click or UPIA install is rejected as unsigned, enabling Developer Mode (UXP Developer Tool → Developer Mode toggle) may be needed as a fallback — treat this as provisional guidance, not a confirmed requirement, until spike 2 is checked off.

## Uninstalling

**Tier 1:**
- Via ComfyUI Manager: find comfyui-photoshop-bridge in your installed custom nodes list and choose Uninstall (or Disable), then restart ComfyUI.
- Manually: delete the `ComfyUI/custom_nodes/comfyui-photoshop-bridge` folder and restart ComfyUI.
- Optional cleanup: delete `ComfyUI/input/cpsb/` to remove hand-off history and thumbnails. This is safe — it only clears the gallery's history, not your workflows or generated images. (You can also leave it alone: entries older than the configured cleanup window are purged automatically on server start.)

**Tier 2:**
- Via the Creative Cloud desktop app: find the plugin under your installed apps/plugins and remove it there.
- Via UPIA: run `UnifiedPluginInstallerAgent --list` to find the plugin's id, then `UnifiedPluginInstallerAgent --remove <id>` (see the platform-specific paths above).
- Removing the plugin doesn't affect Tier 1 — the file hand-off workflow keeps working without it.
