# 本次代码修改说明与玩家状态保存原理

## 1. 本次修改目标

本次主要解决的问题是：

> 玩家进入房间并坐下后，如果刷新页面、关闭标签页，再次打开网站时，仍然可以回到原来的房间和原来的座位。

在这个游戏中，座位号不是普通 UI 显示位置，而是游戏逻辑的一部分。很多规则都依赖座位号，例如：

- 1~5 号玩家围成一圈；
- 1 号和 5 号相邻；
- 夜间击杀、吞食、技能目标会根据“相邻”判断；
- 白天发言顺序按座位号进行；
- 玩家角色、生死、技能状态、投票状态都绑定在对应座位上。

因此，玩家重新进入时必须恢复到原来的 `seat`，不能重新选一个新座位。

---

## 2. 修改过的主要文件

本次主要改动了以下文件：

```text
static/app.js
static/index.html
static/style.css
app/rooms.py
app/protocol.py
app/main.py
README.md
ins.md
ins2.md
```

其中最核心的是：

- `static/app.js`：前端保存和恢复玩家身份；
- `app/rooms.py`：后端允许同一玩家用 secret 恢复原座位，并修复重连竞态；
- `static/index.html` / `static/style.css`：增加“继续游戏”“恢复原座位”的界面；
- `README.md`：更新断线重连说明。

---

## 3. `static/app.js` 的修改

`static/app.js` 是前端的核心逻辑文件，本次改动最多。

### 3.1 增加全局状态字段

原来前端只在内存中保存：

```js
G.roomCode
G.mySeat
G.secret
```

页面刷新或标签页关闭后，这些变量会全部丢失。

现在新增了一些字段：

```js
myName
pendingJoinName
isRestoring
restoreIdentity
suppressReconnect
reconnectTimer
wsGen
```

作用如下：

| 字段 | 作用 |
|---|---|
| `myName` | 当前玩家昵称 |
| `pendingJoinName` | 入座请求发出后、服务端确认前，临时保存昵称 |
| `isRestoring` | 当前是否正在执行恢复身份流程 |
| `restoreIdentity` | 当前房间在浏览器中保存的身份信息 |
| `suppressReconnect` | 恢复失败时阻止继续自动重连 |
| `reconnectTimer` | 保存自动重连定时器，避免重复重连 |
| `wsGen` | 区分新旧 WebSocket，避免旧连接干扰新连接 |

---

### 3.2 新增浏览器身份保存函数

在 `static/app.js` 中新增了这些函数：

```js
identityKey(code)
loadIdentity(code)
saveIdentity(code, seat, name, secret)
clearIdentity(code)
loadLastIdentity()
```

这些函数使用浏览器的 `localStorage` 保存玩家在某个房间中的重连身份。

保存的 key 主要有两个：

```text
wereduck_identity_${roomCode}
wereduck_last_identity
```

例如房间号是 `ABCD`，则保存：

```text
wereduck_identity_ABCD
wereduck_last_identity
```

其中：

- `wereduck_identity_ABCD` 保存该房间的玩家身份；
- `wereduck_last_identity` 保存最近一次进入过的房间号，用于首页显示“继续游戏”。

保存的数据结构类似：

```js
{
  version: 1,
  roomCode: "ABCD",
  seat: 2,
  name: "小明",
  secret: "服务端返回的重连令牌",
  updatedAt: 1780000000000
}
```

注意：

> 前端保存的不是完整游戏状态，而是“恢复身份所需的凭据”。

真正的游戏状态仍然保存在后端内存中。

---

### 3.3 入座成功后保存身份

原来玩家入座成功后，前端只做：

```js
G.mySeat = data.seat;
G.secret = data.secret;
```

也就是说，`secret` 只存在于当前页面内存中。

现在收到服务端 `joined` 消息后，会把身份保存到 `localStorage`：

```js
saveIdentity(G.roomCode, G.mySeat, G.myName, G.secret);
```

这样即使刷新页面或关闭标签页，只要浏览器的 `localStorage` 没被清除，下次打开网站时仍能找到原来的：

- 房间号；
- 座位号；
- 昵称；
- 重连 secret。

---

