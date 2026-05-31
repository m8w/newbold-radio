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
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

CONFIG = {
    'lanes': 4,
    'log_dir': Path.home() / 'ExternalRadio' / 'logs',
    'log_rotate_hours': 12,

    'sources': {
        'youtube': {
            'enabled': True,
            'weight': 87,
            # --- Try these paths in order; first one that exists wins ---
            'csv_paths': [
                Path.home() / 'music' / 'youtube_videos.csv',
                Path.home() / 'ExternalRadio' / 'youtube_videos.csv',
                Path.home() / 'ExternalRadio' / 'youtube_videos_gdrive.csv',
                Path.home() / 'music' / 'Newbold_Archive_Manifest_2026-02-13.csv',
                Path.home() / 'Documents' / 'youtube_videos.csv',
            ],
            'cookies_file': Path.home() / 'ExternalRadio' / 'youtube_cookies.txt',
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
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._start_new_block()

    def _start_new_block(self):
        self._block_start = datetime.now()
        ts = self._block_start.strftime('%Y%m%d_%H%M%S')
        self._path = self.log_dir / f'block_{ts}.txt'
        self._entries: List[str] = []
        with open(self._path, 'w') as f:
            f.write(f'ExternalRadio session — {self._block_start.strftime("%Y-%m-%d %H:%M")}\n')
            f.write('=' * 60 + '\n\n')

    def log(self, source: str, title: str, url: str):
        now = datetime.now()
        # Rotate if needed
        if (now - self._block_start).total_seconds() > self.rotate_hours * 3600:
            self._finalize_block()
            self._start_new_block()

        entry = f'[{now.strftime("%H:%M:%S")}] [{source.upper()}] {title}\n  {url}\n'
        with self._lock:
            self._entries.append(entry)
            with open(self._path, 'a') as f:
                f.write(entry)

    def _finalize_block(self):
        with open(self._path, 'a') as f:
            f.write(f'\n\n— end of block ({len(self._entries)} tracks) —\n')

    def finalize(self):
        self._finalize_block()
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
        cookies = self.cfg.get('cookies_file')
        opts = list(CONFIG['ytdlp_opts'])
        if cookies and Path(cookies).exists():
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
        # Pick a random item and get a random audio file from it
        title, base_url = random.choice(self._catalog)
        identifier = base_url.split('/download/')[-1]
        try:
            resp = requests.get(f'https://archive.org/metadata/{identifier}', timeout=15)
            meta = resp.json()
            files = [f for f in meta.get('files', [])
                     if f.get('format', '').lower() in ('mp3', 'ogg vorbis', 'flac', 'vbr mp3')]
            if files:
                chosen = random.choice(files)
                url = f'https://archive.org/download/{identifier}/{chosen["name"]}'
                fname = chosen.get('title') or chosen['name']
                return ('archive', f'{title} / {fname}', url)
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
    name = 'bandcamp'

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._catalog: List[Tuple[str, str]] = []
        self._loaded = False

    def load(self):
        for bc_url in self.cfg['urls']:
            try:
                cmd = ['yt-dlp', '--flat-playlist', '--print', '%(title)s\t%(url)s',
                       '--quiet', '--no-warnings', bc_url]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
                for line in result.stdout.strip().splitlines():
                    parts = line.split('\t', 1)
                    if len(parts) == 2:
                        title, track_url = parts
                        if track_url.startswith('http'):
                            self._catalog.append((title.strip(), track_url.strip()))
            except Exception as e:
                print(f'  ✗  Bandcamp {bc_url} failed: {e}')
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

    def get_stream_url(self, source: str, url: str) -> Optional[str]:
        """Resolve playback URL for sources that need it (YouTube, Bandcamp)."""
        if source in ('youtube', 'bandcamp'):
            return self._yt.get_stream_url(url)  # yt-dlp works for both
        return url  # archive, alonetone: direct URL


# ══════════════════════════════════════════════════════════════════════════════
#  AUDIO LANE
# ══════════════════════════════════════════════════════════════════════════════

class AudioLane:
    def __init__(self, lane_id: int, router: SourceRouter, log: SessionLog):
        self.lane_id = lane_id
        self.router = router
        self.log = log
        self._proc: Optional[subprocess.Popen] = None
        self._current_title = '—'
        self._current_source = '—'
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._running:
            track = self.router.pick_random()
            if not track:
                time.sleep(5)
                continue

            source, title, url = track

            stream_url = self.router.get_stream_url(source, url)
            if not stream_url:
                continue

            self._current_source = source
            self._current_title = title[:60]
            self.log.log(source, title, url)

            cmd = ['ffplay'] + CONFIG['ffplay_opts'] + [stream_url]
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._proc.wait()
            except Exception:
                pass
            finally:
                self._proc = None

            # Brief gap between tracks
            time.sleep(random.uniform(0.5, 2.0))

    def stop(self):
        self._running = False
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass

    @property
    def status(self) -> str:
        return f'[{self._current_source.upper()[:3]}] {self._current_title}'


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

    print()
    n = CONFIG['lanes']
    print(f'Starting {n} lanes...')
    lanes = [AudioLane(i, router, log) for i in range(n)]

    def shutdown(sig, frame):
        print('\n\nShutting down...')
        for lane in lanes:
            lane.stop()
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
            for i, lane in enumerate(lanes):
                print(f'  Lane {i+1}: {lane.status}')
            print()
            print(f'  Log: {log._path.name}')
            print(f'  Tracks logged: {len(log._entries)}')
            time.sleep(5)
    except Exception:
        shutdown(None, None)


if __name__ == '__main__':
    main()
