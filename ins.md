# Wereduck 网站启动与运行说明

## 1. 准备环境

本项目是 Python FastAPI + WebSocket 后端，前端是原生 HTML/CSS/JS。

需要先安装 Python 依赖：

```bash
pip install -r requirements.txt
```

如果你的电脑同时安装了多个 Python 版本，也可以使用：

```bash
python -m pip install -r requirements.txt
```

## 2. 启动网站

### 方式一：使用项目脚本启动

在项目根目录运行：

```bash
./run.sh
```

该脚本会启动：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 方式二：手动启动

如果脚本无法运行，可以直接执行：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

开发时建议加上 `--reload`，代码改动后会自动重载：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## 3. 打开网站

服务启动后，在浏览器访问：

```text
http://localhost:8000
```

如果其他玩家和你在同一个局域网，可以让他们访问你电脑的局域网 IP，例如：

```text
http://你的电脑IP:8000
```

例如：

```text
http://192.168.1.23:8000
```

如果不在同一个局域网，需要使用内网穿透工具，把本机 `8000` 端口暴露出去。

## 4. 游戏运行流程

1. 打开首页。
2. 点击「创建房间」，系统会生成 4 位房间号。
3. 其他玩家输入该房间号并点击「加入房间」。
4. 每名玩家选择 1~5 号空座位，并输入昵称入座。
5. 5 人全部入座后，所有人点击「准备」。
6. 游戏自动开始。
7. 按页面提示完成夜间行动、白天发言、投票等流程。

## 5. 断线与重新进入

浏览器会保存本房间的重连身份。

如果玩家刷新页面、关闭标签页后重新进入网站，只要服务端房间还存在，就可以在首页点击「继续游戏」回到原来的座位。

注意：以下情况无法恢复：

- 服务端程序重启；
- 房间被系统清理；
- 所有玩家离线超过一段时间；
- 浏览器清除了 localStorage；
- 换了浏览器或换了设备。

## 6. 可选：调整阶段超时时间

可以通过环境变量调整游戏各阶段超时时间，单位是秒。

示例：

```bash
WEREDUCK_NIGHT_TIMEOUT=25 WEREDUCK_SPEECH_TIMEOUT=60 WEREDUCK_VOTE_TIMEOUT=30 ./run.sh
```

含义：

- `WEREDUCK_NIGHT_TIMEOUT`：夜间行动超时时间；
- `WEREDUCK_SPEECH_TIMEOUT`：白天发言超时时间；
- `WEREDUCK_VOTE_TIMEOUT`：投票超时时间。

## 7. 停止网站

在运行服务的终端中按：

```text
Ctrl + C
```

即可停止网站。

停止服务后，当前所有房间都会消失，因为本项目的房间状态保存在服务端内存中。