### 3.4 首页显示“继续游戏”

新增了：

```js
renderLastIdentity()
attemptRestore(id)
```

页面初始化时会读取：

```js
loadLastIdentity()
```

如果浏览器里有上次保存的房间身份，就在首页显示类似：

```text
继续房间 ABCD · 2号 · 小明
```

玩家点击「继续游戏」后，前端会执行恢复流程。

恢复流程大致是：

1. 从 `localStorage` 读取保存的身份；
2. 请求 `GET /api/room/{roomCode}` 检查房间是否还存在；
3. 如果房间不存在，清除本地身份并提示用户；
4. 如果房间存在，用保存的 `seat + secret` 连接 WebSocket；
5. 服务端验证通过后，玩家回到原座位。

---

### 3.5 选座页显示“恢复原座位”

当玩家手动输入房间号进入选座页时，如果浏览器中保存了这个房间的身份，会显示：

```text
检测到你之前是 2号 · 小明
```

并提供按钮：

```text
恢复原座位
不用这个身份
```

这是必要的，因为玩家断线后，原座位在服务端仍然是 `taken=true`，普通选座流程不能点击这个座位。

恢复原座位时，前端不会走普通“选择空座位”逻辑，而是直接用保存的 `secret` 向服务端证明：

> 我就是之前坐在这个座位上的玩家。

---

### 3.6 自动重连逻辑增强

原来前端 WebSocket 断开后，会在部分情况下用内存中的 `G.secret` 自动重连。

现在增强为：

- 使用 `reconnectTimer` 防止重复定时器；
- 使用 `wsGen` 区分新旧 WebSocket；
- 使用 `suppressReconnect` 避免恢复失败后无限重连；
- 游戏结束页、准备页等阶段也允许继续保持连接恢复能力。

---

## 4. `static/index.html` 的修改

`static/index.html` 主要增加了两个 UI 区域。

### 4.1 首页恢复卡片

在首页增加：

```html
<div id="restore-card" class="restore-card hidden">
  <p id="restore-text" class="hint"></p>
  <div class="btn-row">
    <button id="btn-restore" class="btn btn-primary">继续游戏</button>
    <button id="btn-clear-restore" class="btn-small">忘记</button>
  </div>
</div>
```

用途：

- 显示上次保存的房间身份；
- 允许玩家点击“继续游戏”；
- 允许玩家点击“忘记”清除本地身份。

---

### 4.2 选座页恢复卡片

在选座页增加：

```html
<div id="seat-restore-card" class="restore-card hidden">
  <p id="seat-restore-text" class="hint"></p>
  <div class="btn-row">
    <button id="btn-seat-restore" class="btn btn-primary">恢复原座位</button>
    <button id="btn-seat-clear-restore" class="btn-small">不用这个身份</button>
  </div>
</div>
```

用途：

- 当玩家输入房间号后，如果本浏览器保存过该房间身份，则提示恢复；
- 玩家可以选择恢复原座位，也可以放弃该身份重新入座。

---

## 5. `static/style.css` 的修改

增加了恢复卡片样式：

```css
.restore-card
.restore-card .hint
.restore-card .btn-row
.restore-card .btn-small
```

作用是让“继续游戏”和“恢复原座位”的提示与原有 UI 风格保持一致。

---

## 6. `app/rooms.py` 的修改

`app/rooms.py` 是后端房间和 WebSocket 管理的核心。

### 6.1 后端原本已有 secret 重连机制

原来的后端已经有一个很重要的机制：

每个玩家入座时，后端会生成一个 secret：

```python
secret=secrets.token_hex(8)
```

如果某个座位已经有人，后端不会允许别人直接坐上去。

但如果客户端提供的 `secret` 和这个座位原玩家的 `secret` 一致，后端会认为这是原玩家重连，允许恢复这个座位。

也就是说，真正判断“你是不是原玩家”的依据是：

```text
roomCode + seat + secret
```

---

### 6.2 给错误消息增加 code

原来的错误消息只有中文文本。

现在 `send_error()` 增加了可选 `code`：

```python
async def send_error(self, ws: WebSocket, text: str, code: str | None = None) -> None:
```

