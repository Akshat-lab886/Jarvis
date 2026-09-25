"""
Tier A Mobile Vision Relay — route + event bus tests
====================================================

Covers the phone-camera → /api/mobile/vision → hub pipeline WITHOUT
needing a live vision model (the auth/size/empty guards run before the
router is touched, so they're fully testable):

  - /api/mobile/vision: 400 on missing image
  - /api/mobile/vision: 413 on oversized image
  - /api/mobile/vision: 401 with no/invalid Bearer token
  - _extract_conf: parses the "(local siglip conf 0.42)" float, None-safe
  - event_bus.from_mobile_photo: shape (kind=photo, source=mobile,
    device_id + image_meta carried)
  - vision dispatch path: executor.action == 'vision' routes to
    ext_api via the agent tool set (schema present, dispatch present)
"""

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestExtractConf(unittest.TestCase):
    """_extract_conf parses the siglip confidence from a caption string."""

    def test_extracts_valid_confidence(self):
        from utils.server import _extract_conf
        self.assertEqual(_extract_conf("[vision] a whiteboard (conf 0.51)"), 0.51)
        self.assertEqual(_extract_conf("some text (local siglip conf 0.99)"), 0.99)

    def test_none_and_empty(self):
        from utils.server import _extract_conf
        self.assertIsNone(_extract_conf(""))
        self.assertIsNone(_extract_conf(None))
        self.assertIsNone(_extract_conf("no confidence here"))

    def test_round_numbers(self):
        from utils.server import _extract_conf
        self.assertEqual(_extract_conf("conf 0.5"), 0.5)
        self.assertEqual(_extract_conf("conf 1.0"), 1.0)


class TestFromMobilePhoto(unittest.TestCase):
    """from_mobile_photo normalizer shape."""

    def test_event_shape(self):
        from utils.event_bus import get_bus, InternalEvent
        bus = get_bus.__wrapped__() if hasattr(get_bus, "__wrapped__") else get_bus()
        ev = bus.from_mobile_photo(
            "a whiteboard",
            device_id="dev-123",
            image_meta={"media": "image/png", "chars": 4096})
        self.assertIsInstance(ev, InternalEvent)
        self.assertEqual(ev.source, "mobile")
        self.assertEqual(ev.kind, "photo")
        self.assertIn("a whiteboard", ev.text)
        self.assertEqual(ev.meta.get("device_id"), "dev-123")
        self.assertEqual(ev.meta.get("image_meta", {}).get("media"), "image/png")

    def test_defaults(self):
        from utils.event_bus import get_bus, InternalEvent
        bus = get_bus()
        ev = bus.from_mobile_photo("caption")
        self.assertEqual(ev.kind, "photo")
        self.assertEqual(ev.source, "mobile")
        self.assertEqual(ev.meta.get("device_id"), None)


class TestMobileVisionRouteGuards(unittest.TestCase):
    """Pre-vision guards on /api/mobile/vision (auth, size, empty).

    These run before router.chat(), so no vision model is required."""

    def setUp(self):
        # Point the Flask app at a fresh MobileLink in a temp dir so we
        # control pairing without touching real devices.
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="jarvis-vision-test-")
        os.environ["JARVIS_MOBILE_DATA_DIR"] = self.tmp
        import utils.mobile_link as ml
        ml._reset_singleton()
        # Re-import server so it picks up the temp data dir.
        import importlib
        if "utils.server" in sys.modules:
            importlib.reload(sys.modules["utils.server"])
        import utils.server as srv
        self.app = srv.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        os.environ.pop("JARVIS_MOBILE_DATA_DIR", None)
        import utils.mobile_link as ml
        ml._reset_singleton()

    def _pair(self):
        """Create a paired device + valid token for auth."""
        import utils.mobile_link as ml
        link = ml.get_link()
        code, _ = link.create_pairing_code("test-phone")
        ok, payload = link.redeem_pairing_code(code, "test-phone")
        return payload["token"]

    def test_missing_image_returns_400(self):
        token = self._pair()
        r = self.client.post("/api/mobile/vision",
                             json={"wait": True},
                             headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_oversized_image_returns_413(self):
        token = self._pair()
        # > 2,800,000 base64 chars (the route's guard), before vision runs.
        big = "data:image/jpeg;base64," + ("A" * 3_000_000)
        r = self.client.post("/api/mobile/vision",
                             json={"image": big, "wait": True},
                             headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 413)

    def test_no_token_returns_401(self):
        r = self.client.post("/api/mobile/vision",
                             json={"image": "data:image/jpeg;base64,abc"})
        self.assertEqual(r.status_code, 401)

    def test_bad_token_returns_401(self):
        r = self.client.post("/api/mobile/vision",
                             json={"image": "data:image/jpeg;base64,abc"},
                             headers={"Authorization": "Bearer deadbeef"})
        self.assertEqual(r.status_code, 401)

    @patch("utils.server._mobile_auth")
    @patch("utils.llm.router.get_router")
    def test_vision_success_dispatches_router(self, mock_router, mock_auth):
        """With auth bypassed + router stubbed, a valid image yields a
        caption JSON reply."""
        # fake paired device
        mock_auth.return_value = ({"device_id": "dev", "name": "phone"}, None)
        fake_result = MagicMock()
        fake_result.text = "[vision] a whiteboard (conf 0.51)"
        fake_result.provider = "siglip"
        fake_result.model = "siglip-base-patch16-224"
        mock_router.return_value.chat.return_value = fake_result
        # need a bus + from_mobile_photo; stub publish
        with patch("utils.server.bus_publish", create=True, return_value=None):
            r = self.client.post("/api/mobile/vision",
                                 json={"image": "data:image/jpeg;base64,/9j/4AAQ",
                                       "wait": True})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["caption"], "[vision] a whiteboard (conf 0.51)")
        self.assertEqual(body["provider"], "siglip")
        self.assertAlmostEqual(body["confidence"], 0.51, places=2)


if __name__ == "__main__":
    unittest.main()
