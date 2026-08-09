"""Core game engine: night sequential wake-up FSM, day phases, win checks.

Night is a STRICT sequential state machine following rules.txt L32:
  跟踪者→鸽子→工程师→鸭阵营(钻管道)→警长→正义使者→士兵→说客
  →鸭阵营(击杀)→鹈鹕/猎鹰→侦探→殡仪员→白天

Each step wakes exactly one living player (if that role exists), computes
legal options from the CURRENT (already-mutated by prior steps) state, waits
for their action, then immediately applies it — so later steps see the
effects of earlier ones (deaths, pipe, adjacency reroll, etc).
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Callable, Optional

from . import roles as R
from .state import GameState, Player, Phase

import os

NIGHT_TIMEOUT = int(os.environ.get("WEREDUCK_NIGHT_TIMEOUT", 25))
SPEECH_TIMEOUT = int(os.environ.get("WEREDUCK_SPEECH_TIMEOUT", 60))
VOTE_TIMEOUT = int(os.environ.get("WEREDUCK_VOTE_TIMEOUT", 30))


# ---- adjacency ----

def _chain_for(st: GameState, actor_seat: int | None = None) -> list[int]:
    """Living ring. Excludes dead / in-belly / piped players.
    If actor_seat is given, that seat is ALWAYS included (even if piped),
    so a piped actor can still determine who their neighbours are."""
    ring = []
    for s in range(1, 6):
        p = st.players[s]
        if not p.name or not p.alive or p.in_belly_of is not None:
            continue
        if s == actor_seat:
            ring.append(s)
        elif not p.piped:
            ring.append(s)
    return ring


def adjacent_seats(st: GameState, seat: int) -> list[int]:
    """Immediate left + right neighbours of ``seat`` in the living ring.
    The queried seat is always kept in the ring (even when piped)."""
    ring = _chain_for(st, actor_seat=seat)
    if seat not in ring:
        return []
    idx = ring.index(seat)
    n = len(ring)
    if n <= 1:
        return []
    left = ring[(idx - 1) % n]
    right = ring[(idx + 1) % n]
    out = []
    if left != seat:
        out.append(left)
    if right != seat:
        out.append(right)
    return out


def is_adjacent(st: GameState, a: int, b: int) -> bool:
    return b in adjacent_seats(st, a)


# ---- night step definitions ----

NIGHT_SEQUENCE_FIELDS = [
    # (step_key, role_ids_that_act_or_wake, description)
    ("tracker", ["tracker"]),
    ("pigeon", ["pigeon"]),
    ("engineer", ["engineer"]),
    ("duck_pipe", ["duck"]),          # pipe-capable ducks (duck/assassin/spy)
    ("sheriff", ["sheriff"]),
    ("vigilante", ["vigilante"]),
    ("soldier", ["soldier"]),
    ("lobbyist", ["lobbyist"]),
    ("duck_kill", ["duck"]),          # all duck faction kill
    ("pelican_falcon", ["pelican", "falcon"]),
    ("detective", ["detective"]),
    ("mortician", ["mortician"]),
]


def _find_role_player(st: GameState, role_ids: list[str], only_pipe_capable: bool = False) -> Optional[Player]:
    for p in st.players.values():
        if p.name and p.alive and p.in_belly_of is None and p.role in role_ids:
            if only_pipe_capable and p.role not in ("duck", "assassin", "spy"):
                continue
            return p
    return None


def _find_duck_killer(st: GameState) -> Optional[Player]:
    """The single duck-faction player who kills."""
    for p in st.players.values():
        if p.name and p.alive and p.in_belly_of is None and p.is_duck:
            return p
    return None


def night_targets_alive(st: GameState, exclude_seat: int) -> list[dict]:
    """All living, non-piped, non-belly players other than exclude_seat."""
    out = []
    for seat in range(1, 6):
        p = st.players[seat]
        if seat != exclude_seat and p.name and p.alive and p.in_belly_of is None and not p.piped:
            out.append({"seat": seat, "name": p.name})
    return out


def _status_board(st: GameState, for_mortician: bool = False) -> list[dict]:
    """Perceived status of every seated player from the acting player's view.

    For non-mortician: killed / piped / swallowed ALL show as "死亡"
    (indistinguishable, per user requirement).  Already-dead (prior
    nights/days) shows as "已出局".  Alive shows as "存活".

    For mortician: piped & swallowed & alive show as "存活"; only truly-
    killed-this-night shows as "今夜出局"; already-dead shows as "已出局".
    """
    board = []
    for seat in range(1, 6):
        p = st.players.get(seat)
        if p is None or not p.name:
            continue
        if for_mortician:
            if not p.alive and p.died_night and p.in_belly_of is None:
                status = "今夜出局"
            elif not p.alive:
                status = "已出局"
            else:
                status = "存活"
        else:
            if not p.alive and not p.died_night:
                status = "已出局"
            elif not p.alive:
                status = "死亡"
            elif p.in_belly_of is not None:
                status = "死亡"
            elif p.piped:
                status = "死亡"
            else:
                status = "存活"
        board.append({"seat": seat, "name": p.name, "status": status})
    return board


def build_night_steps(st: GameState) -> list[tuple[str, str]]:
    steps = []
    for key, role_ids in NIGHT_SEQUENCE_FIELDS:
        steps.append((key, role_ids[0]))
    return steps


# ---- night prompt builders ----

def _adjacent_kill_targets(st: GameState, actor_seat: int, must_be_adjacent: bool = True,
                           require_non_adjacent: bool = False) -> list[dict]:
    """Build target list for a kill action."""
    out = []
    for seat in range(1, 6):
        if seat == actor_seat:
            continue
        p = st.players[seat]
        if not (p.name and p.alive and p.in_belly_of is None and not p.piped):
            continue
        adj = is_adjacent(st, actor_seat, seat)
        if must_be_adjacent and not adj:
            continue
        if require_non_adjacent and adj:
            # sniper: can only kill non-adjacent, unless ALL living non-self are adjacent
            continue
        out.append({"seat": seat, "name": p.name})
    return out


def _build_prompt(st: GameState, step_key: str) -> Optional[dict]:
    """Build the action prompt for the current night step's player.
    Returns None if this step should be skipped (no eligible player.
    """
    if step_key == "tracker":
        p = _find_role_player(st, ["tracker"])
        if not p:
            return None
        targets = night_targets_alive(st, p.seat)
        st.night_current_seat = p.seat
        return {"step": step_key, "role": "跟踪者", "seat": p.seat,
                "text": "选择一名非己存活玩家跟踪。",
                "targets": targets, "can_skip": True}

    if step_key == "pigeon":
        p = _find_role_player(st, ["pigeon"])
        if not p:
            return None
        st.night_current_seat = p.seat
        # pigeon can either pipe OR infect (not both)
        infect_targets = night_targets_alive(st, p.seat)
        return {"step": step_key, "role": "鸽子", "seat": p.seat,
                "text": "选择「钻进管道」或永久感染一名存活的非己玩家。",
                "infect_targets": infect_targets, "can_pipe": True, "can_skip": False}

    if step_key == "engineer":
        p = _find_role_player(st, ["engineer"])
        if not p:
            return None
        st.night_current_seat = p.seat
        return {"step": step_key, "role": "工程师", "seat": p.seat,
                "text": "是否钻进管道？", "can_pipe": True, "can_skip": False}

    if step_key == "duck_pipe":
        p = _find_role_player(st, ["duck", "assassin", "spy"])
        if not p:
            return None
        st.night_current_seat = p.seat
        return {"step": step_key, "role": R.name_of(p.role), "seat": p.seat,
                "text": "是否钻进管道？", "can_pipe": True, "can_skip": False}

    if step_key == "sheriff":
        p = _find_role_player(st, ["sheriff"])
        if not p:
            return None
        targets = _adjacent_kill_targets(st, p.seat, must_be_adjacent=True)
        st.night_current_seat = p.seat
        return {"step": step_key, "role": "警长", "seat": p.seat,
                "text": "杀死一名相邻的存活玩家，或放弃。", "targets": targets, "can_skip": True}

    if step_key == "vigilante":
        p = _find_role_player(st, ["vigilante"])
        if not p or p.vigilante_used:
            return None
        targets = _adjacent_kill_targets(st, p.seat, must_be_adjacent=True)
        st.night_current_seat = p.seat
        return {"step": step_key, "role": "正义使者", "seat": p.seat,
                "text": "开你的一刀：杀死一名相邻的存活玩家，或放弃。", "targets": targets, "can_skip": True}

    if step_key == "soldier":
        p = _find_role_player(st, ["soldier"])
        if not p:
            return None
        if st.day_count < 2:
            return None  # soldier acts from 2nd night
        targets = _adjacent_kill_targets(st, p.seat, must_be_adjacent=True)
        st.night_current_seat = p.seat
        return {"step": step_key, "role": "士兵", "seat": p.seat,
                "text": "杀死一名相邻的存活玩家，或放弃。", "targets": targets, "can_skip": True}

    if step_key == "lobbyist":
        p = _find_role_player(st, ["lobbyist"])
        if not p:
            return None
        targets = []
        if p.lobbyist_charges > 0 and not p.lobbyist_killed_tonight:
            targets = _adjacent_kill_targets(st, p.seat, must_be_adjacent=True)
        st.night_current_seat = p.seat
        if not targets:
            return None  # skip silently if no charges
        return {"step": step_key, "role": "说客", "seat": p.seat,
                "text": f"你有 {p.lobbyist_charges} 次杀人机会。杀死一名相邻的存活玩家，或放弃。",
                "targets": targets, "can_skip": True}

    if step_key == "duck_kill":
        p = _find_duck_killer(st)
        if not p:
            return None
        st.night_current_seat = p.seat
        prompt: dict[str, Any] = {"step": step_key, "role": R.name_of(p.role), "seat": p.seat}
        if p.role == "sniper":
            # non-adjacent preferred; if all adjacent then adjacent allowed
            non_adj = night_targets_alive(st, p.seat)
            adj = _adjacent_kill_targets(st, p.seat, must_be_adjacent=True)
            all_adj = len(non_adj) == len(adj)
            if all_adj:
                targets = adj
            else:
                targets = [t for t in non_adj if not is_adjacent(st, p.seat, t["seat"])]
            prompt["text"] = "杀死一名非相邻的存活玩家，或放弃。" + ("（所有人已相邻，可杀相邻玩家）" if all_adj else "")
            prompt["targets"] = targets
            prompt["can_skip"] = True
        elif p.role == "cupid":
            targets = _adjacent_kill_targets(st, p.seat, must_be_adjacent=True)
            prompt["text"] = "杀死一名相邻的存活玩家；或选择不杀人并使用「射箭」（连2人为恋人）。"
            prompt["targets"] = targets
            prompt["can_skip"] = False
            prompt["can_arrow"] = not p.cupid_arrow_used
            if p.cupid_arrow_used:
                prompt["text"] = "杀死一名相邻的存活玩家，或放弃。"
                prompt["can_skip"] = True
            else:
                arrow_targets = night_targets_alive(st, p.seat)
                prompt["arrow_targets"] = arrow_targets
        elif p.role == "priest":
            targets = _adjacent_kill_targets(st, p.seat, must_be_adjacent=True)
            double_targets = night_targets_alive(st, p.seat)
            prompt["text"] = "杀死一名相邻的存活玩家（或放弃）；并可选择一名存活非己玩家翻倍其明日投票权。"
            prompt["targets"] = targets
            prompt["can_skip"] = True
            prompt["double_targets"] = double_targets
            prompt["can_skip_double"] = True
        else:
            # duck, invisible_duck, assassin, spy
            targets = _adjacent_kill_targets(st, p.seat, must_be_adjacent=True)
            prompt["text"] = "杀死一名相邻的存活玩家，或放弃。"
            prompt["targets"] = targets
            prompt["can_skip"] = True
        return prompt

    if step_key == "pelican_falcon":
        p = _find_role_player(st, ["pelican", "falcon"])
        if not p:
            return None
        st.night_current_seat = p.seat
        if p.role == "pelican":
            if p.pelican_used:
                return None
            targets = _adjacent_kill_targets(st, p.seat, must_be_adjacent=True)
            return {"step": step_key, "role": "鹈鹕", "seat": p.seat,
                    "text": "吞掉一名相邻的存活玩家（一局一次），或放弃。",
                    "targets": targets, "can_skip": True}
        else:  # falcon
            if p.falcon_used:
                return None
            targets = _adjacent_kill_targets(st, p.seat, must_be_adjacent=True)
            return {"step": step_key, "role": "猎鹰", "seat": p.seat,
                    "text": "开你的一刀：杀死一名相邻的存活玩家，或放弃。",
                    "targets": targets, "can_skip": True}

    if step_key == "detective":
        p = _find_role_player(st, ["detective"])
        if not p:
            return None
        targets = night_targets_alive(st, p.seat)
        st.night_current_seat = p.seat
        return {"step": step_key, "role": "侦探", "seat": p.seat,
                "text": "选择一名非己存活玩家，侦查他今夜是否杀过人。",
                "targets": targets, "can_skip": True}

    if step_key == "mortician":
        p = _find_role_player(st, ["mortician"])
        if not p:
            return None
        st.night_current_seat = p.seat
        # deaths tonight (real kills, not piped, not belly)
        dead_tonight = [s for s in range(1, 6) if st.players[s].died_night]
        names = [st.players[s].name for s in dead_tonight]
        return {"step": step_key, "role": "殡仪员", "seat": p.seat,
                "text": f"今夜出局：{', '.join(names) if names else '无人'}。可选择一名出局玩家查看其角色。",
                "dead_seats": [{"seat": s, "name": st.players[s].name} for s in dead_tonight],
                "can_skip": True}

    return None


# ---- night action application ----

def apply_kill(st: GameState, killer_seat: int, target_seat: int, killer_role: str) -> None:
    """Apply a night kill. Handles adjacency validity, sheriff suicide, pelican-belly release."""
    target = st.players[target_seat]
    if not target.alive or target.in_belly_of is not None or target.piped:
        return
    target.alive = False
    target.died_night = True
    target._killed_by_role = killer_role  # type: ignore[attr-defined]
    # mark killer as having killed tonight (for detective)
    killer = st.players[killer_seat]
    killer._did_kill_tonight = True  # type: ignore[attr-defined]
    # sheriff self-destruct if killed a goose
    if killer_role == "sheriff":
        if target.is_goose:
            kp = st.players[killer_seat]
            kp.alive = False
            kp.died_night = True
    # invisible_duck killing pelican: release belly
    if killer_role == "invisible_duck" and target.role == "pelican" and target.pelican_belly:
        _release_belly(st, target_seat, reveal_role="invisible_duck")


def _release_belly(st: GameState, pelican_seat: int, reveal_role: str | None = None) -> bool:
    """Release swallowed players from a dead pelican. Returns True if released (triggers day shortcut)."""
    pelican = st.players[pelican_seat]
    if not pelican.pelican_belly:
        return False
    for s in pelican.pelican_belly:
        vp = st.players[s]
        vp.in_belly_of = None
        if reveal_role:
            vp.intel_msgs.append(f"鹈鹕（{pelican.name}）被{R.name_of(reveal_role)}杀死，你从其肚中逃脱。")
        else:
            vp.intel_msgs.append(f"鹈鹕（{pelican.name}）在夜晚被杀死，你从其肚中逃脱。")
    pelican.pelican_belly = []
    return True


def apply_night_action(st: GameState, step_key: str, action: dict) -> bool:
    """Apply the player's night action for the current step. Returns True if applied (not skip)."""
    seat = st.night_current_seat
    if seat is None:
        return False
    p = st.players[seat]
    skip = action.get("skip", False)

    if step_key == "tracker":
        if skip:
            return False
        p.tracker_target = action.get("target")
        return True

    if step_key == "pigeon":
        if action.get("pipe"):
            p.piped = True
            p.pigeon_piped = True
        else:
            tgt = action.get("target")
            if tgt and tgt not in p.pigeon_infected:
                p.pigeon_infected.append(tgt)
        return True

    if step_key == "engineer":
        if action.get("pipe"):
            p.piped = True
        return True

    if step_key == "duck_pipe":
        if action.get("pipe"):
            p.piped = True
        return True

    # kill steps
    if step_key in ("sheriff", "vigilante", "soldier", "lobbyist", "pelican_falcon", "duck_kill"):
        return _apply_kill_step(st, step_key, p, action)

    if step_key == "detective":
        if skip:
            return False
        p.detective_target = action.get("target")
        return True

    if step_key == "mortician":
        if skip:
            return False
        tgt = action.get("target")
        if tgt is not None:
            tp = st.players[tgt]
            p.intel_msgs.append(f"你查看了{tp.name}的角色：{R.name_of(tp.role) if tp.role else '未知'}。")
        return True

    return False


def _apply_kill_step(st: GameState, step_key: str, p: Player, action: dict) -> bool:
    skip = action.get("skip", False)
    acted = False

    if step_key == "lobbyist":
        if skip:
            return False
        tgt = action.get("target")
        if tgt is not None and p.lobbyist_charges > 0 and not p.lobbyist_killed_tonight:
            apply_kill(st, p.seat, tgt, "lobbyist")
            p.lobbyist_charges -= 1
            p.lobbyist_killed_tonight = True
            acted = True
        return acted

    if step_key == "pelican_falcon":
        if skip:
            return False
        tgt = action.get("target")
        if tgt is None:
            return False
        if p.role == "pelican" and not p.pelican_used:
            tp = st.players[tgt]
            tp.in_belly_of = p.seat
            p.pelican_belly.append(tgt)
            p.pelican_used = True
            tp.intel_msgs.append(f"你被鹈鹕（{p.name}）吞食。你不参与投票，无法发动技能。")
            return True
        if p.role == "falcon" and not p.falcon_used:
            apply_kill(st, p.seat, tgt, "falcon")
            p.falcon_used = True
            return True
        return False

    if step_key == "duck_kill":
        if p.role == "cupid":
            return _apply_cupid(st, p, action)
        if p.role == "priest":
            # kill + double
            kill_tgt = action.get("target")
            if kill_tgt is not None and not skip:
                apply_kill(st, p.seat, kill_tgt, "priest")
                acted = True
            # un-pipe if duck was piped and killed
            if acted and p.piped:
                p.piped = False
            dbl = action.get("double_target")
            if dbl is not None:
                p.priest_double_target = dbl
            return True  # priest always 'acts' (even if just double or skip)
        # generic duck kill
        if skip:
            return False
        tgt = action.get("target")
        if tgt is None:
            return False
        apply_kill(st, p.seat, tgt, p.role or "duck")
        # if duck was piped, killing un-pipes
        if p.piped:
            p.piped = False
        return True

    # sheriff, vigilante, soldier
    if skip:
        return False
    tgt = action.get("target")
    if tgt is None:
        return False
    apply_kill(st, p.seat, tgt, p.role or "sheriff")
    if step_key == "vigilante":
        p.vigilante_used = True
    return True


def _apply_cupid(st: GameState, p: Player, action: dict) -> bool:
    use_arrow = action.get("use_arrow", False)
    if use_arrow and not p.cupid_arrow_used:
        a = action.get("arrow_a")
        b = action.get("arrow_b")
        if a is not None and b is not None and a != b:
            st.lover_pair = (a, b)
            p.cupid_arrow_used = True
            st.players[a].intel_msgs.append(f"丘比特（{p.name}）将你和{st.players[b].name}结为恋人。若一方出局，另一方立即出局。")
            st.players[b].intel_msgs.append(f"丘比特（{p.name}）将你和{st.players[a].name}结为恋人。若一方出局，另一方立即出局。")
            p.intel_msgs.append(f"你将{st.players[a].name}和{st.players[b].name}结为恋人。")
            return True
        return False
    # kill
    tgt = action.get("target")
    if tgt is not None and not action.get("skip"):
        apply_kill(st, p.seat, tgt, "cupid")
        return True
    # skip with no arrow = pass
    return True


# ---- night flow controller ----

class GameController:
    """Async coordination between player WebSocket actions and the game loop.
    Provides interruptible waits for night actions, speech turns, and votes."""

    def __init__(self, broadcast_fn):
        self._broadcast_fn = broadcast_fn
        self._waits: dict[str, asyncio.Future] = {}

    async def broadcast(self) -> None:
        await self._broadcast_fn()

    async def wait(self, key: str, timeout: float):
        """Wait until woken (returns wake value) or timeout (returns None)."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._waits[key] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._waits.pop(key, None)

    def wake(self, key: str, value=None) -> None:
        fut = self._waits.get(key)
        if fut and not fut.done():
            fut.set_result(value)

    def has_waiter(self, key: str) -> bool:
        fut = self._waits.get(key)
        return fut is not None and not fut.done()


class NightFSM:
    """Drives the night sequential wake-up."""

    def __init__(self, st: GameState, ctl: GameController):
        self.st = st
        self.ctl = ctl
        self._shortcircuit = False

    async def run(self) -> None:
        st = self.st
        steps = build_night_steps(st)
        for key, _ in steps:
            if self._shortcircuit:
                break
            st.night_cursor += 1
            st.night_current_seat = None
            st.night_prompt = None
            prompt = _build_prompt(st, key)
            if prompt is None:
                continue
            prompt["status_board"] = _status_board(st, for_mortician=(key == "mortician"))
            st.night_prompt = prompt
            await self._run_step(key)
        await self._night_end()

    async def _run_step(self, key: str) -> None:
        st = self.st
        await self.ctl.broadcast()
        action = await self.ctl.wait("night_action", NIGHT_TIMEOUT)
        if action is None:
            action = {"skip": True}
        apply_night_action(st, key, action)
        st.night_current_seat = None
        st.night_prompt = None
        # pelican-belly shortcut
        if key in ("sheriff", "vigilante", "soldier", "lobbyist", "duck_kill", "pelican_falcon"):
            for ps in range(1, 6):
                pp = st.players[ps]
                if not pp.alive and pp.role == "pelican" and pp.pelican_belly and pp.died_night:
                    _release_belly(st, ps)
                    self._shortcircuit = True
                    break
        if check_win(st):
            self._shortcircuit = True
        await self.ctl.broadcast()

    async def _night_end(self) -> None:
        st = self.st
        # resolve tracker
        for p in st.players.values():
            if p.role == "tracker" and p.tracker_target is not None and p.alive and p.in_belly_of is None:
                tgt = st.players.get(p.tracker_target)
                if tgt:
                    msgs = []
                    if not tgt.alive and tgt.died_night:
                        killer_role = getattr(tgt, "_killed_by_role", None)
                        msgs.append(f"{tgt.name}今夜出局。" + (f"凶手角色：{R.name_of(killer_role)}。" if killer_role else ""))
                    elif tgt.in_belly_of is not None:
                        msgs.append(f"{tgt.name}今夜被鹈鹕吞食。")
                    elif tgt.piped:
                        msgs.append(f"{tgt.name}今夜钻进了管道。")
                    if msgs:
                        p.intel_msgs.append("跟踪结果：" + " ".join(msgs))
        # resolve detective
        for p in st.players.values():
            if p.role == "detective" and p.detective_target is not None and p.alive and p.in_belly_of is None:
                tgt = st.players.get(p.detective_target)
                if tgt:
                    did_kill = bool(getattr(tgt, "_did_kill_tonight", False))
                    p.intel_msgs.append(f"侦查结果：{tgt.name}今夜{'杀过人' if did_kill else '没有杀过人'}。")
        # clear pipes + collect real deaths
        killed_names = []
        for p in st.players.values():
            if p.piped:
                p.piped = False
            if not p.alive and p.died_night and p.in_belly_of is None:
                killed_names.append(p.name)
        # vulture count
        for p in st.players.values():
            if p.role == "vulture" and p.alive and p.in_belly_of is None:
                night_kills = [s for s in range(1, 6) if st.players[s].died_night and not st.players[s].alive]
                eligible = any(st.players[s].role != "great_goose" and
                               not (st.players[s].role == "pelican" and st.players[s].pelican_belly)
                               for s in night_kills)
                if eligible:
                    p.vulture_count += 1
        st.system_chat(f"🌙 第{st.day_count}夜结束。天亮了。")
        if killed_names:
            st.system_chat(f"今夜出局：{', '.join(killed_names)}")
        else:
            st.system_chat("今夜无人出局。")
        await self.ctl.broadcast()
        if check_win(st):
            await _end_game(st, self.ctl)
            return
        if st.day_count >= 3:
            _declare_goose_win(st, "第三个白天开始，鹅阵营获胜。")
            await self.ctl.broadcast()
            await _end_game(st, self.ctl)
            return
        await _start_day_speech(st, self.ctl)


# ---- day speech ----

async def _start_day_speech(st: GameState, ctl: GameController) -> None:
    st.phase = Phase.DAY_SPEECH
    st.speech_order = [p.seat for p in sorted(st.alive_players(), key=lambda x: x.seat)]
    st.speech_index = 0
    for p in st.players.values():
        if p.role == "raven" and p.alive and p.in_belly_of is None:
            living_roles = [pp.role for pp in st.alive_players() if pp.role and pp.seat != p.seat]
            target_role = random.choice(living_roles) if living_roles else random.choice([r["id"] for r in R.all_roles()])
            p.raven_target_role = target_role
            p.intel_msgs.append(f"渡鸦目标：杀死一个「{R.name_of(target_role)}」。在轮麦阶段可猜测谁是这个角色。")
    st.system_chat(f"☀️ 第{st.day_count}天白天开始。轮麦发言阶段（按1→5顺序）。")
    await ctl.broadcast()
    await _advance_speech(st, ctl)


async def _advance_speech(st: GameState, ctl: GameController) -> None:
    while True:
        while st.speech_index < len(st.speech_order):
            seat = st.speech_order[st.speech_index]
            p = st.players[seat]
            if p.alive and p.in_belly_of is None:
                break
            st.speech_index += 1
        if st.speech_index >= len(st.speech_order):
            await _start_vote(st, ctl)
            return
        seat = st.speech_order[st.speech_index]
        st.speech_deadline = time.time() + SPEECH_TIMEOUT
        st.system_chat(f"轮到 {st.players[seat].name}（{seat}号）发言。")
        await ctl.broadcast()
        await ctl.wait("speech", SPEECH_TIMEOUT)
        st.speech_index += 1


def end_speech_turn(st: GameState, ctl: GameController, seat: int) -> None:
    if st.phase != Phase.DAY_SPEECH:
        return
    if st.speech_index < len(st.speech_order) and st.speech_order[st.speech_index] == seat:
        ctl.wake("speech")


# ---- day guesses (assassin / raven / magpie) during speech ----

async def day_guess(st: GameState, ctl: GameController, guesser_seat: int, target_seat: int, guessed_role: str) -> dict:
    p = st.players[guesser_seat]
    tp = st.players.get(target_seat)
    if tp is None:
        return {"ok": False, "msg": "目标无效"}
    if p.role == "assassin" and not p.assassin_used_today:
        if guessed_role == "great_goose":
            return {"ok": False, "msg": "你不能猜测某位玩家是「大白鹅」。"}
        p.assassin_used_today = True
        correct = tp.role == guessed_role
        if correct:
            st.pending_assassin.append((guesser_seat, target_seat, guessed_role))
            result = f"你猜{tp.name}是{R.name_of(guessed_role)}，猜对了！投票开始时其将被刺杀出局。"
        else:
            st.pending_assassin.append((guesser_seat, guesser_seat, guessed_role))
            result = f"你猜{tp.name}是{R.name_of(guessed_role)}，猜错了！你将在投票开始时被刺杀出局。"
        await ctl.broadcast()
        return {"ok": True, "msg": result}
    if p.role == "raven" and not p.raven_used_today and p.raven_target_role:
        if not (tp.alive and tp.in_belly_of is None):
            return {"ok": False, "msg": "渡鸦只能猜测存活玩家。"}
        p.raven_used_today = True
        correct = tp.role == p.raven_target_role
        if correct:
            st.pending_raven.append((guesser_seat, target_seat, p.raven_target_role))
            result = f"你猜{tp.name}是目标角色（{R.name_of(p.raven_target_role)}），猜对了！其将被刺杀出局。"
        else:
            result = f"你猜{tp.name}是{R.name_of(p.raven_target_role)}，猜错了。无效果。"
        await ctl.broadcast()
        return {"ok": True, "msg": result}
    if p.role == "magpie":
        if target_seat == guesser_seat:
            return {"ok": False, "msg": "不能猜自己。"}
        correct = tp.role == guessed_role
        if correct and target_seat not in p.magpie_correct:
            p.magpie_correct.append(target_seat)
            p.intel_msgs.append(f"你猜{tp.name}是{R.name_of(guessed_role)}，正确！累计猜对{len(p.magpie_correct)}名。")
            if len(p.magpie_correct) >= 2:
                _declare_win(st, [guesser_seat], "喜鹊累计猜对两名不同玩家的身份，获胜！")
                await _end_game(st, ctl)
                return {"ok": True, "msg": f"猜对了！累计{len(p.magpie_correct)}/2。你获胜了！", "game_over": True}
            await ctl.broadcast()
            return {"ok": True, "msg": f"猜对了！可继续猜下一位。累计{len(p.magpie_correct)}/2。"}
        elif correct:
            return {"ok": True, "msg": "猜对了，但该玩家已在正确列表中，不计入。"}
        else:
            p.intel_msgs.append(f"你猜{tp.name}是{R.name_of(guessed_role)}，猜错了。连猜终止。")
            await ctl.broadcast()
            return {"ok": True, "msg": "猜错了，连猜终止。"}
    return {"ok": False, "msg": "当前无法猜测。"}


# ---- voting ----

async def _start_vote(st: GameState, ctl: GameController) -> None:
    st.phase = Phase.DAY_VOTE
    killed = []
    for gseat, tseat, role in st.pending_assassin:
        tp = st.players[tseat]
        if tp.alive:
            tp.alive = False
            tp.died_day = True
            killed.append(tp.name)
            _handle_lover_death(st, tseat)
    for gseat, tseat, role in st.pending_raven:
        tp = st.players[tseat]
        if tp.alive:
            tp.alive = False
            tp.died_day = True
            killed.append(tp.name)
            _handle_lover_death(st, tseat)
    st.pending_assassin = []
    st.pending_raven = []
    if killed:
        st.system_chat(f"刺杀结算：{'、'.join(killed)} 出局。")
    if check_win(st):
        await ctl.broadcast()
        await _end_game(st, ctl)
        return
    st.votes = {}
    st.vote_deadline = time.time() + VOTE_TIMEOUT
    st.system_chat("🗳️ 投票阶段开始。请选择投票对象。")
    await ctl.broadcast()
    await ctl.wait("vote", VOTE_TIMEOUT)
    await _resolve_vote(st, ctl)


async def submit_vote(st: GameState, ctl: GameController, seat: int, target: int) -> None:
    if st.phase != Phase.DAY_VOTE:
        return
    p = st.players[seat]
    if not (p.alive and p.in_belly_of is None):
        return
    if p.role in ("pelican", "falcon") and target != -1:
        target = -1
    if target != -1 and target == seat and p.role not in ("dodo", "lobbyist"):
        return
    st.votes[seat] = target
    await ctl.broadcast()
    # if all alive players voted, end vote early
    alive_voters = [pp.seat for pp in st.players.values() if pp.alive and pp.in_belly_of is None and pp.name]
    if all(s in st.votes for s in alive_voters):
        ctl.wake("vote")


async def _resolve_vote(st: GameState, ctl: GameController) -> None:
    counts: dict[int, int] = {}
    who_voted: dict[int, list[int]] = {}
    for voter, tgt in st.votes.items():
        weight = 1
        doubled_by = next((pp for pp in st.players.values()
                           if pp.role == "priest" and pp.priest_double_target == voter and not pp.priest_double_consumed), None)
        if doubled_by:
            weight = 2
        counts.setdefault(tgt, 0)
        who_voted.setdefault(tgt, [])
        counts[tgt] += weight
        who_voted[tgt].append(voter)
    st.last_vote_counts = counts
    st.last_vote_who_voted = who_voted
    if not counts:
        st.system_chat("投票结束：无人出局（无人投票）。")
        await _post_vote(st, ctl)
        return
    max_votes = max(counts.values())
    top = [s for s, v in counts.items() if v == max_votes]
    parts = []
    for s, v in sorted(counts.items(), key=lambda x: -x[1]):
        name = "弃票" if s == -1 else st.players[s].name
        parts.append(f"{name}({v}票)")
    st.system_chat(f"投票结果：{', '.join(parts)}")
    if len(top) > 1 or (top and top[0] == -1):
        st.system_chat("平票或弃票最高，无人出局。")
    else:
        out_seat = top[0]
        out_p = st.players[out_seat]
        out_p.alive = False
        out_p.died_day = True
        if out_p.role == "dodo":
            st.system_chat(f"{out_p.name}被投票出局！")
            _declare_win(st, [out_seat], "呆呆鸟被投票出局，获胜！")
            await ctl.broadcast()
            await _end_game(st, ctl)
            return
        st.system_chat(f"{out_p.name}（{out_seat}号）被投票出局。")
        _handle_lover_death(st, out_seat)
    # spy intel
    for tgt, voters in who_voted.items():
        if tgt == -1:
            continue
        if len(voters) == 1:
            vp = st.players[voters[0]]
            if vp.role == "spy":
                tp = st.players[tgt]
                vp.spy_intel[tgt] = tp.role or ""
                vp.intel_msgs.append(f"间谍情报：你是唯一投给{tp.name}的人。你得知其角色：{R.name_of(tp.role) if tp.role else '未知'}。")
    await _post_vote(st, ctl)


async def _post_vote(st: GameState, ctl: GameController) -> None:
    for seat, cnt in st.last_vote_counts.items():
        if seat == -1:
            continue
        p = st.players[seat]
        if p.role == "lobbyist":
            p.lobbyist_votes += cnt
            p.lobbyist_votes_since_charge += cnt
            while p.lobbyist_votes_since_charge >= 2:
                p.lobbyist_votes_since_charge -= 2
                p.lobbyist_charges += 1
    for p in st.players.values():
        p.priest_double_consumed = True
    if check_win(st):
        await ctl.broadcast()
        await _end_game(st, ctl)
        return
    await _start_night(st, ctl)


def _handle_lover_death(st: GameState, dead_seat: int) -> None:
    if not st.lover_pair:
        return
    a, b = st.lover_pair
    if dead_seat in (a, b):
        other = b if dead_seat == a else a
        op = st.players.get(other)
        if op and op.alive:
            op.alive = False
            op.died_day = True
            st.system_chat(f"恋人连锁：{op.name}随{st.players[dead_seat].name}出局。")


# ---- win checks ----

def check_win(st: GameState) -> bool:
    if st.phase == Phase.GAME_OVER:
        return True
    alive = [p for p in st.players.values() if p.alive]
    alive_not_belly = [p for p in alive if p.in_belly_of is None]
    n_alive = len(alive_not_belly)

    def role_alive(role_ids: list[str]) -> bool:
        return any(p.role in role_ids and p.alive for p in st.players.values())

    def role_out(role_ids: list[str]) -> bool:
        return not role_alive(role_ids)

    pigeon = next((p for p in st.players.values() if p.role == "pigeon"), None)
    if pigeon and pigeon.alive:
        living = [p for p in alive_not_belly if p.seat != pigeon.seat]
        if living and all(p.seat in pigeon.pigeon_infected for p in living):
            _declare_win(st, [pigeon.seat], "鸽子已感染所有存活非己玩家，获胜！")
            return True
    vulture = next((p for p in st.players.values() if p.role == "vulture"), None)
    if vulture and vulture.alive and vulture.vulture_count >= 2:
        _declare_win(st, [vulture.seat], "秃鹫累计计数2次且存活，获胜！")
        return True
    surv = next((p for p in st.players.values() if p.role == "survivalist"), None)
    if surv and surv.alive and n_alive <= 2 and role_out(["pelican", "falcon", "raven"]):
        _declare_win(st, [surv.seat], "生存主义者存活至2人且鹈鹕/猎鹰/渡鸦出局，获胜！")
        return True
    if n_alive <= 2:
        for role_id in ("pelican", "falcon", "raven"):
            rp = next((p for p in st.players.values() if p.role == role_id), None)
            if rp and rp.alive and rp.in_belly_of is None:
                label = "鹈鹕存活至2人（不含肚中），获胜！" if role_id == "pelican" else f"{R.name_of(role_id)}存活至2人，获胜！"
                _declare_win(st, [rp.seat], label)
                return True
    duck_players = [p for p in st.players.values() if p.is_duck and p.alive and p.in_belly_of is None]
    if duck_players and len(duck_players) * 2 >= n_alive and role_out(["pelican", "falcon", "raven"]):
        _declare_win(st, [p.seat for p in duck_players], "鸭阵营抵达半数且鹈鹕/猎鹰/渡鸦出局，获胜！")
        return True
    goose_alive = [p for p in st.players.values() if p.is_goose and p.alive]
    if role_out(["pelican", "falcon", "raven"]) and role_out(R.DUCK_ROLES):
        _declare_win(st, [p.seat for p in goose_alive], "鸭阵营、鹈鹕、猎鹰、渡鸦全部出局，鹅阵营获胜！")
        return True
    return False


def _declare_win(st: GameState, seats: list[int], reason: str) -> None:
    st.winners = seats
    st.win_reason = reason


def _declare_goose_win(st: GameState, reason: str) -> None:
    _declare_win(st, [p.seat for p in st.players.values() if p.is_goose and p.alive], reason)


async def _end_game(st: GameState, ctl: GameController) -> None:
    st.phase = Phase.GAME_OVER
    st.night_current_seat = None
    st.night_prompt = None
    st.system_chat(f"🏆 {st.win_reason}")
    await ctl.broadcast()


# ---- night / game start ----

async def _start_night(st: GameState, ctl: GameController) -> None:
    st.day_count += 1
    st.phase = Phase.NIGHT
    st.reset_night_transient()
    st.reset_day_transient()
    st.night_snapshot = {
        s: (st.players[s].alive and st.players[s].in_belly_of is None)
        for s in range(1, 6) if st.players[s].name
    }
    st.system_chat(f"🌙 第{st.day_count}夜降临。")
    fsm = NightFSM(st, ctl)
    st._night_fsm = fsm  # type: ignore[attr-defined]
    await fsm.run()


async def start_game(st: GameState, ctl: GameController) -> None:
    roles_list = R.assign_5p_roles()
    seats = sorted(st.occupied_seats())
    for i, seat in enumerate(seats):
        st.players[seat].role = roles_list[i]
        st.players[seat].intel_msgs = []
    st.day_count = 0
    st.lover_pair = None
    st.speech_order = []
    st.speech_index = 0
    st.votes = {}
    st.pending_assassin = []
    st.pending_raven = []
    await ctl.broadcast()
    await _start_night(st, ctl)
