"""Typed local task dispatcher used from Harness bash/background jobs."""
from __future__ import annotations
import json
import os
import shutil
import signal
import time
import uuid
from pathlib import Path
from . import media, finishing
from .store import digest_file, write_json
from .subtitles import validate_cues, parse_subtitles, serialize_subtitles, qc_cues, apply_translations

TASKS = {
    "probe": set(), "proxy": set(), "extract_audio": set(),
    "analyze_shots": {"threshold", "min_shot_seconds", "max_shots"},
    "suggest_edit": {"analysis", "target_duration", "max_segment_seconds", "min_segment_seconds"},
    "suggest_highlights": {"track", "target_duration", "min_segment_seconds", "max_segment_seconds", "analysis"},
    "clean_speech": {"track", "fillers", "max_pause_seconds", "padding_seconds", "remove_fillers", "compress_pauses"},
    "review_plan": {"plan"},
    "diff_plans": {"base_plan", "target_plan"},
    "import_music": {"file", "rights_confirmed", "license_note"},
    "import_subtitles": {"file", "format", "cues"},
    "export_subtitles": {"track", "format"},
    "qc_subtitles": {"track"},
    "refine_subtitles": {"track", "max_chars", "split_punctuations"},
    "apply_translations": {"track", "translations", "language"},
    "transcribe": {"language", "model", "allow_download", "timeout"},
    "translate": {"track", "target_language", "source_language", "provider", "allow_network", "timeout"},
    "plan": {"segments", "remove_silence", "threshold_db", "min_duration", "padding", "aspect", "subtitle_mode", "track", "voice_manifest", "original_volume", "intent", "transition", "transitions", "visual", "music", "analysis"},
    "preview": {"plan"}, "render": {"plan"},
    "tts": {"track", "provider", "voice", "confirmed", "timeout"},
    "qc_media": {"artifact", "expected_duration"},
}


