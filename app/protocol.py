"""WebSocket message protocol + outbound helpers."""
from __future__ import annotations

import json
from typing import Any


def msg(type: str, **data: Any) -> str:
    return json.dumps({"type": type, **data}, ensure_ascii=False)


def parse(raw: str) -> dict:
    try:
        d = json.loads(raw)
    except Exception:
        return {"type": "__invalid__", "raw": raw}
    return d


# ---- outbound message builders ----

def state(payload: dict) -> str:
    return msg("state", **payload)


def error(text: str) -> str:
    return msg("error", text=text)


def chat(seat, name: str, text: str, system: bool = False) -> str:
    return msg("chat", seat=seat, name=name, text=text, system=system)


def night_turn(prompt: dict) -> str:
    return msg("night_turn", prompt=prompt)


def night_idle() -> str:
    return msg("night_idle")
