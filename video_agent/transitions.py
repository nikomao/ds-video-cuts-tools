"""Frame-exact, whitelisted transitions and midpoint-owned subtitle mapping.

Seconds are derived from integer frames. Intervals are half-open.
overlap_seconds is total overlap removed; per-boundary durations live in
timeline["transitions"]. A plan may mix cuts and non-cut transitions; cut
boundaries may add source-time audio lead/lag (J/L), mixed independently.
No user filter is accepted. No external Python dependencies are required.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from fractions import Fraction
import math
from pathlib import Path

from . import media
from .subtitles import validate_cues

__all__ = ["audio_ranges", "build_timeline", "render_sequence", "retime_cues"]
_TYPES = frozenset(("cut", "fade", "dissolve", "fadeblack", "wipeleft",
                    "wiperight", "slideleft", "slideright"))
_SIZES = {"source": None, "16:9": (1280, 720), "9:16": (720, 1280), "1:1": (720, 720)}


def _seconds(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float, Fraction)):
        raise ValueError(f"{name} must be finite nonnegative seconds")
    try:
        valid = math.isfinite(value) and value >= 0
    except (ValueError, OverflowError):
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite nonnegative seconds")
    return value if isinstance(value, Fraction) else Fraction(str(value))


def _rate(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Fraction)):
        raise ValueError("fps must be a finite frame rate in 1..240")
    try:
        rate = Fraction(str(value))
    except (ValueError, ZeroDivisionError, OverflowError):
        raise ValueError("fps must be a finite frame rate in 1..240") from None
    if not 1 <= rate <= 240:
        raise ValueError("fps must be a finite frame rate in 1..240")
    return rate


def _normalize_config(item, rate, warnings, allow_audio=False):
    if item is None:
        item = {"type": "cut", "duration": 0}
    allowed = {"type", "duration", "frames"}
    if allow_audio:
        allowed |= {"jcut", "lcut", "jcut_frames", "lcut_frames"}
    if not isinstance(item, Mapping) or set(item) - allowed:
        raise ValueError("transition requires only type, duration and cut audio offsets")
    kind = item.get("type", "cut")
    if not isinstance(kind, str) or kind not in _TYPES:
        raise ValueError("Unsupported transition type")
    requested = _seconds(item.get("duration", 0), "transition.duration")
    if kind == "cut" and requested:
        raise ValueError("cut transition duration must be zero")
    if kind != "cut" and not requested:
        kind = "cut"
        warnings.append("Zero-duration transition is an effective cut.")
    frames = round(requested * rate)
    if requested and frames == 0:
        raise ValueError("Transition duration collapses on the frame grid")
    jcut = _seconds(item.get("jcut", 0), "jcut")
    lcut = _seconds(item.get("lcut", 0), "lcut")
    if kind != "cut" and (jcut or lcut):
        raise ValueError("J/L audio offsets apply only to cut boundaries")
    jframes, lframes = round(jcut * rate), round(lcut * rate)
    if jcut and jframes == 0 or lcut and lframes == 0:
        raise ValueError("J/L audio offset collapses on the frame grid")
    return kind, requested, frames, jframes, lframes


def build_timeline(segments, transition=None, fps="25", transitions=None) -> dict:
    """Build a JSON-safe timeline without changing inputs.

    A uniform transition applies to every boundary. Alternatively
    "transitions" supplies one entry per boundary, so hard cuts and smooth
    transitions can be mixed in a single plan. Source boundaries and
    transition durations round to the nearest frame (ties to even). Oversized
    requests are rejected before AND after rounding, never clamped. Positive
    durations collapsing to zero frames are rejected. Non-cut zero duration
    becomes cut. A single shot retains the requested transition but has zero
    effective overlap and a warnings entry. At most 50 non-cut boundaries
    (51 shots); cuts allow 500 shots.
    """
    rate = _rate(fps)
    if not isinstance(segments, (list, tuple)) or not 1 <= len(segments) <= 500:
        raise ValueError("segments must contain 1..500 ranges")
    bounds = []
    for segment in segments:
        if not isinstance(segment, Mapping) or not all(k in segment for k in ("sourceIn", "sourceOut")):
            raise ValueError("segment requires sourceIn/sourceOut")
        a = _seconds(segment["sourceIn"], "sourceIn")
        b = _seconds(segment["sourceOut"], "sourceOut")
        if b <= a:
            raise ValueError("sourceOut must exceed sourceIn")
        start, end = round(a * rate), round(b * rate)
        if start >= end:
            raise ValueError("Segment collapses on the frame grid")
        bounds.append((start, end))
    warnings = []
    if transitions is not None:
        if transition is not None:
            raise ValueError("Choose transition OR transitions, not both")
        if not isinstance(transitions, (list, tuple)) or len(transitions) != len(bounds) - 1:
            raise ValueError("transitions must contain exactly one entry per boundary")
        configs = [_normalize_config(item, rate, warnings, allow_audio=True) for item in transitions]
        uniform = None
        if configs and len({(kind, frames, jframes, lframes)
                            for kind, _, frames, jframes, lframes in configs}) == 1:
            kind, _, frames, jframes, lframes = configs[0]
            summary = {"type": kind, "duration": float(frames / rate)}
        elif configs:
            summary = {"type": "mixed", "duration": 0.0}
        else:
            summary = {"type": "cut", "duration": 0.0}
    else:
        if transition is None:
            transition = {"type": "cut", "duration": 0}
        base = _normalize_config(transition, rate, warnings)
        uniform = base
        configs = [base] * (len(bounds) - 1)
        summary = {"type": base[0], "duration": float(base[2] / rate)}
    for index, (kind, requested, frames, _, _) in enumerate(configs):
        if kind == "cut" or not frames:
            continue
        for start, end in (bounds[index], bounds[index + 1]):
            # Inputs originate in normalize_segments: recover exact frame
            # lengths, not subtraction of two rounded float source positions.
            # A nanoframe allowance only absorbs serialization noise.
            if (requested * 2 * rate > end - start + Fraction(1, 10**9)
                    or frames * 2 > end - start):
                raise ValueError("Transition duration must not exceed half of either adjacent segment")
    if sum(1 for kind, _, _, _, _ in configs if kind != "cut") > 50:
        raise ValueError("At most 50 transitions are supported")
    if uniform is not None and len(bounds) == 1 and uniform[0] != "cut":
        warnings.append("Single segment: transition is not applied; effective overlap is zero.")
    if any(kind == "cut" and (jframes or lframes) for kind, _, _, jframes, lframes in configs):
        warnings.append("J/L audio offsets overlap source audio; no automatic ducking or limiting is applied.")
    frame_overlaps = [frames for _, _, frames, _, _ in configs]
    spans, cursor = [], 0
    for index, (start, end) in enumerate(bounds):
        length = end - start
        spans.append({"sourceIn": float(start / rate), "sourceOut": float(end / rate),
                      "outputIn": float(cursor / rate), "outputOut": float((cursor + length) / rate),
                      "start_frame": cursor, "end_frame": cursor + length,
                      "duration": float(length / rate), "source_start_frame": start,
                      "source_end_frame": end, "segment_index": index})
        cursor += length - (frame_overlaps[index] if index < len(bounds) - 1 else 0)
    total_overlap = sum(frame_overlaps)
    uniform_overlap = (frame_overlaps[0] if frame_overlaps and len(set(frame_overlaps)) == 1 else 0)
    return {"spans": spans, "duration": float(cursor / rate), "duration_frames": cursor,
            "overlap_seconds": float(Fraction(total_overlap, 1) / rate),
            "overlap_frames": uniform_overlap, "overlap_frames_total": total_overlap,
            "transition": summary,
            "transitions": [{"type": kind, "duration": float(frames / rate), "frames": frames,
                             "jcut": float(jframes / rate), "lcut": float(lframes / rate),
                             "jcut_frames": jframes, "lcut_frames": lframes}
                            for kind, _, frames, jframes, lframes in configs],
            "frame_rate": str(rate), "warnings": list(dict.fromkeys(warnings))}


def audio_ranges(timeline, max_frames=None) -> list:
    """Return (source_start, source_end, output_start) frames for each span.

    J-cut extends incoming audio before its video and starts it earlier;
    L-cut extends outgoing audio after its video. The extension keeps source
    time aligned with the video at the boundary, so dialogue stays in sync
    once the picture cuts. Extensions must stay inside the source.
    """
    if not isinstance(timeline, Mapping) or not isinstance(timeline.get("spans"), list) or not timeline["spans"]:
        raise ValueError("timeline requires nonempty spans from build_timeline")
    spans = timeline["spans"]
    transitions = timeline.get("transitions")
    if not isinstance(transitions, list) or len(transitions) != len(spans) - 1:
        raise ValueError("timeline transitions must match the span count")
    result = []
    for index, span in enumerate(spans):
        start = span["source_start_frame"]
        end = span["source_end_frame"]
        output = span["start_frame"]
        if index:
            item = transitions[index - 1]
            if item.get("type", "cut") != "cut" and item.get("jcut_frames", 0):
                raise ValueError("J-cut audio offset is only valid on cut boundaries")
            start -= item.get("jcut_frames", 0)
            output -= item.get("jcut_frames", 0)
        if index < len(spans) - 1:
            item = transitions[index]
            if item.get("type", "cut") != "cut" and item.get("lcut_frames", 0):
                raise ValueError("L-cut audio offset is only valid on cut boundaries")
            end += item.get("lcut_frames", 0)
        if start < 0 or (max_frames is not None and end > max_frames):
            raise ValueError("J/L audio extension exceeds the available source")
        if index and output < spans[index - 1]["start_frame"]:
            raise ValueError("J-cut exceeds the previous segment length")
        result.append((start, end, output))
    return result


def _decimal(value):
    # FFmpeg duration AVOptions are decimal seconds, not rational expressions.
    return format(float(value), ".12f")


def render_sequence(source, segments, output, aspect="source", transition=None,
                    transitions=None) -> Path:
    """Render normalized CFR video with per-boundary cuts/xfade, without libass.

    Source must be a local media file. Output is never overwritten (-n through
    media.ffmpeg). No audio stream is fabricated for silent sources. Inputs and
    all filter fragments are validated before invoking FFmpeg. Hard-cut
    boundaries use concat; non-cut boundaries use xfade/acrossfade, so a single
    timeline may mix both.
    """
    source = Path(source).resolve(strict=True)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    if not isinstance(aspect, str) or aspect not in _SIZES:
        raise ValueError("Unsupported aspect")
    info = media.probe(source)
    segments = media.normalize_segments(segments, info)
    rate = media.frame_rate(info)
    timeline = build_timeline(segments, transition, rate, transitions=transitions)
    audio_offsets = any(item.get("jcut_frames") or item.get("lcut_frames")
                        for item in timeline["transitions"])
    if not timeline["overlap_frames_total"] and not audio_offsets:
        media.cut(source, segments, output, aspect)
        return output
    if any(item["duration"] > 60 for item in timeline["transitions"]):
        raise ValueError("FFmpeg xfade supports transition durations up to 60 seconds")
    has_audio = any(s.get("codec_type") == "audio" for s in info["streams"])
    size = _SIZES[aspect]
    if size:
        w, h = size
        resize = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"
    else:
        resize = "pad=ceil(iw/2)*2:ceil(ih/2)*2"
    graph = []
    for i, span in enumerate(timeline["spans"]):
        graph.append(f"[0:v:0]fps={rate},trim=start_frame={span['source_start_frame']}:end_frame={span['source_end_frame']},"
                     f"setpts=PTS-STARTPTS,{resize},setsar=1,format=yuv420p,settb=expr=1/({rate}),setpts=N[v{i}]")
    video = "v0"
    for i, (span, item) in enumerate(zip(timeline["spans"][1:], timeline["transitions"]), 1):
        if item["type"] == "cut" or not item["frames"]:
            graph.append(f"[{video}][v{i}]concat=n=2:v=1:a=0[vc{i}]")
            # concat resets the timebase to 1/1000000; restore the shared grid
            # before a later xfade so both inputs use the same timebase.
            graph.append(f"[vc{i}]settb=expr=1/({rate}),setpts=N[vs{i}]")
            video = f"vs{i}"
        else:
            duration = Fraction(item["frames"], 1) / rate
            graph.append(f"[{video}][v{i}]xfade=transition={item['type']}:duration={_decimal(duration)}:"
                         f"offset={_decimal(Fraction(span['start_frame'], 1) / rate)}[vx{i}]")
            video = f"vx{i}"
    graph.append(f"[{video}]trim=end_frame={timeline['duration_frames']},setpts=N/({rate}*TB),format=yuv420p[vout]")
    if has_audio:
        # Audio is assembled independently so J/L offsets can overlap streams
        # while video still uses concat/xfade. Each stream keeps source-time
        # sync with its own video at the boundary.
        max_frames = max(1, round(info["duration"] * float(rate)))
        ranges = audio_ranges(timeline, max_frames)
        labels = []
        for i, (start, end, output_start) in enumerate(ranges):
            length = end - start
            samples = round(Fraction(length * 48000, 1) / rate)
            chain = (f"[0:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                     f"atrim=start={_decimal(Fraction(start, 1) / rate)}:end={_decimal(Fraction(end, 1) / rate)},"
                     f"asetpts=PTS-STARTPTS,apad,atrim=end_sample={samples},asetpts=N/SR/TB")
            if i:
                item = timeline["transitions"][i - 1]
                if item["type"] != "cut":
                    chain += f",afade=t=in:st=0:d={_decimal(Fraction(item['frames'], 1) / rate)}:curve=tri"
            if i < len(ranges) - 1:
                item = timeline["transitions"][i]
                if item["type"] != "cut":
                    fade_start = Fraction(length - item["frames"], 1) / rate
                    chain += (f",afade=t=out:st={_decimal(fade_start)}:"
                              f"d={_decimal(Fraction(item['frames'], 1) / rate)}:curve=tri")
            delay = round(Fraction(output_start * 48000, 1) / rate)
            if delay:
                chain += f",adelay={delay}S:all=1"
            graph.append(chain + f"[a{i}]")
            labels.append(f"[a{i}]")
        total = round(Fraction(timeline["duration_frames"] * 48000, 1) / rate)
        if len(labels) == 1:
            graph.append(f"{labels[0]}apad,atrim=end_sample={total},asetpts=N/SR/TB[aout]")
        else:
            graph.append("".join(labels) +
                         f"amix=inputs={len(labels)}:duration=longest:normalize=0:dropout_transition=0,"
                         f"apad,atrim=end_sample={total},asetpts=N/SR/TB[aout]")
    args = ["-i", source, "-filter_complex_threads", "1", "-filter_complex", ";".join(graph), "-map", "[vout]"]
    if has_audio:
        args += ["-map", "[aout]", "-c:a", "aac", "-ar", "48000"]
    args += ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-r", str(rate), "-movflags", "+faststart", output]
    media.ffmpeg(args)
    return output


def retime_cues(cues, timeline) -> list:
    """Map Cue dictionaries using half-open transition-midpoint ownership.

    Outgoing shot owns the first half of each overlap, incoming the second.
    Boundaries are rounded once to shared integer milliseconds. Words are
    clipped and mapped with cues. Unknown metadata is deep-copied;
    source_cue_id / segment_index preserve provenance. Repeated IDs receive
    collision-safe ~N suffixes, as in the original subtitle module.
    """
    values = validate_cues(cues)
    if not isinstance(timeline, Mapping) or not isinstance(timeline.get("spans"), list) or not timeline["spans"]:
        raise ValueError("timeline requires nonempty spans from build_timeline")
    # Rebuild rather than trusting caller-supplied output coordinates/duration.
    stored = timeline.get("transitions")
    if stored is None:
        clean = build_timeline(timeline["spans"], timeline.get("transition"),
                               timeline.get("frame_rate", "25"))
        if timeline.get("overlap_frames") != clean["overlap_frames"]:
            raise ValueError("Timeline does not match its frame-grid spans")
    else:
        if not isinstance(stored, list):
            raise ValueError("Timeline transitions must be a list")
        plain = [{"type": item.get("type"), "duration": item.get("duration", 0),
                  "jcut": item.get("jcut", 0), "lcut": item.get("lcut", 0)}
                 if isinstance(item, Mapping) else item for item in stored]
        clean = build_timeline(timeline["spans"], None, timeline.get("frame_rate", "25"),
                               transitions=plain)
        if ([(item.get("type"), item.get("frames"), item.get("jcut_frames", 0),
              item.get("lcut_frames", 0)) for item in stored] !=
                [(item["type"], item["frames"], item["jcut_frames"], item["lcut_frames"])
                 for item in clean["transitions"]]):
            raise ValueError("Timeline transitions do not match frame grid")
    for key in ("duration", "duration_frames"):
        if timeline.get(key) != clean[key]:
            raise ValueError("Timeline does not match its frame-grid spans")
    for original, normalized in zip(timeline["spans"], clean["spans"]):
        if any(original.get(k) != normalized[k] for k in ("outputIn", "outputOut", "start_frame", "end_frame")):
            raise ValueError("Timeline output coordinates do not match frame grid")
    rate = _rate(clean["frame_rate"])
    reserved, used, result = {cue["id"] for cue in values}, set(), []
    duration_ms = math.floor(Fraction(clean["duration_frames"] * 1000, 1) / rate)
    for index, span in enumerate(clean["spans"]):
        start = Fraction(span["start_frame"], 1)
        end = Fraction(span["end_frame"], 1)
        incoming = clean["transitions"][index - 1]["frames"] if index else 0
        outgoing = clean["transitions"][index]["frames"] if index < len(clean["transitions"]) else 0
        left = start + Fraction(incoming, 2)
        right = end - Fraction(outgoing, 2)
        owner_start = round(left * 1000 / rate)
        owner_end = min(duration_ms, round(right * 1000 / rate))
        source_start = Fraction(span["source_start_frame"], 1) / rate
        offset = start * 1000 / rate - source_start * 1000
        for cue in values:
            a = max(owner_start, round(Fraction(cue["start_ms"]) + offset))
            b = min(owner_end, round(Fraction(cue["end_ms"]) + offset))
            if a >= b:
                continue
            item = deepcopy(cue)
            identity, suffix = cue["id"], 2
            if identity in used:
                while f"{identity}~{suffix}" in used or f"{identity}~{suffix}" in reserved:
                    suffix += 1
                identity = f"{identity}~{suffix}"
            item.update(id=identity, start_ms=a, end_ms=b, source_cue_id=cue.get("source_cue_id", cue["id"]), segment_index=index)
            used.add(identity)
            if "words" in item:
                words = []
                for word in item["words"]:
                    x = max(a, round(Fraction(word["start_ms"]) + offset))
                    y = min(b, round(Fraction(word["end_ms"]) + offset))
                    if x < y:
                        word.update(start_ms=x, end_ms=y)
                        words.append(word)
                item["words"] = words
            result.append(item)
    return validate_cues(result, duration_ms)
