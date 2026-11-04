"""Safe argument-array FFmpeg operations; no shell, arbitrary filters or URLs."""
from __future__ import annotations
import importlib.util
import json
import math
import os
import re
import shutil
import signal
import subprocess
import time
from fractions import Fraction
from contextvars import ContextVar

COMMAND_LOG = ContextVar("video_agent_command_log", default=None)
from pathlib import Path


class MediaError(RuntimeError):
    pass


def executable(name):
    value = os.environ.get("VIDEO_AGENT_" + name.upper())
    if value:
        found = shutil.which(value)
        if not found:
            raise MediaError(name + " not installed or configured: " + value)
        return found
    found = shutil.which(name)
    # Check if default found executable runs, or fallback to well-known homebrew paths
    if found:
        try:
            # Validate that the default executable does not crash on dynamic library loading
            res = subprocess.run([found, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            if res.returncode == 0:
                return found
        except Exception:
            pass
    for candidate in ("/opt/homebrew/opt/ffmpeg-full/bin", "/usr/local/bin"):
        p = Path(candidate) / name
        if p.is_file() and os.access(p, os.X_OK):
            try:
                res = subprocess.run([str(p), "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
                if res.returncode == 0:
                    return str(p)
            except Exception:
                continue
    if found:
        return found
    raise MediaError(name + " not installed or configured")


def command(args, timeout=3600, cwd=None):
    # Separate process group lets Harness cancellation kill descendants, not just Python.
    started = time.time()
    proc = subprocess.Popen([str(x) for x in args], cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.communicate(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
        raise
    stdout, stderr = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    if COMMAND_LOG.get():
        with Path(COMMAND_LOG.get()).open("a", encoding="utf-8") as log:
            log.write(json.dumps({"argv": [str(a) for a in args], "cwd": str(cwd) if cwd else None, "exit_code": proc.returncode, "seconds": time.time() - started, "stderr_tail": stderr[-6000:]}, ensure_ascii=False) + "\n")
    if proc.returncode:
        raise MediaError("Command failed (exit %s): %s" % (proc.returncode, stderr[-6000:]))
    return stdout, stderr


def ffmpeg(args, cwd=None):
    return command([executable("ffmpeg"), "-hide_banner", "-nostdin", "-v", "warning", "-n", *args], cwd=cwd)


def probe(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("Input must be a local file")
    out, _ = command([executable("ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", path], timeout=60)
    data = json.loads(out)
    duration = float(data.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise MediaError("Media has no finite positive duration")
    return {"duration": duration, "streams": data.get("streams", []), "format": data.get("format", {})}


def doctor():
    result = {"python": os.sys.version.split()[0], "providers": {k: importlib.util.find_spec(v) is not None
              for k, v in {"asr": "faster_whisper", "argos": "argostranslate"}.items()},
              "piper": bool(shutil.which("piper")), "say": bool(shutil.which("say"))}
    for name in ("ffmpeg", "ffprobe"):
        try:
            path = executable(name)
            out, _ = command([path, "-version"], timeout=15)
            result[name] = {"path": path, "version": out.splitlines()[0]}
        except (MediaError, OSError) as exc:
            result[name] = {"error": str(exc)}
    try:
        out, _ = command([executable("ffmpeg"), "-hide_banner", "-filters"], timeout=15)
        result["hard_subtitles"] = bool(re.search(r"\b(subtitles|ass)\s+V", out))
    except (MediaError, OSError):
        result["hard_subtitles"] = False
    return result


def extract_audio(source, output):
    ffmpeg(["-i", source, "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", output])
    return output


def proxy(source, output):
    ffmpeg(["-i", source, "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2,setsar=1",
            "-map", "0:s:0?", "-c:s", "mov_text", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac", "-movflags", "+faststart", output])
    return output


def validate_segments(segments, duration):
    if not isinstance(segments, list) or not segments or len(segments) > 500:
        raise ValueError("segments must contain 1..500 ranges")
    clean = []
    for s in segments:
        start, end = s.get("sourceIn"), s.get("sourceOut")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (start, end)):
            raise ValueError("Segment times must be finite seconds")
        if start < 0 or end <= start or end > duration + 0.001:
            raise ValueError("Segment outside source duration")
        clean.append({"sourceIn": float(start), "sourceOut": float(end), "reason": str(s.get("reason", "user selection"))[:1000]})
    return clean


def silence_segments(source, threshold_db=-35, min_duration=0.6, padding=0.12):
    if not -80 <= threshold_db <= -5 or not 0.1 <= min_duration <= 10 or not 0 <= padding <= 2:
        raise ValueError("Invalid silence detection settings")
    duration = probe(source)["duration"]
    _, log = command([executable("ffmpeg"), "-hide_banner", "-nostdin", "-i", source,
           "-af", "silencedetect=noise=%sdB:d=%s" % (threshold_db, min_duration), "-f", "null", "-"], timeout=3600)
    silences, active = [], None
    for line in log.splitlines():
        match = re.search(r"silence_start: ([0-9.]+)", line)
        if match:
            active = float(match[1])
        match = re.search(r"silence_end: ([0-9.]+)", line)
        if match and active is not None:
            silences.append((max(0, active + padding), min(duration, float(match[1]) - padding)))
            active = None
    if active is not None:
        silences.append((min(duration, active + padding), duration))
    cursor, keep = 0.0, []
    for start, end in silences:
        if end <= start:
            continue
        if start > cursor + 0.04:
            keep.append({"sourceIn": cursor, "sourceOut": start, "reason": "remove silence"})
        cursor = max(cursor, end)
    if duration > cursor + 0.04:
        keep.append({"sourceIn": cursor, "sourceOut": duration, "reason": "remove silence"})
    if not keep:
        raise ValueError("No non-silent segments remain; nothing to render")
    return keep


def frame_rate(info):
    video = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("Video stream required")
    try:
        rate = Fraction(video.get("avg_frame_rate", "0/0"))
    except (ValueError, ZeroDivisionError):
        raise ValueError("Finite frame rate required") from None
    if not 1 <= rate <= 240:
        raise ValueError("Frame rate must be 1..240 fps")
    return rate


def normalize_segments(segments, info):
    """Normalize VFR to CFR and snap cuts to one shared audio/video/cue frame grid."""
    rate = frame_rate(info)
    video = next(s for s in info["streams"] if s.get("codec_type") == "video")
    duration = min(info["duration"], float(video.get("duration", info["duration"])))
    max_frame = max(1, round(duration * float(rate)))
    result = []
    for s in validate_segments(segments, info["duration"]):
        a, b = round(s["sourceIn"] * float(rate)), min(max_frame, round(s["sourceOut"] * float(rate)))
        if a >= b:
            raise ValueError("Segment collapses after frame-grid quantization")
        result.append({**s, "sourceIn": float(Fraction(a, 1) / rate), "sourceOut": float(Fraction(b, 1) / rate)})
    return result


def cut(source, segments, output, aspect="source"):
    info = probe(source)
    segments = normalize_segments(segments, info)
    rate = frame_rate(info)
    has_audio = any(s.get("codec_type") == "audio" for s in info["streams"])
    if not any(s.get("codec_type") == "video" for s in info["streams"]):
        raise ValueError("Render requires video input")
    graph, inputs = [], []
    for i, segment in enumerate(segments):
        start, end = segment["sourceIn"], segment["sourceOut"]
        graph.append("[0:v]fps=%s,trim=start_frame=%s:end_frame=%s,setpts=N/(%s*TB)[v%s]" % (rate, round(start * float(rate)), round(end * float(rate)), rate, i))
        inputs.append("[v%s]" % i)
        if has_audio:
            graph.append("[0:a]atrim=start=%s:end=%s,asetpts=PTS-STARTPTS[a%s]" % (start, end, i))
            inputs.append("[a%s]" % i)
    graph.append("".join(inputs) + "concat=n=%s:v=1:a=%s[vout]%s" % (len(segments), int(has_audio), "[aout]" if has_audio else ""))
    size = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (720, 720), "source": None}
    if aspect not in size:
        raise ValueError("Unsupported aspect")
    vmap = "[vout]"
    if size[aspect]:
        w, h = size[aspect]
        graph.append("[vout]scale=%s:%s:force_original_aspect_ratio=decrease,pad=%s:%s:(ow-iw)/2:(oh-ih)/2,setsar=1[scaled]" % (w, h, w, h))
        vmap = "[scaled]"
    else:
        graph.append("[vout]pad=ceil(iw/2)*2:ceil(ih/2)*2,setsar=1[scaled]")
        vmap = "[scaled]"
    args = ["-i", source, "-filter_complex", ";".join(graph), "-map", vmap]
    if has_audio:
        args += ["-map", "[aout]", "-c:a", "aac"]
    args += ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", output]
    ffmpeg(args)
    return output


def subtitle_mux(source, subtitle, output, mode):
    if mode == "hard":
        if not doctor()["hard_subtitles"]:
            raise MediaError("Hard subtitle burn requires FFmpeg built with libass. Set VIDEO_AGENT_FFMPEG to that binary; soft subtitles remain available.")
        # Fixed relative filename inside a private job dir avoids filter-grammar injection.
        staging = Path(output).parent / "burn_subtitles.srt"
        shutil.copyfile(subtitle, staging)
        ffmpeg(["-i", Path(source).resolve(), "-vf", "subtitles=burn_subtitles.srt", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", Path(output).resolve()], cwd=staging.parent)
    elif mode == "soft":
        ffmpeg(["-i", source, "-i", subtitle, "-map", "0:v:0", "-map", "0:a:0?", "-map", "1:0",
                "-c", "copy", "-c:s", "mov_text", "-movflags", "+faststart", output])
    else:
        raise ValueError("Subtitle mode must be soft or hard")
    return output


def dub(source, manifest, output, original_volume=0.15):
    if not isinstance(original_volume, (int, float)) or not math.isfinite(original_volume) or not 0 <= original_volume <= 1:
        raise ValueError("original_volume must be 0..1")
    info = probe(source)
    clips = manifest.get("clips", [])
    if not clips or len(clips) > 200:
        raise ValueError("Dubbing supports 1..200 TTS clips per job")
    args, graph, inputs = ["-i", source], [], []
    for i, clip in enumerate(clips, 1):
        path = Path(clip["path"]).resolve(strict=True)
        start, end = clip["start_ms"], clip["end_ms"]
        length = (end - start) / 1000
        actual = probe(path)["duration"]
        factor = actual / length
        if not 0.5 <= factor <= 2:
            raise ValueError("TTS timing requires >2x adjustment; revise the cue instead of silently truncating")
        args += ["-i", path]
        graph.append("[%s:a]atempo=%s,apad,atrim=duration=%s,adelay=%s:all=1[t%s]" % (i, factor, length, int(start), i))
        inputs.append("[t%s]" % i)
    if original_volume and any(s.get("codec_type") == "audio" for s in info["streams"]):
        graph.append("[0:a]volume=%s[original]" % original_volume)
        inputs.append("[original]")
    graph.append("".join(inputs) + "amix=inputs=%s:duration=longest:normalize=0,apad,atrim=duration=%s,loudnorm=I=-16:TP=-1.5:LRA=11[mix]" % (len(inputs), info["duration"]))
    ffmpeg(args + ["-filter_complex", ";".join(graph), "-map", "0:v:0", "-map", "[mix]", "-c:v", "copy", "-c:a", "aac", "-t", str(info["duration"]), "-movflags", "+faststart", output])
    return output


def quality_check(path, expected_duration=None):
    info = probe(path)
    issues = []
    if expected_duration is not None and abs(info["duration"] - expected_duration) > 0.25:
        issues.append({"code": "duration_mismatch", "expected": expected_duration, "actual": info["duration"]})
    # Real full decode check, not a claim about semantic/subjective quality.
    command([executable("ffmpeg"), "-hide_banner", "-v", "error", "-xerror", "-nostdin", "-i", path, "-map", "0:v:0?", "-map", "0:a:0?", "-f", "null", "-"], timeout=3600)
    return {"scope": "container, duration and full A/V decode only", "passed": not issues, "issues": issues, "media": info}
