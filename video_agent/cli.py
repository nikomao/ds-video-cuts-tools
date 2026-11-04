"""JSON-in/JSON-out command interface for DeepSeek Harness, not a separate app."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from . import media
from .store import Project
from .engine import TASKS, submit, run_job


def main(argv=None):
    parser = argparse.ArgumentParser(description="Harness video-agent local tools")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Report installed media and model capabilities without downloads")
    init = commands.add_parser("init", help="Import a local source into a new immutable project")
    init.add_argument("--project", required=True)
    init.add_argument("--input", required=True)
    init.add_argument("--name")
    run = commands.add_parser("run", help="Execute one auditable task; use Harness managed jobs for background work")
    run.add_argument("--project", required=True)
    run.add_argument("--task", choices=sorted(TASKS), required=True)
    run.add_argument("--params", help="UTF-8 JSON parameter file, not executable code")
    run.add_argument("--fresh", action="store_true", help="Explicitly bypass result cache")
    jobs = commands.add_parser("jobs")
    jobs.add_argument("--project", required=True)
    status = commands.add_parser("status")
    status.add_argument("--project", required=True)
    status.add_argument("--job", required=True)
    retry = commands.add_parser("retry", help="Retry a failed/cancelled task into a new attempt directory")
    retry.add_argument("--project", required=True)
    retry.add_argument("--job", required=True)
    approve = commands.add_parser("approve", help="Record human approval of an exact immutable edit plan")
    approve.add_argument("--project", required=True)
    approve.add_argument("--artifact", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            value = media.doctor()
        elif args.command == "init":
            p = Project.create(args.project, args.input, args.name)
            value = {"project": str(p.root), "manifest": p.manifest}
        else:
            p = Project(args.project)
            if args.command == "run":
                params = json.loads(Path(args.params).read_text(encoding="utf-8")) if args.params else {}
                value = submit(p, args.task, params, args.fresh)
            elif args.command == "jobs":
                value = p.jobs()
            elif args.command == "status":
                value = p.job(args.job)
            elif args.command == "retry":
                value = run_job(p, args.job, retry=True)
            elif args.command == "approve":
                from .engine import read_plan
                read_plan(p, args.artifact)
                value = p.approve(args.artifact)
        print(json.dumps({"ok": True, "result": value}, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "Cancelled; task state recorded"}), file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__ + ": " + str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
