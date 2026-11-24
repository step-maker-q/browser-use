#!/usr/bin/env python3
"""Rolling-buffer Twitch clipper with audio/visual highlight detection.

Only record and clip streams you own or have explicit permission to record.
The script never bypasses authentication, ads, or geo restrictions.

Subcommands:
  doctor     check ffmpeg/streamlink/yt-dlp availability
  watch      record a rolling buffer from Twitch or YouTube Live and auto-cut MP4s
  vod        download/analyze a finished YouTube video (or local file) and cut its best moments
  analyze    print the highlight score for a local media file
  selftest   generate a synthetic clip and verify analyze + cut end to end
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd: list[str], *, capture: bool = True, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, text=True, capture_output=capture)
    if check and result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(cmd)}\n{(result.stderr or '')[-2000:]}")
    return result


def find_bin(name: str, override: str | None = None, imageio_fallback: bool = True) -> str | None:
    if override:
        return override
    found = shutil.which(name)
    if found:
        return found
    if imageio_fallback and name == "ffmpeg":
        try:
            import imageio_ffmpeg  # type: ignore

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    return None


def detect_platform(args: argparse.Namespace) -> str:
    requested = getattr(args, "platform", "auto")
    if requested and requested != "auto":
        return requested
    raw = (getattr(args, "url", "") or getattr(args, "channel", "") or "").strip().lower()
    if "youtube" in raw or "youtu.be" in raw or raw.startswith("yt:") or raw.startswith("youtube:"):
        return "youtube"
    return "twitch"


def normalize_youtube_url(value: str) -> str:
    value = value.strip()
    if value.startswith(("http://", "https://")):
        return value
    for prefix in ("youtube:", "yt:"):
        if value.lower().startswith(prefix):
            value = value[len(prefix) :]
            break
    return f"https://www.youtube.com/watch?v={value}"


def ffprobe_duration(path: Path, ffprobe: str | None) -> float:
    if ffprobe:
        out = run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)]
        ).stdout.strip()
        try:
            return float(out)
        except ValueError:
            pass
    # Fallback: parse ffmpeg's "Duration:" line.
    ffmpeg = os.environ.get("CLIP_FFMPEG")
    if ffmpeg:
        out = run([ffmpeg, "-hide_banner", "-i", str(path)], capture=True, check=False).stderr
        for line in out.splitlines():
            if "Duration:" in line:
                clock = line.split("Duration:")[1].split(",")[0].strip()
                h, m, s = clock.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
    return 0.0


@dataclass
class Metrics:
    rms_bins: list[tuple[float, float]] = field(default_factory=list)
    scene_times: list[float] = field(default_factory=list)
    duration: float = 0.0

    @property
    def median_rms(self) -> float:
        vals = [v for _, v in self.rms_bins if math.isfinite(v)]
        return statistics.median(vals) if vals else -90.0

    @property
    def peak_rms(self) -> float:
        vals = [v for _, v in self.rms_bins if math.isfinite(v)]
        return max(vals) if vals else -90.0

    @property
    def p95_rms(self) -> float:
        vals = sorted(v for _, v in self.rms_bins if math.isfinite(v))
        if not vals:
            return -90.0
        return vals[min(len(vals) - 1, int(len(vals) * 0.95))]

    @property
    def loudest_time(self) -> float:
        if not self.rms_bins:
            return 0.0
        return max(self.rms_bins, key=lambda p: p[1])[0]


def analyze_file(path: Path, ffmpeg: str) -> Metrics:
    """Extract per-audio-frame RMS and visual scene-change timestamps."""
    metrics = Metrics(duration=ffprobe_duration(path, os.environ.get("CLIP_FFPROBE")))

    audio_cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-map",
        "0:a:0?",
        "-af",
        "astats=metadata=1:reset=1,ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-",
        "-f",
        "null",
        "-",
    ]
    audio_out = run(audio_cmd, check=False).stdout
    current_time = 0.0
    for line in audio_out.splitlines():
        line = line.strip()
        if line.startswith("lavfi.astats.Overall.RMS_level="):
            try:
                metrics.rms_bins.append((current_time, float(line.split("=", 1)[1])))
            except ValueError:
                pass
        elif "pts_time:" in line:
            try:
                current_time = float(line.split("pts_time:", 1)[1].split()[0])
            except ValueError:
                pass

    video_cmd = [
        ffmpeg,
        "-hide_banner",
        "-i",
        str(path),
        "-map",
        "0:v:0?",
        "-vf",
        "select='gt(scene,0.32)',showinfo",
        "-an",
        "-f",
        "null",
        "-",
    ]
    video_out = run(video_cmd, check=False).stderr
    for line in video_out.splitlines():
        if "pts_time:" in line and "showinfo" in line:
            fragment = line.split("pts_time:", 1)[1].split()[0]
            try:
                metrics.scene_times.append(float(fragment))
            except ValueError:
                pass
    return metrics


def score_metrics(m: Metrics, baseline: float | None = None) -> tuple[float, dict[str, float]]:
    """Heuristic highlight score in [0, 1] with the reasons behind it.

    For a finite video, pass the whole-video median as `baseline` so that a
    loud *segment* is detected even when its own internal RMS is flat.
    """
    base = m.median_rms if baseline is None else baseline
    spike = m.p95_rms - base  # dB above the baseline
    peak = m.peak_rms - base
    minutes = max(m.duration, 1.0) / 60.0
    scene_rate = len(m.scene_times) / minutes

    audio_score = min(max(spike, 0.0) / 12.0, 1.0)
    peak_score = min(max(peak - 6.0, 0.0) / 14.0, 1.0)
    scene_score = min(scene_rate / 18.0, 1.0)
    score = min(0.55 * audio_score + 0.25 * peak_score + 0.20 * scene_score, 1.0)
    return score, {
        "audio_spike_db": round(spike, 2),
        "peak_over_baseline_db": round(peak, 2),
        "scene_changes_per_min": round(scene_rate, 2),
        "audio_component": round(audio_score, 3),
        "peak_component": round(peak_score, 3),
        "scene_component": round(scene_score, 3),
    }


@dataclass
class Segment:
    path: str
    index: int
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


class Clipper:
    def __init__(self, args: argparse.Namespace, ffmpeg: str, ffprobe: str | None, streamlink: str | None) -> None:
        self.args = args
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.streamlink = streamlink
        self.yt_dlp = shutil.which("yt-dlp")
        self.platform = detect_platform(args)
        initial_target = (getattr(args, "url", "") or getattr(args, "channel", "") or "").strip()
        self.source_url = ""
        if initial_target and self.platform == "youtube":
            self.source_url = normalize_youtube_url(initial_target)
        elif initial_target and self.platform == "twitch":
            self.source_url = f"https://www.twitch.tv/{initial_target.lstrip('/')}"
        self._resolve_timing(args)
        self.out = Path(args.out).resolve()
        self.buffer = self.out / "buffer"
        self.clips = self.out / "clips"
        self.state_path = self.out / "state.json"
        self.buffer.mkdir(parents=True, exist_ok=True)
        self.clips.mkdir(parents=True, exist_ok=True)
        self.state: dict[str, Any] = self._load_state()
        self.last_fire = -1e9

    @staticmethod
    def _resolve_timing(args: argparse.Namespace) -> None:
        """Social preset: ~30 s total with the highlight near the front (hook first)."""
        preset = getattr(args, "preset", "social") or "social"
        clip_seconds = getattr(args, "clip_seconds", None)
        pre = getattr(args, "pre", None)
        post = getattr(args, "post", None)
        if clip_seconds:
            pre = min(5.0, clip_seconds / 4)
            post = clip_seconds - pre
        else:
            if pre is None:
                pre = 5.0 if preset == "social" else 15.0
            if post is None:
                post = 25.0 if preset == "social" else 20.0
        args.pre = float(pre)
        args.post = float(post)

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text())
            except json.JSONDecodeError:
                log("state.json was corrupt; starting a new state")
        return {"segments": [], "events": [], "clips": []}

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.state_path)

    def _segments(self) -> list[Segment]:
        return [Segment(**s) for s in self.state["segments"]]

    def _add_new_segments(self) -> None:
        known = {s["index"] for s in self.state["segments"]}
        segs = self._segments()
        last_end = max((s.end for s in segs), default=0.0)
        for path in sorted(self.buffer.glob("seg_*.ts")):
            try:
                index = int(path.stem.split("_")[1])
            except (IndexError, ValueError):
                continue
            if index in known:
                continue
            duration = ffprobe_duration(path, self.ffprobe)
            if duration <= 0.1:
                continue  # ffmpeg is still writing this segment
            self.state["segments"].append(
                {"path": str(path), "index": index, "start": round(last_end, 3), "duration": round(duration, 3)}
            )
            known.add(index)
            last_end += duration
            log(f"buffered segment {index:06d} ({duration:.1f}s)")
        self.state["segments"].sort(key=lambda s: s["index"])
        self._save_state()

    def _prune_buffer(self) -> None:
        segs = self._segments()
        if not segs:
            return
        now_end = segs[-1].end
        keep_before = now_end - self.args.buffer_minutes * 60 - self.args.pre - self.args.post
        pending = [e for e in self.state["events"] if not e.get("cut")]
        protected = min((e["at"] - self.args.pre for e in pending), default=keep_before)
        keep_before = min(keep_before, protected)
        kept = []
        for seg in segs:
            if seg.end < keep_before and seg.index != segs[-1].index:
                try:
                    Path(seg.path).unlink()
                except OSError:
                    pass
            else:
                kept.append(
                    {"path": seg.path, "index": seg.index, "start": seg.start, "duration": seg.duration}
                )
        self.state["segments"] = kept

    def _analyze_new_segments(self) -> None:
        analyzed = {e["segment_index"] for e in self.state["events"]}
        analyzed.update(c.get("segment_index") for c in self.state["clips"] if c.get("segment_index") is not None)
        for seg in self._segments():
            if seg.index in analyzed:
                continue
            path = Path(seg.path)
            if not path.exists():
                continue
            metrics = analyze_file(path, self.ffmpeg)
            score, reasons = score_metrics(metrics)
            event_abs = seg.start + metrics.loudest_time
            log(
                f"segment {seg.index:06d} score={score:.2f} "
                f"spike={reasons['audio_spike_db']}dB scenes={reasons['scene_changes_per_min']}/min"
            )
            self.state.setdefault("analyzed", []).append(
                {"segment_index": seg.index, "score": round(score, 3), "reasons": reasons}
            )
            if score >= self.args.threshold and event_abs - self.last_fire >= self.args.cooldown:
                event = {
                    "segment_index": seg.index,
                    "at": round(event_abs, 3),
                    "local_time": round(metrics.loudest_time, 3),
                    "score": round(score, 3),
                    "reasons": reasons,
                    "fire_at": round(event_abs + self.args.post, 3),
                    "cut": False,
                    "observed_at": time.time(),
                }
                self.state["events"].append(event)
                self.last_fire = event_abs
                log(f"highlight detected at t={event_abs:.1f}s; clip will be cut after t={event['fire_at']:.1f}s")
            self._save_state()

    def _video_args(self) -> list[str]:
        aspect = getattr(self.args, "aspect", "vertical") or "vertical"
        common = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p"]
        audio = ["-c:a", "aac", "-b:a", "160k"]
        if aspect == "vertical":
            # 9:16, 1080x1920, 30 fps. Blurred side bars keep the full gameplay
            # frame visible instead of cropping off the HUD.
            filter_complex = (
                "[0:v]split=2[bg][fg];"
                "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:2[b];"
                "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[f];"
                "[b][f]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30[v]"
            )
            return ["-filter_complex", filter_complex, "-map", "[v]", "-map", "0:a:0?"] + common + audio
        return ["-vf", "fps=30"] + common + audio

    def _cut_due_events(self) -> None:
        max_clips = getattr(self.args, "max_clips", 10**9)
        if len(self.state["clips"]) >= max_clips:
            return
        segs = self._segments()
        if not segs:
            return
        # In VOD mode the whole file is already available; no need to wait for
        # "future" footage to be recorded.
        now = float("inf") if getattr(self.args, "vod", False) else segs[-1].end
        for event in self.state["events"]:
            if event.get("cut") or now + 0.5 < event["fire_at"]:
                continue
            start = max(0.0, event["at"] - self.args.pre)
            end = event["at"] + self.args.post
            covering = [s for s in segs if s.end > start + 0.05 and s.start < end - 0.05]
            if not covering:
                log(f"no buffered segments left for event at {event['at']:.1f}s; skipped")
                event["cut"] = True
                event["skipped"] = "buffer_pruned"
                continue
            first = covering[0]
            offset = max(0.0, start - first.start)
            duration = min(end, covering[-1].end) - (first.start + offset)
            if duration <= 1.0:
                event["cut"] = True
                event["skipped"] = "too_short"
                continue
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(event["observed_at"]))
            prefix = getattr(self.args, "prefix", "gta6") or "clip"
            name = f"{prefix}-{stamp}-{int(event['at']):05d}-{int(event['score'] * 100):03d}.mp4"
            clip_path = self.clips / name
            list_path = self.out / f"concat-{event['segment_index']}.txt"
            list_path.write_text("".join(f"file '{Path(s.path).as_posix()}'\n" for s in covering))
            cmd = [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-ss",
                f"{offset:.3f}",
                "-t",
                f"{duration:.3f}",
                *self._video_args(),
                "-movflags",
                "+faststart",
                str(clip_path),
            ]
            try:
                run(cmd)
                meta = {
                    "file": str(clip_path),
                    "platform": self.platform,
                    "source_url": self.source_url,
                    "preset": getattr(self.args, "preset", "social"),
                    "aspect": getattr(self.args, "aspect", "vertical"),
                    "target_platforms": ["instagram_reels", "youtube_shorts"],
                    "event_at": event["at"],
                    "score": event["score"],
                    "reasons": event["reasons"],
                    "segment_index": event["segment_index"],
                    "clip_start": round(start, 3),
                    "clip_end": round(end, 3),
                }
                (clip_path.with_suffix(".json")).write_text(json.dumps(meta, indent=2))
                self.state["clips"].append(meta)
                event["cut"] = True
                event["clip"] = str(clip_path)
                log(f"saved clip {clip_path} ({duration:.1f}s, score {event['score']:.2f})")
            except RuntimeError as exc:
                log(f"clip render failed: {exc}")
                event["cut"] = True
                event["skipped"] = "ffmpeg_error"
            finally:
                list_path.unlink(missing_ok=True)
                self._save_state()

    def _source_command(self) -> list[str]:
        """Build the producer command for the selected live platform."""
        if self.platform == "twitch":
            if not self.streamlink:
                raise SystemExit("streamlink is not installed; run the doctor subcommand")
            channel = (getattr(self.args, "channel", "") or "").strip().lstrip("/")
            if not channel:
                raise SystemExit("--channel is required for Twitch")
            self.source_url = f"https://www.twitch.tv/{channel}"
            cmd = [self.streamlink, "--stdout", self.source_url, self.args.quality]
            token = os.environ.get("TWITCH_OAUTH_TOKEN", "").strip()
            if token:
                cmd.insert(1, f"--twitch-oauth-token={token}")
            return cmd

        if not self.yt_dlp:
            raise SystemExit("yt-dlp is not installed; run the doctor subcommand")
        target = (getattr(self.args, "url", "") or getattr(self.args, "channel", "") or "").strip()
        if not target:
            raise SystemExit("--url (YouTube live URL or video ID) is required for YouTube")
        self.source_url = normalize_youtube_url(target)
        cmd = [
            self.yt_dlp,
            "--no-playlist",
            "--no-warnings",
            "-f",
            getattr(self.args, "youtube_format", "best[ext=mp4]/best"),
            "-o",
            "-",
        ]
        cookies = getattr(self.args, "cookies", "")
        if cookies:
            cmd.insert(1, f"--cookies={cookies}")
        cmd.append(self.source_url)
        return cmd

    def watch(self) -> None:
        if not self.ffmpeg:
            raise SystemExit("ffmpeg is required; run the doctor subcommand for setup instructions")
        cmd = self._source_command()
        record_cmd = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(self.args.segment_seconds),
            "-segment_start_number",
            "0",
            "-reset_timestamps",
            "1",
            "-segment_format",
            "mpegts",
            str(self.buffer / "seg_%06d.ts"),
        ]
        log(f"recording {self.source_url} via {self.platform} into {self.buffer}")
        log("press Ctrl-C to stop; only clip streams you own or have permission to record")
        producer = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        recorder = subprocess.Popen(record_cmd, stdin=producer.stdout, stderr=subprocess.DEVNULL)
        if producer.stdout:
            producer.stdout.close()
        try:
            while recorder.poll() is None:
                self._add_new_segments()
                self._prune_buffer()
                self._analyze_new_segments()
                self._cut_due_events()
                time.sleep(3)
        except KeyboardInterrupt:
            log("stopping recorder")
        finally:
            recorder.terminate()
            producer.terminate()
            try:
                recorder.wait(timeout=10)
            except subprocess.TimeoutExpired:
                recorder.kill()
            producer.wait(timeout=5)
            self._add_new_segments()
            self._save_state()
            log(f"done. clips are in {self.clips}")


def cmd_doctor(args: argparse.Namespace) -> None:
    ffmpeg = find_bin("ffmpeg", args.ffmpeg)
    ffprobe = find_bin("ffprobe", args.ffprobe, imageio_fallback=False)
    streamlink = shutil.which("streamlink")
    yt_dlp = shutil.which("yt-dlp")
    print(f"ffmpeg:    {ffmpeg or 'MISSING'}")
    print(f"ffprobe:   {ffprobe or 'MISSING (optional)'}")
    print(f"streamlink:{streamlink or 'MISSING (needed for Twitch)'}")
    print(f"yt-dlp:    {yt_dlp or 'MISSING (needed for YouTube Live)'}")
    print(f"twitch token in env: {'yes' if os.environ.get('TWITCH_OAUTH_TOKEN') else 'no (optional)'}")
    if not ffmpeg or not streamlink or not yt_dlp:
        print("\nSetup:")
        print("  python3 -m pip install streamlink yt-dlp")
        print("  # ffmpeg: apt install ffmpeg / brew install ffmpeg, or:")
        print("  python3 -m pip install imageio-ffmpeg")
        print("  CLIP_FFMPEG=$(python3 -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')")


def cmd_analyze(args: argparse.Namespace) -> None:
    ffmpeg = find_bin("ffmpeg", args.ffmpeg)
    if not ffmpeg:
        raise SystemExit("ffmpeg is missing; run the doctor subcommand")
    os.environ["CLIP_FFMPEG"] = ffmpeg
    m = analyze_file(Path(args.file), ffmpeg)
    score, reasons = score_metrics(m)
    print(json.dumps({"duration": m.duration, "score": round(score, 3), "loudest_at": round(m.loudest_time, 3), **reasons}, indent=2))


def cmd_vod(args: argparse.Namespace) -> None:
    ffmpeg = find_bin("ffmpeg", args.ffmpeg)
    if not ffmpeg:
        raise SystemExit("ffmpeg is missing; run the doctor subcommand")
    os.environ["CLIP_FFMPEG"] = ffmpeg
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    source_arg = args.source.strip()
    args.vod = True
    looks_remote = source_arg.startswith(("http://", "https://", "www.", "youtube:", "yt:")) or (
        "youtube.com" in source_arg or "youtu.be" in source_arg
    )
    if looks_remote:
        downloader = shutil.which("yt-dlp")
        if not downloader:
            raise SystemExit("yt-dlp is not installed; run the doctor subcommand")
        args.url = normalize_youtube_url(source_arg)
        args.channel = ""
        args.platform = "youtube"
        log(f"downloading {args.url}")
        dl_cmd = [
            downloader,
            "-f",
            getattr(args, "youtube_format", "best[ext=mp4]/best"),
            "--merge-output-format",
            "mp4",
        ]
        cookies = getattr(args, "cookies", "") or ""
        browser = getattr(args, "cookies_from_browser", "") or ""
        if cookies:
            dl_cmd.append(f"--cookies={cookies}")
        elif browser:
            dl_cmd.append(f"--cookies-from-browser={browser}")
        dl_cmd += ["-o", str(out / "source.%(ext)s"), args.url]
        try:
            run(dl_cmd)
        except RuntimeError as exc:
            log(f"download failed: {exc}")
            raise SystemExit(
                "YouTube download failed. Common fixes:\n"
                "  - 'Sign in to confirm you're not a bot': add --cookies-from-browser chrome\n"
                "  - 'Video unavailable' / members-only: add --cookies /path/to/cookies.txt\n"
                "  - 'ffmpeg not found': pip install imageio-ffmpeg and export CLIP_FFMPEG\n"
                "  - Or upload the video file to this workspace and use: vod /path/to/file.mp4"
            )
        candidates = sorted(out.glob("source.*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            raise SystemExit("yt-dlp finished but no source file was found")
        source = candidates[0]
    else:
        args.url = ""
        args.channel = ""
        args.platform = "local"
        source = Path(source_arg).expanduser().resolve()
        if not source.exists():
            raise SystemExit(f"source file not found: {source}")

    log(f"analyzing {source} for highlights")
    clipper = Clipper(
        args,
        ffmpeg,
        find_bin("ffprobe", args.ffprobe, imageio_fallback=False),
        shutil.which("streamlink"),
    )
    clipper.source_url = args.url or str(source)
    clipper.state = {"segments": [], "events": [], "clips": []}
    for stale in clipper.buffer.glob("seg_*.ts"):
        stale.unlink()
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(args.segment_seconds),
            "-segment_start_number",
            "0",
            "-reset_timestamps",
            "1",
            "-segment_format",
            "mpegts",
            str(clipper.buffer / "seg_%06d.ts"),
        ]
    )
    clipper._add_new_segments()
    segs = clipper._segments()
    if not segs:
        raise SystemExit("no segments were produced from the source")
    measured: list[tuple[Segment, Metrics]] = []
    for seg in segs:
        path = Path(seg.path)
        if not path.exists():
            continue
        metrics = analyze_file(path, ffmpeg)
        measured.append((seg, metrics))
        log(
            f"segment {seg.index:06d} rms={metrics.median_rms:.1f}dB "
            f"scenes={len(metrics.scene_times)} @ {seg.start:.0f}s"
        )
    if not measured:
        raise SystemExit("no readable segments found")
    global_baseline = statistics.median(m.median_rms for _, m in measured)
    log(f"whole-video audio baseline: {global_baseline:.1f} dB")
    now_wall = time.time()
    for seg, metrics in measured:
        score, reasons = score_metrics(metrics, baseline=global_baseline)
        event_at = seg.start + metrics.loudest_time
        clipper.state.setdefault("analyzed", []).append(
            {"segment_index": seg.index, "score": round(score, 3), "reasons": reasons}
        )
        if score < args.threshold:
            continue
        clipper.state["events"].append(
            {
                "segment_index": seg.index,
                "at": round(event_at, 3),
                "local_time": round(metrics.loudest_time, 3),
                "score": round(score, 3),
                "reasons": reasons,
                "fire_at": round(event_at + args.post, 3),
                "cut": False,
                "observed_at": now_wall,
            }
        )
    # Rank by score, then keep only the best non-overlapping events.
    clipper.state["events"].sort(key=lambda e: e["score"], reverse=True)
    kept: list[dict[str, Any]] = []
    for event in clipper.state["events"]:
        if any(abs(event["at"] - other["at"]) < args.cooldown for other in kept):
            continue
        kept.append(event)
        if len(kept) >= args.max_clips:
            break
    clipper.state["events"] = kept
    clipper._save_state()
    clipper._cut_due_events()
    log(f"done: {len(clipper.state['clips'])} clips in {clipper.clips}")


def cmd_selftest(args: argparse.Namespace) -> None:
    ffmpeg = find_bin("ffmpeg", args.ffmpeg)
    if not ffmpeg:
        raise SystemExit("ffmpeg is missing; run the doctor subcommand")
    os.environ["CLIP_FFMPEG"] = ffmpeg
    tmp = Path(args.out) / "selftest"
    tmp.mkdir(parents=True, exist_ok=True)
    src = tmp / "source.ts"
    # 8 s: 4 s quiet sine, then 4 s loud sine with rapid scene changes.
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=8:size=320x180:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4,volume=0.05",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=4,volume=0.9",
            "-filter_complex",
            "[1:a][2:a]concat=n=2:v=0:a=1[a]",
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            "-f",
            "mpegts",
            str(src),
        ]
    )
    m = analyze_file(src, ffmpeg)
    score, reasons = score_metrics(m)
    print(f"synthetic highlight score: {score:.3f} at t={m.loudest_time:.2f}s")
    print(json.dumps(reasons, indent=2))
    clip = tmp / "selftest-clip.mp4"
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-ss",
            f"{max(0.0, m.loudest_time - 1):.3f}",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            str(clip),
        ]
    )
    print(f"wrote {clip} ({ffprobe_duration(clip, find_bin('ffprobe', imageio_fallback=False)):.1f}s)")
    if score < 0.3:
        raise SystemExit("self-test failed: highlight score unexpectedly low")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ffmpeg", help="path to ffmpeg binary")
    p.add_argument("--ffprobe", help="path to ffprobe binary")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check dependencies").set_defaults(func=cmd_doctor)

    a = sub.add_parser("analyze", help="score a local media file")
    a.add_argument("file")
    a.set_defaults(func=cmd_analyze)

    t = sub.add_parser("selftest", help="generate a synthetic stream and verify analysis + cutting")
    t.add_argument("--out", default="/tmp/twitch-clip-selftest")
    t.set_defaults(func=cmd_selftest)

    w = sub.add_parser("watch", help="record a Twitch or YouTube live stream and auto-cut highlights")
    w.add_argument("--platform", choices=["auto", "twitch", "youtube"], default="auto", help="source platform (default: infer from --url)")
    w.add_argument("--channel", help="Twitch channel name, e.g. 'somechannel'; also accepts a YouTube URL/ID")
    w.add_argument("--url", help="YouTube live URL or video ID (e.g. 'https://www.youtube.com/watch?v=VIDEO_ID')")
    w.add_argument("--out", default="clips-output", help="directory for buffer/, clips/, state.json")
    w.add_argument("--quality", default="best", help="Twitch/streamlink quality (best, 720p60, worst, ...)")
    w.add_argument("--youtube-format", default="best[ext=mp4]/best", help="yt-dlp format selector for YouTube Live")
    w.add_argument("--cookies", help="path to a local yt-dlp cookies file for members-only/private streams")
    w.add_argument("--prefix", default="gta6", help="filename prefix for saved clips")
    w.add_argument("--preset", choices=["social", "raw"], default="social", help="social = 30s vertical Reels/Shorts; raw = original aspect")
    w.add_argument("--aspect", choices=["vertical", "original"], default="vertical", help="vertical = 1080x1920 with blurred side bars")
    w.add_argument("--clip-seconds", type=float, help="total clip length; sets a short lead-in and the rest after the highlight")
    w.add_argument("--buffer-minutes", type=float, default=20.0)
    w.add_argument("--segment-seconds", type=int, default=30)
    w.add_argument("--pre", type=float, help="seconds before the highlight (default: 5 in social preset)")
    w.add_argument("--post", type=float, help="seconds after the highlight (default: 25 in social preset)")
    w.add_argument("--threshold", type=float, default=0.65, help="0..1, lower = more clips")
    w.add_argument("--cooldown", type=float, default=90.0, help="minimum seconds between clips")
    w.set_defaults(func=lambda a: Clipper(a, find_bin("ffmpeg", a.ffmpeg) or "", find_bin("ffprobe", a.ffprobe, imageio_fallback=False), shutil.which("streamlink")).watch())

    v = sub.add_parser("vod", help="cut highlights from a finished YouTube video or a local file")
    v.add_argument("source", help="YouTube URL/video ID or a local media file")
    v.add_argument("--out", default="clips-output", help="directory for buffer/, clips/, state.json")
    v.add_argument("--preset", choices=["social", "raw"], default="social")
    v.add_argument("--aspect", choices=["vertical", "original"], default="vertical")
    v.add_argument("--clip-seconds", type=float)
    v.add_argument("--pre", type=float)
    v.add_argument("--post", type=float)
    v.add_argument("--threshold", type=float, default=0.75, help="0..1, higher = only the biggest moments")
    v.add_argument("--cooldown", type=float, default=90.0)
    v.add_argument("--max-clips", type=int, default=10)
    v.add_argument("--segment-seconds", type=int, default=30)
    v.add_argument("--prefix", default="gta6")
    v.add_argument("--youtube-format", default="best[ext=mp4]/best", help="yt-dlp format selector")
    v.add_argument("--cookies", help="path to a local yt-dlp cookies.txt file")
    v.add_argument("--cookies-from-browser", help="e.g. chrome, firefox, edge (uses your logged-in browser session)")
    v.set_defaults(func=cmd_vod)
    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