def cues_from(project, artifact):
    value = json.loads(project.artifact(artifact).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or "cues" not in value:
        raise ValueError("Expected a source-timeline subtitle track artifact")
    if value.get("edit_plan"):
        raise ValueError("Output-timeline track cannot be used as source track; import it into a project created from the edited video")
    if value.get("source_sha256") != project.manifest["source_sha256"]:
        raise ValueError("Track source binding mismatch")
    return validate_cues(value["cues"], round(media.probe(project.source)["duration"] * 1000))


def read_plan(project, artifact, approval=False):
    project.verify_dependencies(artifact)
    path = project.require_approval(artifact) if approval else project.artifact(artifact)
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("schema_version") != 1 or plan.get("source_sha256") != project.manifest["source_sha256"]:
        raise ValueError("Plan schema or source mismatch")
    if plan.get("music") and not plan.get("music_license"):
        raise ValueError("Legacy music plan lacks bound rights evidence; create and approve a new plan")
    return plan


def timeline_for_plan(plan, info):
    """Rebuild the executed frame timeline, including original plans without spans."""
    from .transitions import build_timeline
    segments = media.normalize_segments(plan["segments"], info)
    transitions = plan.get("transitions")
    uniform = None if transitions is not None else plan.get("transition")
    timeline = build_timeline(segments, uniform, str(media.frame_rate(info)), transitions=transitions)
    duration = finishing.number(plan["duration"], "approved duration", .000001, 1e12)
    if abs(timeline["duration"] - duration) > .001:
        raise ValueError("Approved duration does not match the executed frame timeline; create and approve a new plan")
    return timeline


def execute(project, job, work):
    kind, p = job["kind"], json.loads(job["params"])
    source = project.source
    def emit_json(name, value):
        path = work / name
        write_json(path, value)
        return [path], value
    if kind == "analyze_shots":
        from .analysis import analyze_shots
        value = analyze_shots(source, work, threshold=p.get("threshold", .3),
                              min_shot_seconds=p.get("min_shot_seconds", .5), max_shots=p.get("max_shots", 120))
        value["source_sha256"] = project.manifest["source_sha256"]
        for shot in value["shots"]:
            frame = Path(shot["frame_path"]).resolve(strict=True)
            if not frame.is_relative_to(work.resolve()):
                raise ValueError("Analysis frame escaped the private job directory")
            artifact = project.register(job["id"], frame)
            shot["frame_artifact_id"] = artifact["id"]
            shot["frame_path"] = artifact["path"]
        return emit_json("shot-analysis.json", value)
    if kind == "suggest_edit":
        from .analysis import suggest_edit
        analysis = json.loads(project.artifact(p["analysis"]).read_text())
        if analysis.get("source_sha256") != project.manifest["source_sha256"] or "shots" not in analysis:
            raise ValueError("Expected a source-bound shot-analysis artifact")
        value = suggest_edit(analysis, target_duration=p.get("target_duration", 30),
                             max_segment_seconds=p.get("max_segment_seconds", 6), min_segment_seconds=p.get("min_segment_seconds", 1))
        value["analysis"] = p["analysis"]
        value["source_sha256"] = project.manifest["source_sha256"]
        return emit_json("edit-suggestions.json", value)
    if kind == "suggest_highlights":
        from .analysis import suggest_highlights
        cues = cues_from(project, p["track"])
        analysis_data = None
        if p.get("analysis"):
            analysis_data = json.loads(project.artifact(p["analysis"]).read_text())
        duration = media.probe(source)["duration"]
        value = suggest_highlights(cues, duration,
                                   target_duration=p.get("target_duration", 30.0),
                                   min_segment_seconds=p.get("min_segment_seconds", 2.0),
                                   max_segment_seconds=p.get("max_segment_seconds", 8.0),
                                   analysis=analysis_data)
        value["track"] = p["track"]
        value["source_sha256"] = project.manifest["source_sha256"]
        return emit_json("highlight-suggestions.json", value)
    if kind == "import_music":
        original = Path(p["file"]).resolve(strict=True)
        if p.get("rights_confirmed") is not True or digest_file(original) != p["expected_sha256"]:
            raise ValueError("Music rights not confirmed or import content changed")
        info = media.probe(original)
        if not any(s.get("codec_type") == "audio" for s in info["streams"]) or any(s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic") for s in info["streams"]):
            raise ValueError("Music import requires an audio-only local file")
        suffix = original.suffix.lower()
        if len(suffix) > 12 or not suffix.replace(".", "").isalnum():
            raise ValueError("Invalid audio filename extension")
        target = work / ("music" + suffix)
        shutil.copyfile(original, target)
        if digest_file(target) != p["expected_sha256"]:
            raise ValueError("Music changed during import")
        note = {"rights_confirmed": True, "license_note": p.get("license_note", "User confirmed usage rights; not a legal verification"),
                "sha256": p["expected_sha256"], "original_name": original.name, "media": info}
        metadata = work / "music-license.json"
        write_json(metadata, note)
        return [target, metadata], note
    if kind == "probe":
        return emit_json("probe.json", media.probe(source))
    if kind == "proxy":
        path = work / "proxy.mp4"
        media.proxy(source, path)
        return [path], media.probe(path)
    if kind == "extract_audio":
        path = work / "audio.wav"
        media.extract_audio(source, path)
        return [path], media.probe(path)
    if kind == "import_subtitles":
        if "cues" in p:
            cues = validate_cues(p["cues"])
        else:
            path = Path(p["file"]).resolve(strict=True)
            if path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("Subtitle file exceeds 16 MiB")
            cues = parse_subtitles(path.read_text(encoding="utf-8-sig"), p.get("format", path.suffix.lstrip(".")))
        cues = validate_cues(cues, round(media.probe(source)["duration"] * 1000))
        return emit_json("track.json", {"cues": cues, "source_sha256": project.manifest["source_sha256"]})
    if kind == "export_subtitles":
        fmt = p.get("format", "srt").lower()
        if fmt not in ("srt", "vtt", "ass"):
            raise ValueError("Unsupported subtitle format")
        path = work / ("subtitles." + fmt)
        path.write_text(serialize_subtitles(cues_from(project, p["track"]), fmt), encoding="utf-8")
        return [path], {"format": fmt}
    if kind == "apply_translations":
        cues = apply_translations(cues_from(project, p["track"]), p["translations"])
        return emit_json("translated-track.json", {"cues": cues, "language": p.get("language"), "parent_track": p["track"], "provider": "harness-conversation", "source_sha256": project.manifest["source_sha256"]})
    if kind == "qc_subtitles":
        return emit_json("subtitle-qc.json", {"issues": qc_cues(cues_from(project, p["track"]), round(media.probe(source)["duration"] * 1000))})
    if kind == "refine_subtitles":
        from .subtitles import refine_subtitles
        cues = cues_from(project, p["track"])
        refined = refine_subtitles(cues, max_chars=p.get("max_chars", 20),
                                   split_punctuations=p.get("split_punctuations", "，。！？；,!?;"))
        return emit_json("refined-track.json", {"cues": refined, "parent_track": p["track"], "source_sha256": project.manifest["source_sha256"]})
    if kind == "transcribe":
        from .providers import transcribe
        value = transcribe(source, work, language=p.get("language"), model=p.get("model", "small"), allow_download=p.get("allow_download", False), timeout=p.get("timeout", 3600))
        value["cues"] = validate_cues(value["cues"], round(media.probe(source)["duration"] * 1000))
        value["source_sha256"] = project.manifest["source_sha256"]
        return emit_json("track.json", value)
    if kind == "translate":
        from .providers import translate
        cues = translate(cues_from(project, p["track"]), target_language=p["target_language"],
                         source_language=p.get("source_language", "auto"), provider=p.get("provider", "argos"),
                         allow_network=p.get("allow_network", False), timeout=p.get("timeout", 120))
        return emit_json("translated-track.json", {"cues": validate_cues(cues), "language": p["target_language"], "parent_track": p["track"], "source_sha256": project.manifest["source_sha256"]})
    if kind == "plan":
        info = media.probe(source)
        segments = p.get("segments")
        if p.get("remove_silence"):
            if segments is not None:
                raise ValueError("Choose segments OR remove_silence")
            segments = media.silence_segments(source, p.get("threshold_db", -35), p.get("min_duration", 0.6), p.get("padding", 0.12))
        if segments is None:
            segments = [{"sourceIn": 0, "sourceOut": info["duration"], "reason": "keep source"}]
        segments = media.normalize_segments(segments, info)
        aspect = p.get("aspect", "source")
        mode = p.get("subtitle_mode", "none")
        if aspect not in ("source", "16:9", "9:16", "1:1") or mode not in ("none", "soft", "hard"):
            raise ValueError("Unsupported aspect or subtitle mode")
        if mode != "none" and not p.get("track"):
            raise ValueError("Subtitle track required")
        if p.get("track"):
            cues_from(project, p["track"])
        if p.get("voice_manifest"):
            manifest = json.loads(project.artifact(p["voice_manifest"]).read_text())
            if manifest.get("source_sha256") != project.manifest["source_sha256"]:
                raise ValueError("Voice manifest source mismatch")
            if p.get("track"):
                subtitle_cues = cues_from(project, p["track"])
                voice_cues = manifest.get("clips", [])
                signature = lambda items: [(c["id"], c["start_ms"], c["end_ms"]) for c in items]
                if signature(subtitle_cues) != signature(voice_cues):
                    raise ValueError("Voice and subtitle tracks must have identical cue IDs and timing (text may differ)")
            if len(segments) != 1 or segments[0]["sourceIn"] != 0 or abs(segments[0]["sourceOut"] - info["duration"]) > (1 / float(media.frame_rate(info)) + .05):
                raise ValueError("Voice manifests currently require full-source plans; generate a new project from the cut video before dubbing")
        from .transitions import audio_ranges, build_timeline
        if p.get("transition") is not None and p.get("transitions") is not None:
            raise ValueError("Choose transition OR transitions, not both")
        timeline = build_timeline(segments, p.get("transition"), str(media.frame_rate(info)),
                                  transitions=p.get("transitions"))
        if any(item["duration"] > 60 for item in timeline["transitions"]):
            raise ValueError("FFmpeg xfade supports at most 60 seconds per transition")
        if any(item["jcut_frames"] or item["lcut_frames"] for item in timeline["transitions"]):
            if not any(s.get("codec_type") == "audio" for s in info["streams"]):
                raise ValueError("J/L audio offsets require a source audio stream")
            audio_ranges(timeline, max(1, round(info["duration"] * float(media.frame_rate(info)))))
        visual = finishing.normalize_visual(p.get("visual"), timeline["duration"])
        music = finishing.normalize_music(p.get("music"), timeline["duration"])
        music_license = None
        if music:
            music_path = project.artifact(music["artifact_id"])
            with project.connect() as c:
                origin = c.execute("SELECT j.id,j.kind,j.status FROM artifacts a JOIN jobs j ON a.job_id=j.id WHERE a.id=?", (music["artifact_id"],)).fetchone()
            if not origin or origin["kind"] != "import_music" or origin["status"] != "completed":
                raise ValueError("Background music must come from a completed rights-confirmed import_music task")
            license_items = [a for a in project.artifacts_for_job(origin["id"]) if a["path"].endswith("/music-license.json")]
            if len(license_items) != 1:
                raise ValueError("Music import must have one registered rights record")
            music_license = license_items[0]["id"]
            license_data = json.loads(project.artifact(music_license).read_text())
            if license_data.get("rights_confirmed") is not True or license_data.get("sha256") != digest_file(music_path):
                raise ValueError("Music rights record does not bind to this audio content")
            audio_info = media.probe(music_path)
            if not any(s.get("codec_type") == "audio" for s in audio_info["streams"]) or music["offset"] >= audio_info["duration"]:
                raise ValueError("Invalid music audio or offset")
        if p.get("analysis"):
            analysis = json.loads(project.artifact(p["analysis"]).read_text())
            if analysis.get("source_sha256") != project.manifest["source_sha256"] or "shots" not in analysis:
                raise ValueError("Analysis provenance must be a shot-analysis artifact from this source")
        plan = {"schema_version": 1, "source_sha256": project.manifest["source_sha256"], "segments": segments,
                "aspect": aspect, "subtitle_mode": mode, "track": p.get("track"), "voice_manifest": p.get("voice_manifest"),
                "original_volume": p.get("original_volume", 0.15), "intent": p.get("intent", "explicit edit plan"),
                "duration": timeline["duration"], "review_required": True, "timeline": {**timeline, "cuts_snapped_to_frames": True},
                "transition": timeline["transition"], "transitions": timeline["transitions"],
                "visual": visual, "music": music, "music_license": music_license, "analysis": p.get("analysis"),
                "warnings": timeline.get("warnings", []) + (["Music ducking responds to source audio energy, not speech recognition"] if music and music["ducking"] else [])}
        return emit_json("edit-plan.json", plan)
    if kind == "review_plan":
        from .editorial import review_plan
        plan = read_plan(project, p["plan"])
        cues = cues_from(project, plan["track"]) if plan.get("track") else None
        analysis = json.loads(project.artifact(plan["analysis"]).read_text()) if plan.get("analysis") else None
        value = review_plan(plan, cues, analysis)
        value["plan"] = p["plan"]
        return emit_json("editorial-review.json", value)
    if kind == "clean_speech":
        from .analysis import clean_speech
        cues = cues_from(project, p["track"])
        duration = media.probe(source)["duration"]
        value = clean_speech(cues, duration, fillers=p.get("fillers"),
                             max_pause_seconds=p.get("max_pause_seconds", 0.6),
                             padding_seconds=p.get("padding_seconds", 0.08),
                             remove_fillers=p.get("remove_fillers", True),
                             compress_pauses=p.get("compress_pauses", True))
        value["track"] = p["track"]
        value["source_sha256"] = project.manifest["source_sha256"]
        return emit_json("speech-clean-suggestions.json", value)
    if kind == "diff_plans":
        from .editorial import diff_plans
        base = read_plan(project, p["base_plan"])
        target = read_plan(project, p["target_plan"])
        value = diff_plans(base, target)
        value["base_plan"] = p["base_plan"]
        value["target_plan"] = p["target_plan"]
        return emit_json("plan-diff.json", value)
    if kind in ("render", "preview"):
        plan = read_plan(project, p["plan"], approval=kind == "render")
        executed_timeline = timeline_for_plan(plan, media.probe(source))
        base = work / "cut.mp4"
        from .transitions import render_sequence
        if plan.get("transitions") is not None:
            render_sequence(source, plan["segments"], base, plan["aspect"],
                            transitions=plan["transitions"])
        else:
            render_sequence(source, plan["segments"], base, plan["aspect"], plan.get("transition"))
        paths = []
        if plan.get("voice_manifest"):
            voice_path = project.artifact(plan["voice_manifest"])
            manifest = json.loads(voice_path.read_text())
            # Resolve only registered, integrity-checked audio artifacts, never arbitrary manifest paths.
            for clip in manifest["clips"]:
                clip["path"] = str(project.artifact(clip["artifact_id"]))
            dubbed = work / "dubbed.mp4"
            media.dub(base, manifest, dubbed, plan["original_volume"])
            base = dubbed
        if plan.get("music"):
            mixed = work / "music-mixed.mp4"
            finishing.mix_music(base, project.artifact(plan["music"]["artifact_id"]), mixed, plan["music"], plan["duration"])
            base = mixed
        visual = finishing.normalize_visual(plan.get("visual"), plan["duration"])
        if finishing.has_visual_effect(visual):
            styled = work / "styled.mp4"
            finishing.apply_visual(base, styled, visual)
            base = styled
        if plan.get("track"):
            from .transitions import retime_cues as retime_with_transitions
            cues = retime_with_transitions(cues_from(project, plan["track"]), executed_timeline)
            track = work / "edited-track.json"
            write_json(track, {"cues": cues, "edit_plan": p["plan"]})
            subtitle = work / "edited.srt"
            subtitle.write_text(serialize_subtitles(cues, "srt"), encoding="utf-8")
            paths += [track, subtitle]
            if plan["subtitle_mode"] != "none":
                target = work / "subtitled.mp4"
                media.subtitle_mux(base, subtitle, target, plan["subtitle_mode"])
                base = target
        if kind == "preview":
            target = work / "preview.mp4"
            media.proxy(base, target)
            base = target
        qc = media.quality_check(base, plan["duration"])
        qc_path = work / "qc.json"
        write_json(qc_path, qc)
        paths += [base, qc_path]
        if not qc["passed"]:
            raise ValueError("Output duration QC failed; no approved deliverable")
        return paths, {"plan": p["plan"], "preview": kind == "preview", "qc": qc}
    if kind == "tts":
        if p.get("confirmed") is not True:
            raise ValueError("TTS requires explicit confirmed=true after voice review; cloning is not supported")
        from .providers import synthesize
        provider = p.get("provider", "piper")
        voice = Path(p["voice"]).resolve(strict=True) if provider == "piper" and p.get("voice") else p.get("voice")
        value = synthesize(cues_from(project, p["track"]), work, provider=provider, voice=voice, timeout=p.get("timeout", 120))
        clips = value.pop("cues")
        value.pop("manifest", None)
        value["clips"] = clips
        for clip in clips:
            clip_path = Path(clip.pop("wav_path")).resolve(strict=True)
            if not clip_path.is_relative_to(work.resolve()):
                raise ValueError("Provider output escaped private job directory")
            artifact = project.register(job["id"], clip_path)
            clip["artifact_id"] = artifact["id"]
            clip.pop("path", None)
        value["track"] = p["track"]
        value["source_sha256"] = project.manifest["source_sha256"]
        return emit_json("voice-manifest.json", value)
    if kind == "qc_media":
        return emit_json("media-qc.json", media.quality_check(project.artifact(p["artifact"]), p.get("expected_duration")))
    raise ValueError("Unknown task")


def execution_context(params):
    context = {key: os.environ.get(key) for key in ("VIDEO_AGENT_FFMPEG", "VIDEO_AGENT_FFPROBE", "VIDEO_AGENT_MODEL", "VIDEO_AGENT_API_BASE")}
    for key in ("model", "voice"):
        if isinstance(params.get(key), str):
            model_path = Path(params[key])
            if model_path.is_file():
                context[key + "_sha256"] = digest_file(model_path)
                sidecar = Path(str(model_path) + ".json")
                if sidecar.is_file():
                    context[key + "_config_sha256"] = digest_file(sidecar)
            elif model_path.is_dir():
                context[key + "_files"] = {str(f.relative_to(model_path)): digest_file(f) for f in sorted(model_path.rglob("*")) if f.is_file()}
    # Hash implementation code, not API secrets, so upgrades do not reuse stale outputs.
    context["implementation"] = [digest_file(f) for f in sorted(Path(__file__).parent.glob("*.py"))]
    return context


def submit(project, kind, params, fresh=False):
    if kind not in TASKS or not isinstance(params, dict):
        raise ValueError("Unknown task or invalid parameters")
    unknown = set(params) - TASKS[kind]
    if unknown:
        raise ValueError("Unknown parameters: " + ", ".join(sorted(unknown)))
    for key in ("allow_network", "allow_download", "confirmed", "remove_silence", "rights_confirmed", "remove_fillers", "compress_pauses"):
        if key in params and not isinstance(params[key], bool):
            raise ValueError(key + " must be boolean")
    params = dict(params)
    if "timeout" in params:
        import math
        timeout = params["timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 86400:
            raise ValueError("timeout must be 1..86400 seconds")
    # Cache import by CONTENT, not mutable input path. Persist content for safe retry.
    if kind == "import_subtitles" and "file" in params:
        if "cues" in params:
            raise ValueError("Choose file OR cues")
        path = Path(params.pop("file")).resolve(strict=True)
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Subtitle file exceeds 16 MiB")
        params["cues"] = parse_subtitles(path.read_text(encoding="utf-8-sig"), params.get("format", path.suffix.lstrip(".")))
    if kind == "import_music":
        if params.get("rights_confirmed") is not True:
            raise ValueError("Music import requires rights_confirmed=true after checking its usage rights")
        params["file"] = str(Path(params["file"]).resolve(strict=True))
        params["expected_sha256"] = digest_file(params["file"])
    # Validate direct and transitive dependencies even on cached runs.
    project.source
    for key in ("track", "plan", "base_plan", "target_plan", "voice_manifest", "artifact", "analysis"):
        if params.get(key):
            project.verify_dependencies(params[key])
    if params.get("plan"):
        read_plan(project, params["plan"])  # Fail closed for legacy music plans, even on cached runs.
    if params.get("base_plan"):
        read_plan(project, params["base_plan"])
    if params.get("target_plan"):
        read_plan(project, params["target_plan"])
    context = execution_context(params)
    job = project.enqueue(kind, params, nonce=uuid.uuid4().hex if fresh else None, context=context)
    if job["status"] == "completed":
        result = json.loads(job["result"])
        for artifact in result["artifacts"]:
            project.verify_dependencies(artifact["id"])
        return {"cached": True, **result, "job_id": job["id"]}
    if job["status"] != "pending":
        return {"job_id": job["id"], "status": job["status"], "error": job["error"], "hint": "Use retry only after a failed/cancelled job; never duplicate running work"}
    return run_job(project, job["id"])


def run_job(project, job_id, retry=False):
    original = project.job(job_id)
    recorded = project.context(job_id)
    if recorded and recorded != execution_context(json.loads(original["params"])):
        raise ValueError("Execution configuration or implementation changed; submit a new task instead of silently retrying with a different model")
    job = project.claim(job_id, retry=retry)
    work = project.root / "jobs" / job_id / ("attempt-" + str(job["attempts"]))
    previous = signal.getsignal(signal.SIGTERM)
    installed_handler = False
    log_token = media.COMMAND_LOG.set(work / "commands.jsonl")
    try:
        work.mkdir(parents=True, exist_ok=False)
        write_json(work / "request.json", {"kind": job["kind"], "params": json.loads(job["params"]), "context": project.context(job_id), "started": time.time()})
        def terminate(signum, frame):
            raise KeyboardInterrupt("Harness job cancelled")
        signal.signal(signal.SIGTERM, terminate)
        installed_handler = True
        paths, detail = execute(project, job, work)
        artifacts = [project.register(job_id, path) for path in paths]
        direct_ids = {a["id"] for a in artifacts}
        artifacts += [a for a in project.artifacts_for_job(job_id) if a["id"] not in direct_ids]
        result = {"job_id": job_id, "status": "completed", "artifacts": artifacts, "detail": detail}
        write_json(work / "result.json", result)
        project.finish(job_id, "completed", result=result)
        return result
    except BaseException as exc:
        cancelled = isinstance(exc, (KeyboardInterrupt, SystemExit))
        error = type(exc).__name__ + ": " + str(exc)
        project.finish(job_id, "cancelled" if cancelled else "failed", error=error)
        try:
            write_json(work / "error.json", {"error": error, "status": "cancelled" if cancelled else "failed"})
        except OSError:
            pass  # Ledger still records initialization/disk failures.
        raise
    finally:
        media.COMMAND_LOG.reset(log_token)
        if installed_handler:
            signal.signal(signal.SIGTERM, previous)
