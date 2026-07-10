# apple-to-ytmusic, Explained From the Ground Up

*A layer-by-layer walkthrough of what was built, why it was built that way, and
how every piece works — written for someone with a basic understanding of code.*

---

## 1. The problem we started with

You live in **Apple Music** — that's where your playlists are curated, year after
year. But the endgame is an **iPod**, and the cleanest way to get downloadable audio
files is through **YouTube Music** (via the existing `streamlist` downloader). So the
wish was:

> "Take one of my Apple Music playlists, recreate it on YouTube Music under the same
> name, and then download it so I can drop it on my iPod. And keep it fresh — if I add
> songs to the Apple playlist later, mirror those over too, without ever wiping what's
> already there."

There was already a downloader (`streamlist`) and a way to *create* YouTube playlists
by video ID (`channel_to_yt_playlist.py`). The missing middle was the hard part:
**"given a song's name and artist, find the right track on YouTube Music."** This tool
is that missing middle, wrapped into a one-command pipeline.

---

## 2. The key insight: an Apple playlist is just a list of (title, artist)

Everything downstream only needs two things per song: its **title** and its **artist**.
Get those reliably and the rest is search + plumbing. There are two places to get them,
and the tool supports both.

### 2a. From your library (AppleScript)

The Apple Music app is scriptable. A tiny AppleScript walks a playlist and prints each
track. The subtlety: the naive `get {name, artist} of every track` returns two *parallel*
lists, and song titles are full of commas ("Dis, Quand Reviendras-Tu?"). So instead the
tool loops and joins each track's fields with a **unit-separator byte (`\x1f`)** — a
character that never appears in real metadata — one track per line. Split on `\x1f`,
never on comma, and the data survives perfectly.

### 2b. From a public URL (no library needed)

This is the feature that makes the tool useful for playlists you *don't* own. When you
open a shared Apple Music playlist link (`music.apple.com/.../pl.xxxxx`), the page's HTML
already contains the entire track list as JSON — Apple embeds it in a
`<script id="serialized-server-data">` tag so the page can render instantly. Each song
object carries `title`, `artistName`, and `duration` (in milliseconds). The tool fetches
the page (via `curl`, which uses macOS's trust store and sidesteps a Python SSL-cert
quirk), pulls that JSON out with a regex, and walks it for every object whose
`contentDescriptor.kind == "song"`.

No login, no Apple Developer account, no API key. **The playlist doesn't need to be in
your library at all** — it just needs a shareable link.

The one honest limitation: enormous public playlists lazy-load beyond the first page, and
the token needed to page further isn't in the static HTML. The tool reads the playlist's
own declared count and, if the page gave it fewer, prints a warning telling you to add
that playlist to your library for full coverage (library mode has no cap).

---

## 3. The matching problem (and how we score it)

Search YouTube Music for `"<title> <artist>"` and you get several candidates: the studio
version, a live cut, a sped-up edit, a karaoke cover. Picking the *right* one is the whole
game. Each candidate gets a **0–100 score**:

