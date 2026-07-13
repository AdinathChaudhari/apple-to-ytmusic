#!/Library/Frameworks/Python.framework/Versions/3.13/bin/python3
"""
apple_to_ytmusic.py — Mirror an Apple Music playlist to YouTube Music, then
optionally download it for an iPod.

Sources (a playlist can come from either):
  * A shared/public Apple Music playlist URL (music.apple.com/.../pl.xxxx) —
    read straight from the public web page. The playlist does NOT need to be in
    your library.
  * A playlist in your own Apple Music library, read via AppleScript.
  * An Apple Music ARTIST share link — mirrors that artist's ~24 Top Songs as
    "<Artist> — Top Songs".

Stages:
  1. Read a playlist + its tracks (URL scrape or AppleScript).
  2. Search + score each track on YouTube Music (ytmusicapi), create/update a
     mirrored playlist, and write an audit report (match_report.csv).
  3. (optional) Download the resulting YT Music playlist for offline listening
     via the existing `streamlist` tool.

Extras:
  * --sync            Re-check every tracked playlist and ADD new songs only
                      (never deletes). Non-interactive; no download.
  * --install-weekly  Install a launchd job that runs --sync once a week.

See README.md for one-time setup (ytmusicapi auth, Automation permission) and
EXPLAINER.md for a ground-up walkthrough.
"""

import argparse
import csv
import difflib
import json
import os
import re
import subprocess
import sys
import unicodedata
import urllib.request
import urllib.error
from datetime import datetime

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

PY313 = "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
STREAMLIST = "/Users/adinath/Documents/Playground/GitHub/streamlist/streamlist.py"
DEFAULT_CREDS = "browser.json"                 # resolved relative to script dir
RUNS_FILE = "runs.json"                         # relative to script dir
REPORT_FILE = "match_report.csv"                # written into --out dir (see note)
SYNC_LOG = "sync.log"                            # weekly sync log, in script dir
DEFAULT_OUT = os.path.expanduser("~/Music/apple-to-ytmusic")
CONFIDENCE_BAR = 60                             # score >= 60 => "confident"
SEARCH_TOP_N = 5                                # scoring window over search results
DELIM = "\x1f"                                  # AppleScript TSV delimiter (unit separator)
YTM_URL = "https://music.youtube.com/playlist?list={pid}"

# scheduling (launchd)
PLIST_LABEL = "com.adinath.apple-to-ytmusic.sync"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")

# scoring weights
W_ARTIST = 40.0
W_TITLE = 40.0
W_DUR = 20.0

# keywords that mark a candidate as a non-studio variant
VARIANT_KEYWORDS = [
    "live", "remix", "cover", "karaoke", "instrumental", "acoustic",
    "sped up", "slowed", "reverb", "8d", "remaster",
    "demo", "edit", "version", "mix", "extended", "radio edit",
]

APPLE_URL_RE = re.compile(r"https?://(?:[a-z0-9-]+\.)*music\.apple\.com/\S+", re.I)
APPLE_ARTIST_URL_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*music\.apple\.com/(?:[a-z]{2,3}/)?artist/[^/]+/\d+",
    re.I,
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AppleMusicAuthError(Exception):
    """Raised when osascript is not authorized to control Music.app (-1743)."""


class AppleScriptError(Exception):
    """Raised for any other osascript failure."""


class AppleURLError(Exception):
    """Raised when a public Apple Music playlist URL cannot be read/parsed."""


class CredsError(Exception):
    """Raised when ytmusicapi credentials are missing, invalid, or expired."""


# ---------------------------------------------------------------------------
# Stage 1a — Apple Music library (AppleScript)
# ---------------------------------------------------------------------------

def run_osascript(script: str) -> str:
    """Run `osascript -e <script>`, return stdout stripped of trailing newline."""
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        stderr = proc.stderr or ""
        if "-1743" in stderr or "not authorized" in stderr.lower():
            raise AppleMusicAuthError(stderr.strip())
        raise AppleScriptError(stderr.strip())
    return proc.stdout.rstrip("\n")


def list_apple_playlists() -> list:
    """Return Apple Music user playlist names, in Apple's own order."""
    script = (
        'tell application "Music"\n'
        '  set out to ""\n'
        '  repeat with p in (every user playlist)\n'
        '    set out to out & (name of p) & linefeed\n'
        '  end repeat\n'
        '  return out\n'
        'end tell'
    )
    out = run_osascript(script)
    names = [line.strip() for line in out.split("\n")]
    return [n for n in names if n]


