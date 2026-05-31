# Newbold Radio

A multi-source experimental audio mixer for continuous broadcast sessions. Plays 3–6 simultaneous audio lanes drawn from YouTube, Archive.org, Alonetone, and Bandcamp — with EBU R128 loudness normalization, automatic lane watchdog, and 12-hour rotating session logs formatted as ready-to-paste video descriptions.

Built for and by [William Victor Newbold](https://xik6.bandcamp.com/) as part of an ongoing live audio/video broadcast practice.

---

## What It Does

- **Multi-lane playback** — 3–6 simultaneous streams via `ffplay`, each on its own thread
- **Four audio sources** — weighted random selection across your full catalog:
  - YouTube (weight 87) — 86,000+ videos via local CSV manifest
  - Archive.org (weight 20) — streaming via public API
  - Bandcamp (weight 13) — two-level `yt-dlp` crawl for individual track URLs
  - Alonetone (weight 5) — filtered to owner-only tracks at `cdn.alonetone.com`
- **EBU R128 loudness normalization** — all lanes balanced at –16 LUFS via ffmpeg `loudnorm`
- **Lane watchdog** — auto-restarts any lane that dies, preventing silence
- **12-hour log rotation** — session logs rotate every 12 hours without interrupting playback
- **Video description output** — each log block ends with a `VIDEO DESCRIPTION BLOCK` ready to paste into Bandcamp or YouTube uploads

---

## Architecture

```
SourceRouter (weighted random)
├── YouTube CSV        (87) → 86k+ videos
├── Archive.org API    (20) → 20k+ items
├── Alonetone scrape    (5) → 5k+ tracks
└── Bandcamp scrape    (13) → 1.3k tracks
        │
   Lane 1..N (ffplay subprocesses)
        │
   loudnorm filter (EBU R128, –16 LUFS)
        │
   BlackHole 2ch  ──→  OBS / video capture
        │
   Session Log  (~/newbold-radio/logs/block_NNN_TSTAMP.txt)
```

---

## Requirements

- macOS (tested on Mac mini M2 and MacBook Pro)
- Python 3.9+
- `ffmpeg` / `ffplay`
- `yt-dlp`
- BlackHole 2ch (for silent audio routing to video capture)

See `requirements.txt` for Python dependencies.

Install system tools via Homebrew:

```bash
brew install ffmpeg yt-dlp
```

Install BlackHole 2ch from [existential.audio](https://existential.audio/blackhole/).

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/newbold-radio.git
cd newbold-radio
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. YouTube cookies (required for YouTube playback)

Export your YouTube cookies from Safari using the [Cookie-Editor](https://cookie-editor.com/) extension and save them to:

```
~/newbold-radio/youtube_cookies.txt
```

> ⚠️ **Never commit `youtube_cookies.txt` to git.** It is listed in `.gitignore`.

### 4. Run

```bash
python3 radiot.py
```

Playback starts immediately across all lanes. Press `Ctrl+C` to stop cleanly (logs are finalized on exit).

---

## YouTube Video Manifest

`youtube_videos.csv` contains the full index of 86,742 YouTube videos across six channels. All videos are publicly available. The CSV must live in the **same folder as `newbold-radio.py`** — the script loads it on startup for random selection.

Columns: `Title`, `YouTube_URL`

> This snapshot is from February 2026. It will be periodically updated as new videos are uploaded.

---

## Session Logs & Video Descriptions

Logs rotate every 12 hours into:

```
~/newbold-radio/logs/block_NNN_TIMESTAMP.txt
```

Each log ends with a `VIDEO DESCRIPTION BLOCK` — a formatted tracklist suitable for pasting directly into a Bandcamp or YouTube upload description.

```bash
# View the most recent block
cat ~/newbold-radio/logs/block_001_*.txt
```

---

## Configuration

Key parameters at the top of `radiot.py`:

| Variable | Default | Description |
|---|---|---|
| `LANES` | `4` | Number of simultaneous audio lanes |
| `LOG_DIR` | `~/newbold-radio/logs/` | Session log output directory |
| `LOUDNORM_TARGET` | `-16` | EBU R128 target LUFS |
| `SOURCE_WEIGHTS` | `{yt:87, arc:20, bc:13, al:5}` | Relative source selection weights |
| `LOG_ROTATE_HOURS` | `12` | Hours per log block |

---

## Files

```
newbold-radio.py                   # Main script
patch_radiot.py                    # In-place patcher (applies fixes to existing installs)
external_radio_setup.py            # Dependency validator and source tester
youtube_videos.csv                 # YouTube video index (86,742 videos, Feb 2026)
requirements.txt
README.md
.gitignore
```

---

## License

Released as-is, no warranty. Do what you want with it.

---

*Part of the [xik6](https://xik6.bandcamp.com/) broadcast practice.*
