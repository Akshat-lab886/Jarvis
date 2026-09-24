"""
Local vision provider — SigLIP (ONNX), runs on-device (CoreML / CPU).

This is the *offline fallback* for ``require={'vision'}`` requests: when no
cloud vision model (Gemini/OpenAI/Anthropic) is configured or reachable,
the router fails over to this provider so the desktop/mobile computer-use
loop can still "see" via the phone camera without any network vision call.

It is a *caption-ranker*, not a decoder: it encodes the image with SigLIP's
vision encoder, encodes a fixed set of candidate captions with the text
encoder, and returns the highest image-to-text similarity as
``ChatResult.text``.  The agent then reasons over that caption exactly like it
would a real vision model's output, so the chunk-planned computer-use loop
can plan around "a screenshot of code" / "ui with buttons" / "a whiteboard"
etc. without ever hitting a cloud API.

Model cache: ``Xenova/siglip-base-patch16-224`` (ONNX quantized, ~211MB),
located at ``Config.VISION_LOCAL_CACHE`` or ``$JARVIS_VISION_CACHE``.
Zero config past that.  No API key.

On-device inference via onnxruntime + CoreMLExecutionProvider (Apple Silicon
GPU), CPUExecutionProvider fallback on machines without CoreML.

NOTE ON CONFIDENCE: the softmax over candidate captions is a *ranking*
confidence, not a calibrated probability — if no candidate fits, the max
probability is still low and the caller should treat the result as
"unrecognized image" and fail over to a cloud vision model if one is
available, rather than trusting a weak match.
"""

import os
import io
import time
import logging

import numpy as np

from utils.llm.providers.base import BaseProvider, ChatResult, Usage

logger = logging.getLogger("Jarvis.LLM.SigLIP")

# Candidate captions ranked against every image.  Tuned for the "what did
# the camera / phone / screen just capture" framing the computer-use loop
# consumes: screenshots, ui, whiteboards, documents, objects, barcodes.
_CAPTIONS = (
    "a screenshot of computer code or terminal text",
    "a phone or laptop screen showing a user interface with buttons and text",
    "a close-up of a whiteboard full of handwriting or diagrams",
    "a photograph of a printed page - a document with paragraphs of text",
    "a real-world scene with people in an office or indoor setting",
    "a flat lay of objects on a desk - keyboard, phone, notebook",
    "a barcode or QR code on a screen or paper",
    "a blank wall or empty surface with no readable content",
    "a graph, chart, or data visualization on a screen",
    "a hand holding a phone showing a user interface",
)

_IMG = 224          # SigLIP patch16 base input resolution
_TOKEN_LEN = 16     # token length expected by the text encoder


def _default_cache():
    """Default HF cache: ~/.cache/huggingface, mirroring the hf_hub default."""
    return os.path.join(os.path.expanduser("~"), ".cache", "huggingface")


