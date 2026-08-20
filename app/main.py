from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import protocol as P
from . import roles as R
from .rooms import Room, create_room, get_room, room_info, cleanup_loop

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Wereduck 5人局")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
async def _startup():
    asyncio.create_task(cleanup_loop())


@app.get("/")
async def index():
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/roles")
async def api_roles():
    return JSONResponse(R.all_roles())


@app.post("/api/room")
async def api_create_room():
    room = create_room()
    return {"code": room.code}


@app.get("/api/room/{code}")
async def api_room_info(code: str):
    info = room_info(code)
    if not info:
        return JSONResponse({"error": "房间不存在"}, status_code=404)
    return info


@app.websocket("/ws/{code}")
async def ws_endpoint(ws: WebSocket, code: str):
    room = get_room(code)
    if not room:
        await ws.accept()
        await ws.send_text(P.error("房间不存在。", code="room_not_found"))
        await ws.close(code=4000)
        return
    await ws.accept()
    # wait for join
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=30)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        await ws.close()
        return
    data = P.parse(raw)
    if data.get("type") != "join":
        await ws.send_text(P.error("请先发送 join 消息。"))
        await ws.close()
        return
    try:
        seat = int(data.get("seat", 0))
    except (TypeError, ValueError):
        await ws.send_text(P.error("座位号无效（1-5）。", code="invalid_seat"))
        await ws.close()
        return
    name = data.get("name", "")
    secret = data.get("secret", "")
    my_seat = await room.join(ws, seat, name, secret)
    if my_seat is None:
        await ws.close()
        return
    await ws.send_text(P.msg("joined", seat=my_seat, secret=room.st.players[my_seat].secret))
    await room.broadcast()
    await room.handle_ws(ws, my_seat)
