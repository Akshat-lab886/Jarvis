"""
Tests for utils.llm.catalog — the model capability registry.

This is the source of truth for the Tier A router's capability routing:
router.chat(require={'vision'}) consults caps_for/has_caps to decide
which provider gets an image request. A wrong entry here can either
(1) send a vision request to a text-only model (hallucinated refusal)
or (2) fail to route vision to a model that supports it.

Covers:
  - caps_for: exact catalog hits (Vision False for deepseek-v4.1,
    True for gemini-2.5-pro / gpt-4o / siglip), unknown-model heuristic
    vision detection, context defaults, tools/json_mode defaults.
  - has_caps: empty require -> True, vision/long_context gating,
    unknown capability -> True (fail-open so new caps don't block).
  - The controlled Vyce vision=False rule (regression guard: the
    deepseek-v4.1 chat endpoint returns 200 refusals for images, so
    it MUST never be selected for vision).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestCapsFor(unittest.TestCase):
    def test_catalog_hit_returns_overrides(self):
        from utils.llm.catalog import caps_for
        info = caps_for("google", "gemini-2.5-pro")
        self.assertEqual(info.provider, "google")
        self.assertEqual(info.model, "gemini-2.5-pro")
        self.assertEqual(info.context, 1048576)
        self.assertTrue(info.vision)

    def test_unknown_model_uses_heuristic_context(self):
        from utils.llm.catalog import caps_for
        info = caps_for("ollama", "nomic-embed-text")
        # Not in catalog -> defaults: context 32768, tools/json True.
        self.assertEqual(info.context, 32768)
        self.assertTrue(info.tools)
        self.assertTrue(info.json_mode)

    def test_vyce_text_only_no_vision(self):
        """CRITICAL: deepseek-v4.1 returns 200 refusals for images —
        vision MUST be False so the router routes vision elsewhere."""
        from utils.llm.catalog import caps_for
        info = caps_for("vyce", "deepseek-v4.1")
        self.assertFalse(info.vision)
        self.assertEqual(info.context, 128000)

    def test_siglip_multimodal_entry(self):
        from utils.llm.catalog import caps_for
        info = caps_for("siglip", "siglip-base-patch16-224")
        self.assertTrue(info.vision)
        self.assertFalse(info.tools)
        self.assertFalse(info.json_mode)
        self.assertEqual(info.context, 2048)

    def test_heuristic_vision_hint(self):
        """Unknown models with vision-like name fragments get vision=True."""
        from utils.llm.catalog import caps_for
        for model in ["gpt-4o", "llava-13b", "qwen-vl-2b",
                      "pixtral-12b", "gemini-2.0-flash-thinking"]:
            info = caps_for("custom", model)
            self.assertTrue(info.vision,
                            f"'{model}' should be heuristically vision-capable")

    def test_heuristic_no_vision_hint(self):
        from utils.llm.catalog import caps_for
        for model in ["gpt-4o-mini"]:  # 'mini' but contains 'gpt-4o' -> vision
            info = caps_for("openai", model)
            # gpt-4o-mini IS in the catalog with vision=True.
            self.assertTrue(info.vision)
        # A genuinely unknown, non-vision name -> False.
        info = caps_for("huggingface", "meta-llama-3-8b")
        self.assertFalse(info.vision)


class TestHasCaps(unittest.TestCase):
    def test_empty_require_is_true(self):
        from utils.llm.catalog import has_caps
        self.assertTrue(has_caps("openai", "gpt-4o", {}))
        self.assertTrue(has_caps("openai", "gpt-4o", None))

    def test_vision_required(self):
        from utils.llm.catalog import has_caps
        # gpt-4o supports vision -> passes.
        self.assertTrue(has_caps("openai", "gpt-4o", {"vision"}))
        # deepseek-v4.1 does NOT -> fails.
        self.assertFalse(has_caps("vyce", "deepseek-v4.1", {"vision"}))

    def test_long_context_required(self):
        from utils.llm.catalog import has_caps
        # gemini-2.5-pro: 1M context >= 100k -> passes.
        self.assertTrue(has_caps("google", "gemini-2.5-pro",
                                 {"long_context"}))
        # siglip: 2048 context < 100k -> fails long_context.
        self.assertFalse(has_caps("siglip", "siglip-base-patch16-224",
                                  {"long_context"}))

    def test_tools_required(self):
        from utils.llm.catalog import has_caps
        # gpt-4o: tools=True.
        self.assertTrue(has_caps("openai", "gpt-4o", {"tools"}))
        # siglip: tools=False -> fails.
        self.assertFalse(has_caps("siglip", "siglip-base-patch16-224",
                                  {"tools"}))

    def test_unknown_capability_fails_open(self):
        """A capability string we don't recognize -> True (don't block)."""
        from utils.llm.catalog import has_caps
        self.assertTrue(has_caps("openai", "gpt-4o", {"some_future_cap"}))

    def test_multiple_requirements_all_must_pass(self):
        from utils.llm.catalog import has_caps
        # needs both vision + long_context -> deepseek fails both.
        self.assertFalse(has_caps("vyce", "deepseek-v4.1",
                                  {"vision", "long_context"}))
        # needs vision but NOT long_context -> ok on gpt-4o (128k is long
        # enough for >= 100k, and vision=True), but siglip fails long_context.
        self.assertFalse(has_caps("siglip", "siglip-base-patch16-224",
                                  {"vision", "long_context"}))


class TestModelInfoDefaults(unittest.TestCase):
    """ModelInfo dataclass default sanity checks."""

    def test_default_fields(self):
        from utils.llm.catalog import ModelInfo
        info = ModelInfo(provider="x", model="y")
        self.assertEqual(info.context, 32768)
        self.assertFalse(info.vision)
        self.assertTrue(info.tools)
        self.assertTrue(info.json_mode)


if __name__ == "__main__":
    unittest.main()
