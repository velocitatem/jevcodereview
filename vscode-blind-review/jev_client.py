"""Minimal Jev (System One) client for code-chunk review scoring.

One POST per chunk: file context + chunk in, one typed 1-5 judgment out.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

JEV_URL = os.environ.get("JEV_URL", "https://api.typesafe.ai/v1/systemone")
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-latest")

# one shared session with a connection pool big enough for high
# parallelism (requests' default pool_maxsize is 10, which throttles
# any more than 10 concurrent threads)
_SESSION = requests.Session()
_ADAPTER = HTTPAdapter(pool_connections=32, pool_maxsize=64, max_retries=0)
_SESSION.mount("https://", _ADAPTER)
_SESSION.mount("http://", _ADAPTER)


def _key() -> str:
    k = os.environ.get("JEV_KEY")
    if k:
        return k
    env = Path(__file__).resolve().parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("JEV_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("JEV_KEY not found (env or .env)")


# The one bounded question of the MVP (IDEA.md, "the Jev decision"):
#   s_i = P(reviewer should inspect C_i | C_i, F, T)
# expressed as an ordered 1-5 score.
QUESTIONS = {
    "review_value": {
        "type": "score",
        "instructions": (
            "How valuable is it for a human reviewer to inspect this "
            "region in order to understand the behavior and correctness "
            "of this file?"
        ),
        "criteria": [
            "boilerplate / almost no review value",
            "low",
            "somewhat useful",
            "important",
            "critical",
        ],
    },
}


def query(state: dict, *, timeout: int = 90) -> dict:
    """State + the review-value question -> {'score': 1..5, ...}."""
    body = {"state": state, "model": JEV_MODEL, "questions": QUESTIONS}
    last_err = None
    for attempt in range(4):
        try:
            r = _SESSION.post(
                JEV_URL,
                headers={"Authorization": f"Bearer {_key()}",
                         "Content-Type": "application/json"},
                json=body, timeout=timeout,
            )
            if r.status_code == 429 or r.status_code >= 500:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                time.sleep(2 ** attempt * 1.5)
                continue
            r.raise_for_status()
            data = r.json()
            a = data["answers"]["review_value"]
            return {"score": a["score"],
                    "confidence": float(a.get("confidence", 0.0)),
                    "model": data.get("model", JEV_MODEL)}
        except requests.Timeout:
            last_err = "timeout"
        except requests.HTTPError as e:
            last_err = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            if e.response.status_code < 500 and e.response.status_code != 429:
                break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(2 ** attempt * 1.5)
    raise RuntimeError(f"Jev query failed: {last_err}")