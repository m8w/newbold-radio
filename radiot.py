#!/usr/bin/env python3
"""
ExternalRadio — multi-source audio mixer for video soundtracks
==============================================================
Sources: YouTube · Archive.org · Alonetone · Bandcamp
Output:  BlackHole 2ch (or system default) for OBS/video capture
Logs:    ~/ExternalRadio/logs/ — 12-hour rotating blocks

FIXED: YouTube CSV auto-detection and validation
  - Handles Google Takeout anomalous header (filename in row 0)
  - Handles clean 2-col CSV (Title, YouTube_URL)
  - Handles full manifest (Channel, Title, Video_ID, Duration, YouTube_URL)
  - Validates every Video_ID (11 alphanumeric chars); extracts from URL if needed
  - Skips corrupt entries silently; reports count at startup

Author: built for William Victor Newbold / xik6
"""

import subprocess
import threading
import random
import time
import requests
import json
import sys
import signal
import os
import re
import csv
import collections
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

# Folder this script lives in. Files placed right next to radiot.py
# (youtube_videos.csv, youtube_cookies.txt) are found automatically —
# no matter where you clone the repo. This is what makes it "just work."
SCRIPT_DIR = Path(__file__).resolve().parent


# Recent runtime messages (errors, source failures) shown in the terminal
# status panel and the web /api/status — so failures aren't invisible.
RECENT_MSGS = collections.deque(maxlen=8)
_recent_lock = threading.Lock()


def note(msg: str):
    line = f'{datetime.now():%H:%M:%S}  {msg}'
    with _recent_lock:
        RECENT_MSGS.append(line)


def first_existing(paths: List[Path]) -> Optional[Path]:
    """Return the first path in the list that exists, or None."""
    for p in paths:
        try:
            if Path(p).exists():
                return Path(p)
        except Exception:
            continue
    return None


CONFIG = {
    'lanes': 4,
    # Logs live next to the script by default (folder is gitignored).
    'log_dir': SCRIPT_DIR / 'logs',
    'log_rotate_hours': 12,

    # Pin specific lanes to a single source (0-based lane index → source name).
    # Lane 1 is locked to YouTube so YouTube is ALWAYS in the mix; the rest
    # stay weighted-random. Add more, e.g. {0: 'youtube', 1: 'bandcamp'}.
    'pinned_lanes': {0: 'youtube'},

    # Local control panel + OBS overlay web server
    #   Control panel : http://localhost:<port>/
    #   OBS overlay   : http://localhost:<port>/obs   (add as a Browser Source)
    'control_port': 8080,

    'sources': {
        'youtube': {
            'enabled': True,
            'weight': 87,
            # --- Try these paths in order; first one that exists wins ---
            # Script-folder paths come FIRST so dropping the CSV next to
            # radiot.py is all you need — the rest are legacy fallbacks.
            'csv_paths': [
                SCRIPT_DIR / 'youtube_videos.csv',
                SCRIPT_DIR / 'youtube_videos (1).csv',
                SCRIPT_DIR.parent / 'youtube_videos.csv',
                Path.home() / 'music' / 'youtube_videos.csv',
                Path.home() / 'ExternalRadio' / 'youtube_videos.csv',
                Path.home() / 'ExternalRadio' / 'youtube_videos_gdrive.csv',
                Path.home() / 'music' / 'Newbold_Archive_Manifest_2026-02-13.csv',
                Path.home() / 'Documents' / 'youtube_videos.csv',
            ],
            # Cookies: next to the script first, then legacy locations.
            'cookies_files': [
                SCRIPT_DIR / 'youtube_cookies.txt',
                SCRIPT_DIR.parent / 'youtube_cookies.txt',
                Path.home() / 'ExternalRadio' / 'youtube_cookies.txt',
            ],
            # Browser to read cookies from when no cookies.txt file is present.
            # YouTube throws "Sign in to confirm you're not a bot" once your IP
            # is flagged; cookies fix it. macOS BLOCKS reading Safari's store
            # ("Operation not permitted"), but Chrome/Brave/Firefox work.
            # Set '' to send no cookies. A youtube_cookies.txt file (dropped
            # next to radiot.py) always takes priority over this.
            'cookies_browser': 'chrome',
            # yt-dlp player client(s). If YouTube stops playing, the startup
            # self-test will tell you; try 'web', 'ios', or 'ios,tv' here.
            'player_client': 'tv,web',
            # Enables yt-dlp's EJS challenge solver download so the YouTube
            # "n" signature challenge gets solved — WITHOUT this the audio URL
            # is throttled and playback dies after a few seconds.
            # 'ejs:github' (recommended) or 'ejs:npm'; '' to disable.
            'remote_components': 'ejs:github',
        },
        'archive': {
            'enabled': True,
            'weight': 20,
            'creator': 'William Victor Newbold',
            'api_url': 'https://archive.org/advancedsearch.php',
        },
        'alonetone': {
            'enabled': True,
            'weight': 5,
            'user': 'newbold',
            'base_url': 'https://alonetone.com/newbold/tracks',
        },
        'bandcamp': {
            'enabled': True,
            'weight': 13,
            'urls': [
                'https://xik6.bandcamp.com',
                'https://h92o.bandcamp.com',
            ],
        },
    },

    'ffplay_opts': [
        '-nodisp', '-autoexit', '-loglevel', 'quiet',
    ],

    # ── AUDIO PROCESSING (the "equalization" chain) ─────────────────────────
    # An ffmpeg -af filter chain applied to EVERY track so wildly different
    # sources (YouTube / Archive / Bandcamp) sit at a consistent level.
    #   • dynaudnorm  — real-time loudness leveling (smooth; great for a live
    #                   continuous mix — quiet tracks come up, loud ones come down)
    #   • alimiter    — brick-wall limiter to stop clipping/peaks
    # Per-lane volume is appended automatically after this chain.
    #
    # Tone EQ (bass/treble) — add bands to taste, e.g.:
    #   'equalizer=f=80:t=q:w=1:g=4, equalizer=f=3000:t=q:w=1:g=-2'
    # Broadcast EBU R128 alternative (more accurate, slightly more latency):
    #   'loudnorm=I=-16:TP=-1.5:LRA=11'
    'audio_filters':
        'highpass=f=30, dynaudnorm=f=250:g=15:p=0.9:m=10, alimiter=limit=0.95',

    'ytdlp_opts': [
        '--no-warnings',
        '--quiet',
        '--extractor-args', 'youtube:player_client=tv,web',
        '--no-playlist',
    ],
}


# ══════════════════════════════════════════════════════════════════════════════
#  YOUTUBE ID VALIDATOR
# ══════════════════════════════════════════════════════════════════════════════

