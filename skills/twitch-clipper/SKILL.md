---
name: twitch-clipper
description: "Automatically clip highlights from Twitch live streams, YouTube Live, or finished YouTube videos into vertical 9:16 MP4s ready for Instagram Reels and YouTube Shorts. Use when the user wants automatic highlight detection and short social-ready clips from a stream or video."
---

# Live Stream Clipper (Twitch + YouTube → Reels/Shorts)

Record a rolling window of a **Twitch** or **YouTube Live** stream, detect loud/action-heavy moments automatically, and cut **30-second vertical 1080×1920 MP4s** ready to post to Instagram Reels and YouTube Shorts.

## When Not to Use

- The user wants clips hosted by the platform (use Twitch's Clip button or YouTube's Clip feature instead).
- The source is Kick / TikTok / a local file (the `analyze` command still works on a local file).
- The user does not own the stream and has no permission to record it. **Do not use this to re-record someone else's broadcast for reposting.**
- Anything involving bypassing auth, ads, geo-blocks, or DRM.

## Boundaries

- Only streams the user owns or is explicitly allowed to record.
- Never ask for or store Twitch/YouTube credentials in chat; set `TWITCH_OAUTH_TOKEN` or a cookies file in the user's own shell.
- Clips are written locally to `--out/clips`. Do not publish or upload them unless the user asks.
- Detection is heuristic: tell the user to review clips before posting.

## Default Output: Social-Ready

The default `social` preset is tuned for cross-posting:

| Setting | Default | Why |
| --- | --- | --- |
| Duration | **30 s** (`--pre 5`, `--post 25`) | 15–30 s is the safe overlap for Reels + Shorts; gaming highlights also sit in the 15–45 s range. The highlight lands near the front so the hook arrives fast. |
| Aspect | **9:16, 1080×1920** | Required by Reels and Shorts for a full-screen vertical video. |
| Framerate | 30 fps | Standard for vertical feeds. |
| Video/audio | H.264 MP4 / AAC 160 kb/s | Universally accepted by both platforms. |
| Layout | Full frame on a blurred background | Keeps the gameplay HUD visible instead of center-cropping it away. |