例如：

```python
code="invalid_seat"
code="seat_taken"
code="name_required"
code="duplicate_name"
```

这样前端以后可以更稳定地根据错误类型处理恢复失败，而不是只看中文错误文案。

---

### 6.3 重连时替换旧 WebSocket

玩家可能出现这种情况：

1. 旧标签页还没完全断开；
2. 新标签页已经用 secret 恢复成功；
3. 旧 WebSocket 稍后才触发断开事件。

如果不处理，旧连接可能把新连接误标为离线。

因此现在在 secret 匹配成功后，会把旧连接替换成新连接，并尝试关闭旧连接：

```python
old_ws = existing.ws
existing.ws = ws
existing.online = True
self.connections[seat] = ws
if old_ws and old_ws is not ws:
    try:
        await old_ws.close()
    except Exception:
        pass
```

这样同一个座位最终只保留一个当前有效 WebSocket。

---

### 6.4 旧连接断开时不再误清理新连接

新增了：

```python
_mark_disconnected(ws, seat)
```

核心判断是：

```python
if self.connections.get(seat) is not ws:
    return
```

意思是：

> 只有当前断开的 WebSocket 仍然是这个座位的活跃连接时，才把玩家标记为离线。

如果这个座位已经被新 WebSocket 接管，那么旧连接断开时不会影响新连接。

这解决了刷新/多标签页恢复时的新旧连接竞态问题。

---

## 7. `app/protocol.py` 的修改

原来的错误消息函数是：

```python
def error(text: str) -> str:
    return msg("error", text=text)
```

现在改为：

```python
def error(text: str, code: str | None = None) -> str:
    data = {"text": text}
    if code:
        data["code"] = code
    return msg("error", **data)
```

这样后端可以发送：

```json
{
  "type": "error",
  "text": "座位已被占用",
  "code": "seat_taken"
}
```

同时也兼容旧逻辑：如果不传 `code`，仍然只发送文本错误。

---

## 8. `app/main.py` 的修改

`app/main.py` 是 FastAPI 和 WebSocket 的入口。

主要改动：

### 8.1 房间不存在时返回错误 code

原来只返回：

```text
房间不存在。
```

现在附带：

```python
code="room_not_found"
```

### 8.2 非法 seat 更稳健

原来这里直接执行：

```python
seat = int(data.get("seat", 0))
```

如果客户端传了奇怪数据，可能抛异常。

现在改为：

```python
try:
    seat = int(data.get("seat", 0))
except (TypeError, ValueError):
    await ws.send_text(P.error("座位号无效（1-5）。", code="invalid_seat"))
    await ws.close()
    return
```

这样畸形 WebSocket join 消息不会导致后端异常。

---

## 9. `README.md` 的修改

原 README 中写着：

```text
断线刷新后将永远无法回到当局游戏。
```

现在已经不准确，所以更新为：

```text
浏览器会保存本房间重连身份；刷新或关闭标签页后，只要服务端房间仍存在，可从首页继续回到原座位。服务重启、房间被全员离线清理或等待超时后无法恢复。
```

---

## 10. 玩家状态保存方法和原理

### 10.1 重要说明：没有把完整游戏状态存在浏览器

本次没有把完整玩家状态保存在浏览器里。

浏览器没有保存这些敏感或复杂信息：

- 角色；
- 阵营；
- 生死状态；
- 夜间行动结果；
- 投票结果；
- 私密情报；
- 技能使用状态；
- 当前游戏阶段。

这些仍然全部保存在服务端的内存房间对象里。

浏览器只保存“恢复身份所需的信息”：

```text
roomCode + seat + name + secret + updatedAt
```

---

### 10.2 服务端保存真正的玩家状态

后端 `GameState` 中有：

```python
players: dict[int, Player]
```

其中 key 是座位号 `seat`。

每个 `Player` 中保存了：

- 座位号；
- 昵称；
- 角色；
- 是否存活；
- 是否在线；
- 是否准备；
- 当前 WebSocket；
- secret；
- 各种技能和游戏状态。

所以真正的玩家状态在服务端。

---

### 10.3 浏览器保存的是“钥匙”

可以把 `secret` 理解为一把钥匙。

玩家第一次入座时：

1. 前端告诉服务端：我要坐 2 号，昵称是小明；
2. 服务端创建 2 号玩家；
3. 服务端生成一个随机 `secret`；
4. 服务端把这个 `secret` 发给前端；
5. 前端把 `roomCode + seat + secret` 保存到 `localStorage`。

玩家关闭标签页后：

- 服务端还保留 2 号玩家；
- 2 号玩家只是变成离线；
- 浏览器本地还保存着这把“钥匙”。

玩家再次打开网站时：

1. 前端读取 localStorage；
2. 发现上次是 `ABCD` 房间的 2 号；
3. 前端用保存的 `secret` 连接 WebSocket；
4. 服务端检查：这个 secret 是否等于 2 号玩家原来的 secret；
5. 如果相等，服务端认为这是原玩家回来；
6. 玩家恢复到原来的 2 号座位。

---

### 10.4 为什么不能只靠昵称恢复

不能只用昵称恢复，因为昵称不是安全凭据。

例如别人也可以输入“小明”。

如果只靠昵称恢复，别人可能冒充原玩家进入其座位，看到私密角色和情报。

因此后端使用随机生成的 `secret` 作为恢复凭证。

只知道房间号和昵称是不够的，必须同时知道：

```text
房间号 + 座位号 + secret
```

---

### 10.5 为什么使用 localStorage

本次使用 `localStorage`，原因是：

1. 项目原本已经使用 `localStorage` 保存角色标记；
2. 实现简单，适合当前原生 JS 前端；
3. 刷新页面、关闭标签页后仍然存在；
4. WebSocket join 消息需要 JS 主动读取 `secret` 并发送给服务端。

没有优先使用 cookie 的原因：

- 普通 JS cookie 安全性不比 localStorage 高；
- cookie 会自动随请求发送，反而更容易扩大暴露面；
- HttpOnly cookie 虽然更安全，但前端 JS 读不到，需要重写 WebSocket 认证逻辑，改动会更大。

---

### 10.6 localStorage 的限制

该恢复能力不是账号登录系统，它有以下限制：

- 换浏览器无法恢复；
- 换设备无法恢复；
- 清空浏览器数据后无法恢复；
- 隐私模式可能无法长期保存；
- 服务端重启后无法恢复；
- 房间被清理后无法恢复；
- 如果所有玩家离线太久，房间会被删除。

这是因为服务端房间状态是纯内存保存的，不是数据库持久化保存。

---

## 11. 整体恢复流程总结

完整流程如下：

```text
玩家首次入座
→ 服务端创建 Player，并生成 secret
→ 服务端返回 joined { seat, secret }
→ 前端把 roomCode + seat + name + secret 存入 localStorage
→ 玩家关闭标签页
→ 服务端保留 Player，只标记 offline
→ 玩家再次打开网站
→ 前端读取 localStorage，显示“继续游戏”
→ 玩家点击继续
→ 前端用 roomCode + seat + secret 发起 WebSocket join
→ 服务端校验 secret
→ 校验成功：恢复原 Player、原 seat、原游戏状态
→ 校验失败或房间不存在：清理本地记录，提示重新加入
```

---

## 12. 已做的基础验证

已运行以下检查：

```bash
python -m compileall app
node --check static/app.js
```

作用：

- `python -m compileall app`：检查 Python 文件是否有语法错误；
- `node --check static/app.js`：检查前端 JavaScript 是否有语法错误。

两项检查均已通过。

---

## 13. 建议手动测试

建议按以下步骤测试：

1. 启动服务：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

2. 打开：

```text
http://localhost:8000
```

3. 创建房间。
4. 选择座位并输入昵称。
5. 打开浏览器开发者工具，查看 localStorage，确认有：

```text
wereduck_identity_房间号
wereduck_last_identity
```

6. 关闭标签页。
7. 重新打开网站。
8. 首页应出现“继续游戏”。
9. 点击后应回到原房间、原座位。
10. 再测试手动输入同一房间号，选座页应出现“恢复原座位”。

如果等待太久导致房间被服务端清理，则恢复失败是正常现象。
