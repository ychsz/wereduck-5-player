"""Room management, WebSocket connections, and game action dispatch."""
from __future__ import annotations

import asyncio
import random
import secrets
import time
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from . import protocol as P
from . import roles as R
from . import views as V
from .resolver import GameController, start_game, apply_night_action, end_speech_turn, day_guess, submit_vote, check_win
from .state import GameState, Player, Phase

ROOM_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
rooms: dict[str, "Room"] = {}

WAITING_TIMEOUT = 300      # 5 min in waiting/ready
ALL_OFFLINE_TIMEOUT = 60   # 60s all-offline grace


def gen_room_code() -> str:
    for _ in range(100):
        code = "".join(random.choice(ROOM_CODE_CHARS) for _ in range(4))
        if code not in rooms:
            return code
    return "".join(random.choice(ROOM_CODE_CHARS) for _ in range(4))


def get_room(code: str) -> Optional["Room"]:
    return rooms.get(code)


class Room:
    def __init__(self, code: str):
        self.code = code
        self.st = GameState()
        self.connections: dict[int, WebSocket] = {}
        self.ctl: Optional[GameController] = None
        self.game_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self.created_at = time.time()
        self.all_offline_since: Optional[float] = None

    # ---- broadcast ----

    async def broadcast(self) -> None:
        for seat, ws in list(self.connections.items()):
            p = self.st.players.get(seat)
            if p is None or not p.name:
                continue
            try:
                view = V.player_view(self.st, seat)
                await ws.send_text(P.state(view))
            except Exception:
                pass

    async def send_error(self, ws: WebSocket, text: str, code: str | None = None) -> None:
        try:
            await ws.send_text(P.error(text, code=code))
        except Exception:
            pass

    async def send_chat(self, seat: int, name: str, text: str, system: bool = False) -> None:
        msg = P.chat(seat, name, text, system=system)
        for ws in list(self.connections.values()):
            try:
                await ws.send_text(msg)
            except Exception:
                pass

    # ---- joining ----

    async def join(self, ws: WebSocket, seat: int, name: str, secret: str) -> Optional[int]:
        """Try to place/reconnect a player. Returns seat on success, None on failure."""
        if seat < 1 or seat > 5:
            await self.send_error(ws, "座位号无效（1-5）。", code="invalid_seat")
            return None
        existing = self.st.players.get(seat)
        if existing and existing.name:
            # seat taken — only allow if secret matches (reconnect)
            if secret and existing.secret == secret:
                old_ws = existing.ws
                existing.ws = ws
                existing.online = True
                self.connections[seat] = ws
                if old_ws and old_ws is not ws:
                    try:
                        await old_ws.close()
                    except Exception:
                        pass
                return seat
            await self.send_error(ws, f"{seat}号座位已被人占用。", code="seat_taken")
            return None
        # new sit
        if not name.strip():
            await self.send_error(ws, "请输入昵称。", code="name_required")
            return None
        # check name not duplicate
        for p in self.st.players.values():
            if p.name == name.strip():
                await self.send_error(ws, "该昵称已被使用，请换一个。", code="duplicate_name")
                return None
        p = Player(seat=seat, name=name.strip(), ws=ws, online=True,
                    secret=secrets.token_hex(8))
        self.st.players[seat] = p
        self.connections[seat] = ws
        self.st.system_chat(f"{seat}号 {name.strip()} 入座。")
        return seat

    # ---- ready / start ----

    async def set_ready(self, seat: int, ready: bool) -> None:
        p = self.st.players.get(seat)
        if not p:
            return
        p.ready = ready
        self.st.system_chat(f"{seat}号 {p.name} {'已准备' if ready else '取消准备'}。")
        await self.broadcast()
        # check all ready
        if self.st.all_seated() and all(self.st.players[s].ready for s in self.st.occupied_seats()):
            if self.game_task is None or self.game_task.done():
                if self.st.phase in (Phase.WAITING, Phase.READY, Phase.GAME_OVER):
                    self._start_new_game()

    def _start_new_game(self) -> None:
        if self.st.phase == Phase.GAME_OVER:
            self.st.reset_round()
        else:
            for p in self.st.players.values():
                if p.name:
                    p.ready = False
        self.ctl = GameController(self.broadcast)
        self.game_task = asyncio.create_task(self._run_game())

    async def _run_game(self) -> None:
        try:
            await start_game(self.st, self.ctl)  # type: ignore[arg-type]
        except Exception as e:
            self.st.system_chat(f"⚠️ 游戏引擎异常: {e}")
            await self.broadcast()

    # ---- chat ----

    async def handle_chat(self, seat: int, text: str) -> None:
        p = self.st.players.get(seat)
        if not p or not text.strip():
            return
        text = text.strip()[:500]
        allowed = False
        if self.st.phase == Phase.DAY_SPEECH:
            # only current speaker can chat
            if (self.st.speech_order and self.st.speech_index < len(self.st.speech_order)
                    and self.st.speech_order[self.st.speech_index] == seat
                    and p.alive and p.in_belly_of is None):
                allowed = True
        elif self.st.phase == Phase.DAY_VOTE:
            if p.alive and p.in_belly_of is None:
                allowed = True
        if not allowed:
            return
        self.st.add_chat(seat, text)
        await self.broadcast()

    # ---- WS main loop ----

    async def handle_ws(self, ws: WebSocket, seat: int) -> None:
        try:
            while True:
                raw = await ws.receive_text()
                data = P.parse(raw)
                async with self._lock:
                    await self._dispatch(ws, seat, data)
        except WebSocketDisconnect:
            await self._mark_disconnected(ws, seat)
        except Exception:
            await self._mark_disconnected(ws, seat)

    async def _mark_disconnected(self, ws: WebSocket, seat: int) -> None:
        if self.connections.get(seat) is not ws:
            return
        p = self.st.players.get(seat)
        if p:
            p.online = False
            p.ws = None
        self.connections.pop(seat, None)
        await self.broadcast()

    async def _dispatch(self, ws: WebSocket, seat: int, data: dict) -> None:
        t = data.get("type")
        p = self.st.players.get(seat)

        if t == "ready":
            if p and self.st.phase in (Phase.WAITING, Phase.READY, Phase.GAME_OVER):
                await self.set_ready(seat, True)
            return

        if t == "cancel_ready":
            if p and self.st.phase in (Phase.WAITING, Phase.READY, Phase.GAME_OVER):
                await self.set_ready(seat, False)
            return

        if t == "night_action":
            if self.st.phase == Phase.NIGHT and self.ctl and self.st.night_current_seat == seat:
                self.ctl.wake("night_action", value=data.get("action", {"skip": True}))
            return

        if t == "speech":
            if p:
                await self.handle_chat(seat, data.get("text", ""))
            return

        if t == "end_speech":
            if self.st.phase == Phase.DAY_SPEECH and self.ctl:
                end_speech_turn(self.st, self.ctl, seat)
            return

        if t == "vote":
            if self.st.phase == Phase.DAY_VOTE and self.ctl:
                await submit_vote(self.st, self.ctl, seat, int(data.get("target", -1)))
            return

        if t == "day_guess":
            if self.st.phase == Phase.DAY_SPEECH and self.ctl and p:
                result = await day_guess(self.st, self.ctl, seat, int(data.get("target", 0)), data.get("role", ""))
                # send structured result to the guesser; error msg for validation failures
                if result.get("ok"):
                    try:
                        await ws.send_text(P.msg("guess_result", **result))
                    except Exception:
                        pass
                else:
                    await self.send_error(ws, result.get("msg", ""))
            return

        if t == "get_state":
            await self.broadcast()


