"""Run code in many languages via hosted executors: Wandbox (no auth)
and Judge0 CE (free apiKey). Backs coder.py for verifying generated code.

Degrades gracefully: Wandbox needs no key and supports 35+ languages;
Judge0 (JUDGE0_API_KEY / JUDGE0_BASE_URL) is used when configured and
also supports the majority of Judge0 languages through the ephemeral
'/authorize' flow (its access token doubles as the bearer key).

Language auto-detection is a small map of common extensions/markers;
callers may pass an explicit 'lang' (e.g. 'python3', 'cpp17', 'js').
"""
import os
import requests

_TIMEOUT = 25
WANDBOX_COMPILE = "https://wandbox.org/api/compile.json"
JUDGE0_BASE = os.getenv("JUDGE0_BASE_URL", "").strip() or \
    "https://judge0-ce.p.rapidapi.com"
JUDGE0_KEY = (os.getenv("JUDGE0_API_KEY") or "").strip()

# Wandbox supports 'python', 'python3'(alias), 'cpp', 'cpp17', 'gcc',
# 'java', 'js', 'ruby', 'go', 'rust', 'csharp', 'php' — we just pass
# through; Wandbox reports an error for unknown names.

_EXTMAP = {
    "py": "python", "python": "python", "py3": "python3",
    "c": "c", "cc": "cpp", "cpp": "cpp", "c++": "cpp", "hpp": "cpp",
    "java": "java", "js": "js", "mjs": "js", "ts": "ts",
    "rb": "ruby", "go": "go", "rs": "rust", "cs": "csharp",
    "csx": "csharp", "sh": "bash", "php": "php", "swift": "swift",
}


def _lang_from(code, ext=None):
    if ext:
        return _EXTMAP.get(ext.lstrip(".").lower(), "python")
    low = code[:400].lower()
    for marker, lang in [("#!/usr/bin/env python", "python"),
                         ("#!/usr/bin/env node", "js"),
                         ("#!/bin/bash", "bash"),
                         ("using system;", "csharp"),
                         ("package main", "go"),
                         ("public static void main", "java"),
                         ("fn main()", "rust")]:
        if marker in low:
            return lang
    return "python"


_COMPILERS = None  # lazy cache of [name -> language]


def _wandbox_compilers():
    """Cache the Wandbox compiler list: {exact_name: language}."""
    global _COMPILERS
    if _COMPILERS is None:
        try:
            r = requests.get("https://wandbox.org/api/list.json",
                             timeout=_TIMEOUT)
            _COMPILERS = {c["name"]: (c.get("language") or "").lower()
                          for c in r.json()}
        except Exception:
            _COMPILERS = {}
    return _COMPILERS


def _pick_compiler(lang, code):
    """Resolve a compiler name for ``lang`` from the live Wandbox list."""
    comps = _wandbox_compilers()
    if not comps:
        return None
    l = lang.lower()
    # exact language match first (e.g. 'python' -> any python compiler)
    for name, language in comps.items():
        if language == l:
            return name
    # alias handling
    alias = {"py": "python", "python3": "python", "py3": "python",
             "c++": "cpp", "cc": "cpp", "cs": "csharp", "hpp": "cpp",
             "mjs": "javascript", "js": "javascript", "ts": "typescript",
             "node": "javascript", "#": "csharp"}
    lang2 = alias.get(l, l)
    for name, language in comps.items():
        if language == lang2:
            return name
    # direct-name fallback (callers may pass a real compiler name)
    if l in comps:
        return l
    return None


def wandbox_run(code, lang="python", stdin=""):
    """Run code on Wandbox (no auth). Returns verdict dict."""
    try:
        compiler = _pick_compiler(lang, code)
        if not compiler:
            return {"ok": False, "error": f"wandbox: no compiler for "
                                          f"language '{lang}'"}
        r = requests.post(WANDBOX_COMPILE,
                          json={"compiler": compiler, "code": code,
                                "stdin": stdin},
                          timeout=_TIMEOUT)
        if r.status_code != 200:
            return {"ok": False, "error": f"wandbox http {r.status_code}: "
                                          f"{r.text[:120]}"}
        body = r.json()
        err = body.get("compiler_error") or ""
        msg = body.get("compiler_message") or ""
        out = body.get("program_output") or ""
        if err:
            return {"ok": False, "lang": lang, "output": err.strip(),
                    "stderr": msg.strip()}
        return {"ok": True, "lang": lang, "output": out.strip(),
                "stderr": msg.strip()}
    except Exception as e:
        return {"ok": False, "error": f"wandbox unavailable: {e}"}


