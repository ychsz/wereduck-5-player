import json
from pathlib import Path

_ROLES_PATH = Path(__file__).resolve().parent.parent / "roles.json"
_ROLES: list[dict] | None = None
_ROLES_BY_ID: dict[str, dict] | None = None

GOOSE_ROLES: list[str] = []
DUCK_ROLES: list[str] = []
NEUTRAL_ROLES: list[str] = []


def _load() -> None:
    global _ROLES, _ROLES_BY_ID
    if _ROLES is not None:
        return
    with open(_ROLES_PATH, encoding="utf-8") as f:
        _ROLES = json.load(f)
    _ROLES_BY_ID = {r["id"]: r for r in _ROLES}
    GOOSE_ROLES.clear()
    DUCK_ROLES.clear()
    NEUTRAL_ROLES.clear()
    for r in _ROLES:
        if r["faction"] == "goose":
            GOOSE_ROLES.append(r["id"])
        elif r["faction"] == "duck":
            DUCK_ROLES.append(r["id"])
        else:
            NEUTRAL_ROLES.append(r["id"])


def all_roles() -> list[dict]:
    _load()
    return _ROLES  # type: ignore[return-value]


def get(role_id: str) -> dict:
    _load()
    return _ROLES_BY_ID[role_id]  # type: ignore[index]


def faction_of(role_id: str) -> str:
    return get(role_id)["faction"]


def name_of(role_id: str) -> str:
    return get(role_id)["name"]


def search(query: str) -> list[dict]:
    _load()
    q = query.strip().lower()
    if not q:
        return list(_ROLES)  # type: ignore[arg-type]
    out = []
    for r in _ROLES:  # type: ignore[union-attr]
        hay = (r["name"] + " " + r.get("search", "") + " " + r["description"]).lower()
        if q in hay:
            out.append(r)
    return out


def assign_5p_roles() -> list[str]:
    import random
    _load()
    g = random.sample(GOOSE_ROLES, 3)
    d = random.sample(DUCK_ROLES, 1)
    n = random.sample(NEUTRAL_ROLES, 1)
    roles = g + d + n
    random.shuffle(roles)
    return roles