VALID_YT_ID = re.compile(r'^[A-Za-z0-9_\-]{11}$')
YT_URL_ID   = re.compile(r'(?:v=|youtu\.be/|/v/)([A-Za-z0-9_\-]{11})')


def extract_video_id(raw: str) -> Optional[str]:
    """
    Given a string that might be a Video_ID or a full YouTube URL,
    return a valid 11-char ID or None.
    """
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    # Direct match
    if VALID_YT_ID.match(raw):
        return raw
    # Extract from URL
    m = YT_URL_ID.search(raw)
    if m:
        return m.group(1)
    return None


def build_yt_url(video_id: str) -> str:
    return f'https://www.youtube.com/watch?v={video_id}'


# ══════════════════════════════════════════════════════════════════════════════
#  YOUTUBE CSV LOADER  (auto-detects column layout)
# ══════════════════════════════════════════════════════════════════════════════

def load_youtube_csv(csv_paths: List[Path]) -> List[Tuple[str, str]]:
    """
    Try each path in order; load the first one found.
    Returns list of (title, youtube_url) tuples — only valid entries.
    """
    csv_file = None
    for p in csv_paths:
        if p.exists():
            csv_file = p
            break

    if csv_file is None:
        print('  ✗  YouTube CSV not found — checked:')
        for p in csv_paths:
            print(f'       {p}')
        return []

    print(f'  → YouTube CSV: {csv_file}')

    rows = []
    try:
        with open(csv_file, encoding='utf-8', errors='replace') as f:
            raw_lines = f.readlines()
    except Exception as e:
        print(f'  ✗  Could not read CSV: {e}')
        return []

    # ── Find the real header row ──────────────────────────────────────────────
    header_idx = None
    header_row = None
    for i, line in enumerate(raw_lines[:10]):
        lowered = line.lower()
        if 'video_id' in lowered or 'youtube_url' in lowered or 'title' in lowered:
            # Parse it as CSV to get column names
            try:
                cols = next(csv.reader([line]))
                cols_lower = [c.strip().lower() for c in cols]
                if any(k in cols_lower for k in ('video_id', 'youtube_url', 'title')):
                    header_idx = i
                    header_row = cols_lower
                    break
            except Exception:
                continue

    if header_idx is None:
        print('  ✗  Could not find header row in CSV (checked first 10 lines)')
        return []

    # ── Map column names ──────────────────────────────────────────────────────
    def col(name):
        try:
            return header_row.index(name)
        except ValueError:
            return None

    idx_id    = col('video_id')
    idx_url   = col('youtube_url')
    idx_title = col('title')

    # ── Parse data rows ───────────────────────────────────────────────────────
    valid   = 0
    skipped = 0

    reader = csv.reader(raw_lines[header_idx + 1:])
    for row in reader:
        if not row or all(c.strip() == '' for c in row):
            continue
        try:
            title = row[idx_title].strip() if idx_title is not None and idx_title < len(row) else 'Unknown'

            # Try Video_ID column first, then YouTube_URL column, then any column
            vid = None
            if idx_id is not None and idx_id < len(row):
                vid = extract_video_id(row[idx_id])
            if vid is None and idx_url is not None and idx_url < len(row):
                vid = extract_video_id(row[idx_url])
            if vid is None:
                # Last resort: scan all columns
                for cell in row:
                    vid = extract_video_id(cell)
                    if vid:
                        break

            if vid:
                rows.append((title, build_yt_url(vid)))
                valid += 1
            else:
                skipped += 1

        except (IndexError, Exception):
            skipped += 1
            continue

    if skipped > 0:
        print(f'  ✓  YouTube: {valid:,} valid videos ({skipped:,} corrupt entries skipped)')
    else:
        print(f'  ✓  YouTube: {valid:,} videos loaded')

    return rows


# ══════════════════════════════════════════════════════════════════════════════
#  SESSION LOG
# ══════════════════════════════════════════════════════════════════════════════

class SessionLog:
    def __init__(self, log_dir: Path, rotate_hours: float = 12):
        self.log_dir = log_dir
        self.rotate_hours = rotate_hours
        self._lock = threading.Lock()
        self._disabled = False          # set True if the disk can't be written
        self._entries: List[str] = []
        self._block_start = datetime.now()
        self._path = log_dir / 'block_startup.txt'
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._fail(e)
        self._start_new_block()

    def _fail(self, e: Exception):
        """Disk write failed (e.g. No space left) — keep the music going."""
        if not self._disabled:
            self._disabled = True
            msg = f'logging disabled — {e.__class__.__name__}: {e}'
            print(f'  ⚠  {msg}')
            note(f'⚠ {msg}')

    def _start_new_block(self):
        self._block_start = datetime.now()
        ts = self._block_start.strftime('%Y%m%d_%H%M%S')
        self._path = self.log_dir / f'block_{ts}.txt'
        self._entries = []
        if self._disabled:
            return
        try:
            with open(self._path, 'w') as f:
                f.write(f'ExternalRadio session — {self._block_start.strftime("%Y-%m-%d %H:%M")}\n')
                f.write('=' * 60 + '\n\n')
        except OSError as e:
            self._fail(e)

    def log(self, source: str, title: str, url: str):
        now = datetime.now()
        # Rotate if needed
        if (now - self._block_start).total_seconds() > self.rotate_hours * 3600:
            self._finalize_block()
            self._start_new_block()

        entry = f'[{now.strftime("%H:%M:%S")}] [{source.upper()}] {title}\n  {url}\n'
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > 5000:      # cap in-memory list
                self._entries.pop(0)
            if self._disabled:
                return
            try:
                with open(self._path, 'a') as f:
                    f.write(entry)
            except OSError as e:
                self._fail(e)

    def _finalize_block(self):
        if self._disabled:
            return
        try:
            with open(self._path, 'a') as f:
                f.write(f'\n\n— end of block ({len(self._entries)} tracks) —\n')
        except OSError as e:
            self._fail(e)

    def finalize(self):
        self._finalize_block()
        if self._disabled:
            print('\n  Log not saved (disk write was disabled).')
        else:
            print(f'\n  Log saved: {self._path}')


# ══════════════════════════════════════════════════════════════════════════════
#  SOURCE FETCHERS
# ══════════════════════════════════════════════════════════════════════════════

class YouTubeFetcher:
    name = 'youtube'

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._catalog: List[Tuple[str, str]] = []
        self._loaded = False

    def load(self):
        self._catalog = load_youtube_csv(self.cfg['csv_paths'])
        self._loaded = True

    def fetch_random(self) -> Optional[Tuple[str, str, str]]:
        """Returns (source, title, url) or None"""
        if not self._loaded:
            self.load()
        if not self._catalog:
            return None
        title, url = random.choice(self._catalog)
        return ('youtube', title, url)

    def get_stream_url(self, url: str) -> Optional[str]:
        cookies = first_existing(self.cfg.get('cookies_files', []))
        opts = list(CONFIG['ytdlp_opts'])
        if cookies:
            opts += ['--cookies', str(cookies)]
        cmd = ['yt-dlp', '-f', 'bestaudio/best', '--get-url'] + opts + [url]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            lines = result.stdout.strip().splitlines()
            return lines[0] if lines else None
        except Exception:
            return None


