"""
实时通道（WebSocket）：谁改了任务，别人的看板立刻知道。

为什么是现在这套设计
--------------------
* **传输用浏览器原生 WebSocket**：不引 socket.io，也不需要任何 CDN。
  组员在只有热点、没有外网的手机上打开看板时，依然能连上（socket.io 的客户端
  一般从 CDN 加载，那种环境下会直接失效）。
* **服务端只广播事件，不缓存任务状态**：任务真值永远在 SQLite + GitHub 里。
  客户端收到 ``tasks_changed`` 后自己重新拉一次 ``/api/tasks``，
  这样不会出现「服务端内存里的状态」和「数据库状态」两份真相打架。
* **连接必须已登录**：在协议升级**之前**用 Flask session 校验，
  未登录直接返回 401（不会先建立连接再踢掉）。
* **每条连接一个发送锁**：广播可能同时来自多个 HTTP 请求线程，
  而 ``simple_websocket`` 的 ``send`` 不是线程安全的。
* **心跳交给协议层**：``simple_websocket`` 的 ``ping_interval`` 会自动发
  WebSocket ping / 收 pong，断线双方都能尽快发现；客户端另有指数退避重连，
  实在连不上就退回原来的轮询（见 static/app.js），功能不会因为 WS 挂了而不可用。

事件格式（JSON 文本帧）
-----------------------
服务端 → 客户端::

    {"type": "hello",          "protocol": 1, "you": {...}, "server_time": 1730000000.0}
    {"type": "presence",       "count": 2, "users": [{"username": "小明", "display_name": "张三", "connections": 1}]}
    {"type": "tasks_changed",  "action": "update_status", "issue": 12, "by": "小明", "detail": "状态=doing", "at": ...}
    {"type": "pong",           "at": ...}
    {"type": "error",          "message": "..."}

客户端 → 服务端::

    {"type": "ping"}       # 保活（协议层 ping 之外的显式探活，服务端回 pong）
    {"type": "whoami"}     # 想重新拿一次 hello

注意：事件里只出现**代号**（``by`` 字段），真名由前端用自己的映射表翻译，
和「真名不出本地」的约定保持一致。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 取决于运行环境是否装了 simple-websocket
    from simple_websocket import ConnectionClosed, Server as WebSocketServer

    WEBSOCKET_AVAILABLE = True
except ImportError:  # pragma: no cover
    WebSocketServer = None  # type: ignore[assignment]

    class ConnectionClosed(Exception):  # type: ignore[no-redef]
        """没装 simple-websocket 时的占位异常，保证 import 不会炸。"""

    WEBSOCKET_AVAILABLE = False


PROTOCOL_VERSION = 1
PING_INTERVAL = 25          # 协议级 ping 间隔（秒）：保活 + 尽快发现断线
RECEIVE_TIMEOUT = 60        # 等待客户端消息的超时（秒），超时不算断开，只是继续等


class Client:
    """一条已建立的 WebSocket 连接（对应用户的一次页面打开）。"""

    __slots__ = ("id", "ws", "username", "display_name", "peer", "connected_at",
                 "_send_lock", "alive")

    def __init__(self, client_id: int, ws: Any, username: str,
                 display_name: str = "", peer: str = "") -> None:
        self.id = client_id
        self.ws = ws
        self.username = username
        self.display_name = display_name or username
        self.peer = peer
        self.connected_at = time.time()
        self.alive = True
        self._send_lock = threading.Lock()

    def send(self, payload: dict[str, Any]) -> bool:
        """
        给这条连接发一条 JSON 文本帧。

        :param payload: 事件字典
        :return: True 表示发送成功；False 表示连接已死（调用方应把它摘掉）
        """
        if not self.alive:
            return False
        data = json.dumps(payload, ensure_ascii=False)
        with self._send_lock:
            try:
                self.ws.send(data)
                return True
            except Exception as exc:                      # noqa: BLE001 - 断线方式五花八门
                self.alive = False
                logger.debug("实时连接 #%s 发送失败，标记为断开：%s", self.id, exc)
                return False

    def close(self, reason: int = 1000, message: str = "") -> None:
        """主动关闭连接（幂等）。"""
        self.alive = False
        try:
            self.ws.close(reason, message)
        except Exception:                                 # noqa: BLE001
            pass

    def describe(self) -> dict[str, Any]:
        """给 presence 用的公开信息（只有代号，不带真名）。"""
        return {
            "client_id": self.id,
            "username": self.username,
            "connected_at": self.connected_at,
            "peer": self.peer,
        }


class RealtimeHub:
    """连接注册表 + 广播器（进程内单例，线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._clients: dict[int, Client] = {}
        self._next_id = 1
        self.broadcasts = 0          # 累计广播次数（健康检查里能看到）

    # ---------------- 连接管理 ----------------
    def register(self, ws: Any, username: str, display_name: str = "",
                 peer: str = "") -> Client:
        """登记一条新连接并返回它的句柄。"""
        with self._lock:
            client = Client(self._next_id, ws, username, display_name, peer)
            self._next_id += 1
            self._clients[client.id] = client
        logger.info("实时连接已建立：#%s（用户 %s，对端 %s），当前 %d 条",
                    client.id, username, peer or "?", self.count)
        return client

    def unregister(self, client: Client) -> None:
        """摘掉一条连接（幂等）。"""
        with self._lock:
            removed = self._clients.pop(client.id, None)
        if removed is not None:
            removed.alive = False
            logger.info("实时连接已断开：#%s（用户 %s），当前 %d 条",
                        client.id, client.username, self.count)

    @property
    def count(self) -> int:
        """当前连接数。"""
        with self._lock:
            return len(self._clients)

    def clients(self) -> list[Client]:
        """当前所有连接（快照，可安全遍历）。"""
        with self._lock:
            return list(self._clients.values())

    # ---------------- 发送 ----------------
    def send_to(self, client: Client, payload: dict[str, Any]) -> bool:
        """给单条连接发事件，失败时顺手摘掉它。"""
        if client.send(payload):
            return True
        self.unregister(client)
        return False

    def broadcast(self, payload: dict[str, Any], *, exclude: Optional[Client] = None) -> int:
        """
        给所有连接广播一条事件。

        :param payload: 事件字典
        :param exclude: 不需要收到这条事件的连接（例如动作发起者自己）
        :return: 成功送达的连接数
        """
        with self._lock:
            targets = [c for c in self._clients.values() if c is not exclude]

        delivered = 0
        dead: list[Client] = []
        for client in targets:
            if client.send(payload):
                delivered += 1
            else:
                dead.append(client)

        for client in dead:
            self.unregister(client)

        self.broadcasts += 1
        return delivered

    # ---------------- 在线名单 ----------------
    def online_users(self) -> list[dict[str, Any]]:
        """
        汇总在线用户（同一个人开多个页面只算一个，记下连接数）。

        :return: [{"username": 代号, "connections": n, "since": 最早连接时间}]
        """
        users: dict[str, dict[str, Any]] = {}
        for client in self.clients():
            item = users.setdefault(client.username, {
                "username": client.username,
                "connections": 0,
                "since": client.connected_at,
            })
            item["connections"] += 1
            item["since"] = min(item["since"], client.connected_at)
        return sorted(users.values(), key=lambda item: (item["since"], item["username"]))

    def presence_payload(self) -> dict[str, Any]:
        """在线状态事件（真名由前端翻译，这里只给代号）。"""
        users = self.online_users()
        return {
            "type": "presence",
            "count": sum(item["connections"] for item in users),
            "users": users,
            "online": len(users),
        }

    def broadcast_presence(self) -> int:
        """把最新在线名单广播给所有人。"""
        return self.broadcast(self.presence_payload())

    # ---------------- 收客户端消息 ----------------
    def handle_message(self, client: Client, raw: Any) -> None:
        """
        处理客户端发来的一帧。

        只认两类消息：``ping``（回 pong）与 ``whoami``（重发 hello）。
        其它一律忽略并回一条 error —— 服务端不通过这条通道接收任何写操作，
        所有写操作仍然走带权限校验的 HTTP 接口。
        """
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")

        try:
            message = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            self.send_to(client, {"type": "error", "message": "消息不是合法 JSON"})
            return

        if not isinstance(message, dict):
            self.send_to(client, {"type": "error", "message": "消息必须是 JSON 对象"})
            return

        kind = message.get("type")
        if kind == "ping":
            self.send_to(client, {"type": "pong", "at": time.time()})
        elif kind == "whoami":
            self.send_to(client, self.hello_payload(client))
        else:
            self.send_to(client, {"type": "error", "message": f"不支持的消息类型：{kind!r}"})

    @staticmethod
    def hello_payload(client: Client) -> dict[str, Any]:
        """握手成功后的第一条事件。"""
        return {
            "type": "hello",
            "protocol": PROTOCOL_VERSION,
            "you": client.describe(),
            "server_time": time.time(),
        }

    # ---------------- 观测 ----------------
    def stats(self) -> dict[str, Any]:
        """给 /api/health 用的统计信息。"""
        users = self.online_users()
        return {
            "available": WEBSOCKET_AVAILABLE,
            "protocol": PROTOCOL_VERSION,
            "connections": self.count,
            "online_users": [item["username"] for item in users],
            "broadcasts": self.broadcasts,
        }


# 进程内单例：HTTP 线程、同步线程都往这里广播
HUB = RealtimeHub()


def notify_tasks_changed(action: str, *, issue: Optional[int] = None,
                         by: str = "", detail: str = "",
                         origin: Optional[Client] = None) -> int:
    """
    广播「任务有变化」事件。

    前端收到后会重新拉一次 /api/tasks，所以这里**不需要**带完整任务数据。

    :param action: 动作名（create_task / update_status / ...），前端据此写提示语
    :param issue: 相关 Issue 编号；批量操作可以为 None
    :param by: 操作者**代号**（真名由前端翻译，不进事件体）
    :param detail: 简短说明（如「状态=doing」）
    :param origin: 发起这次操作的连接（如果就是他自己，可以不重复通知）
    :return: 成功送达的连接数
    """
    return HUB.broadcast({
        "type": "tasks_changed",
        "action": action,
        "issue": issue,
        "by": by,
        "detail": detail,
        "at": time.time(),
    }, exclude=origin)
