"""Exercise the actual JSON CLI using only labeled synthetic media.
Run from repository root: python3 -m scripts.smoke_test --output .video-agent/smoke-UNIQUE
--say additionally tests an already-installed macOS preset voice; no download.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from video_agent import media
from video_agent.store import write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--say", action="store_true")
    args = parser.parse_args()
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    project = root / "project"
    calls = []
    def cli(*arguments):
        command = [sys.executable, "-m", "video_agent", *map(str, arguments)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        calls.append({"argv": command, "exit_code": result.returncode})
        if result.returncode:
            raise RuntimeError(result.stderr)
        payload = json.loads(result.stdout)
        if not payload["ok"]:
            raise RuntimeError(payload)
        return payload["result"]
    def task(kind, params):
        filename = root / (str(len(calls)) + "-" + kind + ".json")
        write_json(filename, params)
        return cli("run", "--project", project, "--task", kind, "--params", filename)
    source = root / "SYNTHETIC-EXAMPLE.mp4"
    media.ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=4",
                  "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", source])
    environment = cli("doctor")
    cli("init", "--project", project, "--input", source, "--name", "Synthetic example, not user media")
    task("probe", {})
    task("extract_audio", {})
    track = task("import_subtitles", {"cues": [
        {"id": "example", "start_ms": 200, "end_ms": 3200, "text": "Example data: hello world."}]})["artifacts"][0]["id"]
    translation = task("apply_translations", {"track": track, "language": "zh", "translations": {"example": "示例数据：你好，世界。"}})
    tid = translation["artifacts"][0]["id"]
    task("qc_subtitles", {"track": tid})
    exported = task("export_subtitles", {"track": tid, "format": "srt"})
    plan_params = {"track": tid, "subtitle_mode": "soft"}
    speech = None
    if args.say:
        speech = task("tts", {"track": track, "provider": "say", "confirmed": True})
        # Different language text is supported when cue IDs/timing agree.
        plan_params["voice_manifest"] = speech["artifacts"][0]["id"]
    plan = task("plan", plan_params)["artifacts"][0]["id"]
    preview = task("preview", {"plan": plan})
    # Approval is strictly for generated test fixtures, never human media.
    cli("approve", "--project", project, "--artifact", plan)
    rendered = task("render", {"plan": plan})
    cached = task("render", {"plan": plan})
    assert cached.get("cached") is True
    report = {"fixture": "SYNTHETIC EXAMPLE DATA, not ASR or translation model output",
              "environment": environment, "calls": calls, "preview": preview,
              "render": rendered, "subtitle_export": exported, "speech": speech,
              "cache_hit": True, "real_asr_executed": False,
              "real_translation_api_executed": False, "hard_burn_executed": False}
    write_json(root / "report.json", report)
    print(json.dumps({"report": str(root / "report.json"), "status": "passed", "real_say_executed": bool(speech)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
