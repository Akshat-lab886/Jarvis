"""Unit tests for Tools.capture_photo — the webcam capture path.

Mostly covers the camera-resource-leak regression: if cam.read() raises
a hardware error, the VideoCapture handle MUST still be released
(previously release() ran only on the happy path, leaking the device).

cv2 is injected via sys.modules so no native OpenCV is required; the
camera object is a fake that records release() calls and can simulate
a read() raise.
"""

import os
import sys
import types
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeFrame:
    pass


class _FakeCam:
    """Stand-in for cv2.VideoCapture."""
    instances = []
    def __init__(self, *a, **kw):
        self.opened = True
        self.released = False
        self.read_result = (True, _FakeFrame())
        self.read_should_raise = False
        _FakeCam.instances.append(self)

    def isOpened(self):
        return self.opened

    def read(self):
        if self.read_should_raise:
            raise RuntimeError("camera hardware error")
        return self.read_result

    def release(self):
        self.released = True

    def __getattr__(self, name):
        # cv2.imwrite etc. — no-op for the capture path
        def _noop(*a, **k):
            return True
        return _noop


class _FakeCv2:
    VideoCapture = _FakeCam
    imwrite = staticmethod(lambda *a, **k: True)
    imread = staticmethod(lambda *a, **k: None)
    # Constants used by the mock-image generation path.
    FONT_HERSHEY_SIMPLEX = 0
    LINE_AA = 1


class TestCapturePhoto(unittest.TestCase):
    def setUp(self):
        _FakeCam.instances = []
        self._cv2_backup = sys.modules.get('cv2')
        sys.modules['cv2'] = _FakeCv2()

    def tearDown(self):
        if self._cv2_backup is not None:
            sys.modules['cv2'] = self._cv2_backup
        else:
            sys.modules.pop('cv2', None)

    def _make_tools(self):
        """Build a Tools instance without running Secretary side-effects."""
        from utils.tools import Tools
        with patch('utils.tools.Secretary'):
            return Tools()

    def test_camera_released_on_normal_capture(self):
        tools = self._make_tools()
        path = tools.capture_photo()
        self.assertTrue(path)
        self.assertEqual(len(_FakeCam.instances), 1)
        self.assertTrue(_FakeCam.instances[0].released)

    def test_camera_released_when_read_raises(self):
        """REGRESSION: cam.read() raising must NOT skip cam.release()."""
        tools = self._make_tools()
        # Force the read() to raise a hardware error.
        # Patch VideoCapture so every new instance has read_should_raise=True.
        original_vc = _FakeCv2.VideoCapture
        class _ExplodingCam(_FakeCam):
            def read(self):
                raise RuntimeError("I/O error: camera disconnected")
        _FakeCv2.VideoCapture = _ExplodingCam
        try:
            path = tools.capture_photo()  # should NOT raise
        finally:
            _FakeCv2.VideoCapture = original_vc
        # Cam was still released despite the read() exception.
        cams = [c for c in _FakeCam.instances
                if isinstance(c, _ExplodingCam)]
        self.assertTrue(cams, "no exploding-cam instance recorded")
        self.assertTrue(cams[0].released,
                        "Camera handle leaked across a read() exception")

    def test_webcam_not_found_falls_back_to_mock(self):
        tools = self._make_tools()
        with patch.object(_FakeCam, 'isOpened', return_value=False):
            path = tools.capture_photo()
        # No real frame captured; mock image path returned.
        self.assertEqual(len(_FakeCam.instances), 1)
        self.assertTrue(_FakeCam.instances[0].released)


if __name__ == "__main__":
    unittest.main(verbosity=2)
