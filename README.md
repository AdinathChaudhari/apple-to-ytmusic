# apple-to-ytmusic

Mirror an Apple Music playlist to YouTube Music, then download it for an iPod.

## Requirements

- macOS with the Apple Music app installed and signed in.
- The Python 3.13 framework interpreter:
  `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3`
- `streamlist` checked out at `/Users/adinath/Documents/Playground/GitHub/streamlist/streamlist.py`.

**Everything in this tool runs under the 3.13 framework interpreter above** — not the system
`python3` (which is 3.14 and does not have `yt_dlp`/`ytmusicapi` installed).

## Install

```bash
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 -m pip install -r requirements.txt
```

## One-time YouTube Music auth

Recommended: browser-header auth (no Google Cloud project needed).

```bash
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 -m ytmusicapi browser
```

1. Open `music.youtube.com` in a browser, logged in to the account you want the mirrored
   playlists created under.
2. Open DevTools → Network tab. Filter for `/browse` (or any POST request to
   `music.youtube.com`).
3. Copy the request headers from that request.
4. Paste them into the `ytmusicapi browser` prompt, then finish with a blank line / Ctrl-D.

This writes `browser.json` next to `apple_to_ytmusic.py`. The tool auto-detects a missing or
invalid creds file and points you back here.

**Fallback:** OAuth via a "TV & Limited Input" Google client:

```bash
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 -m ytmusicapi oauth
```

This writes `oauth.json` — pass it explicitly with `--creds oauth.json`.

Credentials expire periodically. If the tool reports invalid/expired creds, just re-run the
auth step above.

## Enable Automation permission (Apple Music)

If you see an error mentioning "not authorized (-1743)":

System Settings → Privacy & Security → Automation → [Terminal] → enable **Music**.

Then re-run the tool.

## Usage

Interactive (list playlists, pick one, run all 3 stages, confirm before download):

```bash
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 apple_to_ytmusic.py
```

Flags:

- `--playlist "French Songs"` — skip the interactive picker, run this exact playlist name.
- `--no-download` — run Stage 1+2 (search, create/update YT playlist) and stop; no download.
- `--report-only` — run Stage 1+2 search & scoring, write `match_report.csv`, print a summary,
  but do not create/modify any YouTube Music playlist and do not download.
- `--stage1-only` — run Stage 1 only: extract and print the playlist's tracks from Apple Music.
  Requires **no** ytmusicapi credentials, so it's the quickest way to sanity-check extraction.
- `--out <dir>` — download target directory (default `~/Music/apple-to-ytmusic`); also where
  `match_report.csv` is written.
- `--creds <path>` — path to a ytmusicapi creds file (default: `browser.json` next to the
  script).

## How it works

1. **Apple Music read** (AppleScript/`osascript`) — lists your user playlists, then dumps each
   track's title/artist/album/duration for the chosen playlist.
2. **Match + create on YouTube Music** (`ytmusicapi`) — searches YT Music for each track, scores
   candidates (artist/title similarity + duration proximity), picks the best match (or falls back
   to the raw top search hit if nothing clears the confidence bar), creates or reuses a YT Music
   playlist with the same name, and adds the matched tracks. Writes `match_report.csv` so you can
   audit every match — check the `confidence` column (`confident` / `fallback` / `none`).
3. **Download** (reuses `streamlist`) — shells out to `streamlist.py` to download the resulting YT
   Music playlist as AAC/M4A files with tags, ready to load onto an iPod.

`runs.json` tracks, per Apple playlist name, the YouTube Music playlist id and which videoIds
have already been added. Re-running the tool on the same playlist reuses the same YT Music
playlist and only adds newly-matched tracks — it won't create duplicates.

## Notes / caveats

- Rows marked `fallback` in `match_report.csv` are the raw top search hit — the scorer wasn't
  confident, so nothing was dropped, but it's worth a manual glance.
- If Apple Music has two playlists with the same name, `--playlist "<name>"` targets the first
  one AppleScript finds; use the interactive numbered picker to disambiguate by position.
- Playlist names containing `/` are passed verbatim to `streamlist`, which owns filename
  handling — it may create nested output directories.