class ArchiveFetcher:
    name = 'archive'

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._catalog: List[Tuple[str, str]] = []
        self._loaded = False

    def load(self):
        try:
            params = {
                'q': f'creator:"{self.cfg["creator"]}" mediatype:audio',
                'fl[]': ['identifier', 'title'],
                'rows': 1000,
                'output': 'json',
            }
            resp = requests.get(self.cfg['api_url'], params=params, timeout=20)
            data = resp.json()
            docs = data.get('response', {}).get('docs', [])
            for doc in docs:
                iid = doc.get('identifier', '')
                title = doc.get('title', iid)
                if iid:
                    self._catalog.append((title, f'https://archive.org/download/{iid}'))
            print(f'  ✓  Archive.org: {len(self._catalog):,} items indexed')
        except Exception as e:
            print(f'  ✗  Archive.org load failed: {e}')
        self._loaded = True

    def fetch_random(self) -> Optional[Tuple[str, str, str]]:
        if not self._loaded:
            self.load()
        if not self._catalog:
            return None
        # Pick a random item (the "album") and a random audio file (the "song").
        item_title, base_url = random.choice(self._catalog)
        identifier = base_url.split('/download/')[-1]
        try:
            resp = requests.get(f'https://archive.org/metadata/{identifier}', timeout=15)
            meta = resp.json()
            files = [f for f in meta.get('files', [])
                     if f.get('format', '').lower() in ('mp3', 'ogg vorbis', 'flac', 'vbr mp3')]
            if files:
                chosen = random.choice(files)
                url = f'https://archive.org/download/{identifier}/{chosen["name"]}'
                # Song name: prefer the file's own title, else prettify its filename.
                song = chosen.get('title')
                if not song:
                    song = chosen['name'].rsplit('.', 1)[0]
                    song = song.replace('_', ' ').replace('-', ' ').strip().title()
                # "Song — Album (archive.org)" so the source/account is obvious.
                if item_title and item_title.lower() not in song.lower():
                    display = f'{song} — {item_title} (archive.org)'
                else:
                    display = f'{song} (archive.org)'
                return ('archive', display, url)
        except Exception:
            pass
        return None


