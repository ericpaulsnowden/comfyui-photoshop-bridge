# Plugin icons — TEMPORARY placeholders

These PNGs exist so Adobe's UXP packager (UXP Developer Tool → Package)
has the icon files `manifest.json` declares; without them packaging fails.
They are a generated stand-in (rounded outline + "Ps"), not final art.

| File | Size | Used for |
|---|---|---|
| `plugin-icon.png` / `plugin-icon@2x.png` | 48 / 96 px | plugin list (`species: pluginList`, all themes) |
| `dark.png` / `dark@2x.png` | 23 / 46 px | panel tab icon on darkest/dark/medium UI themes |
| `light.png` / `light@2x.png` | 23 / 46 px | panel tab icon on lightest/light UI themes |

UXP resolves `"scale": [1, 2]` by looking for the declared path at 1x and
the same name with `@2x` inserted before the extension at 2x — keep the
pairs together when replacing them. Replace with final artwork at the same
sizes and filenames and nothing in `manifest.json` needs to change.
