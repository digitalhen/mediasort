#!/usr/bin/env python3
from __future__ import annotations
"""
mediasort - A CLI tool to organize media files into Plex-friendly directory structures.

Uses TMDB API for metadata lookup. Moves TV shows and movies into separate
directories with proper naming conventions.

Plex formats:
  Movies: /dest/Movie Name (Year) {tmdb-ID}/Movie Name (Year).ext
  TV:     /dest/Show Name/Season XX/Show Name - SXXEXX - Episode Title.ext
"""

import argparse
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TMDB_BASE = "https://api.themoviedb.org/3"

# Default format templates (Plex conventions)
DEFAULT_MOVIE_FORMAT = "{title} ({year}) {{tmdb-{tmdb_id}}} {{edition-{edition}}}/{title} ({year}) {{edition-{edition}}} - {quality}{ext}"
DEFAULT_MOVIE_FORMAT_NO_EDITION = "{title} ({year}) {{tmdb-{tmdb_id}}}/{title} ({year}) - {quality}{ext}"
DEFAULT_MOVIE_FORMAT_NO_TMDB = "{title} ({year})/{title} ({year}) - {quality}{ext}"
DEFAULT_TV_FORMAT = "{show}/{season_folder}/{show} - S{season:02d}E{episode:02d} - {episode_title} - {quality}{ext}"
DEFAULT_TV_FORMAT_NO_EPISODE_TITLE = "{show}/{season_folder}/{show} - S{season:02d}E{episode:02d} - {quality}{ext}"

# Persistent TMDB disk cache directory
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".mediasort", "cache")

log = logging.getLogger("mediasort")

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts", ".vob",
}
SUBTITLE_EXTENSIONS = {".srt", ".sub", ".ass", ".ssa", ".idx"}

# Filenames to skip (junk/promo files inside torrent folders)
JUNK_BASENAMES = {
    "sample", "rarbg.com", "rarbg", "www.yts.am", "www.yts.mx",
    "etrg", "ettv", "tgx",
}

# DVD structure filenames to skip
DVD_JUNK_RE = re.compile(r"^(VIDEO_TS|VTS_\d+_\d+)$", re.IGNORECASE)

# Patterns for extras/bonus content filenames (match at start)
EXTRAS_START_RE = re.compile(
    r"^(\d{2,3}\s+)?(B-Roll|Behind the Scenes|Deleted Scenes?|Featurettes?|Interviews?|"
    r"Trailer\s*\d*|Making [Oo]f|Gag Reel|Commentary|Bloopers?|"
    r"Menu Art|Storyboards?|Re-Release|Supporting Cast|Short Film|"
    r"Sneak Peek)",
    re.IGNORECASE,
)
# Extras keywords that can appear anywhere in the filename
EXTRAS_KEYWORD_RE = re.compile(
    r"[\s-]+(Red Band Trailer|Clip\s*[-—]\s*|Teaser\s*\d*)\b",
    re.IGNORECASE,
)

# Known game release groups (to skip game ISOs/content)
GAME_GROUPS = {"reloaded", "skidrow", "codex", "plaza", "fitgirl", "dodi"}

# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

# Patterns that indicate a TV show
TV_PATTERNS = [
    re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})"),                    # S01E01
    re.compile(r"[Ss](\d{1,2})[\.\s]?[Ee](\d{1,3})"),             # S01.E01
    re.compile(r"(\d{1,2})x(\d{1,3})", re.IGNORECASE),            # 1x01
    re.compile(r"[Ss]eason\s*(\d{1,2})\s*[Ee]pisode\s*(\d{1,3})", re.IGNORECASE),  # Season 4 Episode 01
    re.compile(r"(?<![HhXx])[\.\s](?!(?:19|20)\d{2}[\.\s])(\d{1,2})(\d{2})[\.\s](?!\d)"),  # .415. abbreviated (S04E15), skip years and codecs (H.264, x265)
    re.compile(r"[Ss]eason[\s._-]*(\d{1,2})", re.IGNORECASE),     # Season 1
    re.compile(r"[\.\s][Ss](\d{1,2})[\.\s]"),                     # S01 (season pack, with delimiters)
]

# Edition tags to extract before stripping noise
EDITION_RE = re.compile(
    r"\b(REMASTERED|EXTENDED|UNRATED|IMAX|CRITERION|DIRECTORS?[\.\s-]?CUT|"
    r"THEATRICAL|SPECIAL[\.\s-]?EDITION|UNCENSORED|COLLECTORS?[\.\s-]?EDITION|"
    r"ULTIMATE[\.\s-]?EDITION|ANNIVERSARY[\.\s-]?EDITION)\b",
    re.IGNORECASE,
)

# Noise words and tags to strip from filenames
NOISE_PATTERNS = [
    # Release groups and tags in brackets (but preserve years like [1984])
    re.compile(r"\[(?!\d{4}\])[^\]]*\]"),
    # Curly-brace tags (but preserve years like {1997})
    re.compile(r"\{(?!\d{4}\})[^}]*\}"),
    # Common prefixes from torrent sites
    re.compile(r"^(?:www\.\S+\s*-\s*)"),
    re.compile(r"^(?:download\s+from\s+\S+\s*)", re.IGNORECASE),
    re.compile(r"^(?:MoviesCounter|MoviesMeta|Moviescounter|YifyMovies|Torrent9)\.", re.IGNORECASE),
    # Scene group name prefixes like "s4a-" at start
    re.compile(r"^[a-z0-9]{2,6}[-.](?=[A-Z])"),
    # Non-Latin prefixes before English text
    re.compile(r"^[^\x00-\x7F]+\.?"),
    # Quality/source/codec tags
    re.compile(
        r"\b("
        r"1080p|720p|2160p|480p|4[Kk]|UHD|"
        r"BluRay|Blu-ray|BRRip|BDRip|HDRip|WEBRip|WEB-DL|WEB|HDTV|DVDRip|DVDScr|DVDSCR|HDChina|"
        r"UHDrip|UHD\.?BluRay|HDRip|HC[\.\s]?HDRip|"
        r"x264|x265|H\.?264|H\.?265|HEVC|AVC|AV1|XviD|XVID|DivX|DivX5|DXVA|"
        r"AAC\d?\.?\d?|AC3|DTS|DTS-HD|DDP?5\.1|DD\+?5\.1|DD\+?\d\.1|TrueHD|Atmos|DTS-ES|DTS-X|DTSHD|DTS-HD\.?MA|"
        r"DolbyD|Dolby|"
        r"[257]\.1|7\.1|"
        r"10bit|10Bit|8bit|HDR|HDR10|HDR10\+|DoVi|SDR|"
        r"REMUX|REMASTERED|EXTENDED|UNRATED|IMAX|PROPER|LIMITED|READNFO|REAL|"
        r"RARBG|SPARKS|PublicHD|YTS\.\w+|EtHD|rarbg|rartv|TGx|ettv|"
        r"GalaxyRG\d*|GalaxyTV|NeoNoir|Tigole|anoXmous|aXXo|"
        r"AMZN|DSNP|NF|HULU|CRITERION|"
        r"Half[\.\s-]?SBS|H-SBS|3D|SBS|Half[\.\s-]?OU|"
        r"MP3|FLAC|Multi|Subs|DUAL|ESub|"
        r"CHS-ENG|CHS|Eng[\.\s]?Subs?|"
        r"COMPLETE|SEASON|Complete|Season|"
        r"REPACK|INTERNAL|RM4K|"
        r"UNCENSORED|HC|"
        r"airvideo"
        r")\b",
        re.IGNORECASE,
    ),
    # Codec/format suffixes
    re.compile(r"\bMp4Ba\b", re.IGNORECASE),
    # File size indicators
    re.compile(r"\b\d{3,4}MB\b"),
    # Scene group suffixes like -SPARKS, -RARBG
    re.compile(r"-[A-Za-z0-9]{2,15}(?:[\s.\-]|$)"),
    # Trailing " - GROUP" pattern
    re.compile(r"\s+-\s+[A-Za-z0-9]{2,15}$"),
]