class AlonetroneFetcher:
    name = 'alonetone'

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._catalog: List[Tuple[str, str]] = []
        self._loaded = False

    def load(self):
        user = self.cfg['user']
        url = self.cfg['base_url']
        try:
            cmd = ['yt-dlp', '--flat-playlist', '--print', '%(title)s\t%(url)s',
                   '--quiet', '--no-warnings', url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            for line in result.stdout.strip().splitlines():
                parts = line.split('\t', 1)
                if len(parts) == 2:
                    title, track_url = parts
                    # Only include tracks owned by this user
                    if f'/{user}/tracks/' in track_url or f'cdn.alonetone.com' in track_url:
                        self._catalog.append((title.strip(), track_url.strip()))
            # Deduplicate
            seen = set()
            deduped = []
            for t, u in self._catalog:
                if u not in seen:
                    seen.add(u)
                    deduped.append((t, u))
            self._catalog = deduped
            print(f'  ✓  Alonetone: {len(self._catalog):,} tracks indexed')
        except Exception as e:
            print(f'  ✗  Alonetone load failed: {e}')
        self._loaded = True

    def fetch_random(self) -> Optional[Tuple[str, str, str]]:
        if not self._loaded:
            self.load()
        if not self._catalog:
            return None
        title, url = random.choice(self._catalog)
        if not url or not url.startswith('http'):
            return None
        return ('alonetone', title, url)


class BandcampFetcher:
    """
    Two-level crawl so every entry has a real song + album name + account:
      1. Crawl each artist page  → list of album URLs
      2. Crawl each album        → individual /track/ URLs with titles
    Each catalog entry's title reads "Song — Album (account)" and its URL
    is the track's own Bandcamp page — i.e. the link to buy that track.
    """
    name = 'bandcamp'

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._catalog: List[Tuple[str, str]] = []
        self._loaded = False

    @staticmethod
    def _account(url: str) -> str:
        """xik6.bandcamp.com → 'xik6'."""
        try:
            host = url.split('//', 1)[-1].split('/', 1)[0]
            return host.split('.')[0] or 'bandcamp'
        except Exception:
            return 'bandcamp'

    def _flat(self, url: str, timeout: int) -> List[str]:
        """Run yt-dlp --flat-playlist; return 'webpage_url\\ttitle' lines."""
        cmd = ['yt-dlp', '--flat-playlist',
               '--print', '%(webpage_url)s\t%(title)s',
               '--quiet', '--no-warnings', '--no-abort-on-error', url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.stdout.strip().splitlines()
        except Exception:
            return []

    @staticmethod
    def _name_from_url(url: str) -> str:
        """Last URL segment → 'Pretty Title'."""
        slug = url.rstrip('/').split('/')[-1]
        return slug.replace('-', ' ').replace('_', ' ').strip().title() or 'Untitled'

    def load(self):
        for artist_url in self.cfg['urls']:
            account = self._account(artist_url)

            # Step 1 — discover album URLs (and any standalone track URLs).
            album_urls = set()
            direct_tracks = []   # (title, url) tracks found at the artist level
            for line in self._flat(artist_url, 120):
                parts = line.split('\t')
                url = parts[0].strip() if parts and parts[0] else ''
                title = parts[1].strip() if len(parts) > 1 else ''
                if not url.startswith('http'):
                    continue
                if '/album/' in url:
                    album_urls.add(url.split('?')[0])
                elif '/track/' in url:
                    direct_tracks.append((title, url.split('?')[0]))

            # Step 2 — crawl each album for its individual tracks.
            for album_url in sorted(album_urls):
                album_name = self._name_from_url(album_url)
                for line in self._flat(album_url, 60):
                    parts = line.split('\t')
                    url = parts[0].strip() if parts and parts[0] else ''
                    title = parts[1].strip() if len(parts) > 1 else ''
                    if not url.startswith('http') or '/track/' not in url:
                        continue
                    if not title or title in ('NA', 'None'):
                        title = self._name_from_url(url)
                    display = f'{title} — {album_name} ({account})'
                    self._catalog.append((display, url.split('?')[0]))

            # Fold in any standalone tracks not attached to an album.
            for title, url in direct_tracks:
                if not title or title in ('NA', 'None'):
                    title = self._name_from_url(url)
                display = f'{title} ({account})'
                self._catalog.append((display, url))

        # Deduplicate by URL.
        seen, deduped = set(), []
        for t, u in self._catalog:
            if u not in seen:
                seen.add(u)
                deduped.append((t, u))
        self._catalog = deduped

        print(f'  ✓  Bandcamp: {len(self._catalog):,} tracks indexed')
        self._loaded = True

    def fetch_random(self) -> Optional[Tuple[str, str, str]]:
        if not self._loaded:
            self.load()
        if not self._catalog:
            return None
        title, url = random.choice(self._catalog)
        return ('bandcamp', title, url)


# ══════════════════════════════════════════════════════════════════════════════
#  SOURCE ROUTER
# ══════════════════════════════════════════════════════════════════════════════

class SourceRouter:
    def __init__(self, cfg: dict):
        src_cfg = cfg['sources']
        self._fetchers: List = []
        self._weights: List[int] = []

        yt = YouTubeFetcher(src_cfg['youtube'])
        if src_cfg['youtube']['enabled']:
            self._fetchers.append(yt)
            self._weights.append(src_cfg['youtube']['weight'])

        ar = ArchiveFetcher(src_cfg['archive'])
        if src_cfg['archive']['enabled']:
            self._fetchers.append(ar)
            self._weights.append(src_cfg['archive']['weight'])

        al = AlonetroneFetcher(src_cfg['alonetone'])
        if src_cfg['alonetone']['enabled']:
            self._fetchers.append(al)
            self._weights.append(src_cfg['alonetone']['weight'])

        bc = BandcampFetcher(src_cfg['bandcamp'])
        if src_cfg['bandcamp']['enabled']:
            self._fetchers.append(bc)
            self._weights.append(src_cfg['bandcamp']['weight'])

        # Pre-load YouTube synchronously (fast, CSV)
        if src_cfg['youtube']['enabled']:
            yt.load()

        # Load everything else in background threads
        for f in self._fetchers:
            if f.name != 'youtube' and not f._loaded:
                t = threading.Thread(target=f.load, daemon=True)
                t.start()

        self._yt = yt

    def pick_random(self) -> Optional[Tuple[str, str, str]]:
        """Returns (source, title, url) or None"""
        loaded = [(f, w) for f, w in zip(self._fetchers, self._weights) if f._loaded and f._catalog]
        if not loaded:
            return None
        fetchers, weights = zip(*loaded)
        chosen = random.choices(fetchers, weights=weights, k=1)[0]
        return chosen.fetch_random()

    def pick_from(self, source: str) -> Optional[Tuple[str, str, str]]:
        """Pick a track from one specific source (for pinned lanes).
        Returns None if that source isn't ready yet — the lane waits rather
        than falling back, so a pinned lane stays true to its source."""
        for f in self._fetchers:
            if f.name == source and f._loaded and f._catalog:
                return f.fetch_random()
        return None

    def ytdlp_stream_cmd(self, source: str, url: str) -> List[str]:
        """
        Build a yt-dlp command that downloads the audio and writes it to
        stdout ('-o -') so it can be piped straight into ffplay. This is far
        more reliable than '--get-url' for YouTube, whose resolved URLs are
        segmented / throttled / bot-gated and often won't play on their own.
        """
        cmd = ['yt-dlp', '-f', 'bestaudio/best', '-o', '-',
               '--quiet', '--no-warnings', '--no-playlist']
        if source == 'youtube':
            cfg = self._yt.cfg
            cookies = first_existing(cfg.get('cookies_files', []))
            browser = cfg.get('cookies_browser', '')
            if cookies:
                cmd += ['--cookies', str(cookies)]
            elif browser:
                cmd += ['--cookies-from-browser', browser]
            # else: no cookies — public videos play fine without them.
            # Player client choice dodges YouTube's web-client bot detection.
            # Configurable: if YouTube stops working, try 'web' or 'ios,tv'.
            client = cfg.get('player_client', 'tv,web')
            if client:
                cmd += ['--extractor-args', f'youtube:player_client={client}']
            rc = cfg.get('remote_components', '')
            if rc:
                cmd += ['--remote-components', rc]
        cmd.append(url)
        return cmd

    def youtube_probe(self) -> Tuple[bool, str]:
        """Try to resolve one real YouTube video so we can tell, at startup,
        whether YouTube will actually play — and if not, exactly why."""
        yt = self._yt
        if not getattr(yt, '_catalog', None):
            return (False, 'YouTube catalog empty (CSV not loaded)')
        _, url = yt._catalog[0]
        cfg = yt.cfg
        cmd = ['yt-dlp', '-f', 'bestaudio/best', '--simulate',
               '-O', '%(id)s', '--no-warnings', '--no-playlist']
        cookies = first_existing(cfg.get('cookies_files', []))
        browser = cfg.get('cookies_browser', '')
        if cookies:
            cmd += ['--cookies', str(cookies)]
        elif browser:
            cmd += ['--cookies-from-browser', browser]
        client = cfg.get('player_client', 'tv,web')
        if client:
            cmd += ['--extractor-args', f'youtube:player_client={client}']
        rc = cfg.get('remote_components', '')
        if rc:
            cmd += ['--remote-components', rc]
        cmd.append(url)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        except FileNotFoundError:
            return (False, 'yt-dlp not installed (brew install yt-dlp)')
        except Exception as e:
            return (False, f'probe error: {e}')
        if r.returncode == 0 and r.stdout.strip():
            return (True, f'resolved {r.stdout.strip().splitlines()[0]}')
        errs = [l for l in r.stderr.splitlines() if l.strip()]
        return (False, errs[-1].strip() if errs else f'yt-dlp exit {r.returncode}')


# ══════════════════════════════════════════════════════════════════════════════
#  AUDIO LANE
# ══════════════════════════════════════════════════════════════════════════════

class AudioLane:
    """
    One playback lane. Plays random tracks back-to-back via ffplay.

    Live controls (driven by the control panel / OBS overlay server):
      • pause / resume — TRUE pause: suspends the player process so the very
                         same song continues from where it left off on resume
                         (does NOT start a new track)
      • skip           — jumps to the next track immediately
      • volume (0–100) — applied to the next track that starts
      • pin            — lock this lane to one source (or 'random')
    """

    def __init__(self, lane_id: int, router: SourceRouter, log: SessionLog,
                 control: 'RadioControl', pin: Optional[str] = None):
        self.lane_id = lane_id
        self.router = router
        self.log = log
        self.control = control          # shared state (global pause, etc.)
        self.pin = pin                  # if set, this lane only plays this source
        self._proc: Optional[subprocess.Popen] = None      # ffplay
        self._ytdlp: Optional[subprocess.Popen] = None     # yt-dlp feeding the pipe
        self._current_title = '—'
        self._current_source = '—'
        self._current_url = ''
        self._started_at: Optional[datetime] = None
        self._paused_accum = 0.0        # seconds spent paused (for elapsed display)
        self.paused = False             # lane-level pause
        self.volume = 100               # 0–100, applied on next track
        self._skip = threading.Event()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ── control helpers ──────────────────────────────────────────────────────
    def _is_paused(self) -> bool:
        """Effective pause = this lane paused OR everything paused globally."""
        return self.paused or self.control.global_paused

    def _signal_proc(self, sig):
        """Send a signal to the live player process(es)."""
        for p in (self._proc, self._ytdlp):
            if p and p.poll() is None:
                try:
                    p.send_signal(sig)
                except Exception:
                    pass

    def _kill_proc(self):
        for attr in ('_proc', '_ytdlp'):
            p = getattr(self, attr, None)
            if p and p.poll() is None:
                try:
                    p.send_signal(signal.SIGCONT)   # wake if suspended, so it can die
                    p.terminate()
                except Exception:
                    pass
            setattr(self, attr, None)

    def skip(self):
        """Jump to the next track immediately (works even while paused)."""
        self._skip.set()
        self._kill_proc()

    def set_paused(self, value: bool):
        self.paused = bool(value)
        # Suspend/resume happens in the play loop, but apply right away too
        # so a pause feels instant.
        self._signal_proc(signal.SIGSTOP if self._is_paused() else signal.SIGCONT)

    def set_volume(self, value: int):
        self.volume = max(0, min(100, int(value)))

    def set_pin(self, source: Optional[str]):
        """Lock this lane to a source ('' / None / 'random' = unpinned)."""
        source = (source or '').strip().lower()
        self.pin = source if source and source != 'random' else None

    # ── playback loop ────────────────────────────────────────────────────────
    def _loop(self):
        while self._running:
            # If paused before a track even starts, hold here (nothing to
            # resume yet) — don't begin audio until the user un-pauses.
            while self._running and self._is_paused() and self._proc is None:
                time.sleep(0.2)
            if not self._running:
                break

            if self.pin:
                track = self.router.pick_from(self.pin)
            else:
                track = self.router.pick_random()
            if not track:
                time.sleep(5)        # source not ready yet — wait, don't fall back
                continue

            source, title, url = track

            self._current_source = source
            self._current_title = title[:80]
            self._current_url = url
            self._started_at = datetime.now()
            self._paused_accum = 0.0
            self.log.log(source, title, url)

            self._skip.clear()
            self._play(source, url)

            # Brief gap between tracks (skip the wait if user is skipping).
            if not self._skip.is_set():
                time.sleep(random.uniform(0.5, 2.0))

    def _play(self, source: str, url: str):
        """
        Archive.org tracks are direct MP3 URLs — ffplay reads them straight.
        Everything else (YouTube, Bandcamp, Alonetone) is piped:
            yt-dlp  -o -  →  ffplay -i pipe:0
        so yt-dlp handles all the auth / segments / cookies and ffplay just
        plays the bytes it receives. This is what makes YouTube actually play.

        Pause is real: we SIGSTOP the player to freeze it in place and SIGCONT
        to resume the same song — no new track, no lost position.
        """
        # Build the audio filter chain: equalization/leveling first, then the
        # per-lane volume. Keeps every source at a consistent loudness.
        chain = []
        base = CONFIG.get('audio_filters', '').strip()
        if base:
            chain.append(base)
        chain.append(f'volume={self.volume / 100:.2f}')
        af = ','.join(chain)
        ffplay_base = ['ffplay'] + CONFIG['ffplay_opts'] + ['-af', af]
        suspended = False
        pause_started = 0.0
        ytdlp = None                 # local handle so we can read its stderr
        start = time.time()
        try:
            if source == 'archive':
                cmd = ffplay_base + [url]
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                ytdlp_cmd = self.router.ytdlp_stream_cmd(source, url)
                # Capture yt-dlp's stderr so we can report WHY it failed.
                ytdlp = subprocess.Popen(
                    ytdlp_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self._ytdlp = ytdlp
                self._proc = subprocess.Popen(
                    ffplay_base + ['-i', 'pipe:0'],
                    stdin=ytdlp.stdout,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                # Let yt-dlp get SIGPIPE if ffplay exits first.
                ytdlp.stdout.close()

            # If we already started paused, freeze immediately.
            if self._is_paused():
                self._signal_proc(signal.SIGSTOP)
                suspended = True
                pause_started = time.time()

            # Poll: reconcile pause state, and break on finish / skip / stop.
            while self._running:
                if self._proc.poll() is not None:
                    break                   # track finished (or failed) — move on
                if self._skip.is_set():
                    break                   # user skipped
                want_pause = self._is_paused()
                if want_pause and not suspended:
                    self._signal_proc(signal.SIGSTOP)
                    suspended = True
                    pause_started = time.time()
                elif not want_pause and suspended:
                    self._signal_proc(signal.SIGCONT)
                    suspended = False
                    self._paused_accum += time.time() - pause_started
                time.sleep(0.2)
        except FileNotFoundError as e:
            print(f'  ✗  missing tool: {e}')
            self._running = False
        except Exception:
            pass
        finally:
            played = time.time() - start - self._paused_accum
            user_action = (self._skip.is_set() or self._is_paused()
                           or not self._running)
            self._kill_proc()
            # A track that "ends" almost immediately = the stream never played.
            # Surface the real reason instead of silently spinning to the next.
            if ytdlp is not None and played < 4.0 and not user_action:
                err = b''
                try:
                    err = ytdlp.stderr.read() or b''
                except Exception:
                    pass
                lines = [l for l in err.decode('utf-8', 'replace').splitlines()
                         if l.strip()]
                reason = lines[-1].strip() if lines else \
                    f'no audio (yt-dlp exit {ytdlp.returncode})'
                self.control.report_source_error(source, reason)
                note(f'⚠ {source} failed: {reason[:90]}')
                # Back off so a broken source can't spin every few seconds.
                for _ in range(15):
                    if not self._running or self._skip.is_set():
                        break
                    time.sleep(1)
            elif ytdlp is not None:
                try:
                    ytdlp.stderr.read()        # drain to let it exit cleanly
                except Exception:
                    pass

    def stop(self):
        self._running = False
        self._kill_proc()

    @property
    def status(self) -> str:
        if self._is_paused() and self._proc is not None:
            tag = 'PAUSED'
        else:
            tag = self._current_source.upper()
        return f'[{tag}] {self._current_title}'

    def info(self) -> dict:
        """Snapshot for the control panel / OBS overlay."""
        paused = self._is_paused()
        elapsed = ''
        if self._started_at and self._proc is not None:
            secs = int((datetime.now() - self._started_at).total_seconds()
                       - self._paused_accum)
            elapsed = f'{max(0, secs) // 60:02d}:{max(0, secs) % 60:02d}'
        return {
            'lane': self.lane_id + 1,
            'source': self._current_source,
            'title': self._current_title,
            'url': self._current_url,
            'paused': paused,
            'lane_paused': self.paused,
            'volume': self.volume,
            'pin': self.pin or '',
            'playing': self._proc is not None and self._proc.poll() is None,
            'elapsed': elapsed,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  CONTROL STATE
# ══════════════════════════════════════════════════════════════════════════════

class RadioControl:
    """Shared state + actions for the control panel and OBS overlay."""

    def __init__(self):
        self.lanes: List[AudioLane] = []
        self.global_paused = False
        self.log: Optional[SessionLog] = None
        self.started_at = datetime.now()
        self.source_errors: Dict[str, str] = {}   # source -> last error reason
        self._err_lock = threading.Lock()

    def report_source_error(self, source: str, reason: str):
        with self._err_lock:
            self.source_errors[source] = reason

    def _lane(self, lane_num: int) -> Optional[AudioLane]:
        # lane_num is 1-based from the UI; lanes are stored 0-based.
        idx = lane_num - 1
        if 0 <= idx < len(self.lanes):
            return self.lanes[idx]
        return None

    # ── actions ──────────────────────────────────────────────────────────────
    def skip(self, lane_num: int):
        lane = self._lane(lane_num)
        if lane:
            lane.skip()

    def skip_all(self):
        for lane in self.lanes:
            lane.skip()

    def set_pause(self, lane_num: int, value: bool):
        lane = self._lane(lane_num)
        if lane:
            lane.set_paused(value)

    def toggle_pause(self, lane_num: int):
        lane = self._lane(lane_num)
        if lane:
            lane.set_paused(not lane.paused)

    def set_global_pause(self, value: bool):
        self.global_paused = bool(value)
        # Apply immediately to whatever is currently playing.
        sig = signal.SIGSTOP if self.global_paused else signal.SIGCONT
        for lane in self.lanes:
            lane._signal_proc(sig)

    def toggle_global_pause(self):
        self.set_global_pause(not self.global_paused)

    def set_volume(self, lane_num: int, value: int):
        lane = self._lane(lane_num)
        if lane:
            lane.set_volume(value)

    def set_pin(self, lane_num: int, source: Optional[str]):
        lane = self._lane(lane_num)
        if lane:
            lane.set_pin(source)
            lane.skip()      # apply the new source right away

    # ── status snapshot ──────────────────────────────────────────────────────
    def status(self) -> dict:
        secs = int((datetime.now() - self.started_at).total_seconds())
        uptime = f'{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}'
        with _recent_lock:
            messages = list(RECENT_MSGS)
        with self._err_lock:
            errors = dict(self.source_errors)
        return {
            'global_paused': self.global_paused,
            'uptime': uptime,
            'tracks_logged': len(self.log._entries) if self.log else 0,
            'lanes': [lane.info() for lane in self.lanes],
            'messages': messages,
            'source_errors': errors,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  WEB CONTROL PANEL + OBS OVERLAY
# ══════════════════════════════════════════════════════════════════════════════

CONTROL_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ExternalRadio Control</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         background:#0e0f13; color:#e8e8ec; }
  header { padding:16px 20px; background:#16181f; border-bottom:1px solid #262a35;
           display:flex; align-items:center; gap:16px; flex-wrap:wrap; }
  h1 { font-size:18px; margin:0; letter-spacing:.5px; }
  .meta { color:#8a8f9c; font-size:13px; }
  .bar { padding:14px 20px; display:flex; gap:10px; flex-wrap:wrap;
         background:#12141a; border-bottom:1px solid #262a35; }
  main { padding:20px; display:grid; gap:16px;
         grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); }
  .lane { background:#16181f; border:1px solid #262a35; border-radius:12px;
          padding:16px; }
  .lane.paused { opacity:.6; border-color:#6b5a27; }
  select { background:#202533; color:#e8e8ec; border:1px solid #2f3442;
           border-radius:8px; padding:7px 10px; font-size:13px; font-weight:600; }
  .lanehead { display:flex; align-items:center; justify-content:space-between;
              margin-bottom:8px; }
  .badge { font-size:11px; font-weight:700; letter-spacing:.5px; padding:3px 8px;
           border-radius:6px; background:#2a2f3d; color:#aab2c5; }
  .badge.youtube{background:#3a1416;color:#ff6b6b}
  .badge.archive{background:#13233a;color:#5aa6ff}
  .badge.bandcamp{background:#0f2e33;color:#3fd0d8}
  .badge.alonetone{background:#2a1f3a;color:#b78bff}
  .title { font-size:15px; font-weight:600; margin:4px 0; min-height:20px;
           overflow-wrap:anywhere; }
  .url { font-size:12px; color:#6f93c9; overflow-wrap:anywhere; }
  .url a { color:#6f93c9; }
  .elapsed { font-size:12px; color:#8a8f9c; }
  .controls { display:flex; gap:8px; margin-top:12px; flex-wrap:wrap; }
  button { cursor:pointer; border:1px solid #2f3442; background:#202533;
           color:#e8e8ec; padding:8px 14px; border-radius:8px; font-size:13px;
           font-weight:600; transition:background .12s; }
  button:hover { background:#2b3142; }
  button.primary { background:#234d2e; border-color:#2f6b3d; }
  button.primary:hover { background:#2c5e39; }
  button.danger { background:#4d2330; border-color:#6b2f3d; }
  button.danger:hover { background:#5e2c3a; }
  button.on { background:#6b2f3d; border-color:#8a3d4f; }
  .vol { display:flex; align-items:center; gap:8px; margin-top:10px;
         font-size:12px; color:#8a8f9c; }
  input[type=range]{ flex:1; accent-color:#5aa6ff; height:28px; }
  .obs-link { font-size:12px; color:#8a8f9c; }
  .obs-link code { background:#202533; padding:2px 6px; border-radius:4px;
                   color:#9fd0ff; }
  /* Phone-friendly: bigger tap targets, single column, no zoom-on-focus. */
  @media (max-width: 640px) {
    main { grid-template-columns:1fr; padding:12px; gap:12px; }
    h1 { font-size:20px; }
    button { padding:13px 16px; font-size:16px; flex:1 1 auto; }
    select { padding:12px; font-size:16px; width:100%; }
    .bar button { flex:1 1 45%; }
    .title { font-size:17px; }
    input[type=range]{ height:40px; }
  }
  .logbox { margin:0 20px 24px; padding:12px 16px; background:#0b0c10;
            border:1px solid #20242e; border-radius:10px; font-size:12px;
            font-family:ui-monospace,Menlo,monospace; color:#8a8f9c;
            white-space:pre-wrap; }
  .logbox .err { color:#ff8585; font-weight:600; }
</style>
</head>
<body>
<header>
  <h1>🎛 ExternalRadio</h1>
  <span class="meta" id="meta">connecting…</span>
  <span class="obs-link" style="margin-left:auto">
    OBS overlay: <code id="obsurl">/obs</code>
  </span>
</header>
<div class="bar">
  <button class="danger" id="pauseall" onclick="act('/api/global_pause_toggle')">Pause All</button>
  <button onclick="act('/api/skip_all')">Skip All ⏭</button>
</div>
<main id="lanes"></main>
<div class="logbox" id="log"></div>

<script>
const SOURCES = ['random','youtube','archive','bandcamp','alonetone'];
function act(path){ fetch(path,{method:'POST'}).then(refresh); }
function setvol(lane,v){ fetch('/api/volume?lane='+lane+'&v='+v,{method:'POST'}); }
function setpin(lane,src){ fetch('/api/pin?lane='+lane+'&source='+src,{method:'POST'}).then(refresh); }

function refresh(){
  fetch('/api/status').then(r=>r.json()).then(s=>{
    document.getElementById('obsurl').textContent =
        location.origin + '/obs';
    document.getElementById('meta').textContent =
        'uptime ' + s.uptime + '  ·  ' + s.tracks_logged + ' tracks logged'
        + (s.global_paused ? '  ·  ⏸ ALL PAUSED' : '');
    const pb = document.getElementById('pauseall');
    pb.classList.toggle('on', s.global_paused);
    pb.textContent = s.global_paused ? '▶ Resume All' : '⏸ Pause All';

    const root = document.getElementById('lanes');
    root.innerHTML = '';
    s.lanes.forEach(L=>{
      const src = (L.source||'—').toLowerCase();
      const paused = L.paused;
      const urlHtml = L.url
        ? '<a href="'+L.url+'" target="_blank" rel="noopener">'+L.url+'</a>'
        : '—';
      const pinVal = L.pin || 'random';
      const opts = SOURCES.map(o =>
        '<option value="'+o+'"'+(o===pinVal?' selected':'')+'>'
        + (o==='random'?'🎲 Random':'📌 '+o.charAt(0).toUpperCase()+o.slice(1))
        + '</option>').join('');
      const el = document.createElement('div');
      el.className = 'lane' + (paused ? ' paused':'');
      el.innerHTML =
        '<div class="lanehead">'
        +  '<strong>Lane '+L.lane
        +     (L.pin ? ' <span style="color:#8a8f9c;font-weight:400">📌 '
                       +L.pin.toUpperCase()+'</span>' : '')
        +  '</strong>'
        +  '<span class="badge '+src+'">'+(L.source||'—').toUpperCase()+'</span>'
        +'</div>'
        +'<div class="title">'+(L.title||'—')+'</div>'
        +'<div class="url">'+urlHtml+'</div>'
        +'<div class="elapsed">'+(paused?'⏸ paused':('▶ '+(L.elapsed||'')))+'</div>'
        +'<div class="controls">'
        +  '<button class="primary" onclick="act(\\'/api/skip?lane='+L.lane+'\\')">Change Song ⏭</button>'
        +  '<button class="'+(L.lane_paused?'on':'')+'" onclick="act(\\'/api/pause_toggle?lane='+L.lane+'\\')">'
        +     (L.lane_paused?'▶ Resume':'⏸ Pause')+'</button>'
        +  '<select onchange="setpin('+L.lane+',this.value)" title="Lock this lane to a source">'+opts+'</select>'
        +'</div>'
        +'<div class="vol">Vol'
        +  '<input type="range" min="0" max="100" value="'+L.volume+'" '
        +     'onchange="setvol('+L.lane+',this.value)">'
        +  '<span>'+L.volume+'%</span>'
        +'</div>';
      root.appendChild(el);
    });

    // Recent runtime messages / source errors
    const lg = document.getElementById('log');
    const errs = Object.entries(s.source_errors || {});
    let html = '';
    if (errs.length){
      html += errs.map(([k,v]) =>
        '<div class="err">✗ '+k.toUpperCase()+': '+v+'</div>').join('');
    }
    (s.messages || []).slice(-6).forEach(m => { html += '<div>'+m+'</div>'; });
    lg.innerHTML = html || '<div style="color:#5a6072">no messages</div>';
  }).catch(()=>{ document.getElementById('meta').textContent='disconnected'; });
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""


OBS_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Now Playing</title>
<style>
  /* SUBTRACT-BLEND DESIGN
     Solid BLACK background + WHITE text. In OBS, right-click this Browser
     source -> Blending Mode -> Subtract. Black background subtracts nothing
     (your visuals show through); white letters subtract to black, so the
     text reads as clean black type over the video with no box.
     Adjust --size to scale all text at once. */
  :root { --size: 26px; }
  html,body { margin:0; background:#000000; }
  body { font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
         color:#ffffff; padding:24px; font-size:var(--size);
         line-height:1.25; font-weight:700; }
  .song { margin:0 0 22px 0; }
  .l1 { font-size:1em; }
  .l2 { font-size:0.66em; font-weight:600; }
  .l3 { font-size:0.56em; font-weight:500; overflow-wrap:anywhere; }
</style>
</head>
<body>
<div id="rows"></div>
<script>
function refresh(){
  fetch('/api/status').then(r=>r.json()).then(s=>{
    const root = document.getElementById('rows');
    root.innerHTML = '';
    let n = 0;
    s.lanes.forEach(L=>{
      if (L.paused || !L.url) return;        // only currently-playing songs
      n++;
      const pos = L.elapsed ? ('position ' + L.elapsed) : '';
      const srcTxt = L.source ? L.source.toUpperCase() : '';
      const line2 = [pos, srcTxt].filter(Boolean).join('     ');
      const el = document.createElement('div');
      el.className = 'song';
      el.innerHTML =
        '<div class="l1">' + n + ' playing song: ' + (L.title || '') + '</div>'
        + (line2 ? '<div class="l2">' + line2 + '</div>' : '')
        + '<div class="l3">' + L.url + '</div>';
      root.appendChild(el);
    });
  }).catch(()=>{});
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""


def make_control_handler(control: RadioControl):
    class ControlHandler(BaseHTTPRequestHandler):
        # Silence the default per-request stderr logging.
        def log_message(self, *args):
            pass

        def _send(self, body: str, content_type='text/html'):
            data = body.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', f'{content_type}; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj: dict):
            self._send(json.dumps(obj), 'application/json')

        def _qs_int(self, qs, key, default=None):
            try:
                return int(qs.get(key, [default])[0])
            except (TypeError, ValueError):
                return default

        def _route(self):
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path in ('/', '/index.html'):
                return self._send(CONTROL_PAGE)
            if path == '/obs':
                return self._send(OBS_PAGE)
            if path == '/api/status':
                return self._json(control.status())

            # ── actions (accept GET or POST for convenience) ────────────────
            if path == '/api/skip':
                control.skip(self._qs_int(qs, 'lane', 0))
            elif path == '/api/skip_all':
                control.skip_all()
            elif path == '/api/pause_toggle':
                control.toggle_pause(self._qs_int(qs, 'lane', 0))
            elif path == '/api/pause':
                control.set_pause(self._qs_int(qs, 'lane', 0), True)
            elif path == '/api/resume':
                control.set_pause(self._qs_int(qs, 'lane', 0), False)
            elif path == '/api/global_pause_toggle':
                control.toggle_global_pause()
            elif path == '/api/volume':
                control.set_volume(self._qs_int(qs, 'lane', 0),
                                   self._qs_int(qs, 'v', 100))
            elif path == '/api/pin':
                control.set_pin(self._qs_int(qs, 'lane', 0),
                                qs.get('source', [''])[0])
            else:
                self.send_response(404)
                self.end_headers()
                return
            return self._json({'ok': True})

        def do_GET(self):
            self._route()

        def do_POST(self):
            self._route()

    return ControlHandler


def get_lan_ip() -> Optional[str]:
    """Best-effort local network IP so you can reach the panel from a phone."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't actually send anything; just picks the outbound interface.
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        return ip if not ip.startswith('127.') else None
    except Exception:
        return None
    finally:
        s.close()


def start_control_server(control: RadioControl, port: int):
    """Launch the control/OBS web server in a background daemon thread."""
    try:
        handler = make_control_handler(control)
        httpd = ThreadingHTTPServer(('0.0.0.0', port), handler)
    except OSError as e:
        print(f'  ✗  Control server could not bind to port {port}: {e}')
        print(f'     (change CONFIG["control_port"] if the port is in use)')
        return None
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


# ══════════════════════════════════════════════════════════════════════════════
#  DEPENDENCY CHECK
# ══════════════════════════════════════════════════════════════════════════════

def check_deps() -> bool:
    ok = True
    for tool in ('ffplay', 'yt-dlp'):
        result = subprocess.run(['which', tool], capture_output=True)
        if result.returncode == 0:
            print(f'  ✓  {tool}')
        else:
            print(f'  ✗  {tool} not found — install with: brew install {tool}')
            ok = False

    # deno is optional but recent yt-dlp may need it to solve YouTube's JS
    # challenges — without it, YouTube tracks can silently fail to play.
    if subprocess.run(['which', 'deno'], capture_output=True).returncode == 0:
        print('  ✓  deno (YouTube JS solver)')
    else:
        print('  ⚠  deno not found — YouTube may fail. Install: brew install deno')
    return ok


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print()
    print('═' * 60)
    print('  ExternalRadio — multi-source audio mixer')
    print('═' * 60)
    print('Checking dependencies...')
    if not check_deps():
        sys.exit(1)

    print()
    log = SessionLog(CONFIG['log_dir'], CONFIG['log_rotate_hours'])
    router = SourceRouter(CONFIG)

    # Wait a moment for background source loaders to start
    time.sleep(2)

    # Shared control state (pause / skip / volume + status for the web UI)
    control = RadioControl()
    control.log = log

    # YouTube self-test — tells you up front whether YouTube will play.
    if CONFIG['sources']['youtube']['enabled']:
        print('Testing YouTube playback...')
        ok, msg = router.youtube_probe()
        if ok:
            print(f'  ✓  YouTube OK — {msg}')
        else:
            print(f'  ✗  YouTube NOT playable — {msg}')
            note(f'⚠ YouTube self-test failed: {msg[:90]}')
            if 'not a bot' in msg.lower() or 'sign in' in msg.lower():
                print('     YouTube flagged this IP — it needs cookies:')
                print('       1.  make sure Chrome is signed in to YouTube, then')
                print("           set CONFIG cookies_browser = 'chrome'  (default)")
                print('       2.  OR export youtube_cookies.txt next to radiot.py')
                print('           (browser extension: "Get cookies.txt LOCALLY")')
            else:
                print('     Most common fixes:')
                print('       1.  pip install -U yt-dlp      (update; "yt-dlp -U" fails on pip installs)')
                print('       2.  CONFIG remote_components = ejs:github  (solves the n-challenge)')
                print('       3.  brew install deno          (JS runtime for the solver)')
                print('       4.  put youtube_cookies.txt next to radiot.py')
                print("       5.  edit CONFIG player_client to 'web' or 'ios,tv'")

    print()
    n = CONFIG['lanes']
    pinned = CONFIG.get('pinned_lanes', {})
    print(f'Starting {n} lanes...')
    for i in sorted(pinned):
        if i < n:
            print(f'  📌  Lane {i + 1} pinned to {pinned[i].upper()}')
    lanes = [AudioLane(i, router, log, control, pin=pinned.get(i))
             for i in range(n)]
    control.lanes = lanes

    # ── Control panel + OBS overlay web server ──────────────────────────────
    port = CONFIG.get('control_port', 8080)
    httpd = start_control_server(control, port)
    lan_ip = get_lan_ip()
    if httpd:
        print(f'  ✓  Control panel : http://localhost:{port}/')
        print(f'  ✓  OBS overlay   : http://localhost:{port}/obs  '
              f'(add as a Browser Source)')
        if lan_ip:
            print(f'  📱  On your phone (same Wi-Fi): http://{lan_ip}:{port}/')

    def shutdown(sig, frame):
        print('\n\nShutting down...')
        for lane in lanes:
            lane.stop()
        if httpd:
            httpd.shutdown()
        log.finalize()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── Status display ────────────────────────────────────────────────────────
    try:
        while True:
            os.system('clear')
            print('═' * 60)
            print('  ExternalRadio   (Ctrl+C to stop)')
            print('═' * 60)
            for lane in lanes:
                print(f'  Lane {lane.lane_id + 1}: {lane.status}')
            print()
            if control.global_paused:
                print('  ⏸  ALL LANES PAUSED')
            if httpd:
                print(f'  Control panel : http://localhost:{port}/')
                print(f'  OBS overlay   : http://localhost:{port}/obs')
                if lan_ip:
                    print(f'  Phone (Wi-Fi) : http://{lan_ip}:{port}/')
            print(f'  Log: {log._path.name}')
            print(f'  Tracks logged: {len(log._entries)}')
            with _recent_lock:
                msgs = list(RECENT_MSGS)
            if msgs:
                print('  ── recent ──')
                for m in msgs[-5:]:
                    print(f'  {m}')
            time.sleep(5)
    except Exception:
        shutdown(None, None)


if __name__ == '__main__':
    main()
