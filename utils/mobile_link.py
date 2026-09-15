"""
J.A.R.V.I.S. — Mobile Link (hub side, Phase 1)
=============================================

Lets a lightweight phone agent ("Jarvis Lite") talk to this Mac hub:

  pairing   Dashboard generates a single-use 8-char code (10-min TTL).
            The phone redeems it once -> receives a per-device token.
  auth      Every /api/mobile/* call (except redeem) needs
            ``Authorization: Bearer <device-token>``.  Only the SHA-256
            hash is ever stored (``brain/data/mobile_devices.json``,
            chmod 600) — a stolen disk file yields no usable tokens.
            The sync log (``brain/data/mobile_events.jsonl``) holds
            personal note text, so it is chmod 600 as well.
  sync      Append-only event log (``brain/data/mobile_events.jsonl``).
            The phone pushes memory/note events with client UUIDs;
            the hub applies them idempotently and returns log lines the
            phone hasn't seen (cursor = line offset).  Deletes are
            tombstone events so other devices drop cached copies.
  chat      Phone text runs through the SAME think->execute pipeline as
            every other channel, tagged ``_origin='mobile:<device>'`` —
            an untrusted origin, so destructive actions ALWAYS hold for
            a human on the dashboard (never bypasses HITL).
  handoff   Session bundle export/import so a conversation can move
            Mac <-> phone mid-stream.

100% stdlib.  No cloud, no accounts, no fees.

Security notes (read before exposing off-loopback):
  - Bind LAN only on networks you trust (JARVIS_HOST=0.0.0.0), or
    better: keep 127.0.0.1 + reach it over Tailscale / Cloudflare Tunnel
    (both free).  Raw port-forward to the internet is NOT recommended.
  - Auth failures are rate-limited per IP (10 fails / 5 min -> 429).
  - Every pairing / redeem / revoke / failure is audit-logged.
"""

import collections
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time

logger = logging.getLogger("Jarvis.MobileLink")

# Pairing codes avoid look-alikes (no 0/O, 1/I/l) for readout over QR/manual.
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUWXYZ"
_PAIR_TTL_S = int(os.getenv("JARVIS_PAIR_TTL_S", "600") or 600)
_MAX_FAILS = int(os.getenv("JARVIS_MOBILE_RATE_FAILS", "10") or 10)
_FAIL_WINDOW_S = int(os.getenv("JARVIS_MOBILE_RATE_WINDOW_S", "300") or 300)


def _data_dir():
    base = os.getenv("JARVIS_MOBILE_DATA_DIR", "")
    if base:
        os.makedirs(base, exist_ok=True)
        return base
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = os.path.join(here, "brain", "data")
    os.makedirs(d, exist_ok=True)
    return d


def _devices_path():
    return os.path.join(_data_dir(), "mobile_devices.json")


def _events_path():
    return os.path.join(_data_dir(), "mobile_events.jsonl")


