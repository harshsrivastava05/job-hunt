"""Yes/no probabilities from a decision model: TypeSafe Jev, or OpenAI's Decisions API.

decide(kind, state, questions) -> {question name: probability 0..1, or None if the model refused}
state is an ordered dict; its first value should be the part that repeats across calls (rubric + resume)
so OpenAI can cache it. Provider: DECISION_PROVIDER=jev|openai, else Jev if JEV_API_KEY is set, else OpenAI.
Keys come from the environment, or worker/.env when running locally.
"""
import os
import time
from pathlib import Path

import httpx

from jobhunt.db import connect

_env = Path(__file__).parent.parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _k, _sep, _v = _line.partition("=")
        if _sep and not _k.strip().startswith("#"):
            os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

PROVIDER = os.environ.get("DECISION_PROVIDER") or (
    "jev" if os.environ.get("JEV_API_KEY") else "openai" if os.environ.get("OPENAI_API_KEY") else None
)


# USD per million tokens: (input, cached input, output). Jev bills input only. OpenAI's gpt-6-luna rates are from
# third-party price listings, 2026-10-24; check openai.com/api/pricing if they matter.
PRICES = {"jev": (0.042, 0.042, 0.0), "openai": (0.10, 0.01, 0.50)}


def enabled():
    return PROVIDER is not None


def decide(kind, state, questions):
    if PROVIDER == "jev":
        body = {"model": "jev-latest", "state": state, "questions": {k: {"type": "noul", "instructions": q} for k, q in questions.items()}}
        d = _post("https://api.typesafe.ai/v1/systemone", os.environ["JEV_API_KEY"], body)
        answers = {k: a.get("noul") for k, a in (d.get("answers") or {}).items()}
        cached = None
    else:
        first, *rest = state.items()
        body = {
            "model": "gpt-6-luna",
            "input": [
                {"role": "developer", "content": f"{first[0]}:\n{first[1]}"},  # stable prefix -> cache hits
                {"role": "user", "content": "\n\n".join(f"{k}:\n{v}" for k, v in rest)},
            ],
            "questions": [{"type": "predicate", "name": k, "instructions": q} for k, q in questions.items()],
        }
        d = _post("https://api.openai.com/v1/decisions", os.environ["OPENAI_API_KEY"], body)
        answers = {a["name"]: a.get("probability") for a in d.get("answers") or []}  # a refusal has no probability
        cached = ((d.get("usage") or {}).get("input_tokens_details") or {}).get("cached_tokens")
    usage = d.get("usage") or {}
    _log(kind, d.get("model") or PROVIDER, len(questions), usage.get("input_tokens") or 0, usage.get("output_tokens") or 0, cached or 0)
    return {k: answers.get(k) for k in questions}


def _post(url, key, body):
    for attempt in range(4):
        r = httpx.post(url, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=60)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(2 * 2**attempt)
            continue
        r.raise_for_status()
        return r.json()


def _log(kind, model, questions, in_tokens, out_tokens, cached):
    p_in, p_cached, p_out = PRICES[PROVIDER]
    usd = ((in_tokens - cached) * p_in + cached * p_cached + out_tokens * p_out) / 1e6
    conn = connect()
    with conn:
        conn.execute(
            "INSERT INTO llm_calls (kind, provider, model, questions, in_tokens, out_tokens, cached_tokens, usd)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, PROVIDER, model, questions, in_tokens, out_tokens, cached, usd),
        )
    conn.close()