YEAR_RE = re.compile(r"(?:^|[\s.(\[{])((?:19|20)\d{2})(?:[\s.)\]\}\-]|$)")

# Resolution extraction
RESOLUTION_RE = re.compile(r"\b(480p|720p|1080p|2160p|4[Kk])\b", re.IGNORECASE)

# Source/medium extraction (ordered by preference)
SOURCE_RE = re.compile(
    r"\b(UHD[\.\s]?BluRay|BluRay|Blu-ray|REMUX|BDRip|BRRip|"
    r"WEB-DL|WEBRip|WEB|HDTV|DVDRip|DVDScr|HDRip)\b",
    re.IGNORECASE,
)

# Normalize source names for display
SOURCE_DISPLAY = {
    "uhd bluray": "UHD BluRay", "uhd.bluray": "UHD BluRay",
    "bluray": "BluRay", "blu-ray": "BluRay",
    "remux": "Remux", "bdrip": "BDRip", "brrip": "BRRip",
    "web-dl": "WEB-DL", "webrip": "WEBRip", "web": "WEB",
    "hdtv": "HDTV", "dvdrip": "DVDRip", "dvdscr": "DVDScr",
    "hdrip": "HDRip",
}

# Video codec extraction
VCODEC_RE = re.compile(
    r"\b(x264|x265|H\.?264|H\.?265|HEVC|AVC|AV1|XviD|XVID|DivX)\b",
    re.IGNORECASE,
)

VCODEC_DISPLAY = {
    "x264": "x264", "h264": "H.264", "h.264": "H.264",
    "x265": "x265", "h265": "H.265", "h.265": "H.265",
    "hevc": "HEVC", "avc": "AVC", "av1": "AV1",
    "xvid": "XviD", "divx": "DivX",
}

# Audio codec extraction
ACODEC_RE = re.compile(
    r"\b(TrueHD[\.\s]?Atmos|TrueHD|Atmos|DTS-HD[\.\s]?MA|DTS-HD|DTS-X|DTS-ES|DTS|"
    r"DDP?[\.\s]?[257]\.1|DD\+?[\.\s]?[257]\.1|AC3|AAC|FLAC|MP3)\b",
    re.IGNORECASE,
)

# Words that should stay lowercase in titles (unless first word)
TITLE_SMALL_WORDS = {"a", "an", "the", "and", "but", "or", "nor", "in", "on", "at", "to", "for", "of", "with", "vs", "is"}


def title_case(s: str) -> str:
    """Smart title casing that handles small words and preserves acronyms."""
    words = s.split()
    result = []
    for i, w in enumerate(words):
        # Preserve all-caps words (acronyms like II, III, US, UK)
        if w.isupper() and len(w) >= 2:
            result.append(w)
        elif i == 0 or w.lower() not in TITLE_SMALL_WORDS:
            result.append(w.capitalize())
        else:
            result.append(w.lower())
    return " ".join(result)


def extract_edition(text: str) -> str | None:
    """Extract edition tag from filename text, return normalized edition string."""
    m = EDITION_RE.search(text)
    if m:
        edition = m.group(1).upper()
        # Normalize common editions
        edition_map = {
            "REMASTERED": "Remastered",
            "EXTENDED": "Extended",
            "UNRATED": "Unrated",
            "IMAX": "IMAX",
            "UNCENSORED": "Uncensored",
            "THEATRICAL": "Theatrical",
        }
        for key, val in edition_map.items():
            if key in edition:
                return val
        # For compound editions like "DIRECTORS CUT"
        return edition.replace(".", " ").replace("-", " ").strip().title()
    return None


