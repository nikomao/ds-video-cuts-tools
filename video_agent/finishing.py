"""Conservative visual looks, program fades and licensed local music mixing.

These are reproducible presets, not automatic grading, music selection or a claim
of cinematic quality. All numeric controls are bounded and filters are fixed.
"""
from __future__ import annotations
import math
from pathlib import Path
from . import media

LOOKS = {
    "natural": None,
    "warm": "eq=contrast=1.04:saturation=1.06,colorbalance=rs=0.035:bs=-0.025",
    "cool": "eq=contrast=1.04:saturation=0.94,colorbalance=rs=-0.025:bs=0.035",
    "monochrome": "hue=s=0,eq=contrast=1.06:brightness=-0.005",
    "film": "eq=contrast=1.08:saturation=1.05,colorbalance=rs=0.04:gs=-0.01:bs=-0.03:rh=0.03:bh=-0.02",
    "vintage": "eq=contrast=0.96:saturation=0.88:brightness=0.02,colorbalance=rs=0.06:gs=0.02:bs=-0.06",
    "cyberpunk": "eq=contrast=1.15:saturation=1.2,colorbalance=rs=-0.06:bs=0.08:rm=0.05:bm=0.05",
    "vibrant": "eq=contrast=1.06:saturation=1.25:brightness=0.01",
}

MOTIONS = {"none", "zoom_in", "zoom_out", "pan_left", "pan_right"}
INTRO_ANIMATIONS = {"none", "fade", "white_fade", "zoom_in"}


def number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be a finite number in {low}..{high}")
    return float(value)


def normalize_visual(config, duration):
    if config is None:
        config = {}
    allowed = {"look", "fade_in", "fade_out", "letterbox", "motion", "intro_animation"}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError(f"Visual settings allow only {', '.join(sorted(allowed))}")
    look = config.get("look", "natural")
    if not isinstance(look, str) or look not in LOOKS:
        raise ValueError(f"Unsupported visual look: {look}. Choices: {', '.join(sorted(LOOKS))}")
    motion = config.get("motion", "none")
    if not isinstance(motion, str) or motion not in MOTIONS:
        raise ValueError(f"Unsupported motion: {motion}. Choices: {', '.join(sorted(MOTIONS))}")
    intro = config.get("intro_animation", "none")
    if not isinstance(intro, str) or intro not in INTRO_ANIMATIONS:
        raise ValueError(f"Unsupported intro_animation: {intro}. Choices: {', '.join(sorted(INTRO_ANIMATIONS))}")
    incoming = number(config.get("fade_in", 0), "fade_in", 0, min(5, duration / 2))
    outgoing = number(config.get("fade_out", 0), "fade_out", 0, min(5, duration / 2))
    letterbox = number(config.get("letterbox", 0), "letterbox", 0, 0.2)
    return {
        "look": look,
        "fade_in": incoming,
        "fade_out": outgoing,
        "letterbox": letterbox,
        "motion": motion,
        "intro_animation": intro
    }


def normalize_music(config, duration):
    if config is None:
        return None
    if not isinstance(config, dict) or set(config) - {"artifact_id", "volume", "ducking", "fade_in", "fade_out", "loop", "offset"}:
        raise ValueError("Unknown music settings")
    if not isinstance(config.get("artifact_id"), str) or not config["artifact_id"].strip():
        raise ValueError("Music needs a registered artifact_id")
    for key in ("ducking", "loop"):
        if key in config and not isinstance(config[key], bool):
            raise ValueError(key + " must be boolean")
    return {"artifact_id": config["artifact_id"],
            "volume": number(config.get("volume", 0.15), "music.volume", 0, 1),
            "ducking": config.get("ducking", True), "loop": config.get("loop", True),
            "offset": number(config.get("offset", 0), "music.offset", 0, 86400),
            "fade_in": number(config.get("fade_in", min(.3, duration / 2)), "music.fade_in", 0, min(10, duration / 2)),
            "fade_out": number(config.get("fade_out", min(.8, duration / 2)), "music.fade_out", 0, min(10, duration / 2))}


def has_visual_effect(config):
    return (config["look"] != "natural" or
            bool(config.get("fade_in") or config.get("fade_out") or config.get("letterbox")) or
            config.get("motion", "none") != "none" or
            config.get("intro_animation", "none") != "none")