def judge0_run(code, lang="python", stdin=""):
    """Run code on Judge0 CE. Requires JUDGE0_API_KEY."""
    if not JUDGE0_KEY:
        return {"ok": False, "error": "no JUDGE0_API_KEY configured"}
    if JUDGE0_BASE.endswith("rapidapi.com"):
        headers = {"x-rapidapi-key": JUDGE0_KEY,
                   "x-rapidapi-host": "judge0-ce.p.rapidapi.com"}
    else:
        headers = {"X-Auth-Token": JUDGE0_KEY}
    langs = {
        "python": 71, "python3": 71, "cpp": 54, "cpp17": 54, "js": 63,
        "java": 62, "go": 60, "rust": 73, "ruby": 72, "c": 50,
        "csharp": 51, "bash": 46, "ts": 74,
    }
    lid = langs.get(lang, 71)
    try:
        # authorize -> ephemeral token (doubles as the API key)
        auth = requests.post(JUDGE0_BASE + "/authorize",
                             headers=headers, timeout=_TIMEOUT).json()
        token = auth.get("token") or JUDGE0_KEY
        ac_headers = dict(headers)
        ac_headers["X-Auth-Token"] = token
        sub = requests.post(
            JUDGE0_BASE + "/submissions", json={
                "source_code": code, "language_id": lid, "stdin": stdin},
            headers=ac_headers, timeout=_TIMEOUT).json()
        sid = sub.get("token")
        if not sid:
            return {"ok": False, "error": f"judge0: {sub.get('error') or sub}"}
        # poll
        for _ in range(12):
            r = requests.get(JUDGE0_BASE + f"/submissions/{sid}",
                             headers=ac_headers, timeout=_TIMEOUT).json()
            status = r.get("status", {}).get("id")
            if status in (1, 2):  # in queue / processing
                import time
                time.sleep(1)
                continue
            stderr = r.get("stderr") or ""
            out = r.get("stdout") or ""
            comp = r.get("compile_output") or ""
            if status in (11, 12, 13, 14):
                return {"ok": False, "lang": lang,
                        "output": (comp or out or "").strip(), "stderr": stderr.strip()}
            return {"ok": status == 3, "lang": lang,
                    "output": out.strip(), "stderr": stderr.strip()}
        return {"ok": False, "error": "judge0: timed out"}
    except Exception as e:
        return {"ok": False, "error": f"judge0 unavailable: {e}"}


def run(code, lang=None, ext=None, stdin=""):
    """Run code with the best available backend. Returns dict."""
    if not lang:
        lang = _lang_from(code, ext)
    if JUDGE0_KEY:
        res = judge0_run(code, lang, stdin)
        if not res.get("error"):
            return res
    res = wandbox_run(code, lang, stdin)
    return res


def run_to_string(code, lang=None, ext=None, stdin=""):
    """Convenience: run and return a human string, never raising."""
    try:
        res = run(code, lang, ext, stdin)
    except Exception as e:
        return f"run-code error: {e}"
    if res.get("error"):
        return f"run-code: {res['error']}"
    if not res.get("ok"):
        return (f"[{res.get('lang')}] exit nonzero —\n"
                f"stdout: {res.get('output') or '(none)'}\n"
                f"stderr: {res.get('stderr') or '(none)'}")
    return (f"[{res.get('lang')}] OK —\n{res.get('output') or '(no output)'}"
            + (f"\nstderr: {res.get('stderr')}" if res.get("stderr") else ""))


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "print('hello from python')"
    lang = sys.argv[2] if len(sys.argv) > 2 else None
    print(run_to_string(code, lang))