- **Artist similarity (up to 40)** — how well the candidate's artist matches Apple's.
- **Title similarity (up to 40)** — compared both as-is and with parentheticals stripped.
  Candidates carrying "live / remix / cover / karaoke / sped up / …" get a penalty —
  **unless the Apple title itself has that word** (if your track is literally the live
  version, we shouldn't punish the live result).
- **Duration proximity (up to 20)** — within ~3 seconds is a perfect score, decaying from
  there. A cover that's a minute longer loses points.

All similarity is stdlib only (`difflib` + accent-folding normalization) — no heavyweight
fuzzy-match dependency. The highest scorer wins. If nothing clears a confidence bar of 60,
the tool still adds the **raw top search hit** (so a song is never silently dropped) but
marks that row `fallback` in the report so you know to glance at it. No results at all →
`none`.

Every decision is written to **`match_report.csv`** (apple title/artist, chosen
title/artist, videoId, score, confidence) so the whole run is auditable after the fact.

---

## 4. Creating the playlist, and why re-runs are safe

Once matches are chosen, the tool creates a YouTube Music playlist with the same name (via
`ytmusicapi`) and adds the matched video IDs.

The important design choice is **idempotency**, and it lives in a small file called
`runs.json`. For each playlist the tool remembers three things:

```json
{
  "French Songs": {
    "playlist_id": "PLxxxx",
    "source": { "type": "library", "ref": "French Songs" },
    "added_videoIds": ["abc", "def", "..."]
  }
}
```

- `playlist_id` — so a re-run *reuses* the same YouTube Music playlist instead of making a
  duplicate.
- `source` — where the tracks came from (a URL, or a library name), so the weekly sync
  knows how to re-read this playlist without you telling it.
- `added_videoIds` — every track already added, so a re-run only pushes the *new* ones.

Because of `added_videoIds`, running the tool ten times on a playlist is harmless: the
first run adds everything, the next nine add nothing (unless new songs appeared). Nothing
is ever removed.

---

## 5. Downloading for the iPod

If you choose to download, the tool doesn't reinvent anything — it shells out to your
existing **`streamlist`** tool with the new playlist's URL. `streamlist` normalizes the
`music.youtube.com` link, expands the playlist, and downloads AAC/M4A files with tags. You
then drag those into the iPod. The tool deliberately uses streamlist's *default* download
(no special iPod tuning) — that tuning already lives in streamlist for when you want it.

---

## 6. The whole story of one run

1. You run `apple_to_ytmusic.py` (or pass `--url` / `--playlist`).
2. **Read**: it pulls the track list — from the public page (URL) or the app (library).
3. **Match**: for each track it searches YouTube Music, scores the candidates, and picks
   one, printing live progress and writing `match_report.csv`.
4. **Create**: it creates (or reuses) the same-named YouTube Music playlist and adds the
   new matches, recording everything in `runs.json`. It prints the playlist URL.
5. **Choose**: it asks — *download offline now, or just keep the playlist?* Pick download
   and it hands off to streamlist; pick keep and you're done.

---

## 7. Keeping it fresh: the weekly sync

You add songs to your Apple playlists over time. `--install-weekly` writes a **launchd**
job that runs `--sync` every Sunday at 03:00. `--sync` walks *every* playlist in
`runs.json`, re-reads it from its remembered source, re-runs the match, and adds only the
songs that weren't there before. It's non-interactive, never downloads, and — the whole
point — **never deletes**. It logs each playlist's result to `sync.log`.

URL-sourced playlists sync completely headlessly (just `curl` + network). Library-sourced
ones need the Apple Music app running at 3 a.m. to answer AppleScript; if it isn't, that
playlist is skipped and logged, and the next week's run catches up.

Remove it any time with `--uninstall-weekly`.

---

## 8. Map of the code

Everything is one file, `apple_to_ytmusic.py`, grouped by stage:

- **Stage 1a — library**: `run_osascript`, `list_apple_playlists`, `dump_apple_tracks`.
- **Stage 1b — URL**: `is_apple_url`, `fetch_url` (curl-first), `parse_apple_playlist_html`,
  `fetch_apple_url`. Dispatch via `get_tracks_from_source`.
- **Stage 2 — scoring**: `normalize`, `token_set_ratio`, `has_variant_keyword`,
  `duration_score`, `score_candidate`, `choose_match`, `search_track`, `match_playlist`.
- **Stage 2 — persistence**: `load_runs` / `save_runs`, `get_or_create_playlist`,
  `add_matches`, `write_report`.
- **Stage 3 — download**: `download_playlist` (shells out to streamlist).
- **Sync + scheduling**: `run_sync`, `_plist_contents`, `install_weekly`, `uninstall_weekly`.
- **CLI**: `parse_args`, `resolve_source`, the interactive prompts, and `main`.

---

## 9. How you actually use it, day to day

```bash
cd /Users/adinath/Documents/Playground/GitHub/apple-to-ytmusic
PY=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3

# one-time: log in to YouTube Music
$PY -m ytmusicapi browser

# mirror a shared link, decide about download at the end
$PY apple_to_ytmusic.py --url "https://music.apple.com/us/playlist/…/pl.xxxxx"

# or mirror a library playlist
$PY apple_to_ytmusic.py --playlist "French Songs"

# preview matches first, create nothing
$PY apple_to_ytmusic.py --url "…" --report-only

# set up the weekly top-up (and test it right away)
$PY apple_to_ytmusic.py --install-weekly
launchctl kickstart -k gui/$(id -u)/com.adinath.apple-to-ytmusic.sync
```

---

## 10. Keeping your data private (why this repo is safe to open-source)

Nothing sensitive is committed. `.gitignore` excludes your YouTube Music credentials
(`browser.json` / `oauth.json`), the per-playlist state (`runs.json`), the audit report
(`match_report.csv`), the sync log (`sync.log`), and any downloads. The repo is just the
code and the docs.
