"""
J.A.R.V.I.S. — Hyperframe (Hermes parity)
=========================================

Converts written instructions into fully custom text-based web mockups
and parses them into operational workspace videos:

    1. STORYBOARD  — the coder persona turns instructions into a JSON
       storyboard: {title, frames: [{heading, bullets, caption}]}
    2. MOCKUP      — the storyboard renders to a self-playing HTML
       mockup (auto-advancing frames, keyboard controls, print-clean
       text design) — the primary artifact, zero dependencies
    3. VIDEO       — when Pillow + ffmpeg are installed, each frame is
       rasterized and stitched into an .mp4 workspace video; otherwise
       the HTML mockup IS the deliverable (honest degradation)

    from utils.hyperframe import create
    create(brain, "onboarding flow for a fitness app: welcome, plan
    picker, payment")

``JARVIS_HYPERFRAME=0`` disables the module.
"""

import os
import re
import json
import logging
import datetime

logger = logging.getLogger("Jarvis.Hyperframe")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUTPUT_DIR = os.path.join(_BASE_DIR, 'workspace', 'hyperframe')


def enabled():
    return os.getenv('JARVIS_HYPERFRAME', '1') != '0'


# --------------------------------------------------------------------- #
# Storyboard
# --------------------------------------------------------------------- #

def storyboard_from_instructions(brain, instructions):
    """
    Ask the coder persona for a storyboard JSON.  Returns a normalized
    storyboard dict or None.
    """
    if brain is None:
        return None
    prompt = (
        "Turn these instructions into a UI storyboard mockup:\n\n"
        f"INSTRUCTIONS:\n{str(instructions)[:2000]}\n\n"
        "Design 3-6 frames that walk through the concept. Output ONLY "
        "raw JSON: "
        '{"title": "<short title>", "frames": [{"heading": "<frame '
        'title>", "bullets": ["<line>", "..."], "caption": "<one-line '
        'note>"}]}'
    )
    try:
        raw = brain.complete(prompt, agent='coder',
                             timeout=60, max_tokens=900)
    except Exception as e:
        logger.debug("hyperframe storyboard failed: %s", e)
        return None
    text = str(raw or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```(json)?|```$', '', text).strip()
    try:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        data = json.loads(match.group(0)) if match else None
    except (json.JSONDecodeError, ValueError):
        data = None
    if not isinstance(data, dict):
        return None
    frames = []
    for f in data.get('frames') or []:
        if not isinstance(f, dict):
            continue
        heading = str(f.get('heading', '')).strip()
        if not heading:
            continue
        bullets = [str(b)[:120] for b in (f.get('bullets') or [])[:6]
                   if str(b).strip()]
        frames.append({'heading': heading[:80],
                       'bullets': bullets,
                       'caption': str(f.get('caption', ''))[:160]})
    if len(frames) < 2:
        return None
    return {'title': str(data.get('title', 'Untitled'))[:80],
            'frames': frames}


def _slug(text):
    return re.sub(r'[^a-z0-9]+', '-', str(text).lower()).strip('-')[:40] \
        or 'mockup'


# --------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------- #

def render_html(storyboard):
    """Self-playing HTML mockup from a storyboard."""
    frames = storyboard['frames']
    # Escape < and > so storyboard text can't break out of the <script>
    # block (json.dumps doesn't escape them). </> decode back to
    # the real characters when the JSON is parsed at runtime.
    frame_json = json.dumps(frames).replace('<', '\\u003c') \
                                     .replace('>', '\\u003e')
    title = storyboard['title']
    cards = []
    for i, f in enumerate(frames):
        bullets = "\n".join(
            f"<li>{_esc(b)}</li>" for b in f['bullets'])
        caption = (f'<p class="caption">{_esc(f["caption"])}</p>'
                   if f.get('caption') else '')
        cards.append(f"""
      <section class="frame" id="f{i}">
        <div class="frame-number">FRAME {i + 1} / {len(frames)}</div>
        <h2>{_esc(f['heading'])}</h2>
        <ul>{bullets}</ul>
        {caption}
      </section>""")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_esc(title)} — Hyperframe Mockup</title>