def clean_title(raw: str) -> str:
    """Strip noise from a raw filename to extract a clean title."""
    name = raw

    for pat in NOISE_PATTERNS:
        name = pat.sub(" ", name)

    # Replace dots and underscores with spaces
    name = re.sub(r"[._]", " ", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    # Remove trailing hyphens/dashes
    name = re.sub(r"[\s-]+$", "", name)

    return name


def parse_filename(filename: str) -> dict:
    """
    Parse a media filename and return structured info.

    Returns dict with keys: title, year, season, episode, edition, type ('tv'|'movie'|'unknown')
    """
    # Strip extension
    base, ext = os.path.splitext(filename)
    if ext.lower() not in VIDEO_EXTENSIONS:
        # Check if it's a directory name (no ext)
        if ext:
            base = filename
            ext = ""

    result = {"title": None, "year": None, "season": None, "episode": None,
              "edition": None, "resolution": None, "source": None,
              "video_codec": None, "audio_codec": None,
              "type": "unknown", "ext": ext}

    # Extract metadata tags before cleaning
    res_m = RESOLUTION_RE.search(base)
    if res_m:
        r = res_m.group(1).lower()
        result["resolution"] = "2160p" if r in ("4k",) else r

    src_m = SOURCE_RE.search(base)
    if src_m:
        result["source"] = SOURCE_DISPLAY.get(src_m.group(1).lower(), src_m.group(1))

    vc_m = VCODEC_RE.search(base)
    if vc_m:
        result["video_codec"] = VCODEC_DISPLAY.get(vc_m.group(1).lower(), vc_m.group(1))

    ac_m = ACODEC_RE.search(base)
    if ac_m:
        raw_ac = ac_m.group(1)
        # Normalize audio codec display
        ac_norm = raw_ac.upper().replace(".", " ").strip()
        ac_map = {
            "TRUEHD ATMOS": "TrueHD Atmos", "TRUEHD": "TrueHD",
            "DTS-HD MA": "DTS-HD MA", "DTS-HD": "DTS-HD",
            "DTS-X": "DTS-X", "DTS-ES": "DTS-ES", "DTS": "DTS",
            "ATMOS": "Atmos", "AC3": "AC3", "AAC": "AAC",
            "FLAC": "FLAC", "MP3": "MP3",
        }
        for key, val in ac_map.items():
            if key in ac_norm:
                result["audio_codec"] = val
                break
        else:
            # DD/DDP with channel info
            dd_m = re.match(r"DD[P+]?\s*(\d\s*\.\s*\d)", ac_norm)
            if dd_m:
                ch = dd_m.group(1).replace(" ", "")
                if "P" in ac_norm or "+" in raw_ac:
                    result["audio_codec"] = f"DDP{ch}"
                else:
                    result["audio_codec"] = f"DD{ch}"
            else:
                result["audio_codec"] = raw_ac

    result["edition"] = extract_edition(base)

    # Check for TV patterns first
    for pat in TV_PATTERNS:
        m = pat.search(base)
        if m:
            result["type"] = "tv"
            groups = m.groups()
            if len(groups) >= 2:
                result["season"] = int(groups[0])
                result["episode"] = int(groups[1])
            elif len(groups) == 1:
                result["season"] = int(groups[0])

            # Title is everything before the match
            title_part = base[: m.start()]
            title_part = clean_title(title_part)

            # Extract year from title part
            year_m = YEAR_RE.search(title_part)
            if year_m:
                result["year"] = int(year_m.group(1))
                title_part = title_part[: year_m.start()] + title_part[year_m.end() :]

            result["title"] = title_case(title_part.strip(" .-_"))
            return result

    # Not TV — treat as movie
    result["type"] = "movie"

    title_part = clean_title(base)

    # Extract year
    year_m = YEAR_RE.search(title_part)
    if year_m:
        result["year"] = int(year_m.group(1))
        # Title is everything before the year
        title_part = title_part[: year_m.start()].strip()

    stripped = title_part.strip(" .-_")
    result["title"] = title_case(stripped) if stripped else None
    return result


# ---------------------------------------------------------------------------
# TMDB API
# ---------------------------------------------------------------------------

def _title_alternatives(title: str) -> list[str]:
    """Generate alternative search queries from a title for TMDB fallback."""
    alts = []
    # Strip " - subtitle" from title
    if " - " in title:
        alts.append(title.split(" - ")[0].strip())
    # Strip "Part X" suffix
    part_stripped = re.sub(r"\s+Part\s+\d+$", "", title, flags=re.IGNORECASE)
    if part_stripped != title:
        alts.append(part_stripped)
    # For very long titles, try first N words
    words = title.split()
    if len(words) > 6:
        alts.append(" ".join(words[:5]))
        alts.append(" ".join(words[:4]))
    return alts


class TMDBClient:
    def __init__(self, api_key: str, bearer_token: str | None = None, cache_dir: str | None = None):
        self.api_key = api_key
        self.bearer_token = bearer_token
        self._mem_cache: dict[str, any] = {}
        self._last_request = 0.0
        self._cache_dir = cache_dir or CACHE_DIR
        self._lock = threading.Lock()
        os.makedirs(self._cache_dir, exist_ok=True)

    def _disk_cache_path(self, url: str) -> str:
        h = hashlib.sha256(url.encode()).hexdigest()[:16]
        return os.path.join(self._cache_dir, f"{h}.json")

    def _disk_cache_get(self, url: str) -> dict | None:
        path = self._disk_cache_path(url)
        try:
            if os.path.exists(path):
                age = time.time() - os.path.getmtime(path)
                if age < 86400 * 7:  # 7-day TTL
                    with open(path) as f:
                        return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
        return None

    def _disk_cache_set(self, url: str, data: dict):
        try:
            path = self._disk_cache_path(url)
            with open(path, "w") as f:
                json.dump(data, f)
        except OSError:
            pass

    def _get(self, path: str, params: dict | None = None) -> dict:
        if params is None:
            params = {}

        headers = {"Accept": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        else:
            params["api_key"] = self.api_key

        url = f"{TMDB_BASE}{path}?{urllib.parse.urlencode(params)}"

        if url in self._mem_cache:
            return self._mem_cache[url]

        disk = self._disk_cache_get(url)
        if disk is not None:
            self._mem_cache[url] = disk
            return disk

        # Rate limiting: max ~4 requests/sec
        with self._lock:
            elapsed = time.time() - self._last_request
            if elapsed < 0.25:
                time.sleep(0.25 - elapsed)
            self._last_request = time.time()

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                self._mem_cache[url] = data
                self._disk_cache_set(url, data)
                return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2)
                return self._get(path, params)
            raise

    def search_movie(self, title: str, year: int | None = None) -> dict | None:
        params = {"query": title}
        if year:
            params["year"] = year
        data = self._get("/search/movie", params)
        results = data.get("results", [])
        if not results and year:
            # Retry without year
            data = self._get("/search/movie", {"query": title})
            results = data.get("results", [])
        if not results:
            # Retry with cleaned title (strip apostrophes, normalize punctuation)
            cleaned = title.replace("'", "").replace("\u2019", "")
            if cleaned != title:
                data = self._get("/search/movie", {"query": cleaned})
                results = data.get("results", [])
        if not results:
            # Try progressively shorter versions of the title
            for alt in _title_alternatives(title):
                data = self._get("/search/movie", {"query": alt})
                results = data.get("results", [])
                if results:
                    break
        return results[0] if results else None

    def search_tv(self, title: str, year: int | None = None) -> dict | None:
        params = {"query": title}
        if year:
            params["first_air_date_year"] = year
        data = self._get("/search/tv", params)
        results = data.get("results", [])
        if not results and year:
            data = self._get("/search/tv", {"query": title})
            results = data.get("results", [])
        return results[0] if results else None

    def get_tv_episode(self, tv_id: int, season: int, episode: int) -> dict | None:
        try:
            return self._get(f"/tv/{tv_id}/season/{season}/episode/{episode}")
        except Exception:
            return None

    def get_tv_season(self, tv_id: int, season: int) -> dict | None:
        try:
            return self._get(f"/tv/{tv_id}/season/{season}")
        except Exception:
            return None


# ---------------------------------------------------------------------------
# File discovery & probing
# ---------------------------------------------------------------------------

def probe_bitrate(filepath: str) -> int | None:
    """Use ffprobe to get the overall bitrate of a video file. Returns bits/sec or None."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", filepath],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            info = json.loads(result.stdout)
            br = info.get("format", {}).get("bit_rate")
            if br:
                return int(br)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError, ValueError):
        pass
    return None


def bitrate_label(bitrate: int | None) -> str | None:
    """Categorize bitrate into quality tiers."""
    if bitrate is None:
        return None
    mbps = bitrate / 1_000_000
    if mbps < 3:
        return "Low"
    elif mbps < 8:
        return "Medium"
    elif mbps < 20:
        return "High"
    else:
        return "Ultra"


def _file_fingerprint(path: str) -> tuple[int, bytes]:
    """Return (size, partial_hash) for a file.

    Hashes the first and last 1MB for a fast integrity check.
    """
    CHUNK = 1024 * 1024  # 1MB
    size = os.path.getsize(path)
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read(CHUNK))
        if size > CHUNK * 2:
            f.seek(-CHUNK, 2)
            h.update(f.read(CHUNK))
    return size, h.digest()


def _safe_transfer(src: str, dest: str, move: bool = True):
    """Transfer a file using a .copying suffix to mark in-progress transfers.

    Copies to dest.copying first, verifies size + partial hash match,
    then renames to final name. If move=True, deletes the source after
    successful verification. This ensures incomplete or corrupt transfers
    are caught before removing the original.
    """
    tmp_dest = dest + ".copying"
    try:
        # Same-device move can use os.rename directly (atomic)
        if move:
            try:
                os.rename(src, dest)
                return
            except OSError:
                pass  # Cross-device; fall through to copy+delete
        src_fp = _file_fingerprint(src)
        shutil.copy2(src, tmp_dest)
        dest_fp = _file_fingerprint(tmp_dest)
        if src_fp != dest_fp:
            raise IOError(
                f"Integrity check failed for {os.path.basename(src)}: "
                f"src={src_fp[0]} bytes, dest={dest_fp[0]} bytes"
            )
        os.rename(tmp_dest, dest)
        if move:
            os.remove(src)
    except BaseException:
        # Clean up partial copy on any failure
        try:
            os.remove(tmp_dest)
        except OSError:
            pass
        raise


def find_subtitle_files(video_path: str) -> list[dict]:
    """Find subtitle files associated with a video file.

    Looks in the same directory and common sub-directories (Subs, Subtitles).
    Returns list of dicts with 'path' and 'lang' keys.
    """
    video_dir = os.path.dirname(video_path)
    video_base = os.path.splitext(os.path.basename(video_path))[0].lower()
    subs = []

    # Directories to search
    search_dirs = [video_dir]
    for sub_dir in ("Subs", "Subtitles", "Sub", "subs", "subtitles"):
        d = os.path.join(video_dir, sub_dir)
        if os.path.isdir(d):
            search_dirs.append(d)

    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            ext = os.path.splitext(f)[1].lower()
            if ext not in SUBTITLE_EXTENSIONS:
                continue
            fname_lower = f.lower()
            # Try to detect language from filename
            lang = _detect_sub_language(f)
            # Match: same base name, or generic language name (e.g., "English.srt")
            if (video_base in os.path.splitext(fname_lower)[0]
                    or fname_lower.startswith(("english", "eng.", "eng_"))
                    or lang):
                subs.append({
                    "path": os.path.join(d, f),
                    "lang": lang or "en",
                    "ext": ext,
                })

    return subs


# Common language patterns in subtitle filenames
_LANG_PATTERNS = {
    "english": "en", "eng": "en", "en": "en",
    "english_hi": "en_hi", "eng_hi": "en_hi", "sdh": "en_hi",
    "spanish": "es", "spa": "es", "es": "es",
    "french": "fr", "fre": "fr", "fra": "fr", "fr": "fr",
    "german": "de", "ger": "de", "deu": "de", "de": "de",
    "dutch": "nl", "dut": "nl", "nld": "nl", "nl": "nl",
    "italian": "it", "ita": "it", "it": "it",
    "portuguese": "pt", "por": "pt", "pt": "pt",
    "swedish": "sv", "swe": "sv", "sv": "sv",
    "chinese": "zh", "chi": "zh", "zh": "zh",
    "japanese": "ja", "jpn": "ja", "ja": "ja",
    "korean": "ko", "kor": "ko", "ko": "ko",
    "arabic": "ar", "ara": "ar", "ar": "ar",
}


def _detect_sub_language(filename: str) -> str | None:
    """Detect subtitle language from filename patterns."""
    base = os.path.splitext(filename)[0].lower()
    # Check for language suffix like "movie_eng.srt" or "English.srt"
    parts = re.split(r"[._\-\s]", base)
    for part in reversed(parts):
        if part in _LANG_PATTERNS:
            return _LANG_PATTERNS[part]
    return None


def format_subtitle_name(video_dest_path: str, lang: str, sub_ext: str) -> str:
    """Generate Plex-compatible subtitle filename next to the video."""
    video_base = os.path.splitext(video_dest_path)[0]
    return f"{video_base}.{lang}{sub_ext}"


def is_junk_file(filename: str) -> bool:
    """Check if a file is junk/promo/sample that should be skipped."""
    basename = os.path.splitext(filename)[0].lower().strip()
    if basename in JUNK_BASENAMES:
        return True
    if "(sample)" in filename.lower() or "[sample]" in filename.lower():
        return True
    if EXTRAS_START_RE.match(filename):
        return True
    if EXTRAS_KEYWORD_RE.search(os.path.splitext(filename)[0]):
        return True
    if DVD_JUNK_RE.match(os.path.splitext(filename)[0]):
        return True
    return False


def is_game_release(path: str) -> bool:
    """Check if a path looks like a game release (not a video)."""
    parts = path.lower().replace("\\", "/").split("/")
    for part in parts:
        for group in GAME_GROUPS:
            if group in part.lower().split("-"):
                return True
    return False


# Extensions that indicate a file is still being downloaded
DOWNLOAD_MARKER_EXTS = {".part", ".!qb", ".aria2", ".downloading", ".crdownload", ".tmp"}


def is_downloading(filepath: str) -> bool:
    """Check if a file appears to still be downloading."""
    # Check for download marker files alongside the video
    for ext in DOWNLOAD_MARKER_EXTS:
        if os.path.exists(filepath + ext):
            return True
    # Check if the file itself is a partial download
    if os.path.splitext(filepath)[1].lower() in DOWNLOAD_MARKER_EXTS:
        return True
    return False


def is_dir_downloading(dirpath: str) -> bool:
    """Check if any files in a directory are still downloading."""
    for _, _, files in os.walk(dirpath):
        for f in files:
            if os.path.splitext(f)[1].lower() in DOWNLOAD_MARKER_EXTS:
                return True
    return False


def find_video_files(path: str) -> list[str]:
    """Recursively find all video files under a path."""
    videos = []
    if os.path.isfile(path):
        if (os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS
                and not is_junk_file(os.path.basename(path))
                and not is_downloading(path)):
            videos.append(path)
        return videos

    # Skip game releases entirely
    if is_game_release(path):
        return videos

    # Skip directories with active downloads
    if os.path.isdir(path) and is_dir_downloading(path):
        return videos

    for root, dirs, files in os.walk(path):
        # Skip hidden dirs and sample/extras dirs
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in (
            "sample", "extras", "subs", "subtitles", "featurettes", "bonus",
            "behind the scenes", "deleted scenes", "interviews", "trailers",
        )]
        for f in sorted(files):
            ext = os.path.splitext(f)[1].lower()
            if ext in VIDEO_EXTENSIONS and not is_junk_file(f):
                fp = os.path.join(root, f)
                if not is_downloading(fp):
                    videos.append(fp)
    return videos


def infer_from_parent(filepath: str, parsed: dict) -> dict:
    """
    If parsing the filename didn't yield a good title, try parent directory names.
    Climbs up to 3 levels to find a useful title.
    Handles cases like: "Show Name/Season 1/S01E01.m4v"
    """
    if parsed["title"] and len(parsed["title"]) > 2:
        return parsed

    current = os.path.dirname(filepath)
    for _ in range(3):
        parent = os.path.basename(current)
        if not parent:
            break
        parent_parsed = parse_filename(parent)
        if parent_parsed["title"] and len(parent_parsed["title"]) > len(parsed["title"] or ""):
            parsed["title"] = parent_parsed["title"]
            if not parsed["year"] and parent_parsed["year"]:
                parsed["year"] = parent_parsed["year"]
            if parsed["type"] == "unknown":
                parsed["type"] = parent_parsed["type"]
            if not parsed["season"] and parent_parsed["season"]:
                parsed["season"] = parent_parsed["season"]
            if len(parsed["title"]) > 2:
                break
        current = os.path.dirname(current)
    return parsed


# ---------------------------------------------------------------------------
# Path formatting (Plex conventions)
# ---------------------------------------------------------------------------

def sanitize(name: str) -> str:
    """Remove characters that are problematic in filenames."""
    # Replace colons and slashes
    name = name.replace(":", " -").replace("/", "-").replace("\\", "-")
    # Remove other forbidden chars
    name = re.sub(r'[<>"|?*]', "", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _quality_suffix(parsed: dict) -> str:
    """Build a quality tag string like ' - 1080p BluRay x264 DTS' from parsed metadata."""
    parts = []
    if parsed.get("resolution"):
        parts.append(parsed["resolution"])
    if parsed.get("source"):
        parts.append(parsed["source"])
    if parsed.get("video_codec"):
        parts.append(parsed["video_codec"])
    if parsed.get("audio_codec"):
        parts.append(parsed["audio_codec"])
    if parsed.get("bitrate_label"):
        parts.append(f"[{parsed['bitrate_label']}]")
    return " ".join(parts)


def format_movie_path(dest: str, title: str, year: int | None, ext: str,
                      tmdb_id: int | None = None, edition: str | None = None,
                      parsed: dict | None = None, template: str | None = None) -> str:
    title_s = sanitize(title)
    quality = _quality_suffix(parsed) if parsed else ""

    if template:
        return _apply_template(template, dest, title=title_s, year=year, ext=ext,
                               tmdb_id=tmdb_id, edition=edition, quality=quality, parsed=parsed)

    if year:
        base = f"{title_s} ({year})"
    else:
        base = title_s

    folder = base
    if tmdb_id:
        folder = f"{base} {{tmdb-{tmdb_id}}}"
    if edition:
        folder = f"{folder} {{edition-{edition}}}"

    filename_base = base
    if edition:
        filename_base = f"{base} {{edition-{edition}}}"

    if quality:
        filename = f"{filename_base} - {quality}{ext}"
    else:
        filename = f"{filename_base}{ext}"
    return os.path.join(dest, folder, filename)


def format_tv_path(dest: str, show: str, season: int, episode: int,
                   episode_title: str | None, ext: str,
                   parsed: dict | None = None, template: str | None = None) -> str:
    show_s = sanitize(show)
    quality = _quality_suffix(parsed) if parsed else ""

    if template:
        return _apply_template(template, dest, show=show_s, season=season, episode=episode,
                               episode_title=sanitize(episode_title) if episode_title else None,
                               ext=ext, quality=quality, parsed=parsed)

    season_folder = f"Season {season:02d}"
    if episode_title:
        name = f"{show_s} - S{season:02d}E{episode:02d} - {sanitize(episode_title)}"
    else:
        name = f"{show_s} - S{season:02d}E{episode:02d}"

    if quality:
        filename = f"{name} - {quality}{ext}"
    else:
        filename = f"{name}{ext}"
    return os.path.join(dest, show_s, season_folder, filename)


def _apply_template(template: str, dest: str, **kwargs) -> str:
    """Apply a format template with variable substitution.

    Available variables: {title}, {year}, {tmdb_id}, {edition}, {show},
    {season}, {episode}, {episode_title}, {quality}, {resolution}, {source},
    {vcodec}, {acodec}, {bitrate_label}, {ext}, {season_folder}
    """
    parsed = kwargs.pop("parsed", None) or {}
    vals = {
        "title": kwargs.get("title", ""),
        "year": kwargs.get("year") or "",
        "tmdb_id": kwargs.get("tmdb_id") or "",
        "edition": kwargs.get("edition") or "",
        "show": kwargs.get("show", ""),
        "season": kwargs.get("season") or 0,
        "episode": kwargs.get("episode") or 0,
        "episode_title": kwargs.get("episode_title") or "",
        "quality": kwargs.get("quality", ""),
        "resolution": parsed.get("resolution", ""),
        "source": parsed.get("source", ""),
        "vcodec": parsed.get("video_codec", ""),
        "acodec": parsed.get("audio_codec", ""),
        "bitrate_label": parsed.get("bitrate_label", ""),
        "ext": kwargs.get("ext", ""),
        "season_folder": f"Season {(kwargs.get('season') or 0):02d}",
    }

    # Handle conditional blocks: {?edition:text {edition} text}
    def replace_conditional(m):
        var_name = m.group(1)
        content = m.group(2)
        if vals.get(var_name):
            # Substitute vars inside the conditional
            return content.format_map(_SafeDict(vals))
        return ""

    result = re.sub(r"\{\?(\w+):([^}]*(?:\{[^}]*\}[^}]*)*)\}", replace_conditional, template)

    # Substitute remaining variables
    result = result.format_map(_SafeDict(vals))

    # Clean up doubled spaces and trailing separators
    result = re.sub(r"  +", " ", result)
    result = re.sub(r" - (\.|$)", r"\1", result)
    result = re.sub(r"/\s+", "/", result)
    result = re.sub(r"\s+/", "/", result)

    return os.path.join(dest, result.strip())


class _SafeDict(dict):
    """Dict that returns empty string for missing keys in format_map."""
    def __missing__(self, key):
        # Support format specs like {season:02d}
        return ""


def format_tv_season_pack_path(dest: str, show: str) -> str:
    """Return the show folder for season packs (files will be handled individually)."""
    return os.path.join(dest, sanitize(show))


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def process_file(
    filepath: str,
    movie_dest: str,
    tv_dest: str,
    tmdb: "TMDBClient | None",
    dry_run: bool = True,
    move: bool = True,
    probe: bool = True,
    movie_template: str | None = None,
    tv_template: str | None = None,
) -> dict:
    """
    Process a single video file. Returns a dict describing the action taken.
    """
    filename = os.path.basename(filepath)
    parsed = parse_filename(filename)
    parsed = infer_from_parent(filepath, parsed)

    # Probe bitrate from actual file
    if probe:
        br = probe_bitrate(filepath)
        if br is not None:
            parsed["bitrate"] = br
            parsed["bitrate_label"] = bitrate_label(br)

    action = {
        "source": filepath,
        "dest": None,
        "parsed": parsed,
        "status": "skipped",
        "matched": None,
    }

    if not parsed["title"]:
        action["status"] = "skipped"
        action["error"] = "Could not parse title"
        return action

    if parsed["type"] == "tv":
        # Look up TV show
        show_title = parsed["title"]
        episode_title = None

        if tmdb:
            tv_result = tmdb.search_tv(show_title, parsed.get("year"))
            if tv_result:
                show_title = tv_result.get("name", show_title)
                action["matched"] = show_title

                if parsed["season"] and parsed["episode"]:
                    ep_data = tmdb.get_tv_episode(tv_result["id"], parsed["season"], parsed["episode"])
                    if ep_data:
                        episode_title = ep_data.get("name")

        if parsed["season"] is not None and parsed["episode"] is not None:
            dest_path = format_tv_path(tv_dest, show_title, parsed["season"], parsed["episode"], episode_title, parsed["ext"], parsed=parsed, template=tv_template)
        elif parsed["season"] is not None:
            # Season pack — just move into show/season folder
            dest_path = os.path.join(tv_dest, sanitize(show_title), f"Season {parsed['season']:02d}", filename)
        else:
            dest_path = os.path.join(tv_dest, sanitize(show_title), filename)

        action["dest"] = dest_path

    elif parsed["type"] == "movie":
        movie_title = parsed["title"]
        movie_year = parsed.get("year")
        edition = parsed.get("edition")
        tmdb_id = None

        if tmdb:
            movie_result = tmdb.search_movie(movie_title, movie_year)
            if movie_result:
                movie_title = movie_result.get("title", movie_title)
                tmdb_id = movie_result.get("id")
                rd = movie_result.get("release_date", "")
                if rd and len(rd) >= 4:
                    movie_year = int(rd[:4])
                action["matched"] = f"{movie_title} ({movie_year})" if movie_year else movie_title
            else:
                # TMDB lookup failed — try falling back to parent directory title
                parent_title = _try_parent_title(filepath)
                if parent_title and parent_title != movie_title:
                    movie_result = tmdb.search_movie(parent_title, movie_year)
                    if movie_result:
                        movie_title = movie_result.get("title", parent_title)
                        tmdb_id = movie_result.get("id")
                        rd = movie_result.get("release_date", "")
                        if rd and len(rd) >= 4:
                            movie_year = int(rd[:4])
                        action["matched"] = f"{movie_title} ({movie_year})" if movie_year else movie_title

        dest_path = format_movie_path(movie_dest, movie_title, movie_year, parsed["ext"],
                                      tmdb_id=tmdb_id, edition=edition, parsed=parsed,
                                      template=movie_template)
        action["dest"] = dest_path
    else:
        action["status"] = "skipped"
        action["error"] = "Could not determine media type"
        return action

    # Find associated subtitle files
    if action["dest"]:
        subs = find_subtitle_files(filepath)
        sub_actions = []
        for sub in subs:
            sub_dest = format_subtitle_name(action["dest"], sub["lang"], sub["ext"])
            sub_actions.append({"source": sub["path"], "dest": sub_dest, "lang": sub["lang"]})
        action["subtitles"] = sub_actions

    # Execute the move/copy
    if not dry_run and action["dest"]:
        dest_dir = os.path.dirname(action["dest"])
        os.makedirs(dest_dir, exist_ok=True)
        if os.path.exists(action["dest"]):
            action["status"] = "exists"
        else:
            _safe_transfer(filepath, action["dest"], move=move)
            action["status"] = "done"
            # Also move/copy subtitles
            for sa in action.get("subtitles", []):
                if not os.path.exists(sa["dest"]):
                    _safe_transfer(sa["source"], sa["dest"], move=move)
    elif action["dest"]:
        action["status"] = "dry_run"

    return action


def _try_parent_title(filepath: str) -> str | None:
    """Extract a clean title from the parent directory name."""
    parent = os.path.basename(os.path.dirname(filepath))
    if not parent:
        return None
    parsed = parse_filename(parent)
    if parsed["title"] and len(parsed["title"]) > 2:
        return parsed["title"]
    return None


def process_directory_entry(
    entry_path: str,
    movie_dest: str,
    tv_dest: str,
    tmdb: "TMDBClient | None",
    dry_run: bool = True,
    move: bool = True,
    probe: bool = True,
    movie_template: str | None = None,
    tv_template: str | None = None,
) -> list[dict]:
    """Process a top-level directory entry (file or folder) from the source."""
    videos = find_video_files(entry_path)
    results = []

    for vf in videos:
        result = process_file(vf, movie_dest, tv_dest, tmdb, dry_run, move, probe=probe,
                              movie_template=movie_template, tv_template=tv_template)
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_result(r: dict, verbose: bool = False):
    status_colors = {
        "dry_run": "\033[33m",   # yellow
        "done": "\033[32m",      # green
        "exists": "\033[36m",    # cyan
        "skipped": "\033[90m",   # gray
        "error": "\033[31m",     # red
    }
    reset = "\033[0m"
    color = status_colors.get(r["status"], "")
    status = r["status"].upper()

    src = r["source"]
    # Shorten source path for display
    src_display = os.path.basename(src)

    if r["dest"]:
        # Show relative destination
        dest_display = r["dest"]
        print(f"  {color}[{status}]{reset} {src_display}")
        if r.get("matched"):
            print(f"          matched: {r['matched']}")
        print(f"          -> {dest_display}")
        for sa in r.get("subtitles", []):
            print(f"          sub [{sa['lang']}] -> {os.path.basename(sa['dest'])}")
    else:
        error = r.get("error", "")
        print(f"  {color}[{status}]{reset} {src_display} ({error})")

    if verbose and r.get("parsed"):
        print(f"          parsed: {r['parsed']}")


def _print_progress(done: int, total: int, width: int = 40):
    """Print a progress bar to stderr."""
    pct = done / total if total else 0
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    sys.stderr.write(f"\r  [{bar}] {done}/{total} ({pct*100:.0f}%)")
    sys.stderr.flush()
    if done == total:
        sys.stderr.write("\n")


def _execute_action(action: dict, move: bool) -> dict:
    """Execute the move/copy for a single action. Thread-safe."""
    if not action["dest"]:
        return action
    dest_dir = os.path.dirname(action["dest"])
    os.makedirs(dest_dir, exist_ok=True)
    if os.path.exists(action["dest"]):
        action["status"] = "exists"
    else:
        _safe_transfer(action["source"], action["dest"], move=move)
        action["status"] = "done"
        # Also move/copy associated subtitles
        for sa in action.get("subtitles", []):
            if not os.path.exists(sa["dest"]):
                try:
                    _safe_transfer(sa["source"], sa["dest"], move=move)
                except OSError:
                    pass
    return action


# Files to always keep during cleanup (safety)
CLEANUP_KEEP_EXTENSIONS = VIDEO_EXTENSIONS | SUBTITLE_EXTENSIONS

# Known junk file extensions to delete during cleanup
CLEANUP_JUNK_EXTENSIONS = {
    ".nfo", ".txt", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".url",
    ".exe", ".html", ".htm", ".torrent", ".sfv", ".nzb", ".srr",
}


def _is_metadata_dir(name: str) -> bool:
    """Check if a directory is platform metadata (Synology @eaDir, macOS .DS_Store dirs, etc.)."""
    return name in ("@eaDir", ".DS_Store", "__MACOSX", "Thumbs.db")


def _rmtree_safe(path: str, dry_run: bool, cleaned: list[str]):
    """Recursively remove a directory tree (for metadata dirs like @eaDir)."""
    if dry_run:
        cleaned.append(f"  \033[31m[RMTREE]\033[0m {path}")
        return
    import shutil
    try:
        shutil.rmtree(path)
        cleaned.append(path)
    except OSError:
        pass


def _dir_has_video(dirpath: str) -> bool:
    """Check if a directory still contains any non-junk video files."""
    for root, dirs, files in os.walk(dirpath):
        dirs[:] = [d for d in dirs if not _is_metadata_dir(d)]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in VIDEO_EXTENSIONS and not is_junk_file(f):
                return True
    return False


def cleanup_source_dir(source_dir: str, moved_files: set[str], dry_run: bool = False) -> list[str]:
    """Clean up leftover junk files and empty directories after moving videos out.

    Only cleans subdirectories of source_dir where files were successfully moved.
    When all videos have been moved out of a directory, ALL remaining files (including
    subtitles) are considered orphaned and removed — they belong with the video.
    Returns list of cleaned paths.
    """
    cleaned = []
    # Find which top-level subdirectories had files moved
    affected_dirs = set()
    for f in moved_files:
        # Get the top-level subdir relative to source
        rel = os.path.relpath(f, source_dir)
        parts = rel.split(os.sep)
        if len(parts) > 1:
            top_path = os.path.join(source_dir, parts[0])
            if os.path.isdir(top_path):
                affected_dirs.add(top_path)

    for d in sorted(affected_dirs):
        # Check if any non-junk video files remain in this entry
        has_remaining_video = _dir_has_video(d)

        # Walk bottom-up to delete files then empty dirs
        for root, dirs, files in os.walk(d, topdown=False):
            # Remove metadata dirs like @eaDir first
            for dirname in dirs:
                if _is_metadata_dir(dirname):
                    _rmtree_safe(os.path.join(root, dirname), dry_run, cleaned)

            for f in files:
                fp = os.path.join(root, f)
                if fp in moved_files:
                    continue  # Already moved
                ext = os.path.splitext(f)[1].lower()
                # If no videos remain, everything is orphaned — clean it all
                # If videos remain, only clean known junk (keep video + subtitle files)
                if has_remaining_video:
                    if ext in CLEANUP_KEEP_EXTENSIONS and not is_junk_file(f):
                        continue  # Don't delete video/subtitle files that still have a purpose
                else:
                    if ext in VIDEO_EXTENSIONS and not is_junk_file(f):
                        continue  # Safety: never delete non-junk video files
                if dry_run:
                    cleaned.append(f"  \033[31m[DELETE]\033[0m {fp}")
                else:
                    try:
                        os.remove(fp)
                        cleaned.append(fp)
                    except OSError:
                        pass
            # Remove empty directories (including the entry dir itself)
            if not dry_run:
                try:
                    remaining = os.listdir(root)
                    if not remaining:
                        os.rmdir(root)
                        cleaned.append(root)
                except OSError:
                    pass
            else:
                try:
                    remaining = [f for f in os.listdir(root)
                                 if not _is_metadata_dir(f)
                                 and ((os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS
                                       and not is_junk_file(f))
                                      or (has_remaining_video
                                          and os.path.splitext(f)[1].lower() in SUBTITLE_EXTENSIONS)
                                      or os.path.isdir(os.path.join(root, f)))]
                    if not remaining:
                        cleaned.append(f"  \033[31m[RMDIR]\033[0m {root}")
                except OSError:
                    pass

    return cleaned


def check_paths_accessible(*paths: str) -> list[str]:
    """Check that all paths exist and are accessible. Returns list of errors."""
    errors = []
    for p in paths:
        if not os.path.exists(p):
            errors.append(f"Path does not exist: {p}")
        elif os.path.isdir(p) and not os.access(p, os.R_OK):
            errors.append(f"Path not readable: {p}")
    return errors


def run_once(args) -> int:
    """Run a single scan+process cycle. Returns number of files processed."""
    dry_run = not args.execute
    move = not args.copy
    probe = not args.no_probe

    # Rename mode: organize within source directory
    rename_mode = getattr(args, "rename", False)
    if rename_mode:
        args.movies = args.source
        args.tv = args.source

    # Check path accessibility
    errors = check_paths_accessible(args.source)
    if not dry_run and not rename_mode:
        errors += check_paths_accessible(args.movies, args.tv)
    if errors:
        for e in errors:
            log.error(e)
        return 0

    # TMDB setup
    tmdb = None
    if not args.no_tmdb:
        api_key = args.tmdb_key or os.environ.get("TMDB_API_KEY")
        bearer_token = args.tmdb_token or os.environ.get("TMDB_READ_TOKEN")
        if api_key or bearer_token:
            tmdb = TMDBClient(api_key or "", bearer_token)

    # Format templates
    movie_template = args.movie_format if args.movie_format else None
    tv_template = args.tv_format if args.tv_format else None

    # Get top-level entries
    try:
        entries = sorted(os.listdir(args.source))
    except PermissionError:
        log.error(f"Permission denied reading source: {args.source}")
        return 0
    except OSError as e:
        log.error(f"Cannot read source directory: {e}")
        return 0

    if args.filter:
        filt = args.filter.lower()
        entries = [e for e in entries if filt in e.lower() or filt in e.replace(".", " ").lower()]

    # Phase 1: Plan
    all_actions = []
    matched = 0

    for entry in entries:
        entry_path = os.path.join(args.source, entry)
        results = process_directory_entry(entry_path, args.movies, args.tv, tmdb,
                                          dry_run=True, move=move, probe=probe,
                                          movie_template=movie_template, tv_template=tv_template)
        if not results:
            continue
        for r in results:
            if r.get("matched"):
                matched += 1
            all_actions.append(r)

    total = len(all_actions)

    cleanup = getattr(args, "cleanup", False) or rename_mode

    if dry_run:
        for r in all_actions:
            print_result(r, args.verbose)
        if cleanup:
            moved = {r["source"] for r in all_actions if r.get("dest")}
            cleaned = cleanup_source_dir(args.source, moved, dry_run=True)
            if cleaned:
                print()
                print("Cleanup preview:")
                for line in cleaned:
                    print(line)
        print()
        print(f"Total: {total} files")
        if total:
            print(f"TMDB matched: {matched}/{total} ({matched/total*100:.1f}%)")
    else:
        if total:
            log.info(f"Processing {total} files...")
        for r in all_actions:
            print_result(r, args.verbose)

        stats = {"done": 0, "exists": 0, "error": 0}
        lock = threading.Lock()
        done_count = 0
        is_interactive = sys.stderr.isatty()

        def do_action(action):
            nonlocal done_count
            try:
                result = _execute_action(action, move)
            except Exception as e:
                action["status"] = "error"
                action["error"] = str(e)
                result = action
            with lock:
                done_count += 1
                stats[result["status"]] = stats.get(result["status"], 0) + 1
                if is_interactive:
                    _print_progress(done_count, total)
            return result

        actionable = [a for a in all_actions if a["dest"] and a["status"] == "dry_run"]
        for a in actionable:
            a["status"] = "pending"

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = [pool.submit(do_action, a) for a in actionable]
            concurrent.futures.wait(futures)

        skipped = total - len(actionable)

        # Cleanup after successful moves
        if cleanup and stats.get("done", 0) > 0:
            moved = {a["source"] for a in actionable if a["status"] == "done"}
            cleaned = cleanup_source_dir(args.source, moved, dry_run=False)
            if cleaned:
                print(f"  cleaned: {len(cleaned)} files/dirs")

        print()
        print(f"Total: {total} files")
        if total:
            print(f"TMDB matched: {matched}/{total} ({matched/total*100:.1f}%)")
        for k, v in stats.items():
            if v:
                print(f"  {k}: {v}")
        if skipped:
            print(f"  skipped: {skipped}")

    return total


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".mediasort", "config.json")

def load_config() -> dict:
    """Load persistent config from disk."""
    defaults = {
        "source": "/Volumes/Torrents",
        "movies_dest": "/Volumes/Movies",
        "tv_dest": "/Volumes/TV",
        "tmdb_api_key": os.environ.get("TMDB_API_KEY", ""),
        "tmdb_read_token": os.environ.get("TMDB_READ_TOKEN", ""),
        "use_tmdb": True,
        "use_probe": True,
        "execute": False,
        "copy_mode": False,
        "rename_mode": False,
        "cleanup": False,
        "parallel": 5,
        "movie_format": "",
        "tv_format": "",
        "watch_interval": 0,
        "filter": "",
    }
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE) as f:
                saved = json.load(f)
                defaults.update(saved)
    except (OSError, json.JSONDecodeError):
        pass
    return defaults




# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="mediasort",
        description="Organize media files into Plex-friendly directory structures.",
    )
    parser.add_argument("source", nargs="?", default=None, help="Source directory containing disorganized media files")
    parser.add_argument("--movies", "-m", default="/Volumes/Movies", help="Destination for movies (default: /Volumes/Movies)")
    parser.add_argument("--tv", "-t", default="/Volumes/TV", help="Destination for TV shows (default: /Volumes/TV)")
    parser.add_argument("--tmdb-key", "-k", default=None, help="TMDB API key (or set TMDB_API_KEY env var)")
    parser.add_argument("--tmdb-token", default=None, help="TMDB read access token (or set TMDB_READ_TOKEN env var)")
    parser.add_argument("--no-tmdb", action="store_true", help="Skip TMDB lookups, use filename parsing only")
    parser.add_argument("--no-probe", action="store_true", help="Skip ffprobe bitrate detection (faster)")
    parser.add_argument("--dry-run", "-n", action="store_true", default=True, help="Show what would happen without moving files (default)")
    parser.add_argument("--execute", "-x", action="store_true", help="Actually move files (disables dry-run)")
    parser.add_argument("--copy", "-c", action="store_true", help="Copy instead of move")
    parser.add_argument("--rename", "-r", action="store_true", help="Rename files in-place (reorganize within source directory)")
    parser.add_argument("--cleanup", action="store_true", help="Delete leftover junk files (NFOs, samples, txt) and empty dirs after moving")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed parsing info")
    parser.add_argument("--filter", "-f", default=None, help="Only process entries matching this substring")
    parser.add_argument("--parallel", "-j", type=int, default=5, help="Max parallel file operations (default: 5)")
    parser.add_argument("--movie-format", default=None,
                        help="Custom movie path template (variables: {title}, {year}, {tmdb_id}, {edition}, {quality}, {resolution}, {source}, {vcodec}, {acodec}, {bitrate_label}, {ext})")
    parser.add_argument("--tv-format", default=None,
                        help="Custom TV path template (variables: {show}, {season}, {episode}, {episode_title}, {season_folder}, {quality}, {ext})")
    parser.add_argument("--watch", "-w", type=int, default=None, metavar="SECONDS",
                        help="Watch mode: re-scan every N seconds (e.g. --watch 300 for 5 minutes)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging level (default: INFO)")

    args = parser.parse_args()

    # Setup logging
    use_timestamps = bool(args.watch)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s" if use_timestamps else "%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not args.source:
        parser.error("source directory is required")

    dry_run = not args.execute
    move = not args.copy
    probe = not args.no_probe

    # Load config file defaults for TMDB keys if not provided via CLI/env
    cfg = load_config()
    if not args.tmdb_key and not os.environ.get("TMDB_API_KEY"):
        args.tmdb_key = cfg.get("tmdb_api_key") or None
    if not args.tmdb_token and not os.environ.get("TMDB_READ_TOKEN"):
        args.tmdb_token = cfg.get("tmdb_read_token") or None

    # Print config header
    tmdb_status = "disabled"
    if not args.no_tmdb:
        api_key = args.tmdb_key or os.environ.get("TMDB_API_KEY")
        bearer_token = args.tmdb_token or os.environ.get("TMDB_READ_TOKEN")
        if api_key or bearer_token:
            tmdb_status = "enabled"
        else:
            tmdb_status = "disabled (set TMDB_API_KEY or use --tmdb-key)"
    print(f"TMDB lookup: {tmdb_status}")

    if dry_run:
        print("Mode: DRY RUN (use -x to execute)")
    elif args.rename:
        print(f"Mode: RENAME IN-PLACE (parallel: {args.parallel})")
    else:
        action_word = "MOVE" if move else "COPY"
        print(f"Mode: {action_word} (parallel: {args.parallel})")

    if not probe:
        print("Bitrate probe: disabled")

    print(f"Source: {args.source}")
    print(f"Movies -> {args.movies}")
    print(f"TV     -> {args.tv}")
    if args.watch:
        print(f"Watch: every {args.watch}s")
    print()

    if args.watch:
        # Watch mode: run continuously with graceful shutdown
        running = True

        def handle_signal(_signum, _frame):
            nonlocal running
            log.info("Shutting down...")
            running = False

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        while running:
            try:
                run_once(args)
            except Exception as e:
                log.error(f"Error during scan: {e}")

            for _ in range(args.watch):
                if not running:
                    break
                time.sleep(1)
    else:
        if not os.path.exists(args.source):
            print(f"Error: source path does not exist: {args.source}", file=sys.stderr)
            sys.exit(1)
        run_once(args)


if __name__ == "__main__":
    main()
