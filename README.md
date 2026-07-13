# apple-to-ytmusic

Mirror an Apple Music playlist to YouTube Music — from a **shared URL** or from your
**library** — then optionally download it for an iPod. Re-runs (and a weekly auto-sync)
add only *new* songs and never delete anything.

## Ways to point it at a playlist

1. **A public / shared Apple Music URL** (`music.apple.com/.../pl.xxxxx`). The playlist
   does **not** need to be in your library — the tool reads the track list straight from
   the public page. This is the simplest path: paste a link, get a YouTube Music playlist.
2. **An Apple Music ARTIST URL** (`music.apple.com/.../artist/<slug>/<id>`, e.g.
   `https://music.apple.com/in/artist/vidhya-gopal/1121565525`). The tool mirrors that
   artist's **full Top Songs list** as a playlist named `"<Artist> — Top Songs"`. It fetches
   the complete list (typically 50–100 songs) from Apple's public API — not just the ~24 the
   page shows at a glance — and falls back to the page's embedded songs if the API is
   unavailable. `--sync` re-pulls Top Songs weekly, **additively** — songs that drop out of
   Apple's Top Songs are not removed, the mirror only accretes, by design. The `runs.json`
   key is the artist **name**, so the same artist reached via different storefront URLs
   (`/us/`, `/in/`, …) converges to one entry. Downloaded offline, the album-artist tag is
   the artist name (not the default `"Aey - …"`).
3. **A playlist in your own Apple Music library**, read via AppleScript (for private
   playlists you haven't shared).

## Requirements

- macOS with the Apple Music app installed and signed in (only needed for *library* mode).
- The Python 3.13 framework interpreter:
  `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3`
- `streamlist` checked out at `/Users/adinath/Documents/Playground/GitHub/streamlist/streamlist.py`
  (only needed if you download).

**Everything in this tool runs under the 3.13 framework interpreter above** — not the system
`python3` (which is 3.14 and does not have `yt_dlp` / `ytmusicapi`).

## Install

```bash
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 -m pip install -r requirements.txt
```

## One-time YouTube Music auth

Browser-header auth (no Google Cloud project needed). Use the `ytmusicapi` **command**
installed beside the 3.13 python — NOT `python -m ytmusicapi` (that has no `__main__` and errors):

```bash
/Library/Frameworks/Python.framework/Versions/3.13/bin/ytmusicapi browser --file browser.json
```

1. Open `music.youtube.com` in a browser, logged in to the account the playlists should be
   created under.
2. DevTools (Cmd+Option+I) → Network tab → reload → left-click any `browse` request to
   `music.youtube.com`.
3. In the detail panel → **Headers** tab → scroll to **Request Headers** → click the **`Raw`**
   toggle. Select all that text (Cmd+A), copy, paste into the prompt, press Enter, then Ctrl-D.
   (Use **Chrome/Firefox/Edge** — Safari's format doesn't parse. Use the **Raw** view, not the
   expanded name/value view, which pulls in `:authority`/`:path` pseudo-headers and breaks parsing.)

Run it from inside this folder (or keep `--file browser.json`) so the file lands next to the
script, where the tool looks for it. The tool auto-detects a missing/invalid creds file and
points you back here. Credentials expire periodically — just re-run this step.

**Fallback (oauth):** requires a Google "TV & Limited Input" OAuth client:

```bash
/Library/Frameworks/Python.framework/Versions/3.13/bin/ytmusicapi oauth --file oauth.json
```

Then pass it with `--creds oauth.json`.

## Automation permission (library mode only)

If you see "not authorized (-1743)": System Settings → Privacy & Security → Automation →
[Terminal] → enable **Music**, then re-run.

## Usage

Interactive (paste a URL, or pick from your library, then choose whether to download):

```bash
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 apple_to_ytmusic.py
```

Direct:

```bash
# From a shared Apple Music playlist URL (not in your library):
... apple_to_ytmusic.py --url "https://music.apple.com/us/playlist/…/pl.xxxxx"

# From an Apple Music artist URL (mirrors their Top Songs):
... apple_to_ytmusic.py --url "https://music.apple.com/in/artist/vidhya-gopal/1121565525"

# From a library playlist by name:
... apple_to_ytmusic.py --playlist "French Songs"
```

### Flags

- `--url <apple music url>` — mirror a public/shared playlist, OR an artist URL to mirror
  that artist's Top Songs (no library membership needed either way).
- `--playlist "<name>"` — mirror a library playlist by name (skips the picker).
- `--no-download` — create/update the YT Music playlist and stop; no download.
- `--report-only` — search & score only; write `match_report.csv`; create nothing.
- `--stage1-only` — just read and print the tracks. **No credentials needed** — quickest way
  to sanity-check either a URL or a library name.
- `--sync` — re-check **every** tracked playlist and add only new songs. Non-interactive, no
  download. (This is what the weekly job runs.)
- `--install-weekly` / `--uninstall-weekly` — install/remove a launchd job that runs `--sync`
  every Sunday at 03:00. Logs to `sync.log`.
- `--out <dir>` — download / report directory (default `~/Music/apple-to-ytmusic`).
- `--creds <path>` — ytmusicapi creds file (default `browser.json` beside the script).

## Weekly auto-sync

```bash
... apple_to_ytmusic.py --install-weekly
```

Every Sunday at 03:00 the tool re-reads each playlist you've mirrored (from its original URL or
library name, remembered in `runs.json`) and adds any songs that appeared since last time. It
**never removes** tracks. Check `sync.log` for what it did.

- **URL-sourced** playlists sync completely headlessly.
- **Library-sourced** playlists need Apple Music running at sync time (AppleScript talks to the
  app). If it isn't running, that playlist is skipped and logged; the next run catches up.

Test it immediately without waiting a week:

```bash
launchctl kickstart -k gui/$(id -u)/com.adinath.apple-to-ytmusic.sync
```

## How it works

1. **Read** — a URL is scraped from the public Apple Music page (title/artist/duration from the
   embedded `serialized-server-data`); a library playlist is read via AppleScript (`osascript`),
   using a `\x1f`-delimited dump so titles with commas/accents survive intact.
2. **Match + create** (`ytmusicapi`) — searches YT Music per track, scores candidates on
   artist/title similarity + duration proximity (studio-preferred), picks the best match or
   falls back to the raw top hit if nothing clears the confidence bar. Creates or reuses a
   same-named YT Music playlist and adds matched tracks. Writes `match_report.csv` — check the
   `confidence` column (`confident` / `fallback` / `none`).
3. **Download** (optional; reuses `streamlist`) — downloads the YT Music playlist as AAC/M4A
   with tags, ready for an iPod.

`runs.json` remembers, per playlist, its YouTube Music id, its source (URL or library name), and
which videoIds were already added — so re-runs and the weekly sync only add new songs and never
create duplicates.

See **EXPLAINER.md** for a ground-up walkthrough.

## Notes / caveats

- Very large *public* playlists lazy-load beyond the initial page; if the page exposes fewer
  tracks than the playlist declares, the tool warns you and reads what it can. For full coverage,
  add the playlist to your library and run it by name (library mode has no cap).
- `fallback` rows in `match_report.csv` are the raw top search hit — worth a manual glance.
- Playlist names with `/` are passed verbatim to `streamlist`, which owns filename handling.
