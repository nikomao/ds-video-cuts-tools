"""Local, evidence-only shot analysis and conservative edit suggestions.

No model, network, semantic/person/emotion inference, or source deletion. Times
are seconds relative to the first decoded video frame. Failures raise rather
than returning empty/mock success. Frames persist, even after a later failure.
"""
from __future__ import annotations

import math
import re
import tempfile
from pathlib import Path

from . import media

_LIMIT = 10000
_NUMBER = r"([-+]?[0-9]+(?:[.][0-9]*)?(?:[eE][-+]?[0-9]+)?)"


def _number(value, name, minimum=0, maximum=1e9, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name + " must be a finite number")
    try:
        value = float(value)
    except (OverflowError, ValueError):
        raise ValueError(name + " must be a finite number") from None
    if not math.isfinite(value) or value < minimum or value > maximum or (positive and value <= 0):
        raise ValueError(name + " is outside its finite allowed range")
    return value


def _issues(log, duration):
    issues = []
    for match in re.finditer(r"black_start:" + _NUMBER + r"[ \t]+black_end:" + _NUMBER, log):
        start, end = map(float, match.groups())
        start, end = max(0, start), min(duration, end)
        if math.isfinite(start) and math.isfinite(end) and end > start:
            issues.append(dict(type="black", start=start, end=end, duration=end-start,
                               reason="FFmpeg blackdetect: near-black pixels, not semantic judgement"))
    freeze_start = None
    for match in re.finditer(r"freeze_(start|end):[ \t]*" + _NUMBER, log):
        kind, value = match.groups()
        value = min(duration, max(0, float(value)))
        if kind == "start":
            freeze_start = value
        elif freeze_start is not None:
            if value > freeze_start:
                issues.append(dict(type="freeze", start=freeze_start, end=value, duration=value-freeze_start,
                                   reason="FFmpeg freezedetect: low pixel change; may be intentional still imagery"))
            freeze_start = None
    if freeze_start is not None and duration > freeze_start:
        issues.append(dict(type="freeze", start=freeze_start, end=duration, duration=duration-freeze_start,
                           reason="FFmpeg freezedetect: low pixel change continues to video end"))
    return sorted(issues, key=lambda item: (item["start"], item["end"], item["type"]))


