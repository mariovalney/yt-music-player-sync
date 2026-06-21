#!/usr/bin/env python3
"""Sync YouTube Music playlists/albums to an SD card as MP3s."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import browser_cookie3
import psutil
import questionary
from platformdirs import user_cache_dir, user_config_dir
from rich.console import Console
import ytmusicapi
from ytmusicapi import YTMusic
from ytmusicapi.helpers import get_authorization, sapisid_from_cookie

console = Console()

MUSIC_DIR_NAME = "Music"
METADATA_DIR_NAME = ".metadata"
ARCHIVE_NAME = "archive"
INDEX_NAME = "index.json"
SYNCED_NAME = "synced.json"
TYPE_DIRS = {"playlist": "Playlists", "album": "Albums", "artist": "Artists"}
LIBRARY_CACHE_FILE = Path(user_cache_dir("yt-music-player-sync")) / "library.json"
TRACKS_CACHE_FILE  = Path(user_cache_dir("yt-music-player-sync")) / "tracks_cache.json"
CONFIG_FILE = Path(user_config_dir("yt-music-player-sync")) / "config.json"
TRACKS_CACHE_TTL   = 30 * 60  # 30 minutes in seconds

LETTER_GROUPS = [
    ("A–E", "ABCDE"),
    ("F–J", "FGHIJ"),
    ("K–O", "KLMNO"),
    ("P–T", "PQRST"),
    ("U–Z", "UVWXYZ"),
]
AVG_TRACK_BYTES = 7_500_000   # ~7.5 MB: 3 min at 320 kbps
BYTES_PER_SEC   = 40_000      # 320 kbps


# ── JS runtime ────────────────────────────────────────────────────────────────

def check_dependencies():
    missing = []

    if not shutil.which("ffmpeg"):
        missing.append("ffmpeg — required for audio conversion to MP3.")

    if not shutil.which("ffprobe"):
        missing.append("ffprobe — required by ffmpeg (usually bundled with it).")

    js_runtime = None
    for runtime in ("node", "deno", "quickjs"):
        if shutil.which(runtime):
            js_runtime = runtime
            break

    if not js_runtime:
        missing.append(
            "JavaScript runtime — required by yt-dlp for YouTube signatures.\n"
            "  Install Node.js 22+: https://nodejs.org\n"
            "  Or Deno 2.3+: https://deno.com"
        )

    if missing:
        console.print("[red]Missing required dependencies:[/red]")
        for m in missing:
            console.print(f"  • {m}")
        sys.exit(1)

    return js_runtime


# ── yt-dlp ────────────────────────────────────────────────────────────────────

def ytdlp_cmd():
    try:
        subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            check=True, capture_output=True,
        )
        return [sys.executable, "-m", "yt_dlp"]
    except subprocess.CalledProcessError:
        pass

    console.print("[yellow]yt-dlp not found.[/yellow]")
    if not questionary.confirm("Install yt-dlp now?").ask():
        console.print("[red]yt-dlp is required to continue.[/red]")
        sys.exit(1)

    console.print("Installing yt-dlp...")
    subprocess.run([sys.executable, "-m", "pip", "install", "yt-dlp"], check=True)
    console.print("[green]yt-dlp installed.[/green]")
    return [sys.executable, "-m", "yt_dlp"]


# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {}


def save_config(data):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    current = load_config()
    current.update(data)
    CONFIG_FILE.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")


# ── SD card detection ──────────────────────────────────────────────────────────

_SYSTEM_MOUNTPOINTS = {"/boot", "/boot/efi", "/efi", "/System/Volumes"}

def detect_sd_cards():
    fat32_fstypes = {"vfat", "fat32", "msdos"}
    cards = []
    for p in psutil.disk_partitions():
        if not p.fstype or p.fstype.lower() not in fat32_fstypes:
            continue
        if not p.mountpoint:
            continue
        if any(p.mountpoint.startswith(s) for s in _SYSTEM_MOUNTPOINTS):
            continue
        name = Path(p.mountpoint).name or p.mountpoint
        cards.append({"name": name, "mountpoint": p.mountpoint})
    return cards


def select_sd_card(cards):
    if not cards:
        console.print("[red]No FAT32 SD card found. Connect one and try again.[/red]")
        sys.exit(1)

    last = load_config().get("last_device")

    if len(cards) == 1:
        card = cards[0]
        if not questionary.confirm(f"Use '{card['name']}' at {card['mountpoint']}?", default=True).ask():
            sys.exit(0)
        save_config({"last_device": card["mountpoint"]})
        return Path(card["mountpoint"])

    sorted_cards = sorted(cards, key=lambda c: c["mountpoint"] != last)
    choices = [
        questionary.Choice(
            f"{c['name']} ({c['mountpoint']})" + (" [last used]" if c["mountpoint"] == last else ""),
            value=c,
        )
        for c in sorted_cards
    ]
    selected = questionary.select("Select the SD card:", choices=choices).ask()
    if not selected:
        sys.exit(0)
    save_config({"last_device": selected["mountpoint"]})
    return Path(selected["mountpoint"])


# ── YouTube Music auth + library ───────────────────────────────────────────────

def get_ytmusic():
    console.print("Reading cookies from Chrome for music.youtube.com...")

    jar = browser_cookie3.chrome(domain_name="youtube.com")
    filtered = [c for c in jar if c.domain in (".youtube.com", "music.youtube.com")]
    cookies = {c.name: c.value for c in filtered}

    if not cookies:
        console.print("[red]No cookies found. Make sure Chrome is open and logged in to YouTube Music.[/red]")
        sys.exit(1)

    console.print("[green]Cookies read successfully. Nothing will be stored.[/green]")

    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

    try:
        sapisid = sapisid_from_cookie(cookie_str)
    except KeyError:
        console.print("[red]Cookie __Secure-3PAPISID not found. Make sure you are logged in to YouTube Music in Chrome.[/red]")
        sys.exit(1)

    authorization = get_authorization(sapisid + " https://music.youtube.com")
    headers_raw = f"cookie: {cookie_str}\nx-goog-authuser: 0\nauthorization: {authorization}"
    auth_json = ytmusicapi.setup(headers_raw=headers_raw)
    return YTMusic(auth=auth_json), filtered


def _write_cookie_file(jar, path):
    """Write a Netscape-format cookie file from a browser_cookie3 jar."""
    lines = ["# Netscape HTTP Cookie File"]
    for c in jar:
        domain = c.domain or ""
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.secure else "FALSE"
        expiry = str(int(c.expires)) if c.expires else "0"
        lines.append(f"{domain}\t{flag}\t{c.path or '/'}\t{secure}\t{expiry}\t{c.name}\t{c.value}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fetch_library(ytm):
    console.print("Loading YouTube Music library...", end=" ")
    items = []

    for p in ytm.get_library_playlists(limit=None):
        count = p.get("count")
        label = f"[Playlist] {p['title']}" + (f" ({count} tracks)" if count else "")
        items.append({
            "label": label,
            "name": p["title"],
            "type": "playlist",
            "id": p["playlistId"],
            "count": count,
        })

    for a in ytm.get_library_albums(limit=None):
        artist = a["artists"][0]["name"] if a.get("artists") else "?"
        items.append({
            "label": f"[Album]    {a['title']} - {artist}",
            "name": a["title"],
            "type": "album",
            "id": a["browseId"],
        })

    for ar in ytm.get_library_artists(limit=None):
        items.append({
            "label": f"[Artist]   {ar['artist']}",
            "name": ar["artist"],
            "type": "artist",
            "id": ar["browseId"],
        })

    console.print(f"[green]{len(items)} items found.[/green]")

    LIBRARY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LIBRARY_CACHE_FILE.write_text(
        json.dumps({"cached_at": datetime.now(timezone.utc).isoformat(), "items": items}, ensure_ascii=False),
        encoding="utf-8",
    )
    return items


def load_library(ytm):
    if LIBRARY_CACHE_FILE.exists():
        cache = json.loads(LIBRARY_CACHE_FILE.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(cache["cached_at"])
        age = datetime.now(timezone.utc) - cached_at
        hours, remainder = divmod(int(age.total_seconds()), 3600)
        minutes = remainder // 60
        age_str = f"{hours}h {minutes}m ago" if hours else f"{minutes}m ago"

        if questionary.confirm(f"Use cached library ({len(cache['items'])} items, fetched {age_str})?", default=True).ask():
            return cache["items"]

    return _fetch_library(ytm)


_FEATURED_KEYWORDS = {"listen again", "ouvir de novo"}

def get_home_items(ytm):
    """Fetch YouTube Music home feed, keep only 'Listen again' and 'Recaps' sections."""
    try:
        home = ytm.get_home(limit=20)
    except Exception:
        return []

    seen = set()
    items = []
    for section in home:
        title_lower = section.get("title", "").lower()
        if not any(kw in title_lower for kw in _FEATURED_KEYWORDS):
            continue
        for content in section.get("contents", []):
            if content.get("videoId"):
                continue

            playlist_id = content.get("playlistId", "")
            browse_id = content.get("browseId", "")
            name = content.get("title") or content.get("artist") or ""

            if playlist_id and not playlist_id.startswith("RD"):
                item_id, item_type = playlist_id, "playlist"
                label = f"[Playlist] {name}"
            elif browse_id.startswith("MPREb_"):
                item_id, item_type = browse_id, "album"
                artists = content.get("artists", [])
                artist_name = artists[0]["name"] if artists else "?"
                label = f"[Album]    {name} - {artist_name}"
            elif browse_id.startswith("UC") and content.get("subscribers"):
                item_id, item_type = browse_id, "artist"
                label = f"[Artist]   {name}"
            else:
                continue

            if not item_id or not name or item_id in seen:
                continue

            seen.add(item_id)
            items.append({"label": label, "name": name, "type": item_type, "id": item_id})

    return items


def _load_tracks_cache():
    if TRACKS_CACHE_FILE.exists():
        try:
            return json.loads(TRACKS_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_tracks_cache(cache):
    TRACKS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRACKS_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def _get_artist_data(ytm, item):
    """Returns (tracks, songs_playlist_id) for an artist, using cache."""
    cache = _load_tracks_cache()
    entry = cache.get(item["id"])
    if entry and (time.time() - entry["cached_at"]) < TRACKS_CACHE_TTL:
        return entry["tracks"], entry.get("songs_playlist_id")

    artist = ytm.get_artist(item["id"])
    songs_browse_id = artist.get("songs", {}).get("browseId")
    if songs_browse_id:
        result = ytm.get_playlist(songs_browse_id, limit=None)
        raw_tracks = result.get("tracks", [])
    else:
        raw_tracks = artist.get("songs", {}).get("results", [])
        songs_browse_id = None

    tracks = [
        {"videoId": t["videoId"], "title": t["title"], "duration_seconds": t.get("duration_seconds")}
        for t in raw_tracks
        if t.get("videoId")
    ]

    cache[item["id"]] = {"cached_at": time.time(), "tracks": tracks, "songs_playlist_id": songs_browse_id}
    _save_tracks_cache(cache)
    return tracks, songs_browse_id


def get_tracks(ytm, item):
    cache = _load_tracks_cache()
    entry = cache.get(item["id"])
    if entry and (time.time() - entry["cached_at"]) < TRACKS_CACHE_TTL:
        return entry["tracks"]

    if item["type"] == "playlist":
        result = ytm.get_playlist(item["id"], limit=None)
        tracks = result.get("tracks", [])
    elif item["type"] == "album":
        result = ytm.get_album(item["id"])
        tracks = result.get("tracks", [])
    else:
        tracks, _ = _get_artist_data(ytm, item)
        return tracks

    result = [
        {"videoId": t["videoId"], "title": t["title"], "duration_seconds": t.get("duration_seconds")}
        for t in tracks
        if t.get("videoId")
    ]

    cache[item["id"]] = {"cached_at": time.time(), "tracks": result}
    _save_tracks_cache(cache)
    return result


def download_url(item):
    if item["type"] == "playlist":
        return f"https://music.youtube.com/playlist?list={item['id']}"
    return f"https://music.youtube.com/browse/{item['id']}"


# ── Item folder ────────────────────────────────────────────────────────────────

def item_dir(music_dir, item):
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", item["name"]).strip()
    return music_dir / TYPE_DIRS[item["type"]] / safe_name


# ── Synced items (SD card) ─────────────────────────────────────────────────────

def load_synced(music_dir):
    path = music_dir / METADATA_DIR_NAME / SYNCED_NAME
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")).get("items", [])
    return []


def save_synced(music_dir, items):
    path = music_dir / METADATA_DIR_NAME / SYNCED_NAME
    path.write_text(json.dumps({"items": items}, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Index ──────────────────────────────────────────────────────────────────────

def load_index(music_dir):
    path = music_dir / METADATA_DIR_NAME / INDEX_NAME
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_index(music_dir, index):
    path = music_dir / METADATA_DIR_NAME / INDEX_NAME
    path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Download ───────────────────────────────────────────────────────────────────

def run_download(url, item_folder, item_type, music_dir, ytdlp, cookie_path):
    if item_type == "playlist":
        output_template = str(
            item_folder
            / "%(artist,uploader|Unknown Artist)s"
            / "%(album,playlist_title|Unknown Album)s"
            / "%(title)s.%(ext)s"
        )
    elif item_type == "album":
        output_template = str(item_folder / "%(title)s.%(ext)s")
    else:  # artist
        output_template = str(
            item_folder
            / "%(album,playlist_title|Unknown Album)s"
            / "%(title)s.%(ext)s"
        )

    archive = item_folder / ".archive"

    track_file = tempfile.mktemp(suffix=".txt")

    cmd = [
        *ytdlp,
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg", "--add-metadata",
        "--parse-metadata", "%(uploader)s:%(meta_artist)s",
        "--parse-metadata", "%(playlist_title)s:%(meta_album)s",
        "--parse-metadata", "%(playlist_index)s:%(meta_track)s",
        "--download-archive", str(archive),
        "--ignore-errors",
        "--no-warnings",
        "--progress",
        "--sleep-interval", "3",
        "--cookies", cookie_path,
        "--extractor-args", "youtubetab:skip=authcheck",
        "--print", "before_dl:[%(playlist_index)s/%(playlist_count)s] %(title)s",
        "--print-to-file", "after_move:%(id)s\t%(filepath)s", track_file,
        "-o", output_template,
        url,
    ]

    subprocess.run(cmd)

    new_tracks = {}
    if os.path.exists(track_file):
        for line in Path(track_file).read_text(encoding="utf-8").splitlines():
            if "\t" not in line:
                continue
            video_id, filepath = line.split("\t", 1)
            video_id = video_id.strip()
            filepath = filepath.strip()
            if video_id and filepath and Path(filepath).exists():
                rel = Path(os.path.relpath(filepath, music_dir)).as_posix()
                new_tracks[video_id] = rel
        os.unlink(track_file)

    return new_tracks


# ── M3U ───────────────────────────────────────────────────────────────────────

def write_m3u(item_folder, item_name, tracks, item_index, music_dir):
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", item_name).strip()
    m3u_path = item_folder / f"{safe_name}.m3u"

    lines = ["#EXTM3U"]
    included = 0
    for track in tracks:
        vid = track["videoId"]
        if vid not in item_index:
            continue
        abs_track = music_dir / item_index[vid]
        rel = Path(os.path.relpath(abs_track, item_folder)).as_posix()
        lines.append(f"#EXTINF:0,{track['title']}")
        lines.append(rel)
        included += 1

    m3u_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return m3u_path, included


# ── Pre-download estimation ────────────────────────────────────────────────────

def _fmt_bytes(b):
    if b >= 1e9:
        return f"{b / 1e9:.1f} GB"
    return f"{b / 1e6:.0f} MB"


def preflight_check(selected, index, sd_root, ytm):
    """Estimate download size, check disk space, and ask user to confirm."""
    console.print("\nEstimating download size...")

    total_estimated = 0
    total_new = 0

    for item in selected:
        item_index = index.get(item["id"], {})
        already = set(item_index.keys())

        if item.get("track_ids"):
            new_count = sum(1 for t in item["track_ids"] if t not in already)
            total_estimated += new_count * AVG_TRACK_BYTES
            total_new += new_count
        else:
            tracks = get_tracks(ytm, item)
            for t in tracks:
                if t["videoId"] not in already:
                    secs = t.get("duration_seconds") or 180
                    total_estimated += secs * BYTES_PER_SEC
                    total_new += 1

    disk = psutil.disk_usage(sd_root)

    console.print(f"  Estimated download : ~{_fmt_bytes(total_estimated)} ({total_new} new track(s))")
    console.print(f"  Free space on disk : {_fmt_bytes(disk.free)} of {_fmt_bytes(disk.total)} total")

    if total_estimated > disk.total:
        console.print(f"[red]Estimated download exceeds total disk capacity. Cannot proceed.[/red]")
        return False

    if total_estimated > disk.free:
        console.print(f"[yellow]⚠ Estimated download may exceed available free space.[/yellow]")
        console.print("[dim]  (Some tracks may already be downloaded and will be skipped.)[/dim]")
        answer = questionary.confirm("Proceed anyway?", default=False).ask()
        return bool(answer)

    answer = questionary.confirm(
        f"Proceed with download?" + (" (nothing new estimated)" if total_new == 0 else ""),
        default=True,
    ).ask()
    return bool(answer)


# ── Item selection UI ─────────────────────────────────────────────────────────

def select_items_to_sync(ytm, synced):
    """Interactive loop: user manages playlists/albums/artists by type. Returns final list or None."""
    library = load_library(ytm)
    if not library:
        console.print("[red]No items found in your library.[/red]")
        return None

    console.print("Loading home feed for featured items...", end=" ")
    featured = get_home_items(ytm)
    console.print(f"[green]{len(featured)} featured item(s).[/green]")

    pending = {item["id"]: item for item in synced}

    type_labels = {"playlist": "Playlists", "album": "Albums", "artist": "Artists"}
    liked_music = next((i for i in library if i["id"] == "LM"), None)
    library_by_type = {
        t: sorted([i for i in library if i["type"] == t and i["id"] != "LM"], key=lambda x: x["name"].casefold())
        for t in type_labels
    }
    available_types = [t for t in type_labels if library_by_type[t] or (t == "playlist" and liked_music)]

    def _pending_count_of(items):
        ids = {i["id"] for i in items}
        return sum(1 for id_ in ids if id_ in pending)

    def _update_pending(visible_items, selected_items):
        nonlocal pending
        if selected_items is None:
            return
        visible_ids = {i["id"] for i in visible_items}
        pending = {id_: item for id_, item in pending.items() if id_ not in visible_ids}
        for item in selected_items:
            pending[item["id"]] = item

    def _letter_group_nav(items_of_type, type_label, featured_of_type, pinned=None):
        """Inner loop: letter group selector → checkbox → back to letter group. Returns on Back."""
        while True:
            group_choices = []
            items_by_group = {}

            if pinned:
                n_sel = _pending_count_of(pinned)
                items_by_group["__pinned__"] = pinned
                group_choices.append(
                    questionary.Choice(
                        f"{pinned[0]['name']}  ({'selected' if n_sel else 'not selected'})",
                        value="__pinned__",
                    )
                )
                group_choices.append(questionary.Separator())

            if featured_of_type:
                n_sel = _pending_count_of(featured_of_type)
                items_by_group["__featured__"] = featured_of_type
                group_choices.append(
                    questionary.Choice(
                        f"Featured  ({n_sel} selected of {len(featured_of_type)})",
                        value="__featured__",
                    )
                )
                group_choices.append(questionary.Separator())

            for gl_label, letters in LETTER_GROUPS:
                group_items = [i for i in items_of_type if i["name"][:1].upper() in letters]
                if not group_items:
                    continue
                n_sel = _pending_count_of(group_items)
                items_by_group[gl_label] = group_items
                group_choices.append(
                    questionary.Choice(f"{gl_label}  ({n_sel} selected of {len(group_items)})", value=gl_label)
                )

            other_items = [i for i in items_of_type if not i["name"][:1].upper().isalpha()]
            if other_items:
                n_sel = _pending_count_of(other_items)
                items_by_group["#"] = other_items
                group_choices.append(
                    questionary.Choice(f"#  ({n_sel} selected of {len(other_items)})", value="#")
                )

            group_choices += [questionary.Separator(), questionary.Choice("← Back", value="back")]

            console.clear()
            group_action = questionary.select(
                f"Select letter range for {type_label}:", choices=group_choices
            ).ask()

            if group_action is None or group_action == "back":
                return

            visible = items_by_group[group_action]
            pending_ids = {id_ for id_ in pending if id_ in {i["id"] for i in visible}}

            if group_action == "__pinned__":
                item = visible[0]
                is_selected = item["id"] in pending
                if is_selected:
                    pending.pop(item["id"])
                else:
                    pending[item["id"]] = item
                continue
            elif group_action == "__featured__":
                prompt = f"Select featured {type_label}:"
            elif group_action == "#":
                prompt = f"Select {type_label} (#/Other):"
            else:
                first, last = group_action[0], group_action[-1]
                prompt = f"Select {type_label} ({first}–{last}):"

            checkbox_choices = [
                questionary.Choice(title=i["label"], value=i, checked=i["id"] in pending_ids)
                for i in visible
            ]
            console.clear()
            selected = questionary.checkbox(prompt, choices=checkbox_choices).ask()
            _update_pending(visible, selected)
            # Loop back to letter group menu

    # ── Main loop ────────────────────────────────────────────────────────────
    while True:
        counts = {t: sum(1 for i in pending.values() if i["type"] == t) for t in type_labels}
        total = len(pending)

        choices = []

        for t in available_types:
            n_total = len(library_by_type[t])
            choices.append(
                questionary.Choice(f"Manage {type_labels[t]}  ({counts[t]} / {n_total} total)", value=t)
            )

        choices.append(questionary.Separator())

        if total > 0:
            choices.append(
                questionary.Choice(f"Continue → start download ({total} item(s) selected)", value="done")
            )

        choices.append(questionary.Choice("Cancel", value="cancel"))

        console.clear()
        action = questionary.select("Manage your sync list:", choices=choices).ask()

        if action is None or action == "cancel":
            return None
        if action == "done":
            return list(pending.values())

        featured_of_type = [i for i in featured if i["type"] == action]
        pinned = [liked_music] if action == "playlist" and liked_music else None
        _letter_group_nav(library_by_type[action], type_labels[action], featured_of_type, pinned=pinned)


# ── Sync logic ─────────────────────────────────────────────────────────────────

def _remove_from_archive(item_folder, ids):
    """Remove specific video IDs from the item's yt-dlp archive file."""
    archive = item_folder / ".archive"
    if not archive.exists() or not ids:
        return
    to_remove = {f"youtube {vid}" for vid in ids}
    lines = archive.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if line.strip() not in to_remove]
    archive.write_text("\n".join(kept) + "\n", encoding="utf-8")