def escape_applescript_string(s: str) -> str:
    """Escape a string for interpolation inside an AppleScript double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def dump_apple_tracks(playlist_name: str) -> list:
    """Dump (title, artist, album, duration) for every track in a library playlist."""
    escaped = escape_applescript_string(playlist_name)
    script = (
        'tell application "Music"\n'
        '  set out to ""\n'
        f'  repeat with t in (every track of playlist "{escaped}")\n'
        f'    set out to out & (name of t) & "{DELIM}" & (artist of t) & "{DELIM}" & (album of t) & "{DELIM}" & ((duration of t) as string) & linefeed\n'
        '  end repeat\n'
        '  return out\n'
        'end tell'
    )
    out = run_osascript(script)
    tracks = []
    for line in out.split("\n"):
        if not line.strip():
            continue
        parts = line.split(DELIM)
        if len(parts) < 4:
            continue
        title, artist, album, dur_raw = parts[0], parts[1], parts[2], parts[3]
        try:
            duration = float(dur_raw.strip())
        except (ValueError, TypeError):
            duration = 0.0
        tracks.append({
            "title": title,
            "artist": artist,
            "album": album,
            "duration": duration,
        })
    return tracks


# ---------------------------------------------------------------------------
# Stage 1b — Apple Music public URL (scrape, no auth)
# ---------------------------------------------------------------------------

def is_apple_url(s: str) -> bool:
    """True if the string looks like an Apple Music URL."""
    return bool(s) and bool(APPLE_URL_RE.match(s.strip()))


def is_apple_artist_url(s: str) -> bool:
    """True if the string looks like an Apple Music ARTIST page URL."""
    return bool(s) and bool(APPLE_ARTIST_URL_RE.match(s.strip()))


def fetch_url(url: str, timeout: int = 30) -> str:
    """Fetch a URL with a browser User-Agent; return decoded HTML text.

    Prefers `curl` (uses the macOS system trust store, avoiding the
    Python.framework's missing-CA-roots SSL problem); falls back to urllib.
    """
    try:
        proc = subprocess.run(
            ["curl", "-sL", "--max-time", str(timeout), "-A", USER_AGENT,
             "-H", "Accept-Language: en-US,en;q=0.9", url],
            capture_output=True, timeout=timeout + 10,
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout.decode("utf-8", errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # curl unavailable or timed out — try urllib below

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        raise AppleURLError(f"Could not fetch {url}: {e}")
    return raw.decode("utf-8", errors="replace")


def _iter_server_data_songs(html: str) -> list:
    """Extract raw song objects from a page's serialized-server-data block.

    Returns them in DOCUMENT order (caller decides whether to sort). Returns
    [] if the block is missing, unparseable, or contains no songs.
    """
    m2 = re.search(r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>',
                   html, re.S)
    if not m2:
        return []
    try:
        data = json.loads(m2.group(1))
    except json.JSONDecodeError:
        return []
    if data is None:
        return []

    songs = []
    seen_ids = set()

    def walk(o):
        if isinstance(o, dict):
            cd = o.get("contentDescriptor")
            is_song = isinstance(cd, dict) and cd.get("kind") == "song"
            if is_song and o.get("title"):
                key = o.get("id") or (o.get("title"), o.get("artistName"),
                                      o.get("trackNumber"))
                if key not in seen_ids:
                    seen_ids.add(key)
                    songs.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return songs


def _song_obj_to_track(o: dict) -> dict:
    """Convert one raw serialized-server-data song object to our track dict."""
    artist = o.get("artistName") or ""
    if not artist:
        sl = o.get("subtitleLinks")
        if isinstance(sl, list) and sl:
            artist = sl[0].get("title", "")
    dur_ms = o.get("duration")
    try:
        duration = float(dur_ms) / 1000.0 if dur_ms else 0.0
    except (ValueError, TypeError):
        duration = 0.0
    return {
        "title": o.get("title", ""),
        "artist": artist,
        "album": "",
        "duration": duration,
    }


def parse_apple_playlist_html(html: str):
    """Parse an Apple Music playlist page.

    Returns (name, tracks, num_tracks_declared). Tracks are dicts of
    {title, artist, album, duration(seconds)}. num_tracks_declared is the
    playlist's own reported track count (for truncation detection) or None.
    """
    name = None
    num_declared = None

    # (a) JSON-LD gives a reliable name + numTracks.
    m = re.search(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                  html, re.S)
    if m:
        try:
            ld = json.loads(m.group(1))
            name = ld.get("name") or name
            num_declared = ld.get("numTracks")
        except (json.JSONDecodeError, TypeError):
            pass

    # (b) serialized-server-data holds the full per-track detail (title/artist/duration).
    songs = _iter_server_data_songs(html)

    def track_number(o):
        try:
            return int(o.get("trackNumber") or 0)
        except (ValueError, TypeError):
            return 0

    songs.sort(key=track_number)
    tracks = [_song_obj_to_track(o) for o in songs]

    if not name:
        # last resort: slug from the URL path is handled by the caller
        name = None
    return name, tracks, num_declared


def _name_from_apple_url(url: str) -> str:
    """Derive a human-ish playlist name from the URL slug as a last resort."""
    m = re.search(r"/playlist/([^/]+)/", url)
    if m:
        slug = m.group(1).replace("-", " ").strip()
        return slug.title() if slug else "Apple Music Playlist"
    return "Apple Music Playlist"


def _artist_name_from_url(url: str) -> str:
    """Derive a human-ish artist name from the URL slug as a last resort."""
    m = re.search(r"/artist/([^/]+)/", url)
    if m:
        slug = m.group(1).replace("-", " ").strip()
        return slug.title() if slug else "Apple Music Artist"
    return "Apple Music Artist"


def fetch_apple_url(url: str):
    """Read a public Apple Music playlist URL. Returns (name, tracks).

    Prints a warning (does not fail) if the page exposed fewer tracks than the
    playlist declares — very large public playlists lazy-load beyond the initial
    page. For full coverage of such playlists, add them to your library and run
    by name instead.
    """
    if not is_apple_url(url):
        raise AppleURLError(
            f"Not an Apple Music URL: {url!r}. Expected a public/shared "
            "playlist link like music.apple.com/.../pl.xxxx."
        )
    html = fetch_url(url)
    name, tracks, num_declared = parse_apple_playlist_html(html)
    if not name:
        name = _name_from_apple_url(url)
    if not tracks:
        raise AppleURLError(
            f"No tracks found on the page for {url}. Make sure the link is a "
            "public/shared Apple Music playlist (music.apple.com/.../pl.xxxx)."
        )
    if num_declared and len(tracks) < num_declared:
        print(
            f"  ! Warning: page exposed {len(tracks)} of {num_declared} tracks. "
            "This public playlist is larger than its page reveals. For full "
            "coverage, add it to your Apple Music library and run it by name.",
            file=sys.stderr,
        )
    return name, tracks


def parse_apple_artist_html(html: str):
    """Parse an Apple Music artist page's Top Songs.

    Returns (artist_name_or_None, tracks). Tracks are dicts of
    {title, artist, album, duration(seconds)} in the page's OWN order —
    unlike the playlist parser, this is NOT sorted by trackNumber, because
    document order IS Top Songs order (sorting would scramble it).
    """
    name = None

    # (a) JSON-LD MusicGroup gives a reliable artist name.
    m = re.search(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                  html, re.S)
    if m:
        try:
            ld = json.loads(m.group(1))
        except (json.JSONDecodeError, TypeError):
            ld = None
        if isinstance(ld, dict) and ld.get("@type") == "MusicGroup":
            name = ld.get("name") or name
        elif isinstance(ld, list):
            for item in ld:
                if isinstance(item, dict) and item.get("@type") == "MusicGroup":
                    name = item.get("name") or name
                    break

    # (b) fall back to the <title> tag, stripping Apple's " on Apple Music" suffix.
    if not name:
        mt = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
        if mt:
            title = re.sub(r"\s*(?:on|-|\||–|—)\s*Apple\s*Music.*$", "",
                            mt.group(1), flags=re.I | re.S).strip()
            name = title or None

    # (c) serialized-server-data holds Top Songs, in Top Songs order.
    songs = _iter_server_data_songs(html)
    tracks = [_song_obj_to_track(o) for o in songs]

    return name, tracks


def fetch_apple_artist_url(url: str):
    """Read a public Apple Music ARTIST page. Returns (name, tracks).

    `name` is f"{artist} — Top Songs" — that string becomes the mirrored
    playlist's title and the runs.json tracking key.
    """
    if not is_apple_artist_url(url):
        raise AppleURLError(
            f"Not an Apple Music artist URL: {url!r}. Expected a link like "
            "music.apple.com/<cc>/artist/<slug>/<id>."
        )
    html = fetch_url(url)
    artist, tracks = parse_apple_artist_html(html)
    artist = artist or _artist_name_from_url(url)
    if not tracks:
        raise AppleURLError(
            f"No top songs found on the artist page for {url}. Apple may not "
            "surface Top Songs for this artist/region, or the page layout changed."
        )
    return f"{artist} — Top Songs", tracks


# ---------------------------------------------------------------------------
# Stage 1 — source dispatch
# ---------------------------------------------------------------------------

def get_tracks_from_source(source: dict):
    """Given a source descriptor, return (name, tracks).

    source = {"type": "url", "ref": <apple playlist OR artist url>} or
             {"type": "library", "ref": <playlist name>}
    """
    if source["type"] == "url":
        if is_apple_artist_url(source["ref"]):
            return fetch_apple_artist_url(source["ref"])
        return fetch_apple_url(source["ref"])
    # library
    name = source["ref"]
    return name, dump_apple_tracks(name)


# ---------------------------------------------------------------------------
# Stage 2 — scoring
# ---------------------------------------------------------------------------

def normalize(s: str) -> str:
    """Lowercase, strip accents, fold '&'->'and', collapse non-alphanumerics."""
    if not s:
        return ""
    s = s.lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def strip_parentheticals(title: str) -> str:
    """Remove (...) and [...] groups; return the residue."""
    if not title:
        return ""
    return re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title).strip()


def token_set_ratio(a: str, b: str) -> float:
    """Stdlib token-set similarity in [0,1] (difflib-based, no rapidfuzz)."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    intersection = ta & tb
    a_only = ta - tb
    b_only = tb - ta

    sorted_inter = " ".join(sorted(intersection))
    sorted_a = " ".join(sorted(intersection | a_only))
    sorted_b = " ".join(sorted(intersection | b_only))

    r1 = difflib.SequenceMatcher(None, sorted_inter, sorted_a).ratio()
    r2 = difflib.SequenceMatcher(None, sorted_inter, sorted_b).ratio()
    r3 = difflib.SequenceMatcher(None, sorted_a, sorted_b).ratio()
    return max(r1, r2, r3)


def has_variant_keyword(text: str) -> list:
    """Return the subset of VARIANT_KEYWORDS present as whole words in normalize(text)."""
    norm = normalize(text)
    found = []
    for kw in VARIANT_KEYWORDS:
        kw_norm = normalize(kw)
        pattern = r"\b" + re.escape(kw_norm) + r"\b"
        if re.search(pattern, norm):
            found.append(kw)
    return found


def duration_score(apple_dur: float, cand_dur) -> float:
    """Returns [0,1]. None/0 candidate duration -> neutral 0.5."""
    if not cand_dur:
        return 0.5
    diff = abs(apple_dur - cand_dur)
    if diff <= 3:
        return 1.0
    return max(0.0, 1.0 - (diff - 3) / 30.0)


def score_candidate(apple: dict, cand: dict) -> float:
    """Compute 0-100 score for a single candidate against an Apple track."""
    apple_artist = apple["artist"]
    cand_artists = " ".join(
        a["name"] for a in cand.get("artists", []) or [] if a.get("name")
    )
    artist_sim = token_set_ratio(apple_artist, cand_artists)

    apple_title = apple["title"]
    cand_title = cand.get("title", "") or ""

    core_sim = token_set_ratio(strip_parentheticals(apple_title), strip_parentheticals(cand_title))
    full_sim = token_set_ratio(apple_title, cand_title)
    title_sim = max(core_sim, full_sim)

    apple_variants = set(has_variant_keyword(apple_title))
    cand_variants = set(has_variant_keyword(cand_title))
    unwanted = cand_variants - apple_variants
    penalty = 0.0
    if unwanted:
        penalty = min(0.5, 0.15 * len(unwanted))
    title_sim = max(0.0, title_sim - penalty)

    dur_sim = duration_score(apple["duration"], cand.get("duration_seconds"))

    score = (W_ARTIST * artist_sim) + (W_TITLE * title_sim) + (W_DUR * dur_sim)
    return round(score, 2)


def _artist_sim_only(apple: dict, cand: dict) -> float:
    apple_artist = apple["artist"]
    cand_artists = " ".join(
        a["name"] for a in cand.get("artists", []) or [] if a.get("name")
    )
    return token_set_ratio(apple_artist, cand_artists)


def _dur_diff(apple: dict, cand: dict) -> float:
    cand_dur = cand.get("duration_seconds")
    if not cand_dur:
        return 9999.0
    return abs(apple["duration"] - cand_dur)


def choose_match(apple: dict, results: list) -> dict:
    """Score up to SEARCH_TOP_N results; pick the best with a documented fallback rule."""
    if not results:
        return {
            "apple": apple,
            "candidate": None,
            "score": 0,
            "confidence": "none",
            "videoId": None,
        }

    window = results[:SEARCH_TOP_N]
    scored = []
    for idx, cand in enumerate(window):
        s = score_candidate(apple, cand)
        scored.append((idx, cand, s))

    def sort_key(item):
        idx, cand, s = item
        artist_sim = _artist_sim_only(apple, cand)
        dur_diff = _dur_diff(apple, cand)
        return (-s, -artist_sim, dur_diff, idx)

    scored.sort(key=sort_key)
    best_idx, best_cand, best_score = scored[0]

    if best_score >= CONFIDENCE_BAR:
        return {
            "apple": apple,
            "candidate": best_cand,
            "score": best_score,
            "confidence": "confident",
            "videoId": best_cand.get("videoId"),
        }

    top_hit = results[0]
    top_hit_score = score_candidate(apple, top_hit)
    return {
        "apple": apple,
        "candidate": top_hit,
        "score": top_hit_score,
        "confidence": "fallback",
        "videoId": top_hit.get("videoId"),
    }


def search_track(yt, apple: dict) -> list:
    """Search YouTube Music for a track; return [] on any failure."""
    query = f'{apple["title"]} {apple["artist"]}'
    try:
        results = yt.search(query, filter="songs")
    except Exception:
        return []
    if not results:
        return []
    return results[:10]


def match_playlist(yt, tracks: list, quiet: bool = False) -> list:
    """Search + score every track; print live progress unless quiet."""
    matches = []
    total = len(tracks)
    for i, track in enumerate(tracks, start=1):
        results = search_track(yt, track)
        match = choose_match(track, results)
        if not quiet:
            chosen_title = match["candidate"]["title"] if match["candidate"] else "(no match)"
            print(f'[{i}/{total}] {track["title"]} — {track["artist"]} -> {chosen_title} ({match["score"]})')
        matches.append(match)
    return matches


# ---------------------------------------------------------------------------
# Stage 2 — creation + idempotency
# ---------------------------------------------------------------------------

def load_runs() -> dict:
    path = os.path.join(SCRIPT_DIR, RUNS_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_runs(runs: dict) -> None:
    path = os.path.join(SCRIPT_DIR, RUNS_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(runs, f, ensure_ascii=False, indent=2)


def get_or_create_playlist(yt, name: str, runs: dict, source: dict):
    """Reuse an existing mirrored playlist for `name`, or create a new one.

    Records the source (url/library) so --sync knows where to re-read from.
    """
    if name in runs:
        entry = runs[name]
        # backfill source for pre-existing entries
        if "source" not in entry:
            entry["source"] = source
            save_runs(runs)
        return entry["playlist_id"], set(entry.get("added_videoIds", []))

    pid = yt.create_playlist(name, "Mirrored from Apple Music")
    if not isinstance(pid, str):
        raise CredsError(
            f"yt.create_playlist did not return a playlist id string (got {pid!r}). "
            "Check your ytmusicapi credentials."
        )
    runs[name] = {
        "playlist_id": pid,
        "added_videoIds": [],
        "source": source,
        "last_run": datetime.now().isoformat(),
    }
    save_runs(runs)
    return pid, set()


def add_matches(yt, pid: str, matches: list, already: set, runs: dict, name: str) -> int:
    """Add newly-matched videoIds to the playlist. Never removes anything."""
    new_ids = []
    seen = set(already)
    for m in matches:
        vid = m.get("videoId")
        if not vid or vid in seen:
            continue
        new_ids.append(vid)
        seen.add(vid)

    if not new_ids:
        return 0

    try:
        yt.add_playlist_items(pid, new_ids, duplicates=False)
    except Exception:
        return 0

    entry = runs.setdefault(name, {"playlist_id": pid, "added_videoIds": []})
    entry.setdefault("added_videoIds", [])
    entry["added_videoIds"].extend(new_ids)
    entry["last_run"] = datetime.now().isoformat()
    save_runs(runs)
    return len(new_ids)


# ---------------------------------------------------------------------------
# Stage 2 — report
# ---------------------------------------------------------------------------

def write_report(path: str, matches: list) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "apple_title", "apple_artist", "chosen_title", "chosen_artist",
            "videoId", "score", "confidence",
        ])
        for m in matches:
            apple = m["apple"]
            cand = m["candidate"]
            if m["confidence"] == "none" or cand is None:
                chosen_title = ""
                chosen_artist = ""
                video_id = ""
                score = 0
            else:
                chosen_title = cand.get("title", "") or ""
                cand_artists = cand.get("artists", []) or []
                chosen_artist = ", ".join(a.get("name", "") for a in cand_artists if a.get("name"))
                video_id = m.get("videoId") or ""
                score = m["score"]
            writer.writerow([
                apple["title"], apple["artist"], chosen_title, chosen_artist,
                video_id, score, m["confidence"],
            ])


# ---------------------------------------------------------------------------
# Stage 3 — download
# ---------------------------------------------------------------------------

def download_playlist(pid: str, name: str, out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)
    proc = subprocess.run([
        PY313, STREAMLIST,
        "--url", YTM_URL.format(pid=pid),
        "--name", name,
        "--out", out_dir,
    ], check=False)
    return proc.returncode


# ---------------------------------------------------------------------------
# Auth / creds
# ---------------------------------------------------------------------------

def resolve_creds(path: str) -> str:
    if not path:
        path = DEFAULT_CREDS
    if not os.path.isabs(path):
        candidate = os.path.join(SCRIPT_DIR, path)
    else:
        candidate = path
    if os.path.exists(candidate):
        return candidate
    raise CredsError(
        f"Credentials file not found: {candidate}\n"
        "Run the one-time ytmusicapi auth setup — see README.md ('One-time YouTube Music auth')."
    )


def make_ytmusic(creds_path: str):
    try:
        from ytmusicapi import YTMusic
    except ImportError:
        raise CredsError(
            "ytmusicapi is not installed. Run:\n"
            f"  {PY313} -m pip install ytmusicapi"
        )
    yt = YTMusic(creds_path)
    try:
        yt.get_library_playlists(limit=1)
    except Exception as e:
        raise CredsError(
            f"ytmusicapi credentials appear invalid or expired ({e}). "
            "Re-run the auth setup in README.md ('One-time YouTube Music auth')."
        )
    return yt


# ---------------------------------------------------------------------------
# Weekly sync
# ---------------------------------------------------------------------------

def run_sync(creds_path: str) -> int:
    """Re-check every tracked playlist and ADD new songs only. Never deletes.

    Non-interactive. Intended for the weekly launchd job. Logs a per-playlist
    summary to stdout (which launchd routes to sync.log).
    """
    runs = load_runs()
    if not runs:
        print(f"[{datetime.now().isoformat()}] sync: nothing tracked yet.")
        return 0

    try:
        yt = make_ytmusic(resolve_creds(creds_path))
    except CredsError as e:
        print(f"[{datetime.now().isoformat()}] sync: creds error: {e}")
        return 2

    total_added = 0
    for name, entry in list(runs.items()):
        source = entry.get("source") or {"type": "library", "ref": name}
        stamp = datetime.now().isoformat()
        try:
            src_name, tracks = get_tracks_from_source(source)
        except (AppleURLError, AppleMusicAuthError, AppleScriptError) as e:
            print(f"[{stamp}] sync: '{name}' skipped — could not read source ({e}).")
            continue
        if not tracks:
            print(f"[{stamp}] sync: '{name}' — source empty, skipped.")
            continue

        matches = match_playlist(yt, tracks, quiet=True)
        pid = entry["playlist_id"]
        already = set(entry.get("added_videoIds", []))
        added = add_matches(yt, pid, matches, already, runs, name)
        total_added += added
        print(f"[{stamp}] sync: '{name}' — {len(tracks)} tracks, {added} new added.")

    print(f"[{datetime.now().isoformat()}] sync: done. {total_added} new tracks across "
          f"{len(runs)} playlist(s).")
    return 0


# ---------------------------------------------------------------------------
# Scheduling (launchd)
# ---------------------------------------------------------------------------

def _plist_path() -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{PLIST_LABEL}.plist")


def _plist_contents(creds_path: str) -> str:
    script = os.path.join(SCRIPT_DIR, "apple_to_ytmusic.py")
    log = os.path.join(SCRIPT_DIR, SYNC_LOG)
    args = [PY313, script, "--sync"]
    if creds_path and creds_path != DEFAULT_CREDS:
        args += ["--creds", creds_path]
    args_xml = "\n".join(f"    <string>{a}</string>" for a in args)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        f'  <key>Label</key><string>{PLIST_LABEL}</string>\n'
        '  <key>ProgramArguments</key>\n'
        '  <array>\n'
        f'{args_xml}\n'
        '  </array>\n'
        '  <key>StartCalendarInterval</key>\n'
        '  <dict>\n'
        '    <key>Weekday</key><integer>0</integer>\n'
        '    <key>Hour</key><integer>3</integer>\n'
        '    <key>Minute</key><integer>0</integer>\n'
        '  </dict>\n'
        f'  <key>StandardOutPath</key><string>{log}</string>\n'
        f'  <key>StandardErrorPath</key><string>{log}</string>\n'
        '  <key>RunAtLoad</key><false/>\n'
        '</dict>\n'
        '</plist>\n'
    )


