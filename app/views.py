"""Project GameState into per-player views, hiding private info.

This is the information-isolation layer. Every outbound 'state' goes through
`player_view(state, seat)` which strips anything the player must not see.
"""
from __future__ import annotations

from typing import Any
from . import roles as R
from .state import GameState, Player, Phase


NIGHT_SECONDS = 25
SPEECH_SECONDS = 60
VOTE_SECONDS = 30


def _player_public(p: Player, viewer_seat: int, my_marks: dict,
                    night_alive: bool | None = None) -> dict:
    """What everyone can see about a player.
    If ``night_alive`` is not None we're in night phase and the caller passes
    the snapshot value so deaths during the night are hidden from closed-eye
    players."""
    mark = my_marks.get(str(p.seat)) if my_marks else None
    if night_alive is not None:
        alive = night_alive
    else:
        alive = p.alive and p.in_belly_of is None
    d: dict[str, Any] = {
        "seat": p.seat,
        "name": p.name,
        "alive": alive,
        "online": p.online,
        "ready": p.ready,
        "mark": mark,
    }
    d["status"] = "alive" if alive else "dead"
    return d


def _seats_view(st: GameState, viewer_seat: int, my_marks: dict) -> list[dict]:
    is_night = st.phase == Phase.NIGHT and bool(st.night_snapshot)
    out = []
    for seat in range(1, 6):
        p = st.players.get(seat)
        if p is None or not p.name:
            out.append({"seat": seat, "name": "", "status": "empty", "online": False, "ready": False})
        else:
            na = st.night_snapshot.get(seat) if is_night else None
            out.append(_player_public(p, viewer_seat, my_marks, night_alive=na))
    return out


def player_view(st: GameState, seat: int, my_marks: dict | None = None) -> dict:
    """Full personalized view for a seated player. Hides others' roles/actions."""
    p = st.players.get(seat)
    my_marks = my_marks or {}
    view: dict[str, Any] = {
        "phase": st.phase.value,
        "day_count": st.day_count,
        "my_seat": seat,
        "seats": _seats_view(st, seat, my_marks),
        "chat": [_chat_dict(c) for c in st.chat[-100:]],
    }
    # my role info
    if p and p.role:
        rinfo = R.get(p.role)
        view["my_role"] = {
            "id": rinfo["id"],
            "name": rinfo["name"],
            "faction": rinfo["faction"],
            "faction_name": rinfo["faction_name"],
            "description": rinfo["description"],
        }
        if st.phase == Phase.NIGHT and st.night_snapshot:
            view["my_alive"] = st.night_snapshot.get(seat, True)
        else:
            view["my_alive"] = p.alive and p.in_belly_of is None
    # night-specific
    if st.phase == Phase.NIGHT:
        view["night"] = _night_view(st, p)
    elif st.phase == Phase.DAY_SPEECH:
        view["day_speech"] = _day_speech_view(st, p)
    elif st.phase == Phase.DAY_VOTE:
        view["day_vote"] = _day_vote_view(st, p)
    elif st.phase == Phase.GAME_OVER:
        view["game_over"] = _game_over_view(st)
    # spy intel (shown during day phases)
    if p and p.spy_intel and st.phase in (Phase.DAY_SPEECH, Phase.DAY_VOTE, Phase.GAME_OVER):
        view["spy_intel"] = {str(k): R.name_of(v) for k, v in p.spy_intel.items()}
    # private notes for the viewer's role
    if p:
        view["private"] = _private_notes(st, p)
    return view


def _night_view(st: GameState, p: Player | None) -> dict:
    d: dict[str, Any] = {"is_my_turn": False, "prompt": None}
    if p is None:
        return d
    if st.night_current_seat == p.seat and p.alive and p.in_belly_of is None:
        d["is_my_turn"] = True
        d["prompt"] = st.night_prompt
    return d