class SiglipVisionProvider(BaseProvider):
    """ONNX SigLIP, zero-shot image-to-text ranking as a caption sink."""

    name = "siglip"
    _SESSION = None  # shared singleton; ORT inference is thread-safe for reads

    def __init__(self, cache_dir=None, models=None):
        super().__init__(models=models or ["siglip-base-patch16-224"])
        from config import Config
        self.cache_dir = (
            cache_dir
            or os.getenv("JARVIS_VISION_CACHE")
            or getattr(Config, "VISION_LOCAL_CACHE", "")
            or _default_cache()
        )
        self._model_dir = None
        self._tokenizer = None

    # ------------------------------------------------------------------ #

    def _resolve_dir(self):
        """Return the SigLIP snapshot dir inside the HF cache, or None."""
        if self._model_dir:
            return self._model_dir
        hub = os.path.join(self.cache_dir, "hub")
        if not os.path.isdir(hub):
            return None
        prefix = "models--Xenova--siglip-base-patch16-224"
        for root, _dirs, _files in os.walk(hub):
            if os.path.basename(root) != prefix:
                continue
            snap = os.path.join(root, "snapshots")
            if not os.path.isdir(snap):
                continue
            for rev in sorted(os.listdir(snap), reverse=True):
                cand = os.path.join(snap, rev)
                if os.path.isdir(os.path.join(cand, "onnx")):
                    self._model_dir = cand
                    return cand
        return None

    @classmethod
    def _session(cls, model_dir):
        """Load (or return cached) ONNX session; CoreML first, CPU else."""
        if cls._SESSION is not None:
            return cls._SESSION
        import onnxruntime as ort
        onx = os.path.join(model_dir, "onnx", "model_quantized.onnx")
        if not os.path.exists(onx):
            onx = os.path.join(model_dir, "onnx", "model.onnx")
            if not os.path.exists(onx):
                raise FileNotFoundError("no .onnx in " + model_dir)
        sess = ort.InferenceSession(
            onx,
            providers=["CoreMLExecutionProvider", "CPUExecutionProvider"],
        )
        cls._SESSION = sess
        logger.info("SigLIP ONNX loaded with providers=%s", sess.get_providers())
        return sess

    def _load_tokenizer(self):
        """Lazy tokenizer from tokenizer.json in the model snapshot."""
        if self._tokenizer is not None:
            return self._tokenizer
        model_dir = self._resolve_dir()
        if not model_dir:
            raise RuntimeError("model dir not resolved")
        from tokenizers import Tokenizer
        tok = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        tok.enable_padding(length=_TOKEN_LEN)
        tok.enable_truncation(max_length=_TOKEN_LEN)
        self._tokenizer = tok
        return tok

    def available(self):
        """True when the local SigLIP cache is present and loads."""
        d = self._resolve_dir()
        if not d:
            logger.info("siglip not available - no model cache at %s",
                        self.cache_dir)
            return False
        try:
            self._session(d)
            return True
        except Exception as e:
            logger.warning("siglip cache present but failed to load: %s", e)
            return False

    def list_models(self):
        return list(self.models)

    # ------------------------------------------------------------------ #

    def _preprocess_image(self, b64):
        """Decode base64 JPEG/PNG to normalized (1,3,224,224) float32."""
        import base64
        from PIL import Image
        raw = base64.b64decode(b64)
        img = Image.open(io.BytesIO(raw)).convert("RGB").resize((_IMG, _IMG))
        arr = np.asarray(img, dtype=np.float32) / 255.0
        mean = np.array([0.5, 0.5, 0.5], dtype=np.float32)
        std = np.array([0.5, 0.5, 0.5], dtype=np.float32)
        arr = (arr - mean) / std
        return arr.transpose(2, 0, 1)[None, ...]

    def _encode_pair(self, session, pixel_values, input_ids):
        """Run SigLIP with both modalities -> returns logits_per_image."""
        names = [o.name for o in session.get_outputs()]
        out = session.run(None, {
            "pixel_values": pixel_values,   # (1, 3, 224, 224)
            "input_ids": input_ids,          # (n_captions, _TOKEN_LEN)
        })
        li = names.index("logits_per_image")
        return out[li]

    def chat(self, messages, *, model=None, tools=None, max_tokens=None,
             temperature=0.2, timeout=45, stream=False):
        """Rank candidate captions against the first image in the messages."""
        t0 = time.time()
        # BaseProvider._split_images expects a single message's `content`
        # (str or list), NOT the whole messages list, so walk messages and
        # harvest image parts from each content block.
        images = []
        for msg in (messages or []):
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if content is None:
                continue
            _text, imgs = self._split_images(content)
            images.extend(imgs)
        if not images:
            # No image to describe — return empty so the router skips this
            # provider on vision-less requests without crashing.
            return ChatResult(text="", provider=self.name,
                              model=self.models[0], finish_reason="stop",
                              elapsed_s=time.time() - t0)

        media_type, b64 = images[0]
        d = self._model_dir or self._resolve_dir()
        if not d:
            raise RuntimeError("siglip not initialized - cache missing")
        session = self._session(d)
        tok = self._load_tokenizer()

        pixels = self._preprocess_image(b64)  # (1, 3, 224, 224)
        encodings = [tok.encode(c).ids for c in _CAPTIONS]
        padded = [e[:_TOKEN_LEN] + [0] * (_TOKEN_LEN - len(e[:_TOKEN_LEN]))
                  for e in encodings]
        input_ids = np.array(padded, dtype=np.int64)
        logits = self._encode_pair(session, pixels, input_ids)
        # logits_per_image shape is (image_batch, text_batch) = (1, n_captions)
        sims = logits.ravel()
        best = int(np.argmax(sims))
        caption = _CAPTIONS[best]
        # SigLIP logits are scaled cosine similarities in roughly [-1, 1].
        # Softmax over candidate captions gives a normalized ranking
        # confidence: if no caption fits, the max prob stays low and the
        # agent can fail over to a cloud vision model rather than trust a
        # weak match.
        shifted = sims - sims.max()
        probs = np.exp(shifted) / np.exp(shifted).sum()
        conf = float(probs[best])
        text = "[vision] %s (local siglip conf %.2f)" % (caption, conf)
        return ChatResult(text=text, provider=self.name,
                          model=self.models[0], finish_reason="stop",
                          elapsed_s=time.time() - t0, usage=Usage(0, 0),
                          raw={"sim": sims.tolist(), "probs": probs.tolist(),
                               "best": best})
