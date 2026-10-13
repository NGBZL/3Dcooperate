"""WebSocket 实时通道自检（离线，不需要 Token，不动真库、不写 GitHub）。

做的是一个**真的**端到端验证：
  1. 用 kanban.db 的副本 + 临时目录起一个真实的 Werkzeug 服务器（线程里）
  2. 用 simple_websocket.Client 真连 `ws://127.0.0.1:<port>/ws`，带登录 Cookie
  3. 断言：未登录连不上、连上先收到 hello、presence 里有自己
  4. 通过 HTTP 改一张卡片，断言**另一个连接**收到了 tasks_changed（且事件里只有代号）
  5. 断言：客户端断开后在线名单会更新；ping 能收到 pong；未知消息回 error
  6. 断言：/api/health 里能看到实时通道状态

用法：py -3.14 tools/test_ws.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

WORK = BASE / "_wstest_tmp"
shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True, exist_ok=True)

# 用副本库，绝不动真数据
COPY = WORK / "kanban.db"
shutil.copy2(BASE / "kanban.db", COPY)

os.environ["DB_PATH"] = str(COPY)
os.environ["LOG_PATH"] = str(WORK / "ws.log")
os.environ["SECRET_KEY"] = "ws-test-secret"

from werkzeug.serving import make_server  # noqa: E402

import requests  # noqa: E402
import simple_websocket  # noqa: E402
import server  # noqa: E402

PORT = 5071
BASE_URL = f"http://127.0.0.1:{PORT}"
WS_URL = f"ws://127.0.0.1:{PORT}/ws"

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    """记一条断言结果。"""
    if ok:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}" + (f"  -> {extra}" if extra else ""))


def recv_json(client, timeout: float = 5.0):
    """收一条 JSON 消息。"""
    try:
        raw = client.receive(timeout=timeout)
    except Exception:                                    # noqa: BLE001 - 连接已断
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def recv_until(client, kind: str, timeout: float = 5.0):
    """一直收，直到拿到指定类型的事件（其它事件直接跳过）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        message = recv_json(client, timeout=max(0.2, deadline - time.time()))
        if message and message.get("type") == kind:
            return message
    return None


def prepare_user() -> tuple[str, str, int]:
    """
    在副本库里准备一个测试用户 + 一张属于他的任务，并给它设个测试密码。

    **不依赖真库里的现有数据**：真库可能刚换过任务仓库、缓存是空的，
    所以这里自己造夹具（Issue 编号用 9901 这种不会和真任务撞车的号）。

    :return: (代号, 登录用的真名, 测试任务编号)
    """
    from werkzeug.security import generate_password_hash

    from config import assignee_display
    import database

    conn = sqlite3.connect(COPY)
    row = conn.execute("SELECT username FROM users ORDER BY id LIMIT 1").fetchone()
    code = row[0] if row else None
    if not code:
        raise SystemExit("副本库里没有用户，无法测试")

    conn.execute("UPDATE users SET password_hash = ? WHERE username = ?",
                 (generate_password_hash("ws-test-1234"), code))
    conn.commit()
    conn.close()

    issue = 9901
    database.upsert_task(issue, "实时通道自检任务", "", "todo",
                         assignee=code, html_url="", state="open")
    return code, (assignee_display(code) or code), issue


httpd = make_server("127.0.0.1", PORT, server.app, threaded=True)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.6)

print("=" * 68)
print("[0] 环境")
print("=" * 68)
check("simple-websocket 可用（服务端能开 WS）", server.realtime.WEBSOCKET_AVAILABLE)

# 打桩：本用例只验证实时通道，**绝不真的写 GitHub**
# （以前这里会真的改真仓库的 Issue 标签，换仓库后编号还可能指向不存在的 Issue）
import github_client  # noqa: E402

github_client.ensure_status_labels = lambda: []
github_client.set_issue_status = lambda number, status: {"number": number}
github_client.set_issue_assignee = lambda number, assignee, description=None: {"number": number}

code, login_name, test_issue = prepare_user()
check("副本库准备完成（造了测试用户 + 测试任务并设了密码）", bool(code))

session = requests.Session()
resp = session.post(f"{BASE_URL}/login",
                    data={"username": login_name, "password": "ws-test-1234"},
                    allow_redirects=False)
check("HTTP 登录成功（302）", resp.status_code == 302, str(resp.status_code))
cookie = "; ".join(f"{k}={v}" for k, v in session.cookies.items())