def analyze_shots(source: Path, output_dir: Path, threshold=0.3,
                  min_shot_seconds=0.5, max_shots=120) -> dict:
    """Detect hard-cut candidates and write <= max_shots real midpoint JPEGs.

    scene_candidates are dictionaries with time and method. Nearby cuts are
    merged; when capped, the final shot covers the entire remaining tail.
    output_dir receives a unique run directory; existing files are untouched.
    """
    threshold = _number(threshold, "threshold", maximum=1)
    minimum = _number(min_shot_seconds, "min_shot_seconds", positive=True)
    if isinstance(max_shots, bool) or not isinstance(max_shots, int) or not 1 <= max_shots <= 1000:
        raise ValueError("max_shots must be an integer in 1..1000")
    source = Path(source).resolve(strict=True)
    if not source.is_file():
        raise ValueError("source must be a local file")
    info = media.probe(source)
    duration = _number(info.get("duration"), "source duration", positive=True)
    streams = info.get("streams")
    if (not isinstance(streams, list) or len(streams) > _LIMIT
            or any(not isinstance(stream, dict) for stream in streams)):
        raise ValueError("probe streams must be a bounded list of objects")
    if any(not isinstance(stream.get("disposition", {}), dict) for stream in streams):
        raise ValueError("invalid stream disposition")
    video = next((stream for stream in streams if stream.get("codec_type") == "video"
                  and not stream.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        raise ValueError("source has no video stream")
    limitations = ["Pixel hard-cut candidates only; fades and same-luminance cuts may be missed.",
                   "Black/freeze warnings are low-level signals, not narrative, emotion, person or aesthetic understanding.",
                   "Midpoint frames can contain motion blur or black imagery; inspect the original footage.",
                   "Analysis does not remove source footage or render an edit."]
    if video.get("duration") not in (None, "N/A"):
        try:
            if isinstance(video["duration"], bool):
                raise ValueError("invalid video duration")
            duration = _number(float(video["duration"]), "video duration", positive=True)
        except (ValueError, TypeError, OverflowError):
            raise ValueError("video stream has invalid duration") from None
    else:
        limitations.append("Video stream duration unavailable: using container duration; trailing audio may overestimate video coverage.")
    # Explicit info logging is required: media.ffmpeg's default warning hides showinfo.
    ffmpeg = media.executable("ffmpeg")
    video_index = video.get("index", 0)
    if isinstance(video_index, bool) or not isinstance(video_index, int) or video_index < 0:
        raise ValueError("invalid video stream index")
    filters = ("setpts=PTS-STARTPTS,blackdetect=d=0.1:pix_th=0.10:pic_th=0.98,"
               "freezedetect=n=-60dB:d=0.5,select='gt(scene,%.12g)',showinfo" % threshold)
    _, log = media.command([ffmpeg, "-hide_banner", "-nostdin", "-v", "info", "-i", source,
                            "-map", "0:%d" % video_index, "-an", "-vf", filters, "-f", "null", "-"])
    times = sorted(set(float(match.group(1)) for match in
                       re.finditer(r"\bpts_time:[ \t]*" + _NUMBER, log)))
    times = [time for time in times if math.isfinite(time) and 0 < time < duration]
    if len(times) > _LIMIT:
        raise media.MediaError("Too many scene candidates; analysis safety limit exceeded")
    boundaries = [0.0]
    capped = False
    for time in times:
        if time - boundaries[-1] < minimum or duration - time < minimum:
            continue
        if len(boundaries) >= max_shots:
            capped = True
            break
        boundaries.append(time)
    boundaries.append(duration)
    if capped:
        limitations.append("max_shots reached: remaining hard-cut candidates merged into the final shot.")
    quality = _issues(log, duration)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="analysis-", dir=output_dir)).resolve()
    shots = []
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:]), 1):
        frame = run_dir / ("shot-%04d.jpg" % index)
        midpoint = (start + end) / 2
        media.command([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-n", "-i", source,
                       "-ss", "%.9f" % midpoint, "-map", "0:%d" % video_index,
                       "-frames:v", "1", "-q:v", "2", "-update", "1", frame])
        if not frame.is_file() or frame.stat().st_size < 4:
            raise media.MediaError("Representative frame was not generated at %.6f seconds" % midpoint)
        with frame.open("rb") as handle:
            if handle.read(2) != b"\xff\xd8":
                raise media.MediaError("Representative frame is not a JPEG")
        shots.append(dict(id="shot-%04d" % index, start=start, end=end,
                          duration=end-start, frame_path=str(frame)))
    return dict(source=str(source), duration=duration, shots=shots,
                scene_candidates=[dict(time=time, method="FFmpeg select gt(scene,threshold) + showinfo") for time in times],
                quality_issues=quality, method=dict(name="ffmpeg_pixel_analysis", threshold=threshold,
                min_shot_seconds=minimum, max_shots=max_shots, representative_frame="interval midpoint",
                blackdetect="d=0.1:pix_th=0.10:pic_th=0.98", freezedetect="n=-60dB:d=0.5"),
                limitations=limitations)


def _interval(item, label):
    if not isinstance(item, dict):
        raise ValueError(label + " must be an object")
    start = _number(item.get("start"), label + " start")
    end = _number(item.get("end"), label + " end", positive=True)
    if end <= start:
        raise ValueError(label + " must have end > start")
    if "duration" in item:
        duration = _number(item["duration"], label + " duration", positive=True)
        if not math.isclose(duration, end-start, rel_tol=1e-8, abs_tol=1e-7):
            raise ValueError(label + " duration does not match interval")
    return start, end


