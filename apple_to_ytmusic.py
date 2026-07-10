#!/Library/Frameworks/Python.framework/Versions/3.13/bin/python3
"""
apple_to_ytmusic.py — Mirror an Apple Music playlist to YouTube Music, then
download it for an iPod.

Three stages, each independently reachable via CLI flags:
  1. Read a playlist + its tracks from Apple Music (AppleScript / osascript).
  2. Search + score each track on YouTube Music (ytmusicapi), create/update a
     mirrored playlist, and write an audit report (match_report.csv).
  3. Download the resulting YT Music playlist for offline listening via the
     existing `streamlist` tool.

See README.md for one-time setup (ytmusicapi auth, Automation permission).
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
DEFAULT_OUT = os.path.expanduser("~/Music/apple-to-ytmusic")
CONFIDENCE_BAR = 60                             # score >= 60 => "confident"
SEARCH_TOP_N = 5                                # scoring window over search results
DELIM = "\x1f"                                  # AppleScript TSV delimiter (unit separator)
YTM_URL = "https://music.youtube.com/playlist?list={pid}"

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


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AppleMusicAuthError(Exception):
    """Raised when osascript is not authorized to control Music.app (-1743)."""


class AppleScriptError(Exception):
    """Raised for any other osascript failure."""


class CredsError(Exception):
    """Raised when ytmusicapi credentials are missing, invalid, or expired."""


# ---------------------------------------------------------------------------
# Stage 1 — Apple Music (AppleScript)
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
        if "-1743" in stderr or "Not authorized" in stderr or "not authorized" in stderr:
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
    """Dump (title, artist, album, duration) for every track in the given playlist."""
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

    # sort by (-score, -artist_sim, dur_diff, index) for stable tie-break
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

    # fallback: raw top hit (results[0]), but report the top hit's own computed score
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


def match_playlist(yt, tracks: list) -> list:
    """Search + score every track; print live progress."""
    matches = []
    total = len(tracks)
    for i, track in enumerate(tracks, start=1):
        results = search_track(yt, track)
        match = choose_match(track, results)
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


def get_or_create_playlist(yt, name: str, runs: dict):
    if name in runs:
        entry = runs[name]
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
        "last_run": datetime.now().isoformat(),
    }
    save_runs(runs)
    return pid, set()


def add_matches(yt, pid: str, matches: list, already: set, runs: dict, name: str) -> int:
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
        # all-duplicate batches (or transient errors) — treat as no-op
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
# CLI / interactive
# ---------------------------------------------------------------------------

def pick_playlist_interactive(names: list) -> str:
    print("Apple Music playlists:")
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


def confirm(prompt: str) -> bool:
    answer = input(prompt).strip().lower()
    return answer in ("y", "yes")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="apple_to_ytmusic.py",
        description="Mirror an Apple Music playlist to YouTube Music, then download it for an iPod.",
    )
    parser.add_argument("--playlist", type=str, default=None,
                        help="Apple Music playlist name to run (skips interactive picker).")
    parser.add_argument("--no-download", action="store_true",
                        help="Run Stage 1+2 only (search, create, add); skip Stage 3 download.")
    parser.add_argument("--report-only", action="store_true",
                        help="Run Stage 1+2 search & scoring, write match_report.csv, but do not create/modify a YT playlist and do not download.")
    parser.add_argument("--stage1-only", action="store_true",
                        help="Run Stage 1 only: extract the playlist's tracks from Apple Music and print them. Requires no ytmusicapi credentials.")
    parser.add_argument("--out", type=str, default=DEFAULT_OUT,
                        help=f"Download target dir / report dir (default: {DEFAULT_OUT}).")
    parser.add_argument("--creds", type=str, default=DEFAULT_CREDS,
                        help="Path to ytmusicapi creds file (default: browser.json in script dir).")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        names = list_apple_playlists()
    except AppleMusicAuthError:
        print(
            "Apple Music automation is not authorized.\n"
            "Enable it via: System Settings -> Privacy & Security -> Automation -> "
            "[Terminal] -> enable 'Music'. Then re-run this tool."
        )
        return 2
    except AppleScriptError as e:
        print(f"AppleScript error while listing playlists: {e}")
        return 2

    if not names:
        print("No user playlists found in Apple Music.")
        return 0

    if args.playlist:
        if args.playlist not in names:
            print(f"Playlist '{args.playlist}' not found. Available playlists:")
            for n in names:
                print(f"  - {n}")
            return 2
        name = args.playlist
    else:
        name = pick_playlist_interactive(names)
        if not name:
            print("No playlist selected. Exiting.")
            return 0

    try:
        tracks = dump_apple_tracks(name)
    except AppleMusicAuthError:
        print(
            "Apple Music automation is not authorized.\n"
            "Enable it via: System Settings -> Privacy & Security -> Automation -> "
            "[Terminal] -> enable 'Music'. Then re-run this tool."
        )
        return 2
    except AppleScriptError as e:
        print(f"AppleScript error while reading tracks: {e}")
        return 2

    if not tracks:
        print(f"Playlist '{name}' is empty.")
        return 0

    if args.stage1_only:
        print(f"\nPlaylist '{name}' — {len(tracks)} tracks:")
        for i, t in enumerate(tracks, start=1):
            mins, secs = divmod(int(round(t["duration"])), 60)
            print(f'  {i:>3}. {t["title"]} — {t["artist"]}  [{t["album"]}]  ({mins}:{secs:02d})')
        return 0

    try:
        yt = make_ytmusic(resolve_creds(args.creds))
    except CredsError as e:
        print(str(e))
        return 2

    matches = match_playlist(yt, tracks)

    report_dir = args.out if args.out else SCRIPT_DIR
    if args.report_only and not args.out:
        report_dir = SCRIPT_DIR
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

    runs = load_runs()
    try:
        pid, already = get_or_create_playlist(yt, name, runs)
    except CredsError as e:
        print(str(e))
        return 2

    added = add_matches(yt, pid, matches, already, runs, name)
    matched = sum(1 for m in matches if m.get("videoId"))
    print(f"\nYT Music playlist: {YTM_URL.format(pid=pid)}")
    print(f"added {added} / matched {matched} / total {len(matches)} tracks")

    if args.no_download:
        return 0

    if not confirm(f"Download {matched} tracks to {args.out}? [y/N] "):
        print("Skipped download.")
        return 0

    rc = download_playlist(pid, name, args.out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