ws1 = None
ws2 = None
try:
    print()
    print("=" * 68)
    print("[1] 未登录不能连实时通道")
    print("=" * 68)
    rejected = False
    detail = ""
    try:
        bad = simple_websocket.Client(WS_URL)
        try:
            message = bad.receive(timeout=2)
            # 没抛异常但也没拿到 hello，同样算被拒绝
            rejected = message is None
            detail = str(message)[:60]
        finally:
            bad.close()
    except Exception as exc:                             # noqa: BLE001 - 服务端拒绝握手
        rejected = True
        detail = type(exc).__name__
    check("未登录时 /ws 被拒绝（握手失败 / 立即断开）", rejected, detail)

    print()
    print("=" * 68)
    print("[2] 登录后握手 + hello + presence")
    print("=" * 68)
    ws1 = simple_websocket.Client(WS_URL, headers={"Cookie": cookie})
    hello = recv_until(ws1, "hello", timeout=5)
    check("连上后收到 hello", hello is not None)
    check("hello 带协议版本", bool(hello and hello.get("protocol")))
    check("hello 里的身份是代号（事件体里不带真名）",
          bool(hello) and hello["you"]["username"] == code,
          str(hello.get("you") if hello else None))

    presence = recv_until(ws1, "presence", timeout=5)
    check("收到 presence（在线名单）", presence is not None)
    check("在线名单里有自己",
          bool(presence) and any(u["username"] == code for u in presence.get("users", [])),
          str(presence))

    print()
    print("=" * 68)
    print("[3] 第二条连接上线 → 第一条收到更新后的 presence")
    print("=" * 68)
    ws2 = simple_websocket.Client(WS_URL, headers={"Cookie": cookie})
    recv_until(ws2, "hello", timeout=5)
    presence2 = recv_until(ws1, "presence", timeout=6)
    check("ws1 看到连接数变成 2",
          bool(presence2) and (presence2.get("count") or 0) >= 2, str(presence2))

    print()
    print("=" * 68)
    print("[4] 通过 HTTP 改卡片 → 另一个连接收到 tasks_changed")
    print("=" * 68)
    tasks = session.get(f"{BASE_URL}/api/tasks").json()
    visible = [t for t in tasks if not t.get("hidden")]
    check("拿到卡片列表", bool(visible))

    target = next((t for t in visible if t.get("issue_number") == test_issue), None)
    check("找到自己造的测试任务", target is not None,
          f"测试任务 #{test_issue} 不在列表里：{[t.get('issue_number') for t in visible]}")

    if target:
        new_status = "doing" if target.get("status") != "doing" else "todo"
        resp = session.put(f"{BASE_URL}/api/tasks/{target['issue_number']}",
                           json={"status": new_status})
        check(f"HTTP 改状态成功（#{target['issue_number']} → {new_status}）",
              resp.status_code == 200, f"{resp.status_code} {resp.text[:80]}")

        event = recv_until(ws2, "tasks_changed", timeout=6)
        check("ws2 收到 tasks_changed", event is not None)
        check("事件里带动作名", bool(event) and event.get("action") == "update_task",
              str(event))
        check("事件里带 Issue 编号",
              bool(event) and event.get("issue") == target["issue_number"], str(event))
        check("事件里只有代号（不带真名）",
              bool(event) and event.get("by") == code, str(event))
        check("事件带 detail 说明改了什么",
              bool(event) and "状态" in (event.get("detail") or ""), str(event))

    print()
    print("=" * 68)
    print("[5] 保活、非法消息、断开回收")
    print("=" * 68)
    ws1.send(json.dumps({"type": "ping"}))
    check("客户端 ping 能收到 pong", recv_until(ws1, "pong", timeout=5) is not None)

    ws1.send(json.dumps({"type": "不存在的类型"}))
    check("未知消息类型回 error（WS 通道不接受写操作）",
          recv_until(ws1, "error", timeout=5) is not None)

    ws2.close()
    ws2 = None
    left = recv_until(ws1, "presence", timeout=8)
    check("ws2 断开后在线连接数回到 1",
          bool(left) and (left.get("count") or 0) == 1, str(left))

    print()
    print("=" * 68)
    print("[6] 健康检查里能看到实时通道状态")
    print("=" * 68)
    health = session.get(f"{BASE_URL}/api/health").json()
    rt = health.get("realtime") or {}
    check("健康检查含 realtime 字段", bool(rt), str(health.get("realtime")))
    check("realtime.available = True", rt.get("available") is True, str(rt))
    check("realtime.connections 至少 1", (rt.get("connections") or 0) >= 1, str(rt))
    check("realtime 里记录了自己的代号", code in (rt.get("online_users") or []), str(rt))
    check("至少广播过事件", (rt.get("broadcasts") or 0) >= 1, str(rt))
finally:
    for client in (ws1, ws2):
        if client is not None:
            try:
                client.close()
            except Exception:                            # noqa: BLE001
                pass
    httpd.shutdown()

print()
print("=" * 68)
if FAILED:
    print(f"❌ WebSocket 自检未通过，失败 {len(FAILED)} 项：")
    for item in FAILED:
        print("   -", item)
else:
    print(f"✅ WebSocket 实时通道自检全部通过（{len(PASSED)} 项）")
print("=" * 68)

shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILED else 0)
