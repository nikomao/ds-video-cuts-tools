"""Evidence-based mechanical editorial review; not a model or aesthetic score."""
from __future__ import annotations


def review_plan(plan, cues=None, analysis=None):
    issues = []
    def add(code, message, segment_index=None, evidence=None):
        item = {"code": code, "severity": "warning", "message": message}
        if segment_index is not None:
            item["segment_index"] = segment_index
        if evidence is not None:
            item["evidence"] = evidence
        issues.append(item)
    segments = plan["segments"]
    for i, segment in enumerate(segments):
        start, end = segment["sourceIn"], segment["sourceOut"]
        duration = end - start
        if duration < 1:
            add("very_short_shot", "镜头不足1秒；可能适合快剪，但请确认观众能理解画面。", i)
        if duration > 12:
            add("long_shot", "镜头超过12秒；是否保留长镜头应由叙事需要决定。", i)
        if not str(segment.get("reason", "")).strip() or segment.get("reason") in ("keep source", "user selection"):
            add("missing_editorial_reason", "未记录具体的镜头保留理由，请结合素材和目标补充。", i)
        for previous in segments[:i]:
            overlap = min(end, previous["sourceOut"]) - max(start, previous["sourceIn"])
            if overlap > .04:
                add("repeated_source", "重复使用了之前已选择的源画面；请确认是否为刻意重放。", i)
                break
        if analysis:
            for issue in analysis.get("quality_issues", []):
                lo, hi = issue.get("start"), issue.get("end")
                if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and max(start, lo) < min(end, hi):
                    add("source_quality_warning", "选中区间与低层黑场/冻结告警相交，需人工判断是否故意。", i, issue)
        if cues:
            for boundary, value in (("in", start), ("out", end)):
                milliseconds = value * 1000
                # The beginning/end of the whole source is not an edit discontinuity.
                if (boundary == "in" and i == 0 and start == 0):
                    continue
                for cue in cues:
                    words = cue.get("words", [])
                    candidates = words if words else [cue]
                    for unit in candidates:
                        if unit["start_ms"] + 20 < milliseconds < unit["end_ms"] - 20:
                            add("cut_inside_word" if words else "cut_inside_cue",
                                "切点落在词/句时间范围内，可能截断对白；请试听并调整。", i,
                                {"cue_id": cue["id"], "boundary": boundary, "source_seconds": value, "text": unit.get("text", "")})
                            break
                    else:
                        continue
                    break
    boundaries = plan.get("transitions")
    if boundaries is None:
        base = plan.get("transition", {"type": "cut"})
        boundaries = [base] * (len(segments) - 1)
    non_cut = [item for item in boundaries if isinstance(item, dict) and item.get("type", "cut") != "cut"]
    if non_cut:
        if cues:
            add("dialogue_overlap", "视频转场同时交叠原音；对白可能重叠。建议对白优先硬切，或人工检查每个接点。")
        if any(item.get("type") not in ("fade", "dissolve", "fadeblack") for item in non_cut):
            add("stylized_transition", "滑动/擦除是明显风格化转场，不默认等于电影感；确认适合内容。")
    audio_boundaries = plan.get("transitions")
    if isinstance(audio_boundaries, list) and any(
            item.get("jcut_frames") or item.get("lcut_frames") for item in audio_boundaries):
        add("audio_offset_overlap", "J/L 音画错位会让相邻音频重叠，未做自动 ducking/限幅；请试听对白清晰度与削波。")
    music = plan.get("music")
    if music and music.get("ducking"):
        add("energy_not_speech", "配乐压低由源音频能量触发，并非语音识别；音乐或噪声也会触发。")
    if plan.get("visual", {}).get("letterbox"):
        add("letterbox_occlusion", "黑边遮盖上下画面，不会理解主体或自动避开已有字幕；请检查构图。")
    visual_cfg = plan.get("visual", {})
    if visual_cfg.get("motion") and visual_cfg["motion"] != "none":
        add("camera_motion", f"配置了镜头运镜动画 ({visual_cfg['motion']})；画面将平滑微移/缩放，请检查边界关键信息。")
    if visual_cfg.get("intro_animation") and visual_cfg["intro_animation"] != "none":
        add("intro_animation_notice", f"配置了特色入场动画 ({visual_cfg['intro_animation']})；请在预览中确认开场观感。")
    return {"scope": "mechanical editorial heuristics only; no aesthetic or factual certification",
            "review_required": True, "issues": issues, "source_segments": len(segments),
            "output_duration": plan["duration"], "limitations": [
                "不判断人物表情、情绪弧线、故事真实性或镜头连续性。",
                "黑场/冻结可能是有意设计，不能据此自动删除。",
                "没有词级时间戳时只能提示句内切点，不能声称检测到了截词。"]}


