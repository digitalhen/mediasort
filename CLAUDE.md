# filebot

A custom CLI tool to organize media files into Plex-friendly directory structures, inspired by the original FileBot.

## What it does

Scans a source directory (e.g. `/Volumes/Torrents`) for video files, identifies them as TV shows or movies, looks up metadata via TMDB, and moves/copies them into Plex-friendly paths:

- **Movies:** `/Volumes/Movies/Movie Name (Year)/Movie Name (Year).ext`
- **TV:** `/Volumes/TV/Show Name/Season XX/Show Name - SXXEXX - Episode Title.ext`

## Architecture

Single-file Python CLI (`filebot.py`) with zero dependencies (stdlib only). Key components:

- **Filename parser** — regex-based extraction of title, year, season, episode from torrent-style filenames. Strips release group tags, codec info, quality tags, torrent site prefixes, etc.
- **Parent directory inference** — climbs up to 3 directory levels to find titles when filenames are bare (e.g. `S01E01.m4v` inside `Show Name/Season 1/`)
- **TMDB client** — looks up canonical titles and episode names. Supports both API key and bearer token auth. Has caching and rate limiting built in.
- **Plex formatter** — generates proper directory structures with sanitized filenames

## Usage

```bash
# Dry run (default - shows what would happen)
python3 filebot.py /Volumes/Torrents

# With TMDB for proper titles and episode names
TMDB_API_KEY=42e41ebc90757c9dd6d5992a14e5d474 python3 filebot.py /Volumes/Torrents

# Actually move files
python3 filebot.py /Volumes/Torrents -x

# Copy instead of move
python3 filebot.py /Volumes/Torrents -x -c

# Filter to specific files
python3 filebot.py /Volumes/Torrents -f "Night Manager"

# Custom destinations
python3 filebot.py /Volumes/Torrents --movies /path/to/movies --tv /path/to/tv
```

## Environment variables

- `TMDB_API_KEY` — TMDB v3 API key
- `TMDB_READ_TOKEN` — TMDB v4 read access bearer token (alternative to API key)

## Default paths

- Source: provided as first argument
- Movies destination: `/Volumes/Movie`
- TV destination: `/Volumes/TV`

## Known limitations / TODO

- Non-video files (music, books, games, ISOs) are silently skipped — no classification yet
- Title casing is approximate; TMDB corrects it when enabled
- Some edge cases with subtitle files, multi-episode files (S01E01E02), and special episodes
- No interactive mode for confirming ambiguous matches
- No undo/rollback mechanism
