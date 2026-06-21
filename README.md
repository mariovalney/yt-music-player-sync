# yt-music-player-sync

A script to sync your YouTube Music playlists, albums and followed artists to an SD card as MP3 files, so you can listen offline on any music player.

It reads your library directly from your YouTube Music account, lets you pick what to sync, and downloads everything organized by type and name. Each item gets its own M3U file so your player can navigate by playlist, album or artist.

On each run it only downloads what is new, so syncing is fast after the first time.

The script runs on Linux, macOS and Windows.

## Requirements

- Python 3.10+
- [pipenv](https://pipenv.pypa.io)
- ffmpeg + ffprobe
- Node.js 22+ with [yt-dlp-ejs](https://github.com/yt-dlp/ejs) (`npm install -g yt-dlp-ejs`), or Deno 2.3+
- Google Chrome logged into YouTube Music Premium
- An SD card formatted as FAT32

All Python dependencies (including yt-dlp) are installed automatically when you run `pipenv install`.

See [docs/install-dependencies.md](docs/install-dependencies.md) for step-by-step instructions per operating system.

## Installation

```bash
git clone git@github.com:mariovalney/yt-music-player-sync.git
cd yt-music-player-sync
pipenv install
```

## Usage

```bash
pipenv run start
```

On every run the script will:

1. Check that all required dependencies are available
2. Detect connected FAT32 SD cards and ask which one to use
3. Show a menu: **Sync**, **Add or remove**, or **Clear SD card**
4. In **Add or remove**, manage playlists, albums and followed artists separately — navigate by letter group, select items, and confirm when ready. Liked Music is always pinned at the top of the playlists list. Items already on the card are pre-selected
5. Estimate the download size and ask for confirmation before starting
6. Download new tracks to the card, skipping anything already downloaded
7. Update the M3U files for each synced item

## Authentication

The script reads cookies directly from Chrome for `music.youtube.com` on every run. Nothing is stored on disk. Make sure Chrome is open and logged in to YouTube Music before running the script.

## SD card structure

```
Music/
  .metadata/
    synced.json         items selected for sync
    index.json          internal index mapping tracks to file paths
  Playlists/
    Liked Music/
      Liked Music.m3u
      Artist Name/
        Album Name/
          Track Title.mp3
    Some Playlist/
      Some Playlist.m3u
      ...
  Albums/
    Album Name/
      Album Name.m3u
      Track Title.mp3
  Artists/
    Artist Name/
      Artist Name.m3u
      Album Name/
        Track Title.mp3
```

Each item has its own M3U file at the root of its folder. The same track can appear in multiple items — it will be downloaded once per item folder.

## Disclaimer

This project is provided for personal and educational use only.

Downloading content from YouTube Music may violate the [YouTube Terms of Service](https://www.youtube.com/t/terms). You are solely responsible for ensuring your use of this tool complies with the terms of service of any platform you access, as well as the copyright laws and regulations applicable in your country.

The downloaded content belongs to its respective copyright holders. This tool does not circumvent any access controls or DRM and is intended only to facilitate personal offline listening for users who already have legitimate access to the content.

This project is not affiliated with, endorsed by, or in any way connected to Google LLC or YouTube.

The authors and contributors provide this software as-is, without warranty of any kind. Use it at your own risk.
