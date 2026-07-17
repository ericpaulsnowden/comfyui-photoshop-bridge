# Contributing to comfyui-photoshop-bridge

Thanks for taking a look. This project is pre-release — we're in M0, verifying the riskiest technical assumptions ([docs/SPIKES.md](docs/SPIKES.md)) before writing the M1 implementation. Right now, the most valuable contribution is picking up an unclaimed spike and reporting real, hands-on results. Once M1 lands, this file governs day-to-day code contributions.

## Repo layout

As of M0, only `docs/` exists. This is the layout M1 introduces:

- **`cpsb/`** — the Python backend: the ComfyUI node pack itself (`__init__.py`, node classes, server routes registered on `PromptServer`, the watchdog file-watcher, PSD read/write).
- **`web/`** — the ComfyUI frontend extension (`WEB_DIRECTORY`): the context-menu hook, the sidebar gallery tab, and paste-back logic, all in JS.
- **`photoshop_plugin/`** — the UXP plugin source for Tier 2 (manifest, panel UI, `batchPlay` export, WebSocket client), built into the `.ccx` that users install separately.

`photoshop_plugin/` uses CommonJS (`require`) — UXP's documented module system; do not convert to ES modules.

## Development setup

1. Clone (or symlink) this repo into `ComfyUI/custom_nodes/comfyui-photoshop-bridge` — the backend is a ComfyUI custom node pack, not a standalone service, so it needs a real ComfyUI instance to run against.
2. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate      # Windows: .venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   pip install pytest ruff
   ```
   Runtime dependencies are intentionally minimal: `watchdog` (file-watching for Tier 1), `psd-tools` (PSD read/write), `Pillow` (image conversion).
4. For work on `photoshop_plugin/`, you'll also need the UXP Developer Tool to load the plugin into Photoshop without going through the `.ccx` install flow — see [docs/SPIKES.md](docs/SPIKES.md)'s prerequisites for version requirements.

## The protocol is binding

[`docs/PROTOCOL.md`](docs/PROTOCOL.md) is the single source of truth for every interface between the backend, the frontend, and the UXP plugin — route shapes, the WebSocket message protocol, `meta.json`'s schema, file layout. If your change needs a different route, message, or field:

1. Amend `docs/PROTOCOL.md` first, in the same PR, with a clear diff of what changed and why.
2. Then implement against the amended contract.

An implementation that disagrees with `docs/PROTOCOL.md` is a bug — in the code if the doc is right, in the doc if it fell out of date. Never let them drift silently.

## Code style

- **Python (`cpsb/`)**: type hints on every function signature (parameters and return type). No bare `except:`. Formatting and linting via `ruff` — run `ruff check .` and `ruff format .` before committing; the config lives in this repo's `pyproject.toml` under `[tool.ruff]`.
- **JavaScript (`web/`, `photoshop_plugin/`)**: JSDoc comments on every exported function (params, return, and what it does) — this codebase has no TypeScript build step, so JSDoc is the only type documentation readers get.
- **No `TODO` comments in merged code.** If it's worth doing later, open an issue and reference it in a code comment (e.g. `# see #123`), or don't merge it yet.

## Running tests

```bash
pytest
```

Frontend and plugin code doesn't have an automated test setup yet (tracked for M1/M4); until it does, describe your manual verification steps in the PR description.

## Pull request expectations

- **Behavior changes get tests.** If you changed what the code does (not just how it's written), add or update a `pytest` case that would have caught the bug or regression.
- **Photoshop-API-facing changes cite a spike.** Anything touching UXP APIs, `batchPlay` descriptors, or Photoshop preference/event behavior should reference the relevant entry in [docs/SPIKES.md](docs/SPIKES.md) — existing or new. If you discovered the behavior hands-on, add your findings to that spike's Results section so the next person doesn't have to re-derive it.
- **Interface changes touch `docs/PROTOCOL.md` in the same PR** (see above), not a follow-up.
- Keep PRs scoped to one milestone's worth of work where possible (M1 through M6 — ask in the PR or issue if you're not sure which milestone something belongs to). Easier to review, easier to revert.
