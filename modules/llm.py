"""Text generation via Sarvam AI chat, cached in SQLite.
Used only for: freeze letter, win-back offers.
If Sarvam fails, returns the template text you pass as `fallback`.

sarvam-105b is a reasoning model: its hidden "thinking" uses tokens before the answer,
so we ask for low reasoning effort, give a generous budget, and NEVER accept an answer
that was cut off by the token limit (finish_reason == "length")."""
import hashlib
import os
import re

import httpx

import db

SARVAM_CHAT_URL = "https://api.sarvam.ai/v1/chat/completions"


def _clean(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


async def _ask(client, api_key, payload):
    r = await client.post(SARVAM_CHAT_URL, json=payload, headers={"api-subscription-key": api_key})
    if r.status_code == 400 and "reasoning" in r.text.lower() and "reasoning_effort" in payload:
        payload = {k: v for k, v in payload.items() if k != "reasoning_effort"}   # model doesn't accept it
        r = await client.post(SARVAM_CHAT_URL, json=payload, headers={"api-subscription-key": api_key})
    return r


async def generate(prompt: str, system: str = "You are a helpful assistant for Indian small shop owners. "
                                              "Answer directly and completely.",
                   fallback: str = "", max_tokens: int = 4000, validate=None):
    """validate: optional function(text) -> bool; text that fails is never shown or cached."""
    ok = validate or (lambda t: True)
    key_hash = hashlib.sha1(f"{system}|{prompt}".encode()).hexdigest()
    cached = db.query_one("SELECT response FROM llm_cache WHERE key=?", (key_hash,))
    if cached:
        if ok(cached["response"]):
            return {"text": cached["response"], "source": "cache"}
        db.execute("DELETE FROM llm_cache WHERE key=?", (key_hash,))     # drop a bad (e.g. cut-off) copy

    api_key = os.getenv("SARVAM_API_KEY")
    if not api_key:
        return {"text": fallback, "source": "template", "reason": "SARVAM_API_KEY missing"}

    reason = ""
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            for budget in (max_tokens, max_tokens * 2):                  # one retry with double budget
                payload = {
                    "model": os.getenv("SARVAM_CHAT_MODEL", "sarvam-105b"),
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                    "temperature": 0.3, "max_tokens": budget, "reasoning_effort": "low",
                }
                r = await _ask(client, api_key, payload)
                if r.status_code != 200:
                    return {"text": fallback, "source": "template", "reason": f"Sarvam error {r.status_code}: {r.text[:200]}"}
                choice = r.json()["choices"][0]
                text = _clean(choice["message"].get("content"))
                finish = choice.get("finish_reason")
                if text and finish != "length" and ok(text):
                    db.execute("INSERT OR REPLACE INTO llm_cache(key, response) VALUES (?,?)", (key_hash, text))
                    return {"text": text, "source": "sarvam"}
                reason = f"incomplete answer (finish_reason={finish}, {len(text)} chars)"
    except Exception as e:
        reason = f"Sarvam unreachable: {e}"
    return {"text": fallback, "source": "template", "reason": reason}
