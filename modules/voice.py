"""Soundbox voice: Sarvam AI text-to-speech (Bulbul v3) with disk cache.
If Sarvam fails, returns fallback=True so the browser uses its own voice."""
import hashlib
import os
from pathlib import Path

import httpx

CACHE_DIR = Path(__file__).resolve().parent.parent / "tts_cache"
CACHE_DIR.mkdir(exist_ok=True)
SARVAM_URL = "https://api.sarvam.ai/text-to-speech"


def _fallback(reason):
    return {"audio": None, "fallback": True, "reason": reason}


async def speak(text: str, lang: str = "en-IN"):
    cache_file = CACHE_DIR / (hashlib.sha1(f"{lang}|{text}".encode()).hexdigest() + ".b64")
    if cache_file.exists():
        return {"audio": cache_file.read_text(), "format": "wav", "source": "cache"}

    key = os.getenv("SARVAM_API_KEY")
    if not key:
        return _fallback("SARVAM_API_KEY missing in .env")

    payload = {
        "text": text,
        "target_language_code": lang,        # en-IN, hi-IN, mr-IN ...
        "model": "bulbul:v3",
        "speaker": os.getenv("SARVAM_SPEAKER", "shubh"),
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(SARVAM_URL, json=payload, headers={"api-subscription-key": key})
        if r.status_code != 200:
            return _fallback(f"Sarvam error {r.status_code}: {r.text[:200]}")
        audios = r.json().get("audios") or []
        if not audios:
            return _fallback("Sarvam returned no audio")
        cache_file.write_text(audios[0])
        return {"audio": audios[0], "format": "wav", "source": "sarvam"}
    except Exception as e:  # network down at venue etc.
        return _fallback(f"Sarvam unreachable: {e}")
