"""
Jarvis Mobile Link Tests (Phase 1 hub side)
============================================

Covers:
  - Pairing: mint -> redeem once -> second redeem fails
  - Token storage: SHA-256 hash only, never the raw token
  - Auth: verify_token works, wrong token fails, revoke kills it
  - Rate limit: failures trip the limiter
  - Sync: idempotent apply by client UUID, forget logged as tombstone
  - Safety: 'mobile:*' is untrusted — destructive actions always gate
  - Event bus: from_mobile_text normalizer shape
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_link():
    """MobileLink pointed at a temp dir (never touches real devices)."""
    tmp = tempfile.mkdtemp(prefix="jarvis-mobile-test-")
    os.environ["JARVIS_MOBILE_DATA_DIR"] = tmp
    import utils.mobile_link as ml
    ml._reset_singleton()
    link = ml.MobileLink()
    return ml, link, tmp


class TestPairing(unittest.TestCase):
    def setUp(self):
        self.ml, self.link, self.tmp = _fresh_link()

    def tearDown(self):
        os.environ.pop("JARVIS_MOBILE_DATA_DIR", None)
        self.ml._reset_singleton()

    def test_pair_redeem_once(self):
        code, _exp = self.link.create_pairing_code("test phone")
        self.assertEqual(len(code), 8)
        ok, payload = self.link.redeem_pairing_code(code, "Pixel")
        self.assertTrue(ok)
        self.assertIn("token", payload)
        self.assertIn("device_id", payload)
        # Single-use: second redeem of the same code fails
        ok2, _err = self.link.redeem_pairing_code(code, "Pixel")
        self.assertFalse(ok2)

    def test_unknown_code_fails(self):
        ok, _err = self.link.redeem_pairing_code("ZZZZ9999", "x")
        self.assertFalse(ok)

    def test_expired_code_fails(self):
        code, _exp = self.link.create_pairing_code("old")
        # Force expiry by rewriting the stored slot
        data = self.link._load()
        for slot in data["pending_codes"].values():
            slot["expires_at"] = time.time() - 1
        self.link._save(data)
        ok, err = self.link.redeem_pairing_code(code, "x")
        self.assertFalse(ok)
        self.assertIn("expired", str(err))


class TestTokenStorage(unittest.TestCase):
    def setUp(self):
        self.ml, self.link, self.tmp = _fresh_link()

    def tearDown(self):
        os.environ.pop("JARVIS_MOBILE_DATA_DIR", None)
        self.ml._reset_singleton()

    def test_hash_only_storage(self):
        code, _ = self.link.create_pairing_code()
        ok, payload = self.link.redeem_pairing_code(code, "phone")
        self.assertTrue(ok)
        token = payload["token"]
        with open(os.path.join(self.tmp, "mobile_devices.json")) as fh:
            raw = fh.read()
        self.assertNotIn(token, raw)  # raw token never on disk
        data = json.loads(raw)
        dev = data["devices"][payload["device_id"]]
        self.assertIn("token_hash", dev)
        self.assertEqual(len(dev["token_hash"]), 64)  # sha-256 hex

    def test_devices_file_is_private(self):
        code, _ = self.link.create_pairing_code()
        self.link.redeem_pairing_code(code, "phone")
        mode = os.stat(os.path.join(self.tmp,
                                    "mobile_devices.json")).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_events_file_is_private(self):
        from unittest.mock import patch
        with patch("utils.episodic_memory.EpisodicMemory"):
            self.link.apply_sync("dev_x", [{
                "id": "uuid-priv", "type": "memory.remember",
                "payload": {"text": "private note"}}])
        mode = os.stat(os.path.join(self.tmp,
                                    "mobile_events.jsonl")).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_verify_and_revoke(self):
        code, _ = self.link.create_pairing_code()
        ok, payload = self.link.redeem_pairing_code(code, "phone")
        dev = self.link.verify_token(payload["token"])
        self.assertIsNotNone(dev)
        self.assertEqual(dev["device_id"], payload["device_id"])
        self.assertIsNone(self.link.verify_token("bogus-token"))
        # Revoke kills it immediately
        self.assertTrue(self.link.revoke_device(payload["device_id"]))
        self.assertIsNone(self.link.verify_token(payload["token"]))
        self.assertFalse(self.link.revoke_device("dev_nope"))

    def test_list_hides_token_material(self):
        code, _ = self.link.create_pairing_code()
        ok, payload = self.link.redeem_pairing_code(code, "phone")
        devices = self.link.list_devices()
        self.assertEqual(len(devices), 1)
        blob = json.dumps(devices)
        self.assertNotIn(payload["token"], blob)
        self.assertNotIn("token_hash", blob)


class TestRateLimit(unittest.TestCase):
    def setUp(self):
        self.ml, self.link, self.tmp = _fresh_link()

    def tearDown(self):
        os.environ.pop("JARVIS_MOBILE_DATA_DIR", None)
        self.ml._reset_singleton()

    def test_failures_trip_limiter(self):
        ip = "9.9.9.9-test"
        self.assertFalse(self.link.is_rate_limited(ip))
        for _ in range(10):
            self.link.record_auth_failure(ip)
        self.assertTrue(self.link.is_rate_limited(ip))


class TestSync(unittest.TestCase):
    def setUp(self):
        self.ml, self.link, self.tmp = _fresh_link()

    def tearDown(self):
        os.environ.pop("JARVIS_MOBILE_DATA_DIR", None)
        self.ml._reset_singleton()

    def test_remember_applies_and_logs(self):
        from unittest.mock import patch
        with patch("utils.episodic_memory.EpisodicMemory") as mem_cls:
            out = self.link.apply_sync("dev_x", [{
                "id": "uuid-1", "type": "memory.remember",
                "payload": {"text": "Aarav likes filter coffee",
                            "category": "fact"}}])
        self.assertEqual(out, {"applied": 1, "skipped": 0})
        mem_cls.return_value.remember.assert_called_once()
        events, cursor = self.link.read_since(0)
        self.assertEqual(cursor, 1)
        self.assertEqual(events[0]["id"], "uuid-1")
        self.assertEqual(events[0]["device_id"], "dev_x")

    def test_idempotent_replay(self):
        from unittest.mock import patch
        ev = {"id": "uuid-dup", "type": "memory.remember",
              "payload": {"text": "dup"}}
        with patch("utils.episodic_memory.EpisodicMemory"):
            first = self.link.apply_sync("dev_x", [ev])
            second = self.link.apply_sync("dev_x", [ev])
        self.assertEqual(first["applied"], 1)
        self.assertEqual(second, {"applied": 0, "skipped": 1})
        _events, cursor = self.link.read_since(0)
        self.assertEqual(cursor, 1)  # logged exactly once

    def test_forget_logged_as_tombstone(self):
        """Deletes must reach other devices so they drop cached copies."""
        from unittest.mock import patch
        with patch("utils.episodic_memory.EpisodicMemory") as mem_cls:
            self.link.apply_sync("dev_x", [{
                "id": "uuid-forget", "type": "memory.forget",
                "payload": {"keyword": "stale note"}}])
            mem_cls.return_value.forget.assert_called_once()
        events, _ = self.link.read_since(0)
        self.assertEqual(events[0]["type"], "memory.forget")

    def test_unknown_type_skipped_not_logged(self):
        out = self.link.apply_sync("dev_x", [{
            "id": "uuid-bad", "type": "todo.add",
            "payload": {"text": "via chat route only"}}])
        self.assertEqual(out, {"applied": 0, "skipped": 1})
        _events, cursor = self.link.read_since(0)
        self.assertEqual(cursor, 0)

    def test_cursor_pagination(self):
        from unittest.mock import patch
        with patch("utils.episodic_memory.EpisodicMemory"):
            self.link.apply_sync("dev_x", [
                {"id": f"u-{i}", "type": "memory.remember",
                 "payload": {"text": f"note {i}"}} for i in range(3)])
        events, cursor = self.link.read_since(1)
        self.assertEqual(cursor, 3)
        self.assertEqual([e["id"] for e in events], ["u-1", "u-2"])


class TestMobileSafety(unittest.TestCase):
    """A paired phone must NEVER bypass the human-in-the-loop gate."""

    def test_mobile_origin_is_untrusted(self):
        from utils.approvals import TRUSTED_ORIGINS
        self.assertNotIn("mobile", TRUSTED_ORIGINS)
        self.assertFalse(any(str(o).startswith("mobile")
                             for o in TRUSTED_ORIGINS))

    def test_destructive_from_mobile_always_gates(self):
        from utils.approvals import ApprovalManager
        mgr = ApprovalManager.__new__(ApprovalManager)
        for action in ("send_email", "delete_file", "dev_command",
                       "computer_use", "forget_memory"):
            self.assertTrue(
                mgr.requires(action, {"action": action,
                                      "_origin": "mobile:dev_abc"}),
                f"{action} from mobile must hold for a human")

    def test_event_bus_normalizer(self):
        from utils.event_bus import EventBus
        ev = EventBus.from_mobile_text("remind me at 5pm",
                                       device_id="dev_abc")
        self.assertEqual(ev.source, "mobile")
        self.assertEqual(ev.kind, "text")
        self.assertEqual(ev.text, "remind me at 5pm")
        self.assertEqual(ev.meta.get("device_id"), "dev_abc")


class TestHandoff(unittest.TestCase):
    def setUp(self):
        self.ml, self.link, self.tmp = _fresh_link()

    def tearDown(self):
        os.environ.pop("JARVIS_MOBILE_DATA_DIR", None)
        self.ml._reset_singleton()

    def test_export_bundle_shape(self):
        class FakeBrain:
            history = [("u1", "a1"), ("u2", "a2")]

        class FakeTasks:
            def items_json(self):
                return [{"text": "buy milk"}]

        class FakeExecutor:
            tasks = FakeTasks()

        bundle = self.ml.MobileLink.export_bundle(FakeBrain(),
                                                  FakeExecutor())
        self.assertEqual(bundle["version"], 1)
        self.assertEqual(len(bundle["history"]), 2)
        self.assertEqual(bundle["todos"], [{"text": "buy milk"}])
        self.assertIsInstance(bundle["goals"], list)

    def test_import_bundle_never_raises(self):
        from unittest.mock import patch
        # ALL calls stay inside the mock: import_bundle writes to the
        # real episodic store, and tests must never pollute it.
        with patch("utils.episodic_memory.EpisodicMemory"):
            summary = self.ml.MobileLink.import_bundle(
                {"history": [{"user": "buy eggs"}],
                 "goals": [{"title": "run 5k"}]}, "dev_x")
            garbage_none = self.ml.MobileLink.import_bundle(None, "dev_x")
            garbage_weird = self.ml.MobileLink.import_bundle(
                {"weird": 1}, "dev_x")
        self.assertIn("buy eggs", summary)
        # Garbage in → graceful message, never an exception
        self.assertIsInstance(garbage_none, str)
        self.assertIsInstance(garbage_weird, str)


class TestPwaShell(unittest.TestCase):
    """Phase 0 remote: /mobile shell + manifest + SW exist and are wired."""

    def _root(self):
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_mobile_template_exists(self):
        # Template holds the shell (pair/chat views, PWA links) — the
        # API paths live in mobile.js, covered by the wiring test below.
        path = os.path.join(self._root(), "templates", "mobile.html")
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
        for needle in ("id=\"pair\"", "id=\"chat\"", "id=\"msgs\"",
                       "id=\"hub\"", "id=\"code\"",
                       "id=\"plink\"", "id=\"fromlink\"",
                       "mobile-manifest.json", "mobile-sw.js", "mobile.js",
                       "mobile.css", "apple-mobile-web-app-capable"):
            self.assertIn(needle, html)

    def test_mobile_client_wiring(self):
        path = os.path.join(self._root(), "static", "mobile.js")
        with open(path, encoding="utf-8") as fh:
            js = fh.read()
        for needle in ("/api/mobile/redeem", "/api/mobile/chat",
                       "/api/mobile/sync", "Authorization", "localStorage",
                       "outbox", "cursor"):
            self.assertIn(needle, js)

    def test_pair_link_shared_funnel(self):
        # Dashboard QR, PWA paste-link, and Flutter QR/paste all share
        # ONE funnel: jarvis://pair?hub=..&code=.. over POST /redeem.
        # No new protocol, no new trust — the 8-char code stays
        # single-use with a 10-min TTL.
        root = self._root()
        with open(os.path.join(root, "static", "mobile.js"),
                  encoding="utf-8") as fh:
            pwa = fh.read()
        self.assertIn("jarvis://pair", pwa)
        self.assertIn("parsePairLink", pwa)
        # Same no-look-alike alphabet as the hub (_CODE_ALPHABET).
        self.assertIn("23456789ABCDEFGHJKMNPQRSTUWXYZ", pwa)
        with open(os.path.join(root, "static", "script.js"),
                  encoding="utf-8") as fh:
            dash = fh.read()
        self.assertIn("jarvis://pair", dash)
        self.assertIn("renderPairShare", dash)
        self.assertIn("QRCode", dash)
        with open(os.path.join(root, "templates", "index.html"),
                  encoding="utf-8") as fh:
            html = fh.read()
        for needle in ("id=\"mobile-qr\"", "id=\"mobile-link\"",
                       "qrcodejs"):
            self.assertIn(needle, html)

    def test_lite_apk_workflow_exists(self):
        path = os.path.join(self._root(), ".github", "workflows",
                            "lite-apk.yml")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for needle in ("flutter pub get", "flutter analyze", "flutter test",
                       "flutter create --platforms=android",
                       "flutter build apk --debug", "app-debug.apk"):
            self.assertIn(needle, src)

    def test_manifest_and_sw_exist(self):
        import json as _json
        root = self._root()
        with open(os.path.join(root, "static",
                               "mobile-manifest.json")) as fh:
            man = _json.load(fh)
        self.assertEqual(man["start_url"], "/mobile")
        self.assertIn("standalone", man["display"])
        with open(os.path.join(root, "static", "mobile-sw.js")) as fh:
            sw = fh.read()
        self.assertIn("/api/mobile/", sw)  # API bypasses the cache
        self.assertIn("/mobile", sw)

    def test_pwa_routes_registered(self):
        import re
        path = os.path.join(self._root(), "utils", "server.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        routes = set(re.findall(r"@app\.route\('([^']+)'", src))
        for r in ("/mobile", "/mobile-manifest.json", "/mobile-sw.js",
                  "/api/mobile/pair", "/api/mobile/redeem",
                  "/api/mobile/devices", "/api/mobile/chat",
                  "/api/mobile/sync", "/api/mobile/handoff"):
            self.assertIn(r, routes)


if __name__ == "__main__":
    unittest.main()
