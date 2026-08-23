"""
Phase D tests: recurring automations, telegram voice pipeline guards,
and daemon deploy assets.
"""

import os
import sys
import json
import shutil
import tempfile
import datetime
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.recurring import RecurringAutomations


def _make_tmp():
    return tempfile.mkdtemp(prefix="jarvis_phaseD_")


class _SilentMouth:
    def __init__(self):
        self.spoken = []
    def speak(self, text):
        self.spoken.append(text)


def _automation(tmp, **kw):
    """Test instance with the background loop suppressed — we drive
    _check_due() manually with fake clocks, so a live polling thread
    firing against the real wall-clock would corrupt the sequence."""
    ra = RecurringAutomations(
        file_path=os.path.join(tmp, 'jobs.json'),
        poll_interval=999,
        **kw,
    )
    ra._running = True   # makes add()'s internal start() a no-op
    return ra


# ====================================================================== #
# Parsing
# ====================================================================== #

class TestParse(unittest.TestCase):
    def setUp(self):
        self.tmp = _make_tmp()
        self.ra = _automation(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.ra.stop()

    def _parse(self, text):
        job, err = self.ra.parse(text)
        return job, err

    def test_daily_am(self):
        job, err = self._parse("every day at 9am give me my morning briefing")
        self.assertIsNone(err)
        self.assertEqual(job['schedule'], 'daily')
        self.assertEqual((job['hour'], job['minute']), (9, 0))
        self.assertEqual(job['action'], "give me my morning briefing")

    def test_weekdays_24h(self):
        job, err = self._parse("every weekday at 18:30 check my calendar")
        self.assertIsNone(err)
        self.assertEqual(job['schedule'], 'weekdays')
        self.assertEqual((job['hour'], job['minute']), (18, 30))

    def test_named_day_pm(self):
        job, err = self._parse("every friday at 8pm order pizza")
        self.assertIsNone(err)
        self.assertEqual(job['schedule'], [4])
        self.assertEqual(job['hour'], 20)

    def test_pm_crosses_noon(self):
        job, err = self._parse("every day at 12:15am stretch")
        self.assertIsNone(err)
        self.assertEqual((job['hour'], job['minute']), (0, 15))

    def test_invalid_schedule_rejected(self):
        job, err = self._parse("sometime soon do things")
        self.assertIsNone(job)
        self.assertIn("couldn't parse", err.lower())

    def test_empty_action_rejected(self):
        job, err = self._parse("every day at 9am")
        self.assertIsNone(job)
        self.assertTrue(err)


# ====================================================================== #
# CRUD + persistence + firing
# ====================================================================== #

class TestLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = _make_tmp()
        self.ra = _automation(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.ra.stop()

    def test_add_list_remove_roundtrip(self):
        msg = self.ra.add("every day at 7am water the plants")
        self.assertIn("#1", msg)
        self.assertEqual(self.ra.count(), 1)

        listing = self.ra.list_jobs()
        self.assertIn("water the plants", listing)
        self.assertIn("7:00 AM", listing)

        self.assertIn("Cancelled", self.ra.remove("plants"))
        self.assertEqual(self.ra.count(), 0)
        self.assertIn("no recurring", self.ra.list_jobs().lower())

    def test_jobs_persist_across_restart(self):
        self.ra.add("every monday at 9am weekly review")
        revived = _automation(self.tmp)      # fresh instance, same file
        self.assertEqual(revived.count(), 1)
        self.assertIn("weekly review", revived.list_jobs())
        revived.stop()

    def test_due_logic_and_once_per_day(self):
        fired = []
        self.ra.on_fire = lambda job: fired.append(job['id']) or "done"
        self.ra.add("every day at 9am morning briefing")
        job = self.ra._jobs[0]

        friday_0859 = datetime.datetime(2026, 8, 21, 8, 59)   # before 9
        friday_0900 = datetime.datetime(2026, 8, 21, 9, 0)
        friday_0905 = datetime.datetime(2026, 8, 21, 9, 5)
        saturday_0905 = datetime.datetime(2026, 8, 22, 9, 5)

        self.assertEqual(self.ra._check_due(friday_0859), 0)
        self.assertEqual(self.ra._check_due(friday_0900), 1)
        self.assertEqual(fired, [job['id']])
        # Same day again → must NOT refire
        self.assertEqual(self.ra._check_due(friday_0905), 0)
        # Next day → fires again
        self.assertEqual(self.ra._check_due(saturday_0905), 1)
        self.assertEqual(len(fired), 2)

    def test_weekdays_job_skips_weekend(self):
        self.ra.add("every weekday at 9am check email")
        saturday = datetime.datetime(2026, 8, 22, 9, 30)
        monday = datetime.datetime(2026, 8, 24, 9, 30)
        self.assertEqual(self.ra._check_due(saturday), 0)
        # Monday 9:30 is past due time but last_fired is None → fires
        self.assertEqual(self.ra._check_due(monday), 1)

    def test_callback_exception_does_not_crash_loop(self):
        def boom(job):
            raise RuntimeError("callback exploded")
        self.ra.on_fire = boom
        self.ra.add("every day at 9am x")
        self.assertEqual(self.ra._check_due(
            datetime.datetime(2026, 8, 21, 9, 0)), 1)
        # marked as fired even though callback failed
        today = datetime.datetime(2026, 8, 21).strftime('%Y-%m-%d')
        self.assertEqual(self.ra._jobs[0]['last_fired'], today)


# ====================================================================== #
# Telegram voice pipeline guard
# ====================================================================== #

class TestTranscribeGuard(unittest.TestCase):
    def test_missing_file_raises_cleanly(self):
        from utils.telegram_bot import _transcribe_audio_file
        bogus = os.path.join(_make_tmp(), "does_not_exist.ogg")
        with self.assertRaises(Exception):
            _transcribe_audio_file(bogus)

    def test_non_audio_file_reports_failure(self):
        """ffmpeg exits non-zero on garbage input — must surface."""
        tmp = _make_tmp()
        fake = os.path.join(tmp, "fake.ogg")
        with open(fake, 'w') as f:
            f.write("this is not audio at all")
        from utils.telegram_bot import _transcribe_audio_file
        with self.assertRaises(RuntimeError):
            _transcribe_audio_file(fake)
        shutil.rmtree(tmp, ignore_errors=True)


# ====================================================================== #
# Daemon deploy assets
# ====================================================================== #

class TestDeployAssets(unittest.TestCase):
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_shell_scripts_syntax_valid(self):
        for script in ('deploy/jarvisctl.sh', 'deploy/install_daemon.sh'):
            path = os.path.join(self.ROOT, script)
            self.assertTrue(os.path.exists(path), script)
            proc = os.system(f'bash -n "{path}"')
            self.assertEqual(proc, 0, f"{script} has syntax errors")

    def test_plist_template_placeholders(self):
        tpl = open(os.path.join(self.ROOT,
                  'deploy/com.jarvis.assistant.plist.in')).read()
        for ph in ('@PROJECT_DIR@', '@PYTHON_BIN@', '@LOG_FILE@'):
            self.assertIn(ph, tpl)
        self.assertIn('--headless', tpl)
        self.assertIn('KeepAlive', tpl)

    def test_main_supports_headless_flag(self):
        src = open(os.path.join(self.ROOT, 'main.py')).read()
        self.assertIn('--headless', src)
        self.assertIn('JARVIS_HEADLESS', src)
        self.assertIn('SIGTERM', src)


# ====================================================================== #

if __name__ == '__main__':
    print("=" * 60)
    print("JARVIS PHASE D — AUTOMATIONS / MULTIMODAL / DAEMON TESTS")
    print("=" * 60)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromModule(
        sys.modules[__name__]))
    print("\n" + "=" * 60)
    if result.wasSuccessful():
        print(f"ALL {result.testsRun} TESTS PASSED ✅")
    else:
        for test, trace in result.failures + result.errors:
            print(f"  ❌ {test}\n{trace[:300]}")
    print("=" * 60)
