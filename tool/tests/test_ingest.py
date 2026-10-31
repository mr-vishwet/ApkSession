"""Host-side tests for the chunked ingest endpoints (US-002). No device, no adb.

Run from anywhere:  .venv/bin/python -m pytest tool/tests/test_ingest.py -q
The suite boots the real dashboard server in-process on an ephemeral loopback port
and drives it with plain HTTP, so what passes here is the route the app will hit.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from adrd import watch  # noqa: E402

TOKEN = "testtok"
CS = 1024  # chunk size used by most tests


def data_for(n: int, seed: int = 7) -> bytes:
    """Deterministic pseudo-random bytes, so tests can rebuild the source cheaply."""
    out, v = bytearray(), seed
    for i in range(n):
        v = (v * 1103515245 + 12345) & 0xFFFFFFFF
        out.append((v >> (i % 13)) & 0xFF)
    return bytes(out)


def chunk_list(src: bytes, cs: int) -> list[bytes]:
    return [src[i:i + cs] for i in range(0, len(src), cs)] or [b""]


@pytest.fixture()
def server(tmp_path):
    sessions = tmp_path / "sessions"
    alpha = sessions / "alpha"
    alpha.mkdir(parents=True)
    (alpha / "state.json").write_text("{}")
    cfg = {"dir": sessions, "host": "127.0.0.1", "port": 0, "token": TOKEN,
           "loopback": True, "adb": None, "stream_ttl": 1.0, "allow_unmasked": True}
    srv = watch.Server(cfg)
    srv.serve_thread()
    yield srv.server_address[1], alpha
    srv.shutdown_all()


def req(port, method, path, body=b"", ctype="application/octet-stream"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body,
                 headers={"Content-Type": ctype, "Content-Length": str(len(body))})
    r = conn.getresponse()
    payload = r.read()
    conn.close()
    return r.status, json.loads(payload) if payload and \
        r.getheader("Content-Type") == "application/json" else payload


def init(port, filename, src, cs=CS, sha=None, tok=TOKEN):
    body = json.dumps({"filename": filename, "size": len(src),
                       "sha256": sha or hashlib.sha256(src).hexdigest(),
                       "chunk_size": cs}).encode()
    code, out = req(port, "POST", f"/ingest/alpha/init?t={tok}", body,
                    "application/json")
    assert code == 200, out
    return out["upload_id"]


def put(port, uid, n, chunk, tok=TOKEN):
    return req(port, "PUT", f"/ingest/alpha/{uid}/chunk/{n}?t={tok}", chunk)


def status(port, uid, tok=TOKEN):
    return req(port, "GET", f"/ingest/alpha/{uid}/status?t={tok}")


def complete(port, uid, tok=TOKEN):
    return req(port, "POST", f"/ingest/alpha/{uid}/complete?t={tok}")


def upload_all(port, uid, src, cs=CS, order=None):
    chunks = [src[i:i + cs] for i in range(0, len(src), cs)]
    for n in (order if order is not None else range(len(chunks))):
        code, out = put(port, uid, n, chunks[n])
        assert code == 200, (n, out)


# ---- happy path and ordering ----------------------------------------------------

def test_happy_path_assembles_byte_identical(server):
    port, alpha = server
    src = data_for(2 * CS + 400)
    uid = init(port, "clip.bin", src)
    upload_all(port, uid, src)
    code, out = complete(port, uid)
    assert code == 200 and out["ok"]
    assert (alpha / "ingest" / "clip.bin").read_bytes() == src
    assert out["sha256"] == hashlib.sha256(src).hexdigest()


def test_init_is_idempotent_for_same_file(server):
    port, _ = server
    src = data_for(CS + 1)
    uid = init(port, "clip.bin", src)
    code, out = req(port, "POST", f"/ingest/alpha/init?t={TOKEN}",
                    json.dumps({"filename": "clip.bin", "size": len(src),
                                "sha256": hashlib.sha256(src).hexdigest(),
                                "chunk_size": CS}).encode(), "application/json")
    assert code == 200 and out["upload_id"] == uid and out["reused"] is True


def test_out_of_order_arrival(server):
    port, alpha = server
    src = data_for(5 * CS + 9)
    uid = init(port, "ooo.mp4", src, cs=CS)
    upload_all(port, uid, src, order=[5, 1, 0, 3, 2, 4])
    code, out = complete(port, uid)
    assert code == 200
    assert (alpha / "ingest" / "ooo.mp4").read_bytes() == src


def test_duplicate_chunk_is_idempotent_200(server):
    port, alpha = server
    src = data_for(3 * CS)
    uid = init(port, "dup.bin", src)
    chunks = chunk_list(src, CS)
    for n in (0, 1, 2, 1, 1, 0):
        code, out = put(port, uid, n, chunks[n])
        assert code == 200, (n, out)
    code, out = complete(port, uid)
    assert code == 200
    assert (alpha / "ingest" / "dup.bin").read_bytes() == src


def test_resume_reports_exactly_present_indices(server):
    port, alpha = server
    src = data_for(6 * CS + 33)
    uid = init(port, "resume.bin", src)
    chunks = [src[i:i + CS] for i in range(0, len(src), CS)]
    for n in (0, 2, 3):  # a partial transfer that died
        put(port, uid, n, chunks[n])
    code, st = status(port, uid)
    assert code == 200
    assert st["stored"] == [0, 2, 3]
    assert st["missing"] == [1, 4, 5, 6]
    for n in (1, 4, 5, 6):  # client sends only what the status says is missing
        assert put(port, uid, n, chunks[n])[0] == 200
    code, out = complete(port, uid)
    assert code == 200 and out["ok"]
    assert (alpha / "ingest" / "resume.bin").read_bytes() == src


def test_complete_409_names_missing_ranges(server):
    port, alpha = server
    src = data_for(7 * CS)
    uid = init(port, "gaps.bin", src)
    chunks = chunk_list(src, CS)
    for n in (0, 1, 5):
        put(port, uid, n, chunks[n])
    code, out = complete(port, uid)
    assert code == 409
    assert out["missing_ranges"] == "2-4,6"
    assert not (alpha / "ingest" / "gaps.bin").exists()


# ---- rejection paths --------------------------------------------------------------

def test_chunk_for_unknown_upload_rejected(server):
    port, _ = server
    for uid in ("0123456789abcdef", "not-an-id"):
        code, out = put(port, uid, 0, b"x" * CS)
        assert code == 404, (uid, out)
        assert status(port, uid)[0] == 404
        assert complete(port, uid)[0] == 404


def test_oversize_chunk_rejected_stored_chunks_intact(server):
    port, alpha = server
    src = data_for(2 * CS + 400)
    uid = init(port, "over.bin", src)
    assert put(port, uid, 0, src[:CS])[0] == 200
    # body larger than chunk_size is refused before it is even read
    code, _ = put(port, uid, 1, src[CS:CS + 1500])
    assert code == 413
    # last chunk is 400 bytes; 700 is oversize for that index but under chunk_size
    code, out = put(port, uid, 2, b"y" * 700)
    assert code == 400 and "must be 400 bytes" in out["error"]
    st = status(port, uid)[1]
    assert st["stored"] == [0]  # nothing new stored, nothing corrupted
    upload_all(port, uid, src, order=[1, 2])
    assert complete(port, uid)[0] == 200
    assert (alpha / "ingest" / "over.bin").read_bytes() == src


def test_truncated_chunk_rejected_stored_chunks_intact(server):
    port, alpha = server
    src = data_for(2 * CS + 400)
    uid = init(port, "trunc.bin", src)
    put(port, uid, 0, src[:CS])
    code, out = put(port, uid, 2, src[2 * CS:2 * CS + 100])  # 100 of the 400 owed
    assert code == 400 and "must be 400 bytes" in out["error"]
    assert status(port, uid)[1]["stored"] == [0]
    # a lying Content-Length (claims CS, sends less, half-closes) is also rejected
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(f"PUT /ingest/alpha/{uid}/chunk/1?t={TOKEN} HTTP/1.0\r\n"
                 f"Content-Length: {CS}\r\n\r\n".encode() + b"z" * 10)
    sock.shutdown(socket.SHUT_WR)  # half-close so the server sees the truncation now
    resp = sock.recv(200).decode()
    sock.close()
    assert " 400 " in resp.splitlines()[0]
    assert status(port, uid)[1]["stored"] == [0]
    upload_all(port, uid, src, order=[1, 2])
    assert complete(port, uid)[0] == 200
    assert (alpha / "ingest" / "trunc.bin").read_bytes() == src


def test_sha_mismatch_fails_loudly_and_keeps_chunks(server):
    port, alpha = server
    src = data_for(2 * CS)
    uid = init(port, "corrupt.bin", src, sha=hashlib.sha256(b"other").hexdigest())
    upload_all(port, uid, src)
    code, out = complete(port, uid)
    assert code == 409 and "sha256 mismatch" in out["error"]
    assert not (alpha / "ingest" / "corrupt.bin").exists()
    assert (alpha / "ingest" / "uploads" / uid / "chunks" / "0").exists()


def test_filename_traversal_rejected_403(server):
    port, alpha = server
    body = {"size": 0, "sha256": hashlib.sha256(b"").hexdigest(), "chunk_size": CS}
    for bad in ("../evil", "a/b", "sub\\x", "x..y", ".hidden"):
        code, out = req(port, "POST", f"/ingest/alpha/init?t={TOKEN}",
                        json.dumps({**body, "filename": bad}).encode(),
                        "application/json")
        assert code == 403, (bad, out)
    assert sorted(p.name for p in alpha.iterdir()) == ["state.json"]


def test_zero_byte_file_completes(server):
    port, alpha = server
    empty = hashlib.sha256(b"").hexdigest()
    uid = init(port, "nothing.bin", b"", sha=empty)
    assert status(port, uid)[1]["num_chunks"] == 0
    code, out = complete(port, uid)
    assert code == 200
    assert (alpha / "ingest" / "nothing.bin").read_bytes() == b""


# ---- token gate --------------------------------------------------------------------

def test_every_ingest_route_requires_the_token(server):
    port, _ = server
    src = data_for(CS)
    uid = init(port, "auth.bin", src)
    assert req(port, "POST", "/ingest/alpha/init", b"{}")[0] == 401
    assert req(port, "PUT", f"/ingest/alpha/{uid}/chunk/0", b"x" * CS)[0] == 401
    assert status(port, uid, tok="wrong")[0] == 401
    assert complete(port, uid, tok="")[0] == 401
    assert req(port, "GET", "/ingest/alpha/0123456789abcdef/status")[0] == 401


def test_all_artifacts_stay_under_ingest(server):
    port, alpha = server
    src = data_for(3 * CS + 5)
    uid = init(port, "contained.bin", src)
    upload_all(port, uid, src)
    complete(port, uid)
    outside = [p for p in alpha.rglob("*")
               if p.is_file() and p.name != "state.json"
               and not p.relative_to(alpha).parts[0] == "ingest"]
    assert outside == []


def test_unknown_session_is_404(server):
    port, _ = server
    assert req(port, "POST", "/ingest/nope/init?t=" + TOKEN, b"{}")[0] == 404