def apply_visual(source, output, config):
    info = media.probe(source)
    duration = info["duration"]
    config = normalize_visual(config, duration)
    video_stream = next(s for s in info["streams"] if s.get("codec_type") == "video")
    w = int(video_stream.get("width", 1280))
    h = int(video_stream.get("height", 720))
    w = (w // 2) * 2
    h = (h // 2) * 2

    filters = []
    if LOOKS[config["look"]]:
        filters.append(LOOKS[config["look"]])

    dur_str = f"{duration:.3f}"
    motion = config.get("motion", "none")
    if motion == "zoom_in":
        filters.append(f"scale=eval=frame:w='trunc(iw*(1+0.08*min(t,{dur_str})/{dur_str})/2)*2':h='trunc(ih*(1+0.08*min(t,{dur_str})/{dur_str})/2)*2',crop={w}:{h}:(iw-ow)/2:(ih-oh)/2")
    elif motion == "zoom_out":
        filters.append(f"scale=eval=frame:w='trunc(iw*(1.08-0.08*min(t,{dur_str})/{dur_str})/2)*2':h='trunc(ih*(1.08-0.08*min(t,{dur_str})/{dur_str})/2)*2',crop={w}:{h}:(iw-ow)/2:(ih-oh)/2")
    elif motion == "pan_left":
        filters.append(f"scale=trunc(iw*1.08/2)*2:trunc(ih*1.08/2)*2,crop={w}:{h}:x='(iw-ow)*(1-min(t,{dur_str})/{dur_str})':y='(ih-oh)/2'")
    elif motion == "pan_right":
        filters.append(f"scale=trunc(iw*1.08/2)*2:trunc(ih*1.08/2)*2,crop={w}:{h}:x='(iw-ow)*(min(t,{dur_str})/{dur_str})':y='(ih-oh)/2'")

    intro = config.get("intro_animation", "none")
    if intro == "zoom_in":
        snap_dur = min(0.6, duration / 2)
        snap_str = f"{snap_dur:.3f}"
        filters.append(f"scale=eval=frame:w='trunc(iw*(1+0.12*max(0,1-t/{snap_str}))/2)*2':h='trunc(ih*(1+0.12*max(0,1-t/{snap_str}))/2)*2',crop={w}:{h}:(iw-ow)/2:(ih-oh)/2")
    elif intro == "white_fade":
        fade_d = config["fade_in"] if config["fade_in"] else min(0.5, duration / 2)
        filters.append(f"fade=t=in:st=0:d={fade_d}:color=white")

    if config["letterbox"]:
        size = config["letterbox"]
        filters += [f"drawbox=x=0:y=0:w=iw:h=ih*{size}:color=black:t=fill",
                    f"drawbox=x=0:y=ih*(1-{size}):w=iw:h=ih*{size}:color=black:t=fill"]

    if config["fade_in"] and intro != "white_fade":
        filters.append(f"fade=t=in:st=0:d={config['fade_in']}")
    if config["fade_out"]:
        filters.append(f"fade=t=out:st={duration - config['fade_out']}:d={config['fade_out']}")

    if not filters:
        raise ValueError("No visual effect requested; skip this render pass")

    args = ["-i", source, "-map", "0:v:0", "-map", "0:a:0?", "-vf", ",".join(filters),
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p"]
    audio_fades = []
    if any(s.get("codec_type") == "audio" for s in info["streams"]):
        if config["fade_in"]:
            audio_fades.append(f"afade=t=in:st=0:d={config['fade_in']}")
        if config["fade_out"]:
            audio_fades.append(f"afade=t=out:st={duration - config['fade_out']}:d={config['fade_out']}")
    if audio_fades:
        args += ["-af", ",".join(audio_fades), "-c:a", "aac"]
    else:
        args += ["-c:a", "copy"]
    media.ffmpeg(args + ["-movflags", "+faststart", output])
    return output


def mix_music(source, music_path, output, config, duration=None):
    info = media.probe(source)
    duration = info["duration"] if duration is None else number(duration, "duration", .001, info["duration"] + .25)
    config = normalize_music(config, duration)
    music_info = media.probe(music_path)
    if not any(s.get("codec_type") == "audio" for s in music_info["streams"]):
        raise ValueError("Music artifact has no audio stream")
    if config["offset"] >= music_info["duration"]:
        raise ValueError("Music offset exceeds its duration")
    args = ["-i", source]
    if config["loop"]:
        args += ["-stream_loop", "-1"]
    args += ["-ss", str(config["offset"]), "-i", Path(music_path).resolve()]
    graph = [f"[1:a:0]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS,apad,atrim=duration={duration},volume={config['volume']}[music0]"]
    fades = []
    if config["fade_in"]:
        fades.append(f"afade=t=in:st=0:d={config['fade_in']}")
    if config["fade_out"]:
        fades.append(f"afade=t=out:st={duration - config['fade_out']}:d={config['fade_out']}")
    graph.append("[music0]" + (",".join(fades) if fades else "anull") + "[music]")
    has_audio = any(s.get("codec_type") == "audio" for s in info["streams"])
    if has_audio:
        graph.append(f"[0:a:0]aresample=48000,aformat=channel_layouts=stereo,apad,atrim=duration={duration}[voice]")
        if config["ducking"]:
            graph += ["[voice]asplit=2[voice_mix][sidechain]",
                      "[music][sidechain]sidechaincompress=threshold=0.035:ratio=8:attack=20:release=350[ducked]",
                      "[voice_mix][ducked]amix=inputs=2:duration=first:normalize=0[combined]"]
        else:
            graph.append("[voice][music]amix=inputs=2:duration=first:normalize=0[combined]")
    else:
        graph.append("[music]anull[combined]")
    # Limiter preserves the requested music/voice balance; no global loudness lift
    # that could amplify a deliberately quiet music bed.
    graph.append(f"[combined]alimiter=limit=0.95:level=0,apad,atrim=duration={duration}[audio]")
    media.ffmpeg(args + ["-filter_complex", ";".join(graph), "-map", "0:v:0", "-map", "[audio]",
                        "-c:v", "copy", "-c:a", "aac", "-t", str(duration), "-movflags", "+faststart", output])
    return output