def _day_speech_view(st: GameState, p: Player | None) -> dict:
    d: dict[str, Any] = {
        "speaker_seat": None,
        "is_my_turn": False,
        "deadline": st.speech_deadline,
        "can_assassinate": False,
        "can_raven_guess": False,
        "raven_target": None,
        "can_magpie_guess": False,
    }
    if st.speech_order and st.speech_index < len(st.speech_order):
        sp = st.speech_order[st.speech_index]
        d["speaker_seat"] = sp
        d["is_my_turn"] = (p is not None and p.seat == sp and p.alive and p.in_belly_of is None)
    if p and p.alive and p.in_belly_of is None:
        if p.role == "assassin" and not p.assassin_used_today:
            d["can_assassinate"] = True
        if p.role == "raven" and not p.raven_used_today and p.raven_target_role:
            d["can_raven_guess"] = True
            d["raven_target"] = R.name_of(p.raven_target_role)
        if p.role == "magpie":
            d["can_magpie_guess"] = True
            d["magpie_correct"] = [st.players[s].name for s in p.magpie_correct if s in st.players]
    return d


def _day_vote_view(st: GameState, p: Player | None) -> dict:
    d: dict[str, Any] = {
        "deadline": st.vote_deadline,
        "my_vote": None,
        "targets": [],
        "can_vote": False,
    }
    if p and p.alive and p.in_belly_of is None:
        d["can_vote"] = True
        # build targets
        targets = []
        for seat in range(1, 6):
            tp = st.players.get(seat)
            if tp and tp.name and tp.alive and tp.in_belly_of is None and seat != p.seat:
                targets.append({"seat": seat, "name": tp.name})
        # can vote self?
        if p.role in ("dodo", "lobbyist"):
            targets.append({"seat": p.seat, "name": p.name + "（自己）"})
        targets.append({"seat": -1, "name": "弃票"})
        # pelican/falcon must abstain
        if p.role in ("pelican", "falcon"):
            targets = [{"seat": -1, "name": "弃票"}]
        d["targets"] = targets
        d["my_vote"] = st.votes.get(p.seat)
    return d


def _game_over_view(st: GameState) -> dict:
    seats = []
    for seat in range(1, 6):
        p = st.players.get(seat)
        if p and p.name:
            seats.append({
                "seat": seat,
                "name": p.name,
                "role": R.name_of(p.role) if p.role else "?",
                "faction": R.faction_of(p.role) if p.role else "?",
                "won": seat in st.winners,
            })
    return {"seats": seats, "reason": st.win_reason, "winners": st.winners}


def _private_notes(st: GameState, p: Player) -> dict:
    notes: dict[str, Any] = {}
    # pelican knows who's in belly
    if p.role == "pelican" and p.pelican_belly:
        notes["belly"] = [st.players[s].name for s in p.pelican_belly if s in st.players]
    # in belly: you know you're swallowed and by whom
    if p.in_belly_of is not None:
        notes["in_belly_of"] = st.players[p.in_belly_of].name
    # pigeon knows infected
    if p.role == "pigeon" and p.pigeon_infected:
        notes["infected"] = [st.players[s].name for s in p.pigeon_infected if s in st.players]
    # cupid lovers
    if st.lover_pair:
        a, b = st.lover_pair
        if p.seat in (a, b):
            other_seat = b if p.seat == a else a
            op = st.players.get(other_seat)
            if op:
                notes["lover"] = op.name
    # vulture count
    if p.role == "vulture":
        notes["vulture_count"] = p.vulture_count
    # magpie correct count
    if p.role == "magpie":
        notes["magpie_correct_count"] = len(p.magpie_correct)
    # priest double target
    if p.role == "priest" and p.priest_double_target is not None:
        notes["priest_double"] = st.players[p.priest_double_target].name
    # lobbyist charges
    if p.role == "lobbyist":
        notes["lobbyist_charges"] = p.lobbyist_charges
    # revealed info (night results) - stored on player as messages list
    if hasattr(p, "intel_msgs") and p.intel_msgs:
        notes["intel"] = p.intel_msgs
    return notes


def _chat_dict(c) -> dict:
    return {"seat": c.seat, "name": c.name, "text": c.text, "system": c.system}