def sync_one(item, music_dir, ytdlp, ytm, index, cookie_path):
    """Sync a single item. Returns the item updated with current track_ids."""
    console.rule(f"[bold]{item['name']}[/bold]")

    idir = item_dir(music_dir, item)
    idir.mkdir(parents=True, exist_ok=True)
    item_index = index.setdefault(item["id"], {})

    if item["type"] == "artist":
        tracks, songs_playlist_id = _get_artist_data(ytm, item)
    else:
        tracks = get_tracks(ytm, item)
        songs_playlist_id = None
    current_ids = {t["videoId"] for t in tracks}
    previous_ids = set(item.get("track_ids", []))

    # Tracks removed from the YouTube Music source
    removed_from_source = previous_ids - current_ids
    if removed_from_source:
        deleted = 0
        for track_id in removed_from_source:
            if track_id not in item_index:
                continue
            file_path = music_dir / item_index.pop(track_id)
            if file_path.exists():
                _safe_delete(file_path, music_dir)
                deleted += 1
                parent = file_path.parent
                while parent != idir:
                    try:
                        parent.rmdir()
                        parent = parent.parent
                    except OSError:
                        break
        if deleted:
            console.print(f"Removed [red]{deleted}[/red] track(s) no longer in source.")

    # Tracks whose files are missing — remove from archive so yt-dlp re-downloads
    missing_files = [
        vid for vid in current_ids
        if vid in item_index and not (music_dir / item_index[vid]).exists()
    ]
    if missing_files:
        for vid in missing_files:
            item_index.pop(vid, None)
        _remove_from_archive(idir, missing_files)
        console.print(f"[yellow]{len(missing_files)}[/yellow] missing file(s) queued for re-download.")

    console.print(f"Tracks in source: [cyan]{len(tracks)}[/cyan]")

    if item["type"] == "artist" and songs_playlist_id:
        playlist_id = songs_playlist_id[2:] if songs_playlist_id.startswith("VL") else songs_playlist_id
        dl_url = f"https://music.youtube.com/playlist?list={playlist_id}"
    elif item["type"] == "artist" and item["id"].startswith("MPLAUC"):
        channel_id = "UC" + item["id"][len("MPLAUC"):]
        dl_url = f"https://music.youtube.com/channel/{channel_id}"
    else:
        dl_url = download_url(item)
    new_tracks = run_download(dl_url, idir, item["type"], music_dir, ytdlp, cookie_path)
    if new_tracks:
        item_index.update(new_tracks)
        console.print(f"New downloads: [green]{len(new_tracks)}[/green]")
    else:
        console.print("No new tracks.")

    # Remove from archive any videoIds not in the index so they get re-downloaded next sync
    archive = idir / ".archive"
    if archive.exists():
        archived_ids = set()
        for line in archive.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[0] == "youtube":
                archived_ids.add(parts[1])
        unindexed = archived_ids - set(item_index.keys())
        if unindexed:
            _remove_from_archive(idir, unindexed)
            console.print(f"[yellow]{len(unindexed)}[/yellow] track(s) in archive but not indexed — queued for re-download.")

    m3u_path, count = write_m3u(idir, item["name"], tracks, item_index, music_dir)
    console.print(f"M3U: [blue]{m3u_path.relative_to(music_dir.parent)}[/blue] ({count}/{len(tracks)} tracks)")

    return {**item, "track_ids": [t["videoId"] for t in tracks]}