def diff_plans(base_plan: dict, target_plan: dict) -> dict:
    """Compare two EditPlans and produce a structured, human-readable diff.

    Checks duration changes, added/removed coverage intervals, transitions,
    audio, visual presets, and subtitle modes. Fails if plans originate
    from different sources.
    """
    if not isinstance(base_plan, dict) or not isinstance(target_plan, dict):
        raise ValueError("Both base_plan and target_plan must be dictionaries")
    if base_plan.get("source_sha256") != target_plan.get("source_sha256"):
        raise ValueError("Cannot compare plans from different media sources")

    base_dur = float(base_plan.get("duration", 0.0))
    target_dur = float(target_plan.get("duration", 0.0))
    delta_dur = round(target_dur - base_dur, 3)

    # Calculate intervals coverage
    def _extract_spans(plan):
        spans = []
        for s in plan.get("segments", []):
            spans.append((float(s["sourceIn"]), float(s["sourceOut"])))
        return sorted(spans)

    base_spans = _extract_spans(base_plan)
    target_spans = _extract_spans(target_plan)

    def _subtract_spans(spans_a, spans_b):
        # Return portions of spans_a that are NOT covered by spans_b
        res = []
        for a_start, a_end in spans_a:
            cur = [(a_start, a_end)]
            for b_start, b_end in spans_b:
                next_cur = []
                for s, e in cur:
                    if b_end <= s or b_start >= e:
                        next_cur.append((s, e))
                    else:
                        if b_start > s:
                            next_cur.append((s, b_start))
                        if b_end < e:
                            next_cur.append((b_end, e))
                cur = next_cur
            res.extend(cur)
        return [(round(s, 3), round(e, 3)) for s, e in res if e - s > 0.02]

    removed_ranges = _subtract_spans(base_spans, target_spans)
    added_ranges = _subtract_spans(target_spans, base_spans)

    removed_time = round(sum(e - s for s, e in removed_ranges), 3)
    added_time = round(sum(e - s for s, e in added_ranges), 3)

    # Compare transitions
    base_trans = base_plan.get("transitions") or base_plan.get("transition")
    target_trans = target_plan.get("transitions") or target_plan.get("transition")
    transitions_changed = base_trans != target_trans

    # Compare visual & music & subtitles
    visual_changed = base_plan.get("visual") != target_plan.get("visual")
    music_changed = base_plan.get("music") != target_plan.get("music")
    subtitles_changed = (base_plan.get("track") != target_plan.get("track") or
                         base_plan.get("subtitle_mode") != target_plan.get("subtitle_mode"))

    # Construct human readable summary bullet points
    summary_lines = []
    if abs(delta_dur) < 0.001:
        summary_lines.append("成片时长保持不变 (%.2fs)。" % target_dur)
    elif delta_dur > 0:
        summary_lines.append("成片时长延长了 %.2fs (由 %.2fs 增至 %.2fs)。" % (delta_dur, base_dur, target_dur))
    else:
        summary_lines.append("成片时长缩减了 %.2fs (由 %.2fs 缩减至 %.2fs)。" % (abs(delta_dur), base_dur, target_dur))

    if removed_ranges:
        summary_lines.append("新删减了 %d 处原片内容 (共减少 %.2fs 原片素材)。" % (len(removed_ranges), removed_time))
    if added_ranges:
        summary_lines.append("新增召回了 %d 处原片内容 (共增加 %.2fs 原片素材)。" % (len(added_ranges), added_time))

    if transitions_changed:
        summary_lines.append("调整了转场方案 (原配置: %s -> 新配置: %s)。" % (base_trans, target_trans))
    if visual_changed:
        summary_lines.append("更新了视觉滤镜/黑边样式配置。")
    if music_changed:
        summary_lines.append("更新了背景配乐配置。")
    if subtitles_changed:
        summary_lines.append("更新了字幕轨或字幕烧录模式 (原模式: %s -> 新模式: %s)。" %
                             (base_plan.get("subtitle_mode"), target_plan.get("subtitle_mode")))

    return {
        "source_sha256": base_plan.get("source_sha256"),
        "duration": {
            "base": base_dur,
            "target": target_dur,
            "delta": delta_dur
        },
        "coverage": {
            "base_segment_count": len(base_plan.get("segments", [])),
            "target_segment_count": len(target_plan.get("segments", [])),
            "removed_ranges": removed_ranges,
            "removed_seconds": removed_time,
            "added_ranges": added_ranges,
            "added_seconds": added_time
        },
        "changes": {
            "transitions_changed": transitions_changed,
            "visual_changed": visual_changed,
            "music_changed": music_changed,
            "subtitles_changed": subtitles_changed
        },
        "human_summary": " \n".join(summary_lines)
    }