def install_weekly(creds_path: str) -> int:
    """Write and load a launchd agent that runs --sync every Sunday at 03:00."""
    path = _plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_plist_contents(creds_path))

    uid = os.getuid()
    domain = f"gui/{uid}"
    # bootout an old copy if present (ignore failure), then bootstrap the new one.
    subprocess.run(["launchctl", "bootout", f"{domain}/{PLIST_LABEL}"],
                   capture_output=True, text=True)
    proc = subprocess.run(["launchctl", "bootstrap", domain, path],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"Wrote {path}\nbut `launchctl bootstrap` failed: {proc.stderr.strip()}")
        return 2
    print(
        f"Installed weekly sync (launchd label {PLIST_LABEL}).\n"
        f"  Runs: every Sunday at 03:00 -> {PY313} apple_to_ytmusic.py --sync\n"
        f"  Plist: {path}\n"
        f"  Log:   {os.path.join(SCRIPT_DIR, SYNC_LOG)}\n"
        "Note: playlists sourced from your library need Apple Music running at run "
        "time; URL-sourced playlists sync headlessly.\n"
        "Run it now to test:  launchctl kickstart -k " + f"{domain}/{PLIST_LABEL}"
    )
    return 0


def uninstall_weekly() -> int:
    path = _plist_path()
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{PLIST_LABEL}"],
                   capture_output=True, text=True)
    if os.path.exists(path):
        os.remove(path)
        print(f"Removed weekly sync ({PLIST_LABEL}) and deleted {path}.")
    else:
        print(f"No weekly sync plist found at {path}. Nothing to remove.")
    return 0