def remove_deselected(deselected, music_dir, index):
    """Delete the item folder for each deselected item and remove from index."""
    for item in deselected:
        idir = item_dir(music_dir, item)
        if idir.exists():
            _safe_delete(idir, music_dir)
            console.print(f"Removed: [dim]{item['name']}[/dim]")
        index.pop(item["id"], None)


def _safe_delete(path: Path, music_dir: Path):
    """Delete a file or directory only if it is inside music_dir."""
    try:
        path.resolve().relative_to(music_dir.resolve())
    except ValueError:
        console.print(f"[red]Blocked: {path} is outside Music/ and will not be deleted.[/red]")
        return
    if path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def clear_music(music_dir):
    mp3_files = list(music_dir.rglob("*.mp3"))
    m3u_files = list(music_dir.rglob("*.m3u"))
    meta_dir = music_dir / METADATA_DIR_NAME

    console.print(f"\nThis will permanently delete everything inside [bold]{music_dir}[/bold]:")
    console.print(f"  {len(mp3_files)} MP3 file(s)")
    console.print(f"  {len(m3u_files)} playlist file(s)")
    if meta_dir.exists():
        console.print(f"  metadata folder ({METADATA_DIR_NAME}/)")
    console.print("Nothing outside the Music/ folder will be touched.\n")

    if not questionary.confirm("Confirm deletion?", default=False).ask():
        console.print("Cancelled.")
        return

    for item in music_dir.iterdir():
        _safe_delete(item, music_dir)
    console.print("[green]SD card cleared.[/green]")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    js_runtime = check_dependencies()
    ytdlp = ytdlp_cmd() + ["--js-runtimes", js_runtime]

    cards = detect_sd_cards()
    sd_root = select_sd_card(cards)
    music_dir = sd_root / MUSIC_DIR_NAME
    music_dir.mkdir(exist_ok=True)
    (music_dir / METADATA_DIR_NAME).mkdir(exist_ok=True)

    while True:
        synced = load_synced(music_dir)

        if synced:
            action = questionary.select(
                "What would you like to do?",
                choices=[
                    questionary.Choice(f"Sync ({len(synced)} synced item(s))", value="sync"),
                    questionary.Choice("Add or remove playlists", value="add"),
                    questionary.Choice("Clear SD card", value="clear"),
                ],
            ).ask()
            if not action:
                break
        else:
            action = "add"

        if action == "clear":
            clear_music(music_dir)

        elif action == "sync":
            ytm, jar = get_ytmusic()
            index = load_index(music_dir)
            if preflight_check(synced, index, sd_root, ytm):
                fd, cookie_path = tempfile.mkstemp(prefix="ytmusic_cookies_", suffix=".txt")
                os.close(fd)
                try:
                    _write_cookie_file(jar, cookie_path)
                    updated = []
                    for item in synced:
                        updated.append(sync_one(item, music_dir, ytdlp, ytm, index, cookie_path=cookie_path))
                    save_index(music_dir, index)
                    save_synced(music_dir, updated)
                    console.print("\n[bold green]Sync complete.[/bold green]")
                finally:
                    Path(cookie_path).unlink(missing_ok=True)

        elif action == "add":
            ytm, jar = get_ytmusic()
            selected = select_items_to_sync(ytm, synced)

            if selected is not None:
                selected_ids = {item["id"] for item in selected}
                deselected = [item for item in synced if item["id"] not in selected_ids]

                index = load_index(music_dir)

                if deselected:
                    remove_deselected(deselected, music_dir, index)
                    save_index(music_dir, index)

                save_synced(music_dir, selected)

                if preflight_check(selected, index, sd_root, ytm):
                    fd, cookie_path = tempfile.mkstemp(prefix="ytmusic_cookies_", suffix=".txt")
                    os.close(fd)
                    try:
                        _write_cookie_file(jar, cookie_path)
                        updated = []
                        for item in selected:
                            updated.append(sync_one(item, music_dir, ytdlp, ytm, index, cookie_path=cookie_path))
                            save_index(music_dir, index)
                    finally:
                        Path(cookie_path).unlink(missing_ok=True)

                    save_synced(music_dir, updated)
                    console.print("\n[bold green]Sync complete.[/bold green]")
                else:
                    console.print("[yellow]Download skipped.[/yellow]")

        if not questionary.confirm("Do something else?", default=False).ask():
            break


if __name__ == "__main__":
    main()
