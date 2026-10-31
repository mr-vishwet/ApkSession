"""Chunked upload store for the dashboard: numbered chunks in, one verified file out.

Every artefact lives inside the session folder under ingest/. Chunks and metadata
are written temp + fsync + rename so a crash never leaves a torn file that a resume
would trust. The routes in watch.py are thin; all decisions that touch disk are here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{16}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON = 64 * 1024          # init/complete request bodies are tiny; cap them
MAX_CHUNK = 64 * 1024 * 1024  # a chunk bigger than this is refused at init


def _safe_filename(name) -> tuple[bool, str]:
    """Accept a single path component only; separators and '..' are refused (403)."""
    if not isinstance(name, str) or not name:
        return False, "filename must be a non-empty string"
    if "/" in name or "\\" in name or ".." in name:
        return False, "filename must not contain a path separator or .."
    if name in (".", "") or name.startswith("."):
        return False, "filename must not be hidden or relative"
    return True, ""


def upload_id_for(filename: str, sha256: str) -> str:
    """Deterministic so a repeated init for the same file returns the same id."""
    return hashlib.sha256(f"{sha256}\n{filename}".encode()).hexdigest()[:16]


def ingest_root(session_root: Path) -> Path:
    return Path(session_root) / "ingest"


def upload_dir(session_root: Path, upload_id: str) -> Path:
    return ingest_root(session_root) / "uploads" / upload_id


def meta_path(session_root: Path, upload_id: str) -> Path:
    return upload_dir(session_root, upload_id) / "meta.json"


def chunk_path(session_root: Path, upload_id: str, n: int) -> Path:
    return upload_dir(session_root, upload_id) / "chunks" / str(n)


def num_chunks_for(size: int, chunk_size: int) -> int:
    return (size + chunk_size - 1) // chunk_size


def expected_chunk_len(meta: dict, n: int) -> int:
    start = n * meta["chunk_size"]
    return max(0, min(meta["chunk_size"], meta["size"] - start))


def load_meta(session_root: Path, upload_id: str) -> dict | None:
    if not UPLOAD_ID_RE.match(upload_id or ""):
        return None
    try:
        return json.loads(meta_path(session_root, upload_id).read_text())
    except (OSError, ValueError):
        return None


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _stored_indices(session_root: Path, meta: dict) -> list[int]:
    """A chunk counts as present only if the file exists with exactly its expected size."""
    out = []
    for n in range(meta["num_chunks"]):
        p = chunk_path(session_root, meta["upload_id"], n)
        try:
            if p.stat().st_size == expected_chunk_len(meta, n):
                out.append(n)
        except OSError:
            pass
    return out


def _ranges(indices: list[int]) -> str:
    """Compress [0,1,2,5,7,8] into '0-2,5,7-8' for the 409 body."""
    parts, start, prev = [], None, None
    for i in indices + [None]:
        if start is not None and (i is None or i != prev + 1):
            parts.append(f"{start}-{prev}" if prev > start else f"{start}")
            start = None
        if i is not None and start is None:
            start = prev = i
        elif i is not None:
            prev = i
    return ",".join(parts)


def init(session_root: Path, body: dict) -> tuple[int, dict]:
    ok, why = _safe_filename(body.get("filename"))
    if not ok:
        return 403, {"error": why}
    filename = body["filename"]
    sha = body.get("sha256")
    size = body.get("size")
    chunk_size = body.get("chunk_size")
    if not (isinstance(sha, str) and SHA256_RE.match(sha)):
        return 400, {"error": "sha256 must be 64 hex chars"}
    if not (isinstance(size, int) and not isinstance(size, bool) and size >= 0):
        return 400, {"error": "size must be a non-negative integer"}
    if not (isinstance(chunk_size, int) and not isinstance(chunk_size, bool)
            and 0 < chunk_size <= MAX_CHUNK):
        return 400, {"error": f"chunk_size must be 1..{MAX_CHUNK} bytes"}
    upload_id = upload_id_for(filename, sha)
    meta_path(session_root, upload_id).parent.mkdir(parents=True, exist_ok=True)
    existing = load_meta(session_root, upload_id)
    if existing is not None:
        if (existing["size"] != size or existing["chunk_size"] != chunk_size
                or existing["filename"] != filename):
            return 409, {"error": "upload_id exists with different init parameters"}
        return 200, {"upload_id": upload_id, "reused": True,
                     "stored": _stored_indices(session_root, existing),
                     "complete": bool(existing.get("complete"))}
    meta = {"upload_id": upload_id, "filename": filename, "size": size,
            "sha256": sha, "chunk_size": chunk_size,
            "num_chunks": num_chunks_for(size, chunk_size),
            "complete": False, "created": time.time()}
    _atomic_write(meta_path(session_root, upload_id), json.dumps(meta).encode())
    (upload_dir(session_root, upload_id) / "chunks").mkdir(exist_ok=True)
    return 200, {"upload_id": upload_id, "reused": False, "stored": [],
                 "num_chunks": meta["num_chunks"]}


def put_chunk(session_root: Path, upload_id: str, n: int, data: bytes) -> tuple[int, dict]:
    meta = load_meta(session_root, upload_id)
    if meta is None:
        return 404, {"error": "unknown upload"}
    if meta.get("complete"):
        return 409, {"error": "upload already complete"}
    if not (0 <= n < meta["num_chunks"]):
        return 400, {"error": f"chunk index {n} outside 0..{meta['num_chunks'] - 1}"}
    want = expected_chunk_len(meta, n)
    if len(data) != want:
        return 400, {"error": f"chunk {n} must be {want} bytes, got {len(data)}"}
    _atomic_write(chunk_path(session_root, upload_id, n), data)
    return 200, {"ok": True, "index": n, "stored": _stored_indices(session_root, meta)}


def status(session_root: Path, upload_id: str) -> tuple[int, dict]:
    meta = load_meta(session_root, upload_id)
    if meta is None:
        return 404, {"error": "unknown upload"}
    stored = _stored_indices(session_root, meta)
    missing = [i for i in range(meta["num_chunks"]) if i not in set(stored)]
    return 200, {"upload_id": upload_id, "filename": meta["filename"],
                 "size": meta["size"], "chunk_size": meta["chunk_size"],
                 "num_chunks": meta["num_chunks"], "stored": stored,
                 "missing": missing, "complete": bool(meta.get("complete"))}


def complete(session_root: Path, upload_id: str) -> tuple[int, dict]:
    meta = load_meta(session_root, upload_id)
    if meta is None:
        return 404, {"error": "unknown upload"}
    stored = set(_stored_indices(session_root, meta))
    missing = [i for i in range(meta["num_chunks"]) if i not in stored]
    if missing:
        return 409, {"error": f"missing chunks {_ranges(missing)}",
                     "missing_ranges": _ranges(missing)}
    udir = upload_dir(session_root, upload_id)
    tmp = udir / "assembled.tmp"
    digest = hashlib.sha256()
    try:
        with open(tmp, "wb") as out:
            for n in range(meta["num_chunks"]):
                with open(chunk_path(session_root, upload_id, n), "rb") as f:
                    while True:
                        block = f.read(1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                        out.write(block)
            out.flush()
            os.fsync(out.fileno())
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return 500, {"error": f"assembly failed: {exc}"}
    got = digest.hexdigest()
    if got != meta["sha256"]:
        tmp.unlink(missing_ok=True)  # chunks stay for diagnosis; nothing was published
        return 409, {"error": f"sha256 mismatch: expected {meta['sha256']} got {got}"}
    dest = ingest_root(session_root) / meta["filename"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, dest)
    meta["complete"] = True
    meta["assembled"] = f"ingest/{meta['filename']}"
    _atomic_write(meta_path(session_root, upload_id), json.dumps(meta).encode())
    return 200, {"ok": True, "path": meta["assembled"], "sha256": got,
                 "size": meta["size"]}
