# Immich-Swipe-Triage

A local web app for triaging Immich photos and videos that are **not in any album** and taken within an age window
(min/max age in days). It shows one asset at a time, newest first by default, or oldest first. You press an arrow key to add the asset to an album, trash it, or skip it.

Requires **Immich v3.2+**, because it uses the structured `filter` search API.

## Setup

```bash
cp .env.example .env    # set IMMICH_URL and IMMICH_API_KEY
uv run app.py           # → http://127.0.0.1:8765
```

The API key stays on the backend; the browser only talks to `127.0.0.1:8765`. The key needs these permissions:
`user.read`, `album.read`, `album.create`, `albumAsset.create`, `albumAsset.delete`, `asset.read`,
`asset.statistics`, `asset.view`, `asset.download`, `asset.delete`.

The app writes your key bindings, age window and sort order to `config.json` and pre-fills them on the next launch.

## Keys (triage screen)

| Key | Action |
| --- | --- |
| ← → ↑ ↓ | Bound action: add to album, trash, or skip |
| `1` `2` `3` | Optional extra bindings (unassigned by default) |
| `Z` / `Backspace` | Undo (multiple steps) |
| `M` | Toggle video sound |
| `H` | Show/hide the key overlay on the photo |
| `Esc` | Back to setup; the queue is re-fetched on Start |

## Notes

- **Trash** is a soft delete (`force: false`), and undo restores the asset from the trash. If trash is disabled on the server, Immich purges "trashed" assets at the next nightly job, so the app refuses the Trash action.
- **Queue actions** run one at a time, in order. Undo adds the reverse call to the same queue. A failed call shows a toast in the bottom-right corner, and the queue moves on.
- **Skips** only last for the current session. They reappear after a page reload or a restart.
- **Scope:** your own assets in the timeline. Archived, locked, and Live Photo motion parts are excluded. Partner assets are hidden too, but they're still included in the "total" count.
- **Media:** originals are shown when the browser can render them. HEIC, RAW, and similar formats use `thumbnail?size=fullsize`, which is only full resolution if *Admin → Image settings → Full-size preview* is enabled. Otherwise Immich serves the regular preview. Videos that fail to decode (e.g. HEVC on Linux) fall back to Immich's transcoded playback.
