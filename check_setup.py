"""Run: python check_setup.py   -> checks files, packages, .env keys, Sarvam voice and text."""
import importlib
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
ok = True


def report(passed, label, hint=""):
    global ok
    ok = ok and passed
    print(("  [OK]   " if passed else "  [FAIL] ") + label + ("" if passed else f"\n         -> {hint}"))


print("\n1. Python")
in_venv = sys.prefix != sys.base_prefix
report(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}", "Install Python 3.11 or newer")
report(in_venv, "Virtual environment active", r"Run: venv\Scripts\activate  (you should see (venv) in the terminal)")

print("\n2. Project files")
for f in ["main.py", "db.py", "modules/__init__.py", "modules/voice.py", "modules/llm.py", "static", ".env"]:
    hint = "Copy .env.example to .env and add your Sarvam key" if f == ".env" else "Re-extract the zip; keep the folder structure"
    report((BASE / f).exists(), f, hint)

print("\n3. Packages")
for pkg in ["fastapi", "uvicorn", "pandas", "numpy", "faker", "dotenv", "httpx", "qrcode", "PIL"]:
    try:
        importlib.import_module(pkg)
        report(True, pkg)
    except ImportError:
        report(False, pkg, "Run: pip install -r requirements.txt")

print("\n4. Sarvam key")
try:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
except ImportError:
    pass
key = os.getenv("SARVAM_API_KEY", "")
report(bool(key) and "your-sarvam-key" not in key, "SARVAM_API_KEY set in .env", "Put your real key in .env")

if key and "your-sarvam-key" not in key:
    import httpx
    print("\n5. Sarvam voice (Bulbul v3)")
    try:
        r = httpx.post("https://api.sarvam.ai/text-to-speech", timeout=30,
                       headers={"api-subscription-key": key},
                       json={"text": "Munshi AI is ready.", "target_language_code": "en-IN",
                             "model": "bulbul:v3", "speaker": os.getenv("SARVAM_SPEAKER", "shubh")})
        report(r.status_code == 200 and r.json().get("audios"), "Text-to-speech works", f"{r.status_code}: {r.text[:300]}")
    except Exception as e:
        report(False, "Text-to-speech works", str(e))

    print("\n6. Sarvam text (chat)")
    try:
        r = httpx.post("https://api.sarvam.ai/v1/chat/completions", timeout=60,
                       headers={"api-subscription-key": key},
                       json={"model": os.getenv("SARVAM_CHAT_MODEL", "sarvam-105b"), "max_tokens": 1500,
                             "messages": [{"role": "user", "content": "Reply with exactly: Munshi AI ready"}]})
        passed = r.status_code == 200
        report(passed, "Chat works" + (f" -> {r.json()['choices'][0]['message'].get('content', '')[-60:].strip()!r}" if passed else ""),
               f"{r.status_code}: {r.text[:300]}")
    except Exception as e:
        report(False, "Chat works", str(e))

print("\n" + ("ALL CHECKS PASSED. Start the server: uvicorn main:app --reload" if ok else "Fix the [FAIL] items above, then run this again.") + "\n")