<style>
  :root {{ --bg:#0c0f0d; --fg:#e8e4d8; --amber:#ffb000; --dim:#7a7466; }}
  * {{ box-sizing: border-box; margin: 0; }}
  body {{ background: var(--bg); color: var(--fg);
         font-family: "SF Mono", Menlo, monospace;
         display: flex; flex-direction: column; align-items: center;
         min-height: 100vh; padding: 5vh 4vw; }}
  h1 {{ color: var(--amber); font-size: clamp(20px, 3.4vw, 40px);
       letter-spacing: .12em; margin-bottom: 1.2rem; }}
  .deck {{ width: min(860px, 94vw); }}
  .frame {{ display: none; border: 1px solid #2c2a24; padding: 2.2rem;
           background: #101311; }}
  .frame.active {{ display: block; }}
  .frame-number {{ color: var(--dim); font-size: 12px;
                  letter-spacing: .3em; margin-bottom: 1rem; }}
  h2 {{ color: var(--amber); font-size: clamp(18px, 2.4vw, 30px);
      margin-bottom: 1rem; }}
  ul {{ list-style: none; }}
  li {{ padding: .45rem 0 .45rem 1.4rem; position: relative;
       line-height: 1.5; }}
  li::before {{ content: "▸"; position: absolute; left: 0;
               color: var(--amber); }}
  .caption {{ margin-top: 1.2rem; color: var(--dim); font-size: 14px; }}
  .controls {{ margin-top: 1.4rem; display: flex; gap: 1rem;
              align-items: center; color: var(--dim); font-size: 13px; }}
  button {{ background: none; border: 1px solid var(--amber);
           color: var(--amber); font: inherit; padding: .35rem 1rem;
           cursor: pointer; }}
  button:hover {{ background: var(--amber); color: var(--bg); }}
  .progress {{ height: 2px; background: #2c2a24; margin-top: 1rem; }}
  .progress i {{ display: block; height: 100%; background: var(--amber);
                width: 0; transition: width .2s linear; }}
</style>
</head>
<body>
  <h1>{_esc(title)}</h1>
  <div class="deck">
    {''.join(cards)}
    <div class="controls">
      <button onclick="step(-1)">◀ PREV</button>
      <button id="play" onclick="toggle()">❚❚ PAUSE</button>
      <button onclick="step(1)">NEXT ▶</button>
      <span>click or use ← → keys</span>
    </div>
    <div class="progress"><i id="bar"></i></div>
  </div>
<script>
  const FRAMES = {frame_json};
  let idx = 0, timer = null;
  const N = FRAMES.length, DELAY = 4200;
  function show() {{
    document.querySelectorAll('.frame').forEach((el, i) =>
      el.classList.toggle('active', i === idx));
    document.getElementById('bar').style.width =
      ((idx + 1) / N * 100) + '%';
  }}
  function step(d) {{ idx = (idx + d + N) % N; show(); }}
  function toggle() {{
    const b = document.getElementById('play');
    if (timer) {{ clearInterval(timer); timer = null;
                 b.textContent = '▶ PLAY'; }}
    else {{ timer = setInterval(() => step(1), DELAY);
           b.textContent = '❚❚ PAUSE'; }}
  }}
  document.addEventListener('keydown', e => {{
    if (e.key === 'ArrowRight') step(1);
    if (e.key === 'ArrowLeft') step(-1);
  }});
  show(); toggle();
</script>
</body>
</html>"""
    return html


def _esc(text):
    return (str(text).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


# --------------------------------------------------------------------- #
# Video (Pillow + ffmpeg, both optional)
# --------------------------------------------------------------------- #

def render_video(storyboard, out_path):
    """Rasterize frames with Pillow and stitch with ffmpeg.  Returns
    the video path, or None with a reason."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None, "Pillow not installed — HTML mockup only"
    try:
        import shutil
        if not shutil.which('ffmpeg'):
            return None, "ffmpeg not installed — HTML mockup only"
    except Exception:
        return None, "ffmpeg probe failed"

    import subprocess
    import tempfile
    W, H = 1280, 720
    try:
        font_h = ImageFont.truetype(
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf", 44)
        font_b = ImageFont.truetype(
            "/System/Library/Fonts/Supplemental/Arial.ttf", 28)
        font_s = ImageFont.truetype(
            "/System/Library/Fonts/Supplemental/Arial.ttf", 22)
    except Exception:
        font_h = font_b = font_s = ImageFont.load_default()

    tmp = tempfile.mkdtemp(prefix='hyperframe_')
    try:
        for i, frame in enumerate(storyboard['frames']):
            img = Image.new('RGB', (W, H), (12, 15, 13))
            draw = ImageDraw.Draw(img)
            draw.rectangle([0, 0, W, 8], fill=(255, 176, 0))
            draw.text((80, 70), storyboard['title'],
                      fill=(255, 176, 0), font=font_s)
            draw.text((80, 130), frame['heading'],
                      fill=(232, 228, 216), font=font_h)
            y = 230
            for b in frame['bullets']:
                draw.text((110, y), "▸ " + b,
                          fill=(200, 196, 184), font=font_b)
                y += 54
            if frame.get('caption'):
                draw.text((80, H - 90), frame['caption'],
                          fill=(122, 116, 102), font=font_s)
            draw.text((W - 160, H - 60), f"{i + 1}/{len(storyboard['frames'])}",
                      fill=(122, 116, 102), font=font_s)
            img.save(os.path.join(tmp, f"frame_{i:03d}.png"))

        fps = 0.5        # 2s per frame — a readable storyboard video
        proc = subprocess.run(
            ['ffmpeg', '-y', '-loglevel', 'error', '-framerate',
             str(fps), '-i', os.path.join(tmp, 'frame_%03d.png'),
             '-pix_fmt', 'yuv420p', '-r', '30', out_path],
            capture_output=True, timeout=120)
        if proc.returncode != 0:
            return None, f"ffmpeg failed: {proc.stderr.decode()[:200]}"
        return out_path, None
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------- #

def create(brain, instructions):
    """
    Full pipeline: instructions → storyboard → HTML mockup (+ video
    when possible).  Returns a status message with artifact paths.
    """
    if not enabled():
        return "Hyperframe disabled (JARVIS_HYPERFRAME=0)."
    if not str(instructions or '').strip():
        return "Give me instructions to mock up."
    storyboard = storyboard_from_instructions(brain, instructions)
    if storyboard is None:
        return ("I couldn't structure that into a storyboard — try "
                "describing the screens you want.")

    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    out_dir = os.path.join(_OUTPUT_DIR, f"{stamp}_{_slug(storyboard['title'])}")
    os.makedirs(out_dir, exist_ok=True)

    html_path = os.path.join(out_dir, 'mockup.html')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(render_html(storyboard))
    with open(os.path.join(out_dir, 'storyboard.json'),
              'w', encoding='utf-8') as f:
        json.dump(storyboard, f, indent=2)

    video_path = os.path.join(out_dir, 'mockup.mp4')
    result, reason = render_video(storyboard, video_path)
    base = (f"Hyperframe mockup ready: {len(storyboard['frames'])} frames "
            f"— {storyboard['title']}\nHTML: {html_path}")
    if result:
        return f"{base}\nVideo: {video_path}"
    return f"{base}\n(Video skipped: {reason})"


def list_mockups(limit=10):
    if not os.path.isdir(_OUTPUT_DIR):
        return "No hyperframe mockups yet."
    entries = sorted(os.listdir(_OUTPUT_DIR), reverse=True)[:limit]
    lines = ["Hyperframe mockups (newest first):"]
    for e in entries:
        lines.append(f"- {e}")
    return "\n".join(lines)
