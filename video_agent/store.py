"""Project-local immutable artifacts and transactional job ledger (no server)."""
from __future__ import annotations
import hashlib
import json
import shutil
import sqlite3
import time
import uuid
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Callers use fresh, uniquely named destinations. Never overwrite artifacts.
    with path.open("x", encoding="utf-8") as f:
        f.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


class Project:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if not (self.root / "project.json").is_file():
            raise ValueError("Not a video-agent project; run init first")
        self.manifest = json.loads((self.root / "project.json").read_text())
        self.db = self.root / "jobs.sqlite3"
        with self.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, params TEXT NOT NULL,
              cache_key TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
              created REAL NOT NULL, updated REAL NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS artifacts (
              id TEXT PRIMARY KEY, job_id TEXT NOT NULL, path TEXT NOT NULL,
              sha256 TEXT NOT NULL, size INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS approvals (
              digest TEXT PRIMARY KEY, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS job_context (
              job_id TEXT PRIMARY KEY, context TEXT NOT NULL);
            """)

    def connect(self):
        c = sqlite3.connect(self.db, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    @classmethod
    def create(cls, root, source, name=None):
        root, source = Path(root).resolve(), Path(source).resolve(strict=True)
        if not source.is_file():
            raise ValueError("Source must be a regular local file")
        if root.exists() and any(root.iterdir()):
            raise ValueError("Project directory must be new or empty")
        if source.is_relative_to(root):
            raise ValueError("Source cannot be inside the new project directory")
        root.mkdir(parents=True, exist_ok=True)
        (root / "assets").mkdir()
        sha = digest_file(source)
        suffix = source.suffix.lower()
        if len(suffix) > 12 or not suffix.replace(".", "").isalnum():
            suffix = ".media"
        target = root / "assets" / (sha + suffix)
        shutil.copyfile(source, target)
        if digest_file(target) != sha:
            raise ValueError("Source changed during import; create a new project")
        write_json(root / "project.json", {"schema_version": 1, "id": uuid.uuid4().hex,
                   "name": name or source.stem, "source": str(target.relative_to(root)),
                   "source_sha256": sha, "original_name": source.name})
        return cls(root)

    @property
    def source(self):
        path = self.resolve(self.manifest["source"])
        if digest_file(path) != self.manifest["source_sha256"]:
            raise ValueError("Source integrity mismatch; original asset is immutable")
        return path

    def resolve(self, relative):
        path = (self.root / relative).resolve(strict=True)
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("Artifact path must be a regular file inside this project")
        return path

    def artifact(self, artifact_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if not row:
            raise ValueError("Unknown artifact id")
        path = self.resolve(row["path"])
        if digest_file(path) != row["sha256"]:
            raise ValueError("Artifact integrity mismatch; import edited subtitles as a new version")
        return path

    def register(self, job_id, path):
        path = Path(path).resolve(strict=True)
        if not path.is_relative_to(self.root):
            raise ValueError("Output must be inside project")
        item = {"id": uuid.uuid4().hex, "job_id": job_id,
                "path": str(path.relative_to(self.root)), "sha256": digest_file(path),
                "size": path.stat().st_size}
        with self.connect() as c:
            c.execute("INSERT INTO artifacts VALUES (:id,:job_id,:path,:sha256,:size)", item)
        return item

    def verify_dependencies(self, artifact_id, seen=None):
        seen = set() if seen is None else seen
        if artifact_id in seen:
            return
        seen.add(artifact_id)
        path = self.artifact(artifact_id)
        if path.suffix != ".json":
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        refs = {"track", "parent_track", "edit_plan", "plan", "voice_manifest", "artifact_id", "analysis", "frame_artifact_id", "music_license"}
        def visit(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in refs and isinstance(child, str):
                        self.verify_dependencies(child, seen)
                    elif isinstance(child, (dict, list)):
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(data)

    def enqueue(self, kind, params, nonce=None, context=None):
        key = hashlib.sha256(canonical({"schema": 1, "kind": kind, "params": params,
              "source": self.manifest["source_sha256"], "nonce": nonce, "context": context}).encode()).hexdigest()
        now = time.time()
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO jobs(id,kind,params,cache_key,status,created,updated) VALUES(?,?,?,?,?,?,?)",
                      (uuid.uuid4().hex, kind, canonical(params), key, "pending", now, now))
            row = c.execute("SELECT * FROM jobs WHERE cache_key=?", (key,)).fetchone()
            c.execute("INSERT OR IGNORE INTO job_context VALUES(?,?)", (row["id"], canonical(context or {})))
        return dict(row)

    def claim(self, job_id, retry=False):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("Unknown job")
            allowed = ("failed", "cancelled") if retry else ("pending",)
            if row["status"] not in allowed:
                raise ValueError("Job cannot run from status " + row["status"])
            c.execute("UPDATE jobs SET status='running',attempts=attempts+1,updated=?,error=NULL WHERE id=?",
                      (time.time(), job_id))
        return self.job(job_id)

    def finish(self, job_id, status, result=None, error=None):
        with self.connect() as c:
            c.execute("UPDATE jobs SET status=?,result=?,error=?,updated=? WHERE id=?",
                      (status, canonical(result) if result is not None else None, error, time.time(), job_id))

    def context(self, job_id):
        with self.connect() as c:
            row = c.execute("SELECT context FROM job_context WHERE job_id=?", (job_id,)).fetchone()
        return json.loads(row["context"]) if row else {}

    def job(self, job_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown job")
        return dict(row)

    def jobs(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 100")]

    def artifacts_for_job(self, job_id):
        with self.connect() as c:
            return [dict(row) for row in c.execute("SELECT * FROM artifacts WHERE job_id=? ORDER BY rowid", (job_id,))]

    def approve(self, artifact_id):
        path = self.artifact(artifact_id)
        sha = digest_file(path)
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO approvals VALUES(?,?)", (sha, time.time()))
        return {"artifact_id": artifact_id, "approved_digest": sha}

    def require_approval(self, artifact_id):
        path = self.artifact(artifact_id)
        with self.connect() as c:
            row = c.execute("SELECT digest FROM approvals WHERE digest=?", (digest_file(path),)).fetchone()
        if not row:
            raise ValueError("Human approval required: review plan then run approve --artifact ID")
        return path