def create_room() -> Room:
    code = gen_room_code()
    room = Room(code)
    rooms[code] = room
    return room


def _remove_room(code: str) -> None:
    r = rooms.pop(code, None)
    if r and r.game_task and not r.game_task.done():
        r.game_task.cancel()


async def cleanup_loop() -> None:
    """Periodically dissolve rooms where all players are offline or that have
    been in the waiting room for more than 5 minutes."""
    while True:
        await asyncio.sleep(15)
        now = time.time()
        to_remove: list[str] = []
        for code, r in list(rooms.items()):
            # waiting-room 5-minute timeout
            if r.st.phase in (Phase.WAITING, Phase.READY) and now - r.created_at > WAITING_TIMEOUT:
                to_remove.append(code)
                continue
            # all-offline check
            occupied = r.st.occupied_seats()
            if occupied and all(not r.st.players[s].online for s in occupied):
                if r.all_offline_since is None:
                    r.all_offline_since = now
                elif now - r.all_offline_since > ALL_OFFLINE_TIMEOUT:
                    to_remove.append(code)
            else:
                r.all_offline_since = None
        for code in to_remove:
            _remove_room(code)


def room_info(code: str) -> dict | None:
    r = get_room(code)
    if not r:
        return None
    return {
        "code": code,
        "players": len(r.st.occupied_seats()),
        "seats": [
            {"seat": s, "name": r.st.players[s].name if s in r.st.players else "", "taken": s in r.st.players and bool(r.st.players[s].name)}
            for s in range(1, 6)
        ],
    }
