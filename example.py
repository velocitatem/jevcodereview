"""Candidate-action generation + counterfactual recomputation.

Generation: cheap LLM (Groq) turns raw state (emails, tasks, deadlines)
into 5-8 *concrete* candidate actions.  Deterministic seed candidates
(always include manual tasks) so the engine is never empty.

Counterfactuals: re-run the ranking under perturbed states.
"""
from __future__ import annotations

import json
import os

import requests

from . import context as cx

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", "openai/gpt-oss-120b")


def _groq_key() -> str:
    k = os.environ.get("GROQ_API_KEY")
    if k:
        return k
    for line in (cx.ROOT / ".env").read_text().splitlines():
        if line.startswith("GROQ_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("GROQ_API_KEY not found")


def _chat(prompt: str, system: str = "", timeout: int = 60) -> str:
    r = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {_groq_key()}",
                 "Content-Type": "application/json"},
        json={
            "model": EXTRACT_MODEL,
            "temperature": 0.4,
            "messages": (
                ([{"role": "system", "content": system}] if system else [])
                + [{"role": "user", "content": prompt}]),
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


PROMPT = """You are the proposal stage of a personal decision engine.

Given the person's state below, propose 6 candidate actions for the NEXT
work block.  Concrete and small: each is 25-90 minutes.  Draw from the
manual tasks, overdue deadlines, unread-email obligations and goals.
Include ONE low-effort admin/action (email triage type), and at least one
aligned with a long-term goal.

{state}

Answer ONLY with JSON:
{{"candidates":[{{"id":"a1","action":"<imperative, <10 words>","duration_min":40,"source":"task|email|goal|deadline"}}]}}
"""


def generate_candidates(state: dict) -> list[dict]:
    """LLM-proposed + task-derived action list."""
    # 1. deterministic floor: manual tasks always offered
    seeded = []
    for i, t in enumerate(state.get("tasks", [])):
        if isinstance(t, str):
            t = {"title": t}
        if t.get("done"):
            continue
        seeded.append({
            "id": f"t{i}",
            "action": t.get("title", str(t)),
            "duration_min": t.get("duration_min", 40),
            "source": "task",
        })

    # 2. LLM proposals
    try:
        txt = _chat(PROMPT.format(state=json.dumps(state, indent=1)[:6000]))
        m = txt[txt.index("{"): txt.rindex("}") + 1]
        data = json.loads(m)
        proposals = data.get("candidates", [])
    except Exception as e:
        print(f"[gen] LLM proposal skipped: {e}")
        proposals = []

    have = {p["action"].lower().strip() for p in seeded}
    words = lambda s: set(s.lower().split())
    for p in proposals:
        a = (p.get("action") or "").lower().strip()
        if not a:
            continue
        # fuzzy dedupe: skip if it shares >60% content words with an existing task
        dup = any(len(words(a) & words(h)) / max(1, len(words(a))) > 0.6
                  for h in have)
        if not dup:
            p.setdefault("source", "proposed")
            p["action"] = a
            have.add(a)
            seeded.append(p)
    return seeded[:10]


CFBOXES = {
    "deadline_tomorrow": "Pretend the person just learnt that the most "
                         "important upcoming deadline is TOMORROW at 17:00.",
    "only_25_min": "The person now has only 25 minutes free, not the full "
                   "block they thought.",
    "low_energy": "Energy has dropped: they are tired and slightly "
                  "overwhelmed.  Deep difficult work is unrealistic this "
                  "hour, but a small concrete step is still possible.",
    "no_career": "Ignore career and application goals entirely for this "
                 "decision — judge purely on learning, wellbeing, admin "
                 "and personal life.",
}


def counterfactuals(state: dict, candidates: list[dict],
                    boxes: list[str], base_weights: dict) -> dict:
    """Re-rank the same candidates under perturbed states.

    Returns {box_name: ranked_candidates} — note the FULL re-computation,
    not an LLM explanation.
    """
    from . import jev_client
    out = {}
    for box in boxes:
        tweak = CFBOXES.get(box, box)
        st = dict(state)
        st["counterfactual"] = tweak
        out[box] = jev_client.rank(st, candidates, weights=base_weights,
                                   workers=4)
    return out