# ---------------------------------------------------------------------------
# CLI / interactive
# ---------------------------------------------------------------------------

def pick_playlist_interactive(names: list):
    print("Your Apple Music library playlists:")
    for i, n in enumerate(names, start=1):
        print(f"  {i}. {n}")
    while True:
        choice = input(f"Pick a playlist [1-{len(names)}] (q to quit): ").strip()
        if choice.lower() in ("q", ""):
            return None
        try:
            idx = int(choice)
        except ValueError:
            print("Please enter a number.")
            continue
        if 1 <= idx <= len(names):
            return names[idx - 1]
        print(f"Please enter a number between 1 and {len(names)}.")


def choose_source_interactive() -> dict:
    """Prompt for an Apple Music URL, or fall back to a library picker."""
    raw = input(
        "Paste an Apple Music playlist or artist URL, or press Enter to pick from your library: "
    ).strip()
    if raw:
        if not is_apple_url(raw):
            print("That doesn't look like an Apple Music URL — treating it as a library name.")
            return {"type": "library", "ref": raw}
        return {"type": "url", "ref": raw}

    try:
        names = list_apple_playlists()
    except AppleMusicAuthError:
        print(_automation_help())
        return None
    if not names:
        print("No user playlists found in Apple Music.")
        return None
    name = pick_playlist_interactive(names)
    if not name:
        return None
    return {"type": "library", "ref": name}


def _automation_help() -> str:
    return (
        "Apple Music automation is not authorized.\n"
        "Enable it via: System Settings -> Privacy & Security -> Automation -> "
        "[Terminal] -> enable 'Music'. Then re-run this tool."
    )


def confirm(prompt: str) -> bool:
    answer = input(prompt).strip().lower()
    return answer in ("y", "yes")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="apple_to_ytmusic.py",
        description="Mirror an Apple Music playlist (URL or library) to YouTube Music, then optionally download it.",
    )
    parser.add_argument("--url", type=str, default=None,
                        help="Public Apple Music playlist OR artist URL. Artist links "
                             "(music.apple.com/.../artist/...) mirror that artist's Top Songs.")
    parser.add_argument("--playlist", type=str, default=None,
                        help="Apple Music LIBRARY playlist name to run (skips interactive picker).")
    parser.add_argument("--no-download", action="store_true",
                        help="Run Stage 1+2 only (search, create, add); skip the download.")
    parser.add_argument("--report-only", action="store_true",
                        help="Run Stage 1+2 search & scoring, write match_report.csv, but do not create/modify a YT playlist and do not download.")
    parser.add_argument("--stage1-only", action="store_true",
                        help="Run Stage 1 only: read the playlist's tracks and print them. No ytmusicapi credentials needed.")
    parser.add_argument("--sync", action="store_true",
                        help="Re-check every tracked playlist and ADD new songs only (never deletes). Non-interactive; no download.")
    parser.add_argument("--install-weekly", action="store_true",
                        help="Install a launchd job that runs --sync every Sunday at 03:00.")
    parser.add_argument("--uninstall-weekly", action="store_true",
                        help="Remove the weekly --sync launchd job.")
    parser.add_argument("--out", type=str, default=DEFAULT_OUT,
                        help=f"Download target dir / report dir (default: {DEFAULT_OUT}).")
    parser.add_argument("--creds", type=str, default=DEFAULT_CREDS,
                        help="Path to ytmusicapi creds file (default: browser.json in script dir).")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def resolve_source(args) -> dict:
    """Determine the playlist source from flags or interactively."""
    if args.url:
        return {"type": "url", "ref": args.url}
    if args.playlist:
        return {"type": "library", "ref": args.playlist}
    return choose_source_interactive()


