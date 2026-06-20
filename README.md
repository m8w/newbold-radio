# Newbold Radio

A multi-source experimental audio mixer for continuous broadcast sessions. Plays 3–6 simultaneous audio lanes drawn from YouTube, Archive.org, Alonetone, and Bandcamp — with EBU R128 loudness normalization, automatic lane watchdog, and 12-hour rotating session logs formatted as ready-to-paste video descriptions.

Built for and by [William Victor Newbold](https://xik6.bandcamp.com/) as part of an ongoing live audio/video broadcast practice.

---

## What It Does

- **Live control panel** — browser-based mute / change-song / volume controls for every lane (see below)
- **OBS "Now Playing" overlay** — a transparent browser-source page listing each lane's song, source, and where it lives on the internet
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

Export your YouTube cookies from Safari using the [Cookie-Editor](https://cookie-editor.com/) extension and save them **right next to `radiot.py`**:

```
<repo folder>/youtube_cookies.txt
```

The script auto-detects it there — no config editing. (It also still checks `~/ExternalRadio/youtube_cookies.txt` as a fallback for older setups.)

> ⚠️ **Never commit `youtube_cookies.txt` to git.** It is listed in `.gitignore`.

### 4. Run

```bash
python3 radiot.py
```

Playback starts immediately across all lanes, and the control panel + OBS
overlay server come up at `http://localhost:8080/`. Press `Ctrl+C` to stop
cleanly (logs are finalized on exit).

---

## YouTube Video Manifest

`youtube_videos.csv` contains the full index of 86,742 YouTube videos across six channels. All videos are publicly available. Just drop the CSV in the **same folder as `radiot.py`** and it's loaded automatically on startup — no config editing. (Legacy locations like `~/music/` and `~/ExternalRadio/` are still checked as fallbacks.)

Columns: `Title`, `YouTube_URL`

> This snapshot is from February 2026. It will be periodically updated as new videos are uploaded.

---

## Live Control Panel

When the radio starts it also launches a tiny local web server (no extra
dependencies — Python stdlib only). Open the control panel in any browser on
the same machine:

```
http://localhost:8080/
```

From the panel you can, per lane:

- **Change Song ⏭** — skip the current track and jump to a new one
- **Pause / Resume** — *true* pause: freezes the player so the **same song
  continues from where it left off** when you resume (it does not start a new track)
- **Source pin** — a dropdown to lock the lane to one source (YouTube / Archive /
  Bandcamp / Alonetone) or set it back to 🎲 Random; takes effect immediately
- **Volume** — set the lane level (applies to the next track that starts)

…plus global controls in the top bar:

- **Pause All** — instantly freeze every lane (handy to drop the soundtrack on cue);
  **Resume All** continues every song exactly where it paused
- **Skip All ⏭** — reroll every lane at once

The status auto-refreshes every couple of seconds, so the panel always reflects
what's actually playing.

> The port is configurable via `CONFIG['control_port']` near the top of
> `radiot.py` (default `8080`). The server binds to `0.0.0.0`, so you can
> also reach it from another device on your LAN at `http://<this-mac-ip>:8080/`.

## OBS "Now Playing" Overlay

Add a live on-screen tracklist to your broadcast — it lists each lane's song
title, its source (YouTube / Archive / Bandcamp / Alonetone), and the URL where
the track lives on the internet.

In OBS:

1. **Sources → + → Browser**
2. Set the URL to:

   ```
   http://localhost:8080/obs
   ```

3. Set the width/height to taste (e.g. 760 × 400) and check **"Shutdown source
   when not visible"** if you like.

The overlay has a transparent background, so it composites cleanly over your
video. Muted lanes are automatically hidden from the overlay. It refreshes
every few seconds on its own — no need to reload the source.

## Troubleshooting YouTube

If YouTube tracks flash by every few seconds without playing, `yt-dlp` is
failing to fetch the audio. The app now tells you why:

- On startup it runs a **YouTube self-test** and prints the exact error.
- Failing tracks no longer spin — the lane backs off and the reason appears in
  the terminal "recent" panel and at the bottom of the control panel.

Most common fixes, in order:

1. Update yt-dlp. If it was installed with pip (common with Anaconda), run
   `pip install -U yt-dlp` — `yt-dlp -U` fails on pip installs.
2. The "n" signature challenge: yt-dlp needs its EJS solver script. The app
   passes `--remote-components ejs:github` for you (see
   `CONFIG['sources']['youtube']['remote_components']`). Without it the audio
   URL is throttled and playback dies after a few seconds.
3. `brew install deno` — the JavaScript runtime the solver uses.
4. Public videos need no cookies. The app does **not** read browser cookies by
   default (macOS blocks reading Safari's store — "Operation not permitted").
   If a video is private/age-gated, drop a `youtube_cookies.txt` next to
   `radiot.py`, or set `CONFIG['sources']['youtube']['cookies_browser']` to a
   browser yt-dlp can read (e.g. `'chrome'`).
5. In `CONFIG['sources']['youtube']`, change `'player_client'` to `'web'` or
   `'ios,tv'` if the default stops working.

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
radiot.py                   # Main script
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