def _sha256(text):
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _write_private(path, payload):
    """Write JSON with 0600 perms (same pattern as the LLM keystore)."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class MobileLink:
    """Pairing codes + device tokens + append-only sync log."""

    def __init__(self):
        self._lock = threading.RLock()
        self._seen_ids = None          # lazy-loaded set of applied event ids
        self._failures = collections.defaultdict(collections.deque)

    # ------------------------------------------------------------------ #
    # Device store
    # ------------------------------------------------------------------ #
    def _load(self):
        try:
            with open(_devices_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                data.setdefault("devices", {})
                data.setdefault("pending_codes", {})
                return data
        except (OSError, ValueError):
            pass
        return {"devices": {}, "pending_codes": {}}

    def _save(self, data):
        _write_private(_devices_path(), data)

    # ------------------------------------------------------------------ #
    # Pairing
    # ------------------------------------------------------------------ #
    def create_pairing_code(self, label=""):
        """Mint a single-use code.  Returns (code, expires_at)."""
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
        now = time.time()
        with self._lock:
            data = self._load()
            # Prune expired codes on every mint (bounded file growth).
            data["pending_codes"] = {
                h: c for h, c in data["pending_codes"].items()
                if c.get("expires_at", 0) > now
            }
            data["pending_codes"][_sha256(code)] = {
                "label": str(label or "")[:80],
                "created": now,
                "expires_at": now + _PAIR_TTL_S,
            }
            self._save(data)
        try:
            from utils.audit import get_audit
            get_audit().log("mobile_pair_code", {"label": label},
                            outcome="ok", origin="dashboard")
        except Exception:
            pass
        return code, now + _PAIR_TTL_S

    def redeem_pairing_code(self, code, device_name=""):
        """
        Exchange a code for (token, device_id).
        Returns (ok, payload-or-error).  Single-use: the code is burned
        whether or not the device name is pretty.
        """
        code = str(code or "").strip().upper()
        if not code:
            return False, "pairing code required"
        digest = _sha256(code)
        now = time.time()
        with self._lock:
            data = self._load()
            slot = data["pending_codes"].pop(digest, None)
            self._save(data)  # burn immediately (single-use even on error)
        if slot is None:
            self._audit("mobile_pair_redeem", outcome="denied",
                        detail="unknown or already-used code")
            return False, "invalid or already-used pairing code"
        if slot.get("expires_at", 0) <= now:
            self._audit("mobile_pair_redeem", outcome="denied",
                        detail="expired code")
            return False, "pairing code expired — generate a fresh one"

        device_id = "dev_" + secrets.token_hex(6)
        token = secrets.token_urlsafe(32)
        with self._lock:
            data = self._load()
            data["devices"][device_id] = {
                "name": str(device_name or slot.get("label") or "phone")[:80],
                "token_hash": _sha256(token),
                "created": now,
                "last_seen": now,
            }
            self._save(data)
        self._audit("mobile_pair_redeem", outcome="ok",
                    detail=f"device {device_id}", origin=f"mobile:{device_id}")
        return True, {"token": token, "device_id": device_id}

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #
    def verify_token(self, token):
        """Return the device dict when the bearer token matches, else None.

        Never returns token_hash — auth material stays server-side.
        """
        if not token:
            return None
        digest = _sha256(token)
        now = time.time()
        with self._lock:
            data = self._load()
            for device_id, dev in data["devices"].items():
                if hmac.compare_digest(dev.get("token_hash", ""), digest):
                    # Throttle last_seen persistence: every chat/sync poll
                    # authenticates, and each save rewrites the file.
                    try:
                        stale = now - float(dev.get("last_seen", 0) or 0)
                    except (TypeError, ValueError):
                        stale = 61
                    if stale > 60:
                        dev["last_seen"] = now
                        self._save(data)
                    return {"device_id": device_id,
                            "name": dev.get("name", ""),
                            "created": dev.get("created", 0),
                            "last_seen": dev.get("last_seen", 0)}
        return None

    def list_devices(self):
        with self._lock:
            data = self._load()
            return [
                {"device_id": did,
                 "name": d.get("name", ""),
                 "created": d.get("created", 0),
                 "last_seen": d.get("last_seen", 0)}
                for did, d in data["devices"].items()
            ]

    def revoke_device(self, device_id):
        with self._lock:
            data = self._load()
            if device_id not in data["devices"]:
                return False
            del data["devices"][device_id]
            self._save(data)
        self._audit("mobile_revoke", outcome="ok",
                    detail=f"device {device_id}")
        return True

    def record_auth_failure(self, ip):
        now = time.time()
        with self._lock:
            dq = self._failures[ip or "?"]
            dq.append(now)
            while dq and dq[0] < now - _FAIL_WINDOW_S:
                dq.popleft()
            count = len(dq)
        if count >= _MAX_FAILS:
            self._audit("mobile_auth_fail", outcome="denied",
                        detail=f"rate-limited ip={ip} fails={count}")
        return count

    def is_rate_limited(self, ip):
        now = time.time()
        with self._lock:
            dq = self._failures.get(ip or "?", collections.deque())
            while dq and dq[0] < now - _FAIL_WINDOW_S:
                dq.popleft()
            return len(dq) >= _MAX_FAILS

    # ------------------------------------------------------------------ #
    # Sync log (append-only JSONL, cursor = line offset)
    # ------------------------------------------------------------------ #
    def _seen(self):
        with self._lock:
            if self._seen_ids is None:
                ids = set()
                try:
                    with open(_events_path(), "r", encoding="utf-8") as fh:
                        for line in fh:
                            try:
                                ids.add(json.loads(line).get("id", ""))
                            except ValueError:
                                continue
                except OSError:
                    pass
                self._seen_ids = ids
            return self._seen_ids

    def _append_log(self, event):
        line = json.dumps(event, default=str)
        with self._lock:
            with open(_events_path(), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            try:
                os.chmod(_events_path(), 0o600)
            except OSError:
                pass
            self._seen().add(event.get("id", ""))

    def read_since(self, cursor=0):
        """Return (events_after_cursor, new_cursor).

        Cursor = raw line offset (including blank/corrupt lines) so a
        single bad line can never shift the stream and cause duplicates.
        """
        try:
            cursor = max(0, int(cursor or 0))
        except (TypeError, ValueError):
            cursor = 0
        events = []
        pos = 0
        try:
            with open(_events_path(), "r", encoding="utf-8") as fh:
                for line in fh:
                    if pos >= cursor and line.strip():
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            pass  # corrupt line: skip but still advance
                    pos += 1
        except OSError:
            pass
        return events, pos

    def apply_sync(self, device_id, inbound):
        """
        Supported types (Phase 1): memory.remember, memory.forget.
        Todos/notes arrive via the chat route (full approval gating).
        Returns {'applied': n, 'skipped': m}.

        Sizes are bounded — a hostile or buggy client can't smuggle a
        megabyte blob into hub memory or the JSONL log in one event.
        """
        applied, skipped = 0, 0
        for ev in inbound or []:
            if not isinstance(ev, dict):
                skipped += 1
                continue
            eid = str(ev.get("id") or "")[:128]
            etype = str(ev.get("type") or "")[:64]
            payload = ev.get("payload") or {}
            if not eid or not etype or not isinstance(payload, dict):
                skipped += 1
                continue
            payload = self._bound_payload(etype, payload)
            if payload is None:
                skipped += 1
                continue
            if eid in self._seen():
                skipped += 1
                continue
            try:
                self._apply_one(etype, payload)
            except Exception as e:
                logger.warning("mobile sync apply failed %s: %s", etype, e)
                skipped += 1
                continue
            logged = {"id": eid, "type": etype, "payload": payload,
                      "device_id": device_id, "created": time.time()}
            self._append_log(logged)
            applied += 1
        if applied:
            self._audit("mobile_sync", outcome="ok",
                        detail=f"applied={applied} skipped={skipped}",
                        origin=f"mobile:{device_id}")
        return {"applied": applied, "skipped": skipped}

    # Per-type size caps: (max text chars, max item count, per-item chars)
    _PAYLOAD_BOUNDS = {
        "memory.remember": (2000, 20, 64),   # text / tags / tag
        "memory.forget": (200, 0, 0),        # keyword only
    }

    @classmethod
    def _bound_payload(cls, etype, payload):
        """Clamp one inbound payload to _PAYLOAD_BOUNDS.

        Returns the cleaned payload, or None to skip (unknown type,
        empty text, or otherwise unusable — never raises).
        """
        bounds = cls._PAYLOAD_BOUNDS.get(etype)
        if bounds is None:
            return None
        max_text, max_items, max_item = bounds
        try:
            if etype == "memory.remember":
                text = str(payload.get("text", "") or "").strip()
                if not text:
                    return None
                tags = payload.get("tags") or []
                if not isinstance(tags, list):
                    tags = []
                try:
                    importance = int(payload.get("importance", 5) or 5)
                except (TypeError, ValueError):
                    importance = 5
                clean = {"text": text[:max_text],
                         "category": str(payload.get("category",
                                                     "fact") or "fact")[:32],
                         "tags": [str(t)[:max_item]
                                  for t in tags[:max_items]
                                  if str(t).strip()],
                         "importance": max(1, min(10, importance))}
                return clean
            if etype == "memory.forget":
                clean = {}
                if payload.get("memory_id") is not None:
                    clean["memory_id"] = payload.get("memory_id")
                if payload.get("keyword"):
                    clean["keyword"] = str(payload.get("keyword"))[:max_text]
                if payload.get("category"):
                    clean["category"] = str(payload.get("category"))[:32]
                if not clean:
                    return None
                return clean
        except Exception:
            return None
        return None

    def _apply_one(self, etype, payload):
        # Payloads arrive pre-cleaned by _bound_payload (clamped sizes,
        # sane importance) — but a direct caller could still pass raw
        # data, so every field is defensively coerced here as well.
        if etype == "memory.remember":
            from utils.episodic_memory import EpisodicMemory
            try:
                importance = int(payload.get("importance", 5) or 5)
            except (TypeError, ValueError):
                importance = 5
            EpisodicMemory().remember(
                text=str(payload.get("text", ""))[:2000],
                category=str(payload.get("category", "fact") or "fact")[:32],
                tags=[str(t)[:64] for t in
                      (payload.get("tags") or [])[:20]
                      if str(t).strip()],
                importance=max(1, min(10, importance)),
                source="mobile",
                metadata={"origin": "mobile-sync"})
        elif etype == "memory.forget":
            from utils.episodic_memory import EpisodicMemory
            EpisodicMemory().forget(
                memory_id=payload.get("memory_id"),
                keyword=payload.get("keyword"),
                category=payload.get("category"))
        else:
            raise ValueError(f"unknown sync event type: {etype}")

    # ------------------------------------------------------------------ #
    # Handoff bundles (conversation moves Mac <-> phone mid-stream)
    # ------------------------------------------------------------------ #
    @staticmethod
    def export_bundle(brain, executor, limit=20):
        """Snapshot the live session for the phone: recent exchanges,
        open goals, open todos.  Plain JSON — rendered as a QR-able
        deep link (jarvis://handoff) by the client."""
        bundle = {"version": 1, "exported": time.time(),
                  "history": [], "goals": [], "todos": []}
        try:
            hist = getattr(brain, "history", []) or []
            bundle["history"] = [
                {"user": u, "assistant": a} for u, a in hist[-limit:]
            ]
        except Exception:
            pass
        try:
            from utils.goals import get_goals
            bundle["goals"] = get_goals().list()[:20]
        except Exception:
            pass
        try:
            bundle["todos"] = executor.tasks.items_json()
        except Exception:
            pass
        return bundle

    @staticmethod
    def import_bundle(bundle, device_id=""):
        """Fold a phone-side bundle back into hub memory.  Returns a
        human-readable summary (never raises)."""
        try:
            bundle = bundle or {}
            lines = []
            for ex in (bundle.get("history") or [])[-10:]:
                u = str(ex.get("user", ""))[:200]
                if u:
                    lines.append(f"- Mobile note: {u}")
            for g in bundle.get("goals") or []:
                t = str(g.get("title", ""))[:120]
                if t:
                    lines.append(f"- Mobile goal: {t}")
            summary = "\n".join(lines)[:2000] or "Empty handoff bundle."
            try:
                from utils.episodic_memory import EpisodicMemory
                EpisodicMemory().remember(
                    text=f"Handoff from mobile ({device_id}):\n{summary}",
                    category="context", tags=["handoff", "mobile"],
                    source="mobile", importance=6)
            except Exception as e:
                logger.warning("handoff memory write failed: %s", e)
            return summary
        except Exception as e:
            logger.warning("handoff import failed: %s", e)
            return "Handoff import failed."

    # ------------------------------------------------------------------ #
    @staticmethod
    def _audit(action, outcome="ok", detail="", origin=""):
        try:
            from utils.audit import get_audit
            get_audit().log(action, {"detail": detail}, outcome=outcome,
                            origin=origin)
        except Exception:
            pass


_link = None
_lock = threading.Lock()


def get_link():
    global _link
    with _lock:
        if _link is None:
            _link = MobileLink()
        return _link


def _reset_singleton():
    global _link
    with _lock:
        _link = None
