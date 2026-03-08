# filebot

A custom CLI tool to organize media files into Plex-friendly directory structures, inspired by the original FileBot.

## What it does

Scans a source directory (e.g. `/Volumes/Torrents`) for video files, identifies them as TV shows or movies, looks up metadata via TMDB, and moves/copies them into Plex-friendly paths:

- **Movies:** `/Volumes/Movies/Movie Name (Year) {tmdb-ID} {edition-...}/Movie Name (Year) {edition-...} - quality.ext`
- **TV:** `/Volumes/TV/Show Name/Season XX/Show Name - SXXEXX - Episode Title - quality.ext`

## Architecture

Single-file Python CLI (`filebot.py`) with zero dependencies (stdlib only + optional ffprobe). Key components:

- **Filename parser** — regex-based extraction of title, year, season, episode, edition, resolution, source, codecs
- **Parent directory inference** — climbs up to 3 directory levels to find titles when filenames are bare
- **TMDB client** — looks up canonical titles and episode names. Persistent 7-day disk cache in `~/.filebot/cache/`
- **Plex formatter** — generates proper directory structures with `{tmdb-ID}` and `{edition-...}` tags
- **Format templates** — configurable output path patterns via `--movie-format` / `--tv-format`
- **Web UI** — built-in HTTP server for browser-based configuration with folder browsing
- **Watch mode** — continuous scanning at configurable intervals with graceful SIGTERM/SIGINT shutdown
- **Docker support** — Dockerfile included, web UI binds to `0.0.0.0:8080`

## Usage

```bash
# Dry run (default)
python3 filebot.py /Volumes/Torrents

# With TMDB
TMDB_API_KEY=your_key python3 filebot.py /Volumes/Torrents

# Actually move files
python3 filebot.py /Volumes/Torrents -x

# Web UI
python3 filebot.py --web

# Watch mode (re-scan every 5 min)
python3 filebot.py /Volumes/Torrents -x --watch 300
```

## Environment variables

- `TMDB_API_KEY` — TMDB v3 API key
- `TMDB_READ_TOKEN` — TMDB v4 read access bearer token (alternative to API key)

## Default paths

- Source: provided as first argument
- Movies destination: `/Volumes/Movies`
- TV destination: `/Volumes/TV`

## Config persistence

- Config stored in `~/.filebot/config.json` (used by web UI)
- TMDB cache stored in `~/.filebot/cache/`

## Development workflow

- **Auto-commit and push** changes to git as work progresses (standing instruction from user)
- Git remote: `https://github.com/digitalhen/filebot.git`, branch `main`
- Keep README.md and CLAUDE.md up to date when making changes

## Known limitations / TODO

- Non-video files (music, books, games, ISOs) are silently skipped
- Multi-episode files (S01E01E02) not fully handled
- No undo/rollback mechanism
- No leftover cleanup flag yet (trailers, NFOs, etc. left in source)