Limits to remember: YouTube Shorts cap at 3 minutes; Instagram allows longer uploads but Reels over 3 minutes are not recommended to non-followers, so stay under 3 minutes for reach. See [platform length comparison](https://alignedvisionmedia.co.uk/blog/video-sizes-and-lengths-for-reels-shorts-and-youtube/) and [Shorts length guide](https://adcreate.com/blog/youtube-shorts-length-guide-2026).

## Setup

```bash
python3 -m pip install streamlink yt-dlp
# ffmpeg (choose one):
#   macOS:  brew install ffmpeg
#   Ubuntu: sudo apt install ffmpeg
#   pip:    python3 -m pip install imageio-ffmpeg
```

Check dependencies:

```bash
python3 skills/twitch-clipper/scripts/twitch_clip.py doctor
```

If ffmpeg came from `imageio-ffmpeg`:

```bash
export CLIP_FFMPEG="$(python3 -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
```

Optional credentials (set locally, never in chat or a committed file):

```bash
export TWITCH_OAUTH_TOKEN='oauth:...'   # Twitch, for your own channel / reliability
# YouTube members-only or age-gated streams:
python3 ... watch --platform youtube --url VIDEO_ID --cookies /path/to/cookies.txt
```

## Quick Start — Twitch

```bash
python3 skills/twitch-clipper/scripts/twitch_clip.py watch \
  --platform twitch \
  --channel YOUR_TWITCH_CHANNEL \
  --out gta6-clips
```

## Quick Start — YouTube Live

```bash
python3 skills/twitch-clipper/scripts/twitch_clip.py watch \
  --platform youtube \
  --url 'https://www.youtube.com/watch?v=VIDEO_ID' \
  --out gta6-clips
```

`--url` also accepts a bare video ID (`VIDEO_ID`), `youtube:VIDEO_ID`, or a `youtu.be/...` link. `--platform` defaults to `auto`.

## Quick Start — Finished YouTube Video (VOD)

If the YouTube link is a normal uploaded video rather than a live stream, use `vod` instead of `watch`. It downloads the video, scores the whole file against a global audio baseline, keeps the best non-overlapping moments, and renders the same vertical 30 s MP4s:

```bash
python3 skills/twitch-clipper/scripts/twitch_clip.py vod \
  'https://www.youtube.com/watch?v=VIDEO_ID' \
  --out clips-youtube \
  --max-clips 10 \
  --threshold 0.75
```

A local file works too:

```bash
python3 skills/twitch-clipper/scripts/twitch_clip.py vod /path/to/video.mp4 --out clips-youtube
```

`--max-clips` (default 10) caps how many clips are rendered; raise `--threshold` for fewer/bigger moments or lower it to `0.6` for more.

Press Ctrl-C to stop a `watch` session. Outputs:

```
gta6-clips/
├── buffer/                # rolling .ts segments (pruned automatically)
├── clips/                 # 9:16 MP4s + .json metadata
└── state.json             # segment timings, detected events, cut clips
```

Each clip's `.json` records `platform`, `source_url`, `preset`, `aspect`, `target_platforms`, score and reasons. The `.json` files are gitignored in this repo; keep large MP4 folders out of commits.

## Tuning

| Flag | Default | Effect |
| --- | --- | --- |
| `--preset social` | on | 30 s vertical clips for Reels + Shorts. |
| `--preset raw` | off | Original aspect ratio, 15 s before / 20 s after the highlight. |
| `--clip-seconds N` | — | Total length override, e.g. `--clip-seconds 20` (5 s lead-in + 15 s payoff). |
| `--pre` / `--post` | `5` / `25` | Manual timing; overrides preset defaults. |
| `--aspect original` | — | Keep the source aspect ratio instead of 9:16. |
| `--threshold` | `0.65` | Lower → more clips, more false positives. Raise → only big moments. |
| `--cooldown` | `90` | Minimum seconds between clips; raise if one moment produces several. |
| `--buffer-minutes` | `20` | How much past video is kept on disk. |
| `--segment-seconds` | `30` | Recording granularity; smaller = tighter cuts, more files. |
| `--quality` | `best` | Twitch streamlink quality. |
| `--youtube-format` | `best[ext=mp4]/best` | yt-dlp format selector for YouTube. |
| `--prefix` | `gta6` | Filename prefix for saved clips. |

Test thresholds before a long session:

```bash
python3 skills/twitch-clipper/scripts/twitch_clip.py analyze path/to/segment.ts
python3 skills/twitch-clipper/scripts/twitch_clip.py selftest
```

## How It Works

1. `streamlink --stdout` (Twitch) or `yt-dlp -o -` (YouTube Live) feeds the live stream to ffmpeg, which writes keyframe-aligned 30 s MPEG-TS segments.
2. Old segments are pruned to keep only the rolling window (plus pending events).
3. Each finished segment is scored from 1) audio RMS spikes vs. that segment's baseline, 2) peak loudness, and 3) visual scene-change rate. The loudest moment becomes the clip anchor.
4. After `--post` seconds of extra footage exist in the buffer, ffmpeg re-encodes `[anchor - pre, anchor + post]` into a vertical 1080×1920 MP4 and writes a `.json` with score and reasons.
5. `state.json` makes the run resumable: restarting `watch` continues with the existing buffer.

## Troubleshooting

- **No audio in clips** — try `--youtube-format best` or a different `--quality` on Twitch.
- **Too many boring clips** — raise `--threshold` to `0.75`–`0.85` and increase `--cooldown`.
- **No clips at all** — lower `--threshold` to `0.5`; check that the stream has audio and that `analyze` reports a nonzero score.
- **Black bars look wrong** — that's the blurred background keeping the full gameplay frame; use `--aspect original` if you want the raw 16:9 instead.
- **YouTube: "Sign in to confirm you're not a bot" / members-only** — pass `--cookies /path/to/cookies.txt` from your own logged-in browser session.
- **YouTube: format unavailable** — try `--youtube-format best`.
- **Stream is offline / protected** — the script cannot bypass that; use the broadcaster's own stream or authorized cookies.
