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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

# Folder this script lives in. Files placed right next to radiot.py
# (youtube_videos.csv, youtube_cookies.txt) are found automatically —
# no matter where you clone the repo. This is what makes it "just work."
SCRIPT_DIR = Path(__file__).resolve().parent


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
    """
    One playback lane. Plays random tracks back-to-back via ffplay.

    Live controls (driven by the control panel / OBS overlay server):
      • mute / unmute  — silences this lane immediately (stops playback;
                          resumes with a fresh track when unmuted)
      • skip           — jumps to the next random track right now
      • volume (0–100) — applied to the next track that starts
    """

    def __init__(self, lane_id: int, router: SourceRouter, log: SessionLog,
                 control: 'RadioControl'):
        self.lane_id = lane_id
        self.router = router
        self.log = log
        self.control = control          # shared state (global mute, etc.)
        self._proc: Optional[subprocess.Popen] = None
        self._current_title = '—'
        self._current_source = '—'
        self._current_url = ''
        self._started_at: Optional[datetime] = None
        self.muted = False
        self.volume = 100               # 0–100, applied on next track
        self._skip = threading.Event()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ── control helpers ──────────────────────────────────────────────────────
    def _silenced(self) -> bool:
        return self.muted or self.control.global_muted

    def _kill_proc(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self._proc = None

    def skip(self):
        """Jump to the next track immediately."""
        self._skip.set()
        self._kill_proc()

    def set_muted(self, value: bool):
        self.muted = bool(value)
        if self.muted:
            self._kill_proc()

    def set_volume(self, value: int):
        self.volume = max(0, min(100, int(value)))

    # ── playback loop ────────────────────────────────────────────────────────
    def _loop(self):
        while self._running:
            # Hold here while muted (lane-level or global) — no audio out.
            if self._silenced():
                self._kill_proc()
                self._current_source = '—'
                self._current_title = '(muted)'
                self._current_url = ''
                self._started_at = None
                time.sleep(0.3)
                continue

            track = self.router.pick_random()
            if not track:
                time.sleep(5)
                continue

            source, title, url = track

            stream_url = self.router.get_stream_url(source, url)
            if not stream_url:
                continue

            self._current_source = source
            self._current_title = title[:80]
            self._current_url = url
            self._started_at = datetime.now()
            self.log.log(source, title, url)

            af = f'volume={self.volume / 100:.2f}'
            cmd = ['ffplay'] + CONFIG['ffplay_opts'] + ['-af', af, stream_url]
            self._skip.clear()
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                # Poll so mute / skip / shutdown can interrupt mid-track.
                while self._running:
                    if self._proc.poll() is not None:
                        break               # track finished naturally
                    if self._skip.is_set() or self._silenced():
                        break               # user skipped or muted
                    time.sleep(0.3)
            except Exception:
                pass
            finally:
                self._kill_proc()

            # Brief gap between tracks (skip the wait if user is skipping).
            if not self._skip.is_set():
                time.sleep(random.uniform(0.5, 2.0))

    def stop(self):
        self._running = False
        self._kill_proc()

    @property
    def status(self) -> str:
        tag = 'MUTE' if self._silenced() else self._current_source.upper()[:4]
        return f'[{tag}] {self._current_title}'

    def info(self) -> dict:
        """Snapshot for the control panel / OBS overlay."""
        elapsed = ''
        if self._started_at and not self._silenced():
            secs = int((datetime.now() - self._started_at).total_seconds())
            elapsed = f'{secs // 60:02d}:{secs % 60:02d}'
        return {
            'lane': self.lane_id + 1,
            'source': self._current_source,
            'title': self._current_title,
            'url': self._current_url,
            'muted': self._silenced(),
            'lane_muted': self.muted,
            'volume': self.volume,
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
        self.global_muted = False
        self.log: Optional[SessionLog] = None
        self.started_at = datetime.now()

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

    def set_mute(self, lane_num: int, value: bool):
        lane = self._lane(lane_num)
        if lane:
            lane.set_muted(value)

    def toggle_mute(self, lane_num: int):
        lane = self._lane(lane_num)
        if lane:
            lane.set_muted(not lane.muted)

    def set_global_mute(self, value: bool):
        self.global_muted = bool(value)

    def toggle_global_mute(self):
        self.global_muted = not self.global_muted

    def set_volume(self, lane_num: int, value: int):
        lane = self._lane(lane_num)
        if lane:
            lane.set_volume(value)

    # ── status snapshot ──────────────────────────────────────────────────────
    def status(self) -> dict:
        secs = int((datetime.now() - self.started_at).total_seconds())
        uptime = f'{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}'
        return {
            'global_muted': self.global_muted,
            'uptime': uptime,
            'tracks_logged': len(self.log._entries) if self.log else 0,
            'lanes': [lane.info() for lane in self.lanes],
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
  .lane.muted { opacity:.55; border-color:#5a2730; }
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
  input[type=range]{ flex:1; accent-color:#5aa6ff; }
  .obs-link { font-size:12px; color:#8a8f9c; }
  .obs-link code { background:#202533; padding:2px 6px; border-radius:4px;
                   color:#9fd0ff; }
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
  <button class="danger" id="muteall" onclick="act('/api/global_mute_toggle')">Mute All</button>
  <button onclick="act('/api/skip_all')">Skip All ⏭</button>
</div>
<main id="lanes"></main>

<script>
function act(path){ fetch(path,{method:'POST'}).then(refresh); }
function setvol(lane,v){ fetch('/api/volume?lane='+lane+'&v='+v,{method:'POST'}); }

function refresh(){
  fetch('/api/status').then(r=>r.json()).then(s=>{
    document.getElementById('obsurl').textContent =
        location.origin + '/obs';
    document.getElementById('meta').textContent =
        'uptime ' + s.uptime + '  ·  ' + s.tracks_logged + ' tracks logged'
        + (s.global_muted ? '  ·  ALL MUTED' : '');
    const mb = document.getElementById('muteall');
    mb.classList.toggle('on', s.global_muted);
    mb.textContent = s.global_muted ? 'Unmute All' : 'Mute All';

    const root = document.getElementById('lanes');
    root.innerHTML = '';
    s.lanes.forEach(L=>{
      const src = (L.source||'—').toLowerCase();
      const muted = L.muted;
      const urlHtml = L.url
        ? '<a href="'+L.url+'" target="_blank" rel="noopener">'+L.url+'</a>'
        : '—';
      const el = document.createElement('div');
      el.className = 'lane' + (muted ? ' muted':'');
      el.innerHTML =
        '<div class="lanehead">'
        +  '<strong>Lane '+L.lane+'</strong>'
        +  '<span class="badge '+src+'">'+(L.source||'—').toUpperCase()+'</span>'
        +'</div>'
        +'<div class="title">'+(L.title||'—')+'</div>'
        +'<div class="url">'+urlHtml+'</div>'
        +'<div class="elapsed">'+(muted?'muted':('▶ '+(L.elapsed||'')))+'</div>'
        +'<div class="controls">'
        +  '<button class="primary" onclick="act(\\'/api/skip?lane='+L.lane+'\\')">Change Song ⏭</button>'
        +  '<button class="'+(L.lane_muted?'on':'')+'" onclick="act(\\'/api/mute_toggle?lane='+L.lane+'\\')">'
        +     (L.lane_muted?'Unmute':'Mute')+'</button>'
        +'</div>'
        +'<div class="vol">Vol'
        +  '<input type="range" min="0" max="100" value="'+L.volume+'" '
        +     'onchange="setvol('+L.lane+',this.value)">'
        +  '<span>'+L.volume+'%</span>'
        +'</div>';
      root.appendChild(el);
    });
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
  /* Transparent background so it composites cleanly over video in OBS. */
  html,body { margin:0; background:transparent; }
  body { font-family:-apple-system,Segoe UI,Roboto,Helvetica,sans-serif;
         color:#fff; padding:18px; }
  .wrap { display:inline-block; background:rgba(8,9,13,0.62);
          backdrop-filter:blur(4px); border-radius:14px; padding:16px 20px;
          max-width:720px; }
  .head { font-size:13px; letter-spacing:2px; text-transform:uppercase;
          color:#9fd0ff; margin-bottom:10px; font-weight:700; }
  .row { display:flex; align-items:baseline; gap:10px; padding:5px 0;
         border-top:1px solid rgba(255,255,255,.08); }
  .row:first-of-type { border-top:none; }
  .tag { font-size:11px; font-weight:700; letter-spacing:.5px; padding:2px 7px;
         border-radius:5px; background:rgba(255,255,255,.14);
         white-space:nowrap; }
  .tag.youtube{background:rgba(255,80,80,.30)}
  .tag.archive{background:rgba(90,166,255,.30)}
  .tag.bandcamp{background:rgba(63,208,216,.28)}
  .tag.alonetone{background:rgba(183,139,255,.30)}
  .info { display:flex; flex-direction:column; }
  .t { font-size:16px; font-weight:600; text-shadow:0 1px 3px rgba(0,0,0,.7); }
  .u { font-size:12px; color:#bcd6ff; text-shadow:0 1px 3px rgba(0,0,0,.7);
       overflow-wrap:anywhere; }
  .row.muted { opacity:.4; }
</style>
</head>
<body>
<div class="wrap">
  <div class="head">♫ ExternalRadio — Now Playing</div>
  <div id="rows"></div>
</div>
<script>
function refresh(){
  fetch('/api/status').then(r=>r.json()).then(s=>{
    const root = document.getElementById('rows');
    root.innerHTML = '';
    s.lanes.forEach(L=>{
      if (L.muted) return;                 // hide silenced lanes from the overlay
      const src = (L.source||'').toLowerCase();
      const el = document.createElement('div');
      el.className = 'row';
      el.innerHTML =
        '<span class="tag '+src+'">'+(L.source||'—').toUpperCase()+'</span>'
        +'<div class="info">'
        +  '<span class="t">'+(L.title||'—')+'</span>'
        +  '<span class="u">'+(L.url||'')+'</span>'
        +'</div>';
      root.appendChild(el);
    });
  }).catch(()=>{});
}
refresh();
setInterval(refresh, 3000);
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
            elif path == '/api/mute_toggle':
                control.toggle_mute(self._qs_int(qs, 'lane', 0))
            elif path == '/api/mute':
                control.set_mute(self._qs_int(qs, 'lane', 0), True)
            elif path == '/api/unmute':
                control.set_mute(self._qs_int(qs, 'lane', 0), False)
            elif path == '/api/global_mute_toggle':
                control.toggle_global_mute()
            elif path == '/api/volume':
                control.set_volume(self._qs_int(qs, 'lane', 0),
                                   self._qs_int(qs, 'v', 100))
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

    # Shared control state (mute / skip / volume + status for the web UI)
    control = RadioControl()
    control.log = log

    print()
    n = CONFIG['lanes']
    print(f'Starting {n} lanes...')
    lanes = [AudioLane(i, router, log, control) for i in range(n)]
    control.lanes = lanes

    # ── Control panel + OBS overlay web server ──────────────────────────────
    port = CONFIG.get('control_port', 8080)
    httpd = start_control_server(control, port)
    if httpd:
        print(f'  ✓  Control panel : http://localhost:{port}/')
        print(f'  ✓  OBS overlay   : http://localhost:{port}/obs  '
              f'(add as a Browser Source)')

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
            if control.global_muted:
                print('  ⚠  ALL LANES MUTED')
            if httpd:
                print(f'  Control panel : http://localhost:{port}/')
                print(f'  OBS overlay   : http://localhost:{port}/obs')
            print(f'  Log: {log._path.name}')
            print(f'  Tracks logged: {len(log._entries)}')
            time.sleep(5)
    except Exception:
        shutdown(None, None)


if __name__ == '__main__':
    main()