def prompt_download_choice() -> bool:
    """Interactive: download offline now, or just keep the YT Music playlist."""
    print("\nWhat next?")
    print("  1. Download the playlist offline now (for your iPod)")
    print("  2. Just keep the YouTube Music playlist (done)")
    while True:
        choice = input("Choose [1/2]: ").strip()
        if choice == "1":
            return True
        if choice in ("2", "", "q"):
            return False
        print("Please enter 1 or 2.")


def main(argv=None) -> int:
    args = parse_args(argv)

    # --- scheduling commands ---
    if args.install_weekly:
        return install_weekly(args.creds)
    if args.uninstall_weekly:
        return uninstall_weekly()
    if args.sync:
        return run_sync(args.creds)

    # --- determine the source (URL / library / interactive) ---
    source = resolve_source(args)
    if not source:
        print("No playlist selected. Exiting.")
        return 0

    # --- Stage 1: read tracks ---
    try:
        name, tracks = get_tracks_from_source(source)
    except AppleMusicAuthError:
        print(_automation_help())
        return 2
    except AppleScriptError as e:
        if source["type"] == "library":
            print(f"Playlist '{source['ref']}' could not be read: {e}\n"
                  "If the name is wrong, run without --playlist to pick from a list.")
        else:
            print(f"AppleScript error: {e}")
        return 2
    except AppleURLError as e:
        print(str(e))
        return 2

    if not tracks:
        print(f"Playlist '{name}' is empty.")
        return 0

    if args.stage1_only:
        if source["type"] == "url":
            origin = "artist URL" if is_apple_artist_url(source["ref"]) else "URL"
        else:
            origin = "library"
        print(f"\nPlaylist '{name}' ({origin}) — {len(tracks)} tracks:")
        for i, t in enumerate(tracks, start=1):
            mins, secs = divmod(int(round(t["duration"])), 60)
            album = f'  [{t["album"]}]' if t["album"] else ""
            print(f'  {i:>3}. {t["title"]} — {t["artist"]}{album}  ({mins}:{secs:02d})')
        return 0

    # --- Stage 2: match ---
    try:
        yt = make_ytmusic(resolve_creds(args.creds))
    except CredsError as e:
        print(str(e))
        return 2

    matches = match_playlist(yt, tracks)

    report_dir = args.out if args.out else SCRIPT_DIR
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, REPORT_FILE)
    write_report(report_path, matches)

    n_confident = sum(1 for m in matches if m["confidence"] == "confident")
    n_fallback = sum(1 for m in matches if m["confidence"] == "fallback")
    n_none = sum(1 for m in matches if m["confidence"] == "none")
    print(f"\nMatch summary: {n_confident} confident, {n_fallback} fallback, {n_none} none (of {len(matches)} tracks)")
    print(f"Report written to: {report_path}")

    if args.report_only:
        print("Report only. No playlist created.")
        return 0

    # --- Stage 2: create + add (idempotent) ---
    runs = load_runs()
    try:
        pid, already = get_or_create_playlist(yt, name, runs, source)
    except CredsError as e:
        print(str(e))
        return 2

    added = add_matches(yt, pid, matches, already, runs, name)
    matched = sum(1 for m in matches if m.get("videoId"))
    print(f"\nYT Music playlist: {YTM_URL.format(pid=pid)}")
    print(f"added {added} / matched {matched} / total {len(matches)} tracks")

    if args.no_download:
        return 0

    # --- Stage 3: download or just keep ---
    want_download = prompt_download_choice()
    if not want_download:
        print("Done — playlist is on YouTube Music. (Re-run any time; it only adds new songs.)")
        return 0

    rc = download_playlist(pid, name, args.out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
