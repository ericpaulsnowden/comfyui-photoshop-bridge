# Roadmap

Planned, researched work broken into user-facing milestones. Each file is a living roadmap — updated as milestones ship or priorities change. These are *direction*, not commitments; the source of truth for shipped behavior stays `docs/PROTOCOL.md`.

| Roadmap | Status | Summary |
| --- | --- | --- |
| [ps-external-decode.md](ps-external-decode.md) | M1 shipped (v0.5.40); M2 (.ai node) pending | Decode `.ai` (then `.dng`) by routing the file through the connected Photoshop plugin instead of third-party Python libs — a new dedicated Tier-2 node. |
| [realtime-drawing.md](realtime-drawing.md) | planned, spikes not run | Draw in Photoshop, get ~1s live ComfyUI re-renders: save-free capture loop → Live Canvas node → result in a docked PS preview panel. Architecture ported from Krita AI Diffusion's proven live mode. |

Convention: milestones are ordered so each is independently useful; risky, plugin-dependent milestones are spike-gated (a live Photoshop test must pass before they ship), mirroring `docs/SPIKES.md`.
