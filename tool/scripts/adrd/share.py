"""Read-only live links and the two emails a run sends: it started, and it finished.

The share token is scoped to viewing one session. It is not the dashboard access token,
so a recipient can watch and download the masked results and nothing else. Every trigger
is written to the session's outbox before delivery is attempted; without SMTP settings
the outbox is the whole story, and nothing is silently dropped.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from pathlib import Path

EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
_LOCK = threading.Lock()

SHAREABLE = {
    "render/demo_debranded.mp4",
    "render/demo_debranded.vtt",
    "render/sections.json",
    "render/SESSION_DOC.md",
    "render/SESSION_DOC.pdf",
    "CASES.md",
}

_EVENT_COPY = {
    "link": ("Live link: {app}", "Here is the live session page:\n\n{link}\n\n"
                                "You will get a note when the run starts and again when "
                                "the masked results are ready."),
    "started": ("Live now: {app}", "The run started. Follow it live:\n\n{link}\n\n"
                                  "The page also lists the section clips as they are cut."),
    "completed": ("Ready: {app}", "The run finished. The results are here:\n\n{link}\n\n"
                                  "{sections} section clips and the full masked video are "
                                  "ready to play."),
    "failed": ("Run stopped: {app}", "The run stopped before finishing. What exists so far "
                                     "is here:\n\n{link}\n\nSee the stage strip on the page "
                                     "for the stage that failed."),
    "render": ("Results updated: {app}", "The video was re-rendered with the latest edits. "
                                         "Open the results:\n\n{link}"),
}


def _dir(root: Path) -> Path:
    p = Path(root) / "share"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def ensure(root: Path) -> dict:
    p = _dir(root) / "share.json"
    rec = read_json(p, {})
    if rec.get("token"):
        return rec
    rec = {"token": secrets.token_urlsafe(12),
           "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    _write(p, rec)
    return rec


def token(root: Path) -> str | None:
    return (read_json(_dir(root) / "share.json", {}) or {}).get("token")


def valid(root: Path, given: str | None) -> bool:
    want = token(root)
    return bool(want and given and secrets.compare_digest(want, given))


def link(root: Path, base: str) -> str:
    return f"{base.rstrip('/')}/live/{Path(root).name}?t={ensure(root)['token']}"


def subscribers(root: Path) -> list[str]:
    data = read_json(_dir(root) / "subscribers.json", [])
    return [s.get("email") for s in data if isinstance(s, dict) and s.get("email")]


def subscribe(root: Path, email: str) -> dict:
    email = (email or "").strip().lower()
    if not EMAIL.match(email):
        raise ValueError("enter an email like name@company.com")
    with _LOCK:
        p = _dir(root) / "subscribers.json"
        data = read_json(p, [])
        if not any(s.get("email") == email for s in data):
            data.append({"email": email,
                         "added": time.strftime("%Y-%m-%d %H:%M:%S")})
            _write(p, data)
    return {"ok": True, "email": email, "subscribers": len(data)}


def shareable(rel: str) -> bool:
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if ".." in rel.split("/"):
        return False
    if rel.startswith("render/sections/"):
        return rel.endswith((".mp4", ".jpg"))
    return rel in SHAREABLE


def recent_events(root: Path, limit: int = 8) -> list[dict]:
    p = _dir(root) / "events.jsonl"
    try:
        lines = p.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("delivery"):
            continue  # the follow-up record for one event, not a second event
        out.append({k: e.get(k) for k in ("at", "event", "subject")})
    return out


def smtp_configured() -> bool:
    return _smtp() is not None


def _smtp() -> dict | None:
    host = os.environ.get("ADRD_SMTP_HOST", "").strip()
    if not host:
        return None
    user = os.environ.get("ADRD_SMTP_USER", "").strip()
    return {
        "host": host,
        "port": int(os.environ.get("ADRD_SMTP_PORT", "587") or 587),
        "user": user,
        "password": os.environ.get("ADRD_SMTP_PASS", ""),
        "sender": os.environ.get("ADRD_SMTP_FROM", "").strip() or user,
        "tls": os.environ.get("ADRD_SMTP_TLS", "starttls").lower(),
    }


def _send(cfg: dict, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = cfg["sender"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if cfg["tls"] == "ssl":
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15,
                              context=ssl.create_default_context()) as s:
            if cfg["user"]:
                s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
        return
    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as s:
        s.ehlo()
        if cfg["tls"] == "starttls":
            s.starttls(context=ssl.create_default_context())
            s.ehlo()
        if cfg["user"]:
            s.login(cfg["user"], cfg["password"])
        s.send_message(msg)


def _deliver(root: Path, to: list[str], subject: str, body: str) -> dict:
    cfg = _smtp()
    out = _dir(root) / "outbox"
    out.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    queued, sent, errors = 0, 0, []
    for i, addr in enumerate(to):
        rec = {"to": addr, "subject": subject, "body": body,
               "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        if cfg:
            try:
                _send(cfg, addr, subject, body)
                rec["status"] = "sent"
                sent += 1
            except Exception as exc:
                rec["status"] = f"send failed: {exc}"
                errors.append(str(exc))
        else:
            rec["status"] = "queued: set ADRD_SMTP_* to send"
            queued += 1
        name = f"{stamp}-{i:02d}-{re.sub(r'[^a-z0-9]+', '-', addr.lower())}.json"
        (out / name).write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    return {"sent": sent, "queued": queued, "errors": errors}


def notify(root: Path, event: str, base: str = "", kind: str = "run",
           blocking: bool = False, only: str | None = None) -> dict:
    """Record one trigger and hand it to every subscriber. The event is on disk before
    any network call, so a mail failure cannot lose the fact that the run started.

    `only` narrows delivery to one address. A person asking for the link must not cause
    the same mail to land in every other subscriber's inbox.
    """
    root = Path(root)
    state = read_json(root / "state.json", {}) or {}
    manifest = read_json(root / "render" / "sections.json", {}) or {}
    url = link(root, base or "http://127.0.0.1:8734")
    app = state.get("app") or root.name
    key = "render" if event == "completed" and kind == "render" else event
    subject, template = _EVENT_COPY.get(key, _EVENT_COPY["completed"])
    body = template.format(app=app, link=url,
                           sections=manifest.get("count", 0))
    subject = subject.format(app=app)
    event_rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "event": key,
                 "subject": subject, "link": url}
    with _LOCK:
        with (_dir(root) / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event_rec) + "\n")
    to = [only] if only else subscribers(root)
    result = {"event": key, "subscribers": len(to), "link": url,
              "sent": 0, "queued": 0, "errors": []}
    if not to:
        return result

    def deliver() -> dict:
        got = _deliver(root, to, subject, body)
        with (_dir(root) / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**event_rec, **got, "delivery": True}) + "\n")
        return got

    if blocking:
        result.update(deliver())
    else:
        threading.Thread(target=deliver, daemon=True).start()
        result["pending"] = len(to)
    return result
