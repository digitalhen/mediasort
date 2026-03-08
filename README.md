# mediasort

A zero-dependency Python CLI tool to organize media files into Plex-friendly directory structures.

## Features

- **Smart filename parsing** — regex-based extraction of title, year, season, episode from torrent-style filenames
- **TMDB integration** — looks up canonical titles, years, and episode names via The Movie Database API
- **Plex-compatible naming** — generates proper directory structures with `{tmdb-ID}` and `{edition-...}` tags
- **Quality metadata** — resolution, source, video/audio codec, and bitrate tier in filenames
- **Subtitle organization** — discovers and organizes existing subtitle files alongside videos
- **Download detection** — skips files still being downloaded (`.part`, `.!qb`, `.aria2`, etc.)
- **Watch mode** — continuous monitoring with configurable scan interval
- **Docker support** — run as a container with volume mounts
- **Parallel execution** — threaded file operations with progress bar
- **Persistent TMDB cache** — 7-day disk cache to avoid redundant API calls
- **Configurable format templates** — customize output path patterns
- **Rename in-place** — reorganize files within the source directory

## Quick Start

```bash
# Dry run (default - shows what would happen)
python3 mediasort.py /Volumes/Torrents

# With TMDB for proper titles and episode names
TMDB_API_KEY=your_key python3 mediasort.py /Volumes/Torrents

# Actually move files
python3 mediasort.py /Volumes/Torrents -x

# Copy instead of move
python3 mediasort.py /Volumes/Torrents -x -c

# Rename in-place (reorganize within source)
python3 mediasort.py /Volumes/Torrents -x --rename

# Filter to specific files
python3 mediasort.py /Volumes/Torrents -f "Night Manager"

# Custom destinations
python3 mediasort.py /Volumes/Torrents --movies /path/to/movies --tv /path/to/tv
```

## Watch Mode

```bash
# Re-scan every 5 minutes
python3 mediasort.py /Volumes/Torrents -x --watch 300

# Responds to SIGINT/SIGTERM for graceful shutdown
```

## Docker

```bash
# Build
docker build -t mediasort .

# Run
docker run --rm \
  -v /path/to/downloads:/source \
  -v /path/to/movies:/movies \
  -v /path/to/tv:/tv \
  -e TMDB_API_KEY=your_key \
  mediasort /source -x --movies /movies --tv /tv

# Docker Compose
```

```yaml
# docker-compose.yml
services:
  mediasort:
    build: .
    volumes:
      - /Volumes/Torrents:/source
      - /Volumes/Movies:/movies
      - /Volumes/TV:/tv
    environment:
      - TMDB_API_KEY=your_key
    restart: unless-stopped
```

## Cron

```bash
# Run every 30 minutes
*/30 * * * * TMDB_API_KEY=your_key python3 /path/to/mediasort.py /Volumes/Torrents -x --movies /Volumes/Movies --tv /Volumes/TV >> /var/log/mediasort.log 2>&1
```

## Output Format

### Default Movie Format
```
/Movies/Movie Name (2024) {tmdb-12345} {edition-Extended}/Movie Name (2024) {edition-Extended} - 1080p BluRay x265 DTS [High].mkv
```

### Default TV Format
```
/TV/Show Name/Season 01/Show Name - S01E01 - Episode Title - 720p WEB-DL x264 AAC [Medium].mkv
```

## Custom Format Templates

Use `--movie-format` and `--tv-format` to customize output paths:

```bash
# Simple movie format without TMDB ID
python3 mediasort.py /source -x --movie-format "{title} ({year})/{title} ({year}) - {quality}{ext}"

# TV with resolution in folder
python3 mediasort.py /source -x --tv-format "{show}/{season_folder} [{resolution}]/{show} - S{season:02d}E{episode:02d}{ext}"
```

### Available Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `{title}` | Movie title | `Inception` |
| `{show}` | TV show name | `Breaking Bad` |
| `{year}` | Release year | `2010` |
| `{tmdb_id}` | TMDB ID | `27205` |
| `{edition}` | Edition tag | `Extended` |
| `{season}` | Season number (use `:02d` for zero-pad) | `1` |
| `{episode}` | Episode number (use `:02d` for zero-pad) | `5` |
| `{episode_title}` | Episode name | `Pilot` |
| `{season_folder}` | Pre-formatted season folder | `Season 01` |
| `{quality}` | Full quality string | `1080p BluRay x265 DTS [High]` |
| `{resolution}` | Resolution | `1080p` |
| `{source}` | Source/medium | `BluRay` |
| `{vcodec}` | Video codec | `x265` |
| `{acodec}` | Audio codec | `DTS` |
| `{bitrate_label}` | Bitrate tier | `High` |
| `{ext}` | File extension | `.mkv` |

## CLI Options

| Option | Description |
|--------|-------------|
| `source` | Source directory (required) |
| `-m, --movies` | Movies destination (default: `/Volumes/Movies`) |
| `-t, --tv` | TV destination (default: `/Volumes/TV`) |
| `-k, --tmdb-key` | TMDB API key |
| `--tmdb-token` | TMDB v4 read access token |
| `--no-tmdb` | Skip TMDB lookups |
| `--no-probe` | Skip ffprobe bitrate detection |
| `-x, --execute` | Actually move files (default is dry run) |
| `-c, --copy` | Copy instead of move |
| `-r, --rename` | Rename in-place (organize within source) |
| `-v, --verbose` | Show detailed parsing info |
| `-f, --filter` | Only process entries matching substring |
| `-j, --parallel` | Max parallel workers (default: 5) |
| `--movie-format` | Custom movie path template |
| `--tv-format` | Custom TV path template |
| `-w, --watch` | Watch mode: re-scan every N seconds |
| `--log-level` | Logging level: DEBUG/INFO/WARNING/ERROR |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `TMDB_API_KEY` | TMDB v3 API key |
| `TMDB_READ_TOKEN` | TMDB v4 read access bearer token |

## Bitrate Tiers

| Tier | Range | Typical Use |
|------|-------|-------------|
| Low | < 3 Mbps | Low-quality web rips |
| Medium | 3-8 Mbps | Standard web downloads |
| High | 8-20 Mbps | Good BluRay encodes |
| Ultra | 20+ Mbps | Remux / high-bitrate |

## Requirements

- Python 3.8+
- No pip dependencies (stdlib only)
- Optional: `ffprobe` (for bitrate detection)
- Optional: TMDB API key (for metadata lookup)

## License

MIT