def _merged(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def suggest_edit(analysis: dict, target_duration=30, max_segment_seconds=6,
                 min_segment_seconds=1) -> dict:
    """Return review-required source-ordered candidates, excluding black ranges.

    Greedy scoring uses only eligible duration and freeze overlap. No source
    interval repeats, and short material is never looped to reach the target.
    """
    target = _number(target_duration, "target_duration", positive=True)
    maximum = _number(max_segment_seconds, "max_segment_seconds", positive=True)
    minimum = _number(min_segment_seconds, "min_segment_seconds", positive=True)
    if minimum > maximum or target < minimum:
        raise ValueError("require min_segment_seconds <= max_segment_seconds and target_duration")
    if not isinstance(analysis, dict):
        raise ValueError("analysis must be an object")
    shots = analysis.get("shots")
    issues = analysis.get("quality_issues", [])
    if not isinstance(shots, list) or not 1 <= len(shots) <= _LIMIT:
        raise ValueError("analysis must contain 1..10000 shots")
    if not isinstance(issues, list) or len(issues) > _LIMIT:
        raise ValueError("quality_issues must be a list of at most 10000 objects")
    source_duration = None
    if "duration" in analysis:
        source_duration = _number(analysis["duration"], "analysis duration", positive=True)
    valid_shots, previous_end, ids = [], 0.0, set()
    for index, shot in enumerate(shots):
        start, end = _interval(shot, "shot")
        if start < previous_end or (source_duration is not None and end > source_duration + 1e-7):
            raise ValueError("shots must be ordered, non-overlapping and within source duration")
        identifier = shot.get("id", "shot-%04d" % (index+1))
        if not isinstance(identifier, (str, int)) or isinstance(identifier, bool) or len(str(identifier)) > 200 or not str(identifier):
            raise ValueError("shot id must be a non-empty short string or integer")
        if str(identifier) in ids:
            raise ValueError("shot ids must be unique")
        ids.add(str(identifier))
        valid_shots.append((start, end, identifier))
        previous_end = end
    black, freeze = [], []
    for issue in issues:
        start, end = _interval(issue, "quality issue")
        if end > (source_duration if source_duration is not None else previous_end) + 1e-7:
            raise ValueError("quality issue exceeds source duration")
        if issue.get("type") not in ("black", "freeze", "long_shot"):
            raise ValueError("unknown quality issue type")
        if issue["type"] == "black":
            black.append((start, end))
        elif issue["type"] == "freeze":
            freeze.append((start, end))
    black, freeze = _merged(black), _merged(freeze)
    candidates = []
    for start, end, identifier in valid_shots:
        clean = [(start, end)]
        for left, right in black:
            clean = [(a, b) for lo, hi in clean for a, b in
                     ((lo, min(hi, left)), (max(lo, right), hi)) if b > a]
        for lo, hi in clean:
            cursor = lo
            while hi-cursor >= minimum:
                stop = min(hi, cursor+maximum)
                # Absolute timestamp addition may round UP; enforce literal bounds.
                while stop-cursor > maximum:
                    stop = math.nextafter(stop, cursor)
                if stop <= cursor or stop-cursor < minimum:
                    break
                frozen = sum(max(0, min(stop, b)-max(cursor, a)) for a, b in freeze)
                score = (stop-cursor) / (1 + 3*frozen/(stop-cursor))
                candidates.append((score, cursor, stop, identifier))
                if len(candidates) > _LIMIT:
                    raise ValueError("too many candidate segments; increase max_segment_seconds")
                cursor = stop
    if not candidates:
        raise ValueError("no usable non-black shot meets min_segment_seconds")
    selected, total = [], 0.0
    for score, start, end, identifier in sorted(candidates, key=lambda item: (-item[0], item[1])):
        length = min(end-start, target-total)
        if length < minimum:
            continue
        end = min(end, start+length)
        existing_lengths = [item["sourceOut"]-item["sourceIn"] for item in selected]
        while end-start > maximum or math.fsum(existing_lengths + [end-start]) > target:
            end = math.nextafter(end, start)
        length = end-start
        if length < minimum:
            continue
        frozen = sum(max(0, min(end, b)-max(start, a)) for a, b in freeze)
        reason = ("Duration rule: %.3fs eligible interval; no overlap with detected black ranges; "
                  "freeze overlap %.3fs (low pixel change, not semantic judgement)." % (length, frozen))
        selected.append(dict(sourceIn=start, sourceOut=end, shot_id=identifier, reason=reason))
        total = math.fsum(item["sourceOut"]-item["sourceIn"] for item in selected)
    if not selected:
        raise ValueError("no usable segments within target duration")
    selected.sort(key=lambda item: item["sourceIn"])
    # Never advertise a requested duration in place of the real interval sum.
    total = math.fsum(item["sourceOut"]-item["sourceIn"] for item in selected)
    limitations = ["Rule-based candidate only: duration and black/freeze pixel warnings, not semantic or aesthetic selection.",
                   "Static intentional imagery may be penalized; missed black/freeze events remain possible.",
                   "No repeated footage, no automatic deletion or rendering; human review required."]
    if total < target:
        limitations.append("Available eligible material/segment constraints do not reach target; shortfall %.6fs." % (target-total))
    return dict(segments=selected, total_duration=total, target_duration=target,
                shortfall_seconds=max(0.0, target-total), review_required=True,
                method="duration/(1+3*freeze_fraction), black intervals excluded, greedy selection then source order",
                limitations=limitations)


DEFAULT_FILLERS = (
    "嗯", "啊", "呃", "额", "就是说", "然后呢", "那个", "这个", "那么", "其实", "基本上", "可以说",
    "um", "uh", "er", "ah", "like", "you know", "basically", "literally"
)


def clean_speech(cues: list, source_duration: float, fillers=None,
                 max_pause_seconds=0.6, padding_seconds=0.08,
                 remove_fillers=True, compress_pauses=True) -> dict:
    """Analyze speech cues to suggest speech-focused edit segments.

    Detects hesitation words (fillers) and long inter-speech pauses to construct
    tight talking-head edit segments. Returns review-required segments and
    traceable evidence. Does not alter original tracks or execute rendering.
    """
    if not isinstance(cues, list) or not cues:
        raise ValueError("cues must be a non-empty list of subtitle cues")
    source_duration = _number(source_duration, "source_duration", positive=True)
    max_pause = _number(max_pause_seconds, "max_pause_seconds", minimum=0.05, maximum=30.0)
    padding = _number(padding_seconds, "padding_seconds", minimum=0.0, maximum=2.0)
    if not isinstance(remove_fillers, bool) or not isinstance(compress_pauses, bool):
        raise ValueError("remove_fillers and compress_pauses must be booleans")

    if fillers is None:
        target_fillers = set(DEFAULT_FILLERS)
    elif isinstance(fillers, (list, tuple, set)):
        target_fillers = {str(f).strip().lower() for f in fillers if str(f).strip()}
        if not target_fillers:
            raise ValueError("fillers cannot be empty when provided")
    else:
        raise ValueError("fillers must be a collection of strings or None")

    units = []
    for cue in cues:
        if not isinstance(cue, dict) or "start_ms" not in cue or "end_ms" not in cue:
            raise ValueError("Malformed cue object in cues")
        cue_id = str(cue.get("id", ""))
        words = cue.get("words")
        if isinstance(words, list) and words:
            for w in words:
                w_start = _number(w.get("start_ms", 0) / 1000.0, "word start")
                w_end = _number(w.get("end_ms", 0) / 1000.0, "word end")
                if w_end > w_start:
                    units.append({
                        "text": str(w.get("text", "")).strip(),
                        "start": w_start, "end": w_end,
                        "cue_id": cue_id, "is_word": True,
                        "probability": w.get("probability")
                    })
        else:
            c_start = _number(cue["start_ms"] / 1000.0, "cue start")
            c_end = _number(cue["end_ms"] / 1000.0, "cue end")
            if c_end > c_start:
                units.append({
                    "text": str(cue.get("text", "")).strip(),
                    "start": c_start, "end": c_end,
                    "cue_id": cue_id, "is_word": False,
                    "probability": None
                })

    if not units:
        raise ValueError("No valid speech units found in cues")

    units.sort(key=lambda u: (u["start"], u["end"]))

    # Detect fillers
    fillers_detected = []
    keep_units = []
    for u in units:
        norm_text = re.sub(r"[^\w\u4e00-\u9fa5]", "", u["text"]).strip().lower()
        if remove_fillers and norm_text and norm_text in target_fillers:
            fillers_detected.append({
                "text": u["text"],
                "normalized": norm_text,
                "start": u["start"],
                "end": u["end"],
                "duration": round(u["end"] - u["start"], 3),
                "cue_id": u["cue_id"],
                "is_word": u["is_word"],
                "confidence": u["probability"]
            })
        else:
            keep_units.append(u)

    if not keep_units:
        raise ValueError("All speech units were detected as fillers; cannot produce non-empty plan")

    # Build initial keep intervals with padding
    raw_intervals = []
    for u in keep_units:
        start = max(0.0, u["start"] - padding)
        end = min(source_duration, u["end"] + padding)
        if end > start:
            raw_intervals.append((start, end))

    raw_intervals.sort()
    merged = []
    for start, end in raw_intervals:
        if not merged:
            merged.append((start, end))
        else:
            prev_start, prev_end = merged[-1]
            if start <= prev_end + 0.02:  # merge touching or slightly overlapping intervals
                merged[-1] = (prev_start, max(prev_end, end))
            else:
                merged.append((start, end))

    # If compress_pauses is False, we keep the original flow except cutting out fillers
    final_intervals = []
    if not compress_pauses:
        # Cut only the detected fillers from the source coverage
        cursor = 0.0
        filler_spans = [(f["start"], f["end"]) for f in fillers_detected]
        filler_spans.sort()
        for f_start, f_end in filler_spans:
            if f_start > cursor:
                final_intervals.append((cursor, f_start))
            cursor = max(cursor, f_end)
        if cursor < source_duration:
            final_intervals.append((cursor, source_duration))
    else:
        # Merge gaps that are shorter than max_pause
        bridged = []
        for start, end in merged:
            if not bridged:
                bridged.append((start, end))
            else:
                prev_start, prev_end = bridged[-1]
                gap = start - prev_end
                if gap <= max_pause:
                    # Bridge this short natural pause
                    bridged[-1] = (prev_start, end)
                else:
                    bridged.append((start, end))
        final_intervals = bridged

    segments = []
    for idx, (start, end) in enumerate(final_intervals):
        start = round(max(0.0, start), 3)
        end = round(min(source_duration, end), 3)
        if end > start + 0.05:
            reason = "Speech segment #%d (%.2fs - %.2fs); speech preserved, fillers/pauses trimmed" % (idx + 1, start, end)
            segments.append({
                "sourceIn": start,
                "sourceOut": end,
                "reason": reason
            })

    if not segments:
        raise ValueError("No viable segments produced after speech cleaning")

    total_output = round(math.fsum(s["sourceOut"] - s["sourceIn"] for s in segments), 3)
    saved_time = round(max(0.0, source_duration - total_output), 3)

    return {
        "segments": segments,
        "source_duration": source_duration,
        "estimated_output_duration": total_output,
        "saved_seconds": saved_time,
        "fillers_detected": fillers_detected,
        "fillers_count": len(fillers_detected),
        "settings": {
            "max_pause_seconds": max_pause,
            "padding_seconds": padding,
            "remove_fillers": remove_fillers,
            "compress_pauses": compress_pauses
        },
        "review_required": True,
        "limitations": [
            "语气词与停顿消除基于文本规则与词级时间戳，并非人类编导语义审阅。",
            "可能误切作为重音或刻意留白的停顿；必须通过 preview 人工试听确认。",
            "生成的 segments 需由 Agent 或用户提交至 plan 任务生成正式剪辑计划。"
        ]
    }


HIGHLIGHT_KEYWORDS = (
    "为什么", "怎么做", "千万不要", "核心", "关键", "秘诀", "重点", "真相", "第一", "记住",
    "必须", "秘密", "其实", "最重要的", "注意", "注意看", "why", "how", "secret", "crucial",
    "key", "important", "remember", "never", "always", "tip"
)


def suggest_highlights(cues: list, source_duration: float, target_duration=30.0,
                       min_segment_seconds=2.0, max_segment_seconds=8.0,
                       analysis=None) -> dict:
    """Recommend highlight edit segments based on speech density, keywords, and shot quality.

    Combines character speech density, emphasis keywords, sentence question/exclamation
    signals, and shot quality penalties into an interpretable score (0..10).
    Greedily selects the highest-scoring candidate windows up to target_duration.
    """
    if not isinstance(cues, list) or not cues:
        raise ValueError("cues must be a non-empty list of subtitle cues")
    source_dur = _number(source_duration, "source_duration", positive=True)
    target = _number(target_duration, "target_duration", positive=True)
    min_seg = _number(min_segment_seconds, "min_segment_seconds", minimum=0.5, maximum=60.0)
    max_seg = _number(max_segment_seconds, "max_segment_seconds", minimum=min_seg, maximum=120.0)

    # Black and freeze issue ranges
    black_spans = []
    if isinstance(analysis, dict):
        for issue in analysis.get("quality_issues", []):
            if issue.get("type") == "black":
                s, e = issue.get("start", 0), issue.get("end", 0)
                if e > s:
                    black_spans.append((s, e))

    candidates = []
    # Build candidate segments by grouping contiguous cues up to max_seg
    num_cues = len(cues)
    for i in range(num_cues):
        start_ms = cues[i]["start_ms"]
        text_buf = []
        words_buf = []

        for j in range(i, num_cues):
            end_ms = cues[j]["end_ms"]
            seg_len = (end_ms - start_ms) / 1000.0

            if seg_len > max_seg and j > i:
                break

            text_buf.append(str(cues[j].get("text", "")).strip())
            if cues[j].get("words"):
                words_buf.extend(cues[j]["words"])

            if seg_len >= min_seg:
                combined_text = " ".join(text_buf).strip()
                s_in = round(start_ms / 1000.0, 3)
                s_out = round(end_ms / 1000.0, 3)

                # 1. Density Score (Characters per second)
                char_count = len(re.sub(r"\s+", "", combined_text))
                cps = char_count / max(0.1, seg_len)
                # Optimal speech rate is around 3.0 ~ 5.0 chars/sec
                if cps < 1.0:
                    density_score = 3.0
                elif cps <= 4.5:
                    density_score = 4.0 + (cps / 4.5) * 4.0  # up to 8.0
                else:
                    density_score = 7.5

                # 2. Keywords and Rhetorical Signals
                bonus = 0.0
                norm_lower = combined_text.lower()
                matched_keywords = [kw for kw in HIGHLIGHT_KEYWORDS if kw in norm_lower]
                if matched_keywords:
                    bonus += min(2.0, len(matched_keywords) * 0.8)

                if any(p in combined_text for p in ("？", "?", "！", "!")):
                    bonus += 0.5

                # 3. Quality penalty (Black frames overlap)
                penalty = 0.0
                for b_s, b_e in black_spans:
                    overlap = max(0.0, min(s_out, b_e) - max(s_in, b_s))
                    if overlap > 0.05:
                        penalty += 5.0

                final_score = round(max(0.0, min(10.0, density_score + bonus - penalty)), 2)

                reasons = []
                reasons.append("Speech density: %.1f chars/s" % cps)
                if matched_keywords:
                    reasons.append("Keywords: " + ", ".join(matched_keywords[:3]))
                if penalty > 0:
                    reasons.append("Black frame overlap penalty")

                candidates.append({
                    "sourceIn": s_in,
                    "sourceOut": s_out,
                    "score": final_score,
                    "length": seg_len,
                    "reason_text": "; ".join(reasons),
                    "text": combined_text
                })

            if seg_len >= max_seg:
                break

    if not candidates:
        raise ValueError("No candidate segments could be formed within duration constraints")

    # Greedy selection of non-overlapping top-scored segments
    candidates.sort(key=lambda c: (-c["score"], c["sourceIn"]))

    selected = []
    total_len = 0.0

    for c in candidates:
        if total_len + c["length"] > target + (min_seg * 0.5):
            continue
        # Check overlap with already selected
        overlap = False
        for s in selected:
            if max(c["sourceIn"], s["sourceIn"]) < min(c["sourceOut"], s["sourceOut"]):
                overlap = True
                break
        if not overlap:
            selected.append(c)
            total_len += c["length"]

    if not selected:
        # Fallback to the single highest-scoring segment
        selected = [candidates[0]]
        total_len = candidates[0]["length"]

    selected.sort(key=lambda s: s["sourceIn"])

    segments = []
    for idx, s in enumerate(selected, 1):
        reason = "Highlight #%d (Score: %.1f/10): %s" % (idx, s["score"], s["reason_text"])
        segments.append({
            "sourceIn": s["sourceIn"],
            "sourceOut": s["sourceOut"],
            "score": s["score"],
            "reason": reason
        })

    return {
        "segments": segments,
        "total_duration": round(sum(s["sourceOut"] - s["sourceIn"] for s in segments), 3),
        "target_duration": target,
        "selected_count": len(segments),
        "review_required": True,
        "method": "Multi-modal scoring combining character speech density, rhetoric keywords, and shot quality",
        "limitations": [
            "基于文本密度与关键词的启发式打分，不代表人类高阶审美或故事完整性判断。",
            "生成的 segments 需由用户或 Agent 结合上下文二次确认后提交 plan 任务执行。"
        ]
    }
