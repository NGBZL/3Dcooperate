#!/usr/bin/env python3
"""
后端安全与权限自检（离线，不需要真实 GitHub）
------------------------------------------------
覆盖：

1. **每个接口的登录保护**（结构性检查：遍历所有 /api/* 路由，未登录必须 401）
   —— 这条是为一次真实事故加的：`POST /api/tasks` 曾经漏了登录装饰器，
   未登录也能在仓库里创建 Issue
2. 登录：只能用真实姓名；密码错误被拒
3. 权限：自己的 / 别人的 / 未分配 任务分别能不能操作
4. 防「假完成」：不能手动设 done，且不会打到 GitHub
5. 负责人规则：不能把任务指派给别人，只能认领给自己或放回未分配
6. 「从看板移除」：移除后同步不会拉回来，可恢复
7. 同步的增量行为：没有变化就不写库（避免每次同步都刷 updated_at）

用法：
    python tools/test_auth.py

所有 GitHub 写操作都被替换成假的记录函数，**不会碰真实仓库**。
退出码：0 = 全部通过；1 = 有失败
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# 必须在导入 server 之前把环境准备好。
# 注意：测试库放在项目目录下（不是系统临时目录），避免受文件沙箱限制。
WORKDIR = BASE / "_selftest_tmp"
shutil.rmtree(WORKDIR, ignore_errors=True)
WORKDIR.mkdir(parents=True, exist_ok=True)
os.environ["DB_PATH"] = str(WORKDIR / "board.db")
os.environ["LOG_PATH"] = str(WORKDIR / "board.log")
os.environ["SECRET_KEY"] = "selftest-secret"
os.environ["DEBUG"] = "false"
os.environ["GITHUB_TOKEN"] = os.environ.get("GITHUB_TOKEN") or ("ghp_" + "0" * 36)
os.environ["GITHUB_REPO"] = os.environ.get("GITHUB_REPO") or "demo/repo"

import auth  # noqa: E402
import database  # noqa: E402
import github_client  # noqa: E402
import server  # noqa: E402
from config import assignee_display  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FAILURES: list[str] = []
GH_CALLS: list[tuple] = []


def check(name: str, ok: bool, extra: object = "") -> None:
    """打印一条断言结果。"""
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  -> {extra}" if not ok and extra != "" else ""))
    if not ok:
        FAILURES.append(name)


# ---------------------------------------------------------------
# 打桩：绝不真的写 GitHub
# ---------------------------------------------------------------
github_client.ensure_status_labels = lambda: []
github_client.set_issue_status = lambda n, s: (GH_CALLS.append(("status", n, s)), {"number": n})[1]
github_client.set_issue_assignee = lambda n, a, description=None: (
    GH_CALLS.append(("assignee", n, a)), {"number": n})[1]
github_client.create_issue = lambda title, body="", labels=None: (
    GH_CALLS.append(("create", title)),
    {"number": 900 + len([c for c in GH_CALLS if c[0] == "create"]),
     "title": title, "state": "open"})[1]
github_client.validate_token = lambda: {"login": "demo", "repo": "demo/repo", "private": False}

database.init_db()
auth.ensure_default_users()

ME, OTHER = "小美", "小明"          # 代号
# 登录要用真名，但真名只存在本地 assignees.local.json 里，**不能写进代码**，
# 所以这里从本地映射表现取；没有映射表时退回代号（那种情况下代号也能登录）。
ME_NAME = assignee_display(ME) or ME
OTHER_NAME = assignee_display(OTHER) or OTHER

client = server.app.test_client()
client.post("/login", data={"username": ME_NAME, "password": "123456"})
database.upsert_task(1, "我的任务", "", "doing", assignee=ME)
database.upsert_task(2, "别人的任务", "", "doing", assignee=OTHER)
database.upsert_task(3, "没人认领", "", "todo", assignee="")

print("[1] 每个 /api/* 接口都必须要求登录")
print("=" * 68)
anon = server.app.test_client()

# 故意允许匿名访问的接口（登录页需要显示访问地址）
PUBLIC_ENDPOINTS = {"server_info", "static", "login_page", "logout"}

for rule in sorted(server.app.url_map.iter_rules(), key=lambda r: str(r)):
    path = str(rule)
    if not path.startswith("/api/"):
        continue
    if rule.endpoint in PUBLIC_ENDPOINTS:
        check(f"{path} 允许匿名（有意为之）",
              anon.get(path.replace("<int:issue_number>", "1")).status_code != 401)
        continue

    method = next((m for m in ("GET", "POST", "PUT", "DELETE")
                   if m in rule.methods and m not in ("HEAD", "OPTIONS")), "GET")
    url = path.replace("<int:issue_number>", "1")
    resp = anon.open(url, method=method, json={})
    check(f"{method:6} {path:40} 未登录 → 401", resp.status_code == 401,
          f"实际 {resp.status_code}")

print()
print("[2] 登录")
print("=" * 68)
fresh = server.app.test_client()
check("用真名登录 → 302", fresh.post("/login", data={"username": ME_NAME, "password": "123456"}).status_code == 302)
fresh2 = server.app.test_client()
check("用代号登录 → 401（已按需求取消）",
      fresh2.post("/login", data={"username": ME, "password": "123456"}).status_code == 401)
fresh3 = server.app.test_client()
check("密码错误 → 401", fresh3.post("/login", data={"username": ME_NAME, "password": "x"}).status_code == 401)
auth._clear_failures(ME)
check("登录后 /api/me 可用", client.get("/api/me").status_code == 200)
check("/api/me 显示真名", client.get("/api/me").get_json()["display_name"] == ME_NAME)

print()
print("[3] 权限隔离")
print("=" * 68)
check("改自己的任务 → 200", client.put("/api/tasks/1", json={"status": "todo"}).status_code == 200)
check("改别人的任务 → 403", client.put("/api/tasks/2", json={"status": "todo"}).status_code == 403)
check("未认领就想改状态 → 403", client.put("/api/tasks/3", json={"status": "doing"}).status_code == 403)
check("认领未分配的任务 → 200", client.put("/api/tasks/3/assignee", json={"assignee": ME}).status_code == 200)
check("认领后可以改状态 → 200", client.put("/api/tasks/3", json={"status": "doing"}).status_code == 200)
check("删别人的任务 → 403", client.delete("/api/tasks/2").status_code == 403)

print()
print("[4] 防「假完成」")
print("=" * 68)
before_calls = len(GH_CALLS)
check("手动设 done → 403", client.put("/api/tasks/1", json={"status": "done"}).status_code == 403)
check("被拒时没有调用 GitHub", len(GH_CALLS) == before_calls, GH_CALLS[before_calls:])
check("数据库状态没变", database.get_task_by_issue(1)["status"] == "todo")
check("新建时直接选 done → 403",
      client.post("/api/tasks", json={"title": "x", "status": "done"}).status_code == 403)

print()
print("[4b] 已完成的任务不能再被移出去（Done 是终态）")
print("=" * 68)
# 造一张「已完成」的任务：用同步写入（Agent 关 Issue 的路径），而不是 API
github_client.fetch_all_issues = lambda state="all", max_pages=5: [
    {"number": 2, "title": "别人已完成的", "body": f"**负责人**: {OTHER}", "state": "closed",
     "html_url": "", "labels": [{"name": "done"}], "comments": 0},
]
client.post("/api/sync?with_meta=0&with_reviews=0")
done_task = database.get_task_by_issue(2)
check("同步可以把任务写进 done（Agent 路径不受影响）",
      done_task is not None and done_task["status"] == "done",
      done_task["status"] if done_task else "无此任务")

if done_task and done_task["status"] == "done":
    before_calls = len(GH_CALLS)
    resp = client.put("/api/tasks/2", json={"status": "doing"})
    check("把已完成的任务改回 doing → 403", resp.status_code == 403, resp.status_code)
    check("被拒时没有调用 GitHub", len(GH_CALLS) == before_calls, GH_CALLS[before_calls:])
    check("数据库里仍然是 done", database.get_task_by_issue(2)["status"] == "done")
    body = resp.get_json() or {}
    check("提示里说明了原因", "完成" in str(body.get("error", "")), body.get("error"))

    # 把这张卡改成自己名下，确认「本人也不能拖出去」（否则等于绕过审核）
    database.upsert_task(issue_number=2, title="别人已完成的", description="",
                         status="done", assignee=ME, html_url="", state="closed")
    resp = client.put("/api/tasks/2", json={"status": "todo"})
    check("负责人本人把已完成改回 todo → 也被拒（403）", resp.status_code == 403, resp.status_code)
    check("数据库里依然是 done", database.get_task_by_issue(2)["status"] == "done")

    # 改负责人不属于「移出 Done」，不应该被这条规则挡住（未分配任务谁都能认领）
    database.upsert_task(issue_number=2, title="别人已完成的", description="",
                         status="done", assignee="", html_url="", state="closed")
    resp = client.put("/api/tasks/2/assignee", json={"assignee": ME})
    check("已完成的任务仍可认领（改负责人不算移出 Done）", resp.status_code == 200, resp.status_code)

# 收尾：上面的同步只返回了 #2，会把 #1/#3 剪掉，这里把夹具恢复原样，别影响后面的用例
github_client.fetch_all_issues = lambda state="all", max_pages=5: [
    {"number": 1, "title": "我的任务", "body": f"**负责人**: {ME}", "state": "open",
     "html_url": "", "labels": [{"name": "todo"}], "comments": 0},
    {"number": 2, "title": "别人的任务", "body": f"**负责人**: {OTHER}", "state": "open",
     "html_url": "", "labels": [{"name": "doing"}], "comments": 0},
    {"number": 3, "title": "没人认领", "body": f"**负责人**: {ME}", "state": "open",
     "html_url": "", "labels": [{"name": "doing"}], "comments": 0},
]
database.upsert_task(1, "我的任务", "", "doing", assignee=ME)
database.upsert_task(2, "别人的任务", "", "doing", assignee=OTHER)
database.upsert_task(3, "没人认领", "", "todo", assignee="")
database.hide_task(2, False)

print()
print("[5] 负责人：不能指派给别人")
print("=" * 68)
check("把自己的任务转给别人 → 403",
      client.put("/api/tasks/1/assignee", json={"assignee": OTHER}).status_code == 403)
check("把未分配的任务指派给别人 → 403",
      client.put("/api/tasks/3/assignee", json={"assignee": OTHER}).status_code == 403)
check("把自己名下的放回未分配 → 200",
      client.put("/api/tasks/1/assignee", json={"assignee": ""}).status_code == 200)
check("替别人建任务 → 403",
      client.post("/api/tasks", json={"title": "x", "assignee": OTHER}).status_code == 403)

print()
print("[5b] 未分配的任务：谁都能「拖进自己的区域」= 认领并推进")
print("=" * 68)
database.upsert_task(4, "未分配的第二个任务", "", "todo", assignee="")
check("未分配 + 只改状态（不带 assignee）→ 403",
      client.put("/api/tasks/4", json={"status": "doing"}).status_code == 403)
check("未分配 + 想把负责人写成别人 → 403",
      client.put("/api/tasks/4", json={"assignee": OTHER, "status": "doing"}).status_code == 403)
check("未分配 + 认领给自己 + 改状态 → 200（一次请求完成认领与推进）",
      client.put("/api/tasks/4", json={"assignee": ME, "status": "doing"}).status_code == 200)
check("认领后负责人是自己", database.get_task_by_issue(4)["assignee"] == ME)
check("认领后状态已推进", database.get_task_by_issue(4)["status"] == "doing")
check("被拒的那次没有改库", database.get_task_by_issue(4)["status"] == "doing")

other_client = server.app.test_client()
other_client.post("/login", data={"username": OTHER_NAME, "password": "123456"})
check("认领之后别人再改状态 → 403",
      other_client.put("/api/tasks/4", json={"status": "todo"}).status_code == 403)
check("认领之后别人再抢负责人 → 403",
      other_client.put("/api/tasks/4/assignee", json={"assignee": OTHER}).status_code == 403)
check("本人仍然可以继续推进自己的任务 → 200",
      client.put("/api/tasks/4", json={"status": "todo"}).status_code == 200)

print()
print("[6] 「从看板移除」不会被同步拉回来")
print("=" * 68)
check("移除自己的任务 → 200", client.delete("/api/tasks/1").status_code == 200)
check("默认列表里看不到", all(t["issue_number"] != 1 for t in client.get("/api/tasks").get_json()))
check("带 include_hidden=1 能看到",
      any(t["issue_number"] == 1 for t in client.get("/api/tasks?include_hidden=1").get_json()))

github_client.fetch_all_issues = lambda state="all", max_pages=5: [
    {"number": 1, "title": "我的任务", "body": f"**负责人**: {ME}", "state": "open",
     "html_url": "", "labels": [{"name": "todo"}], "comments": 0},
]
client.post("/api/sync?with_meta=0&with_reviews=0")
check("同步后仍然是移除状态（不会被拉回）",
      all(t["issue_number"] != 1 for t in client.get("/api/tasks").get_json()))
check("恢复显示 → 200", client.post("/api/tasks/1/restore").status_code == 200)
check("恢复后能看到", any(t["issue_number"] == 1 for t in client.get("/api/tasks").get_json()))

print()
print("[7] 同步只写真正的变化")
print("=" * 68)
github_client.fetch_all_issues = lambda state="all", max_pages=5: [
    {"number": 1, "title": "我的任务", "body": f"**负责人**: {ME}", "state": "open",
     "html_url": "", "labels": [{"name": "todo"}], "comments": 0},
    {"number": 2, "title": "别人的任务", "body": f"**负责人**: {OTHER}", "state": "open",
     "html_url": "", "labels": [{"name": "doing"}], "comments": 0},
]
first = client.post("/api/sync?with_meta=0&with_reviews=0").get_json()
print(f"    第一次: 更新 {first['updated']}，未变化 {first['unchanged']}，变更 {len(first['changes'])} 条")
stamp_after_first = database.get_task_by_issue(2)["updated_at"]

second = client.post("/api/sync?with_meta=0&with_reviews=0").get_json()
print(f"    第二次: 更新 {second['updated']}，未变化 {second['unchanged']}")
check("第二次同步没有任何写库（更新=0）", second["updated"] == 0, second["updated"])
check("第二次同步报告未变化数", second["unchanged"] == 2, second["unchanged"])
check("updated_at 没有被刷新", database.get_task_by_issue(2)["updated_at"] == stamp_after_first)

# 远端把 #2 改成 done（模拟 Agent / 别人改）
github_client.fetch_all_issues = lambda state="all", max_pages=5: [
    {"number": 1, "title": "我的任务", "body": f"**负责人**: {ME}", "state": "open",
     "html_url": "", "labels": [{"name": "todo"}], "comments": 0},
    {"number": 2, "title": "别人的任务", "body": f"**负责人**: {OTHER}", "state": "closed",
     "html_url": "", "labels": [{"name": "done"}], "comments": 0},
]
third = client.post("/api/sync?with_meta=0&with_reviews=0").get_json()
print(f"    第三次（远端把 #2 改成 done）: 更新 {third['updated']}，"
      f"变更 {[item['detail'] for item in third['changes']]}")
check("检测到并应用了远端变化", third["updated"] == 1, third["updated"])
check("把变化说清楚了", any(item["issue"] == 2 for item in third["changes"]), third["changes"])
check("同步消息里提到被更新的卡片", "被 GitHub 更新的卡片" in third["message"], third["message"][:80])
check("本地状态跟着变成 done", database.get_task_by_issue(2)["status"] == "done")
logs = database.recent_logs(limit=50)
check("变更写进了操作日志",
      any(item["action"] == "sync_update" and item["issue_number"] == 2 for item in logs),
      [item["action"] for item in logs[:6]])

print()
print("[9] 换任务仓库时的缓存重建（证据优先：html_url 说了算）")
print("=" * 68)
# 造两张「属于 A 仓」的缓存（html_url 是硬证据）
database.upsert_task(801, "A 仓的任务一", "", "todo", assignee=ME,
                     html_url="https://github.com/ownerA/repoA/issues/801")
database.upsert_task(802, "A 仓的任务二", "", "doing", assignee=ME,
                     html_url="https://github.com/ownerA/repoA/issues/802")
check("能从 html_url 反推出缓存属于哪个仓库",
      database.infer_cached_repo() == "ownera/repoa", database.infer_cached_repo())

users_before = len(database.list_users())
logs_before = len(database.recent_logs(limit=999))

# ① 记录里的仓库与配置一致、证据也一致 → 不动数据
database.set_meta("repo", "ownerA/repoA")
same = database.bind_repo("ownerA/repoA")
check("同仓库启动：不清缓存", same["changed"] is False and same["cleared"] == 0, same)
check("同仓库启动：任务还在", database.get_task_by_issue(801) is not None)

# ② 记录被改坏了（说是别的仓库），但证据指向 A 仓 → 以证据为准，不清
database.set_meta("repo", "ownerB/repoB")
fixed = database.bind_repo("ownerA/repoA")
check("记录过时但证据一致：不清缓存", fixed["changed"] is False, fixed)
check("记录被修正回当前仓库", database.get_meta("repo") == "ownerA/repoA")

# ③ 换成 B 仓 → 清空任务，但用户与日志保留
switched = database.bind_repo("ownerB/repoB")
check("换仓库：报告为已重建", switched["changed"] is True, switched)
check("换仓库：任务被清空", database.count_tasks() == 0, database.count_tasks())
check("换仓库：用户没被动", len(database.list_users()) == users_before)
check("换仓库：操作日志没被动", len(database.recent_logs(limit=999)) == logs_before)
check("换仓库：记录已更新", database.get_meta("repo") == "ownerB/repoB")

# ④ 没有记录、又能推断出归属（A 仓），但配置是 B 仓 → 依然要清
database.upsert_task(803, "A 仓的任务三", "", "todo", assignee=ME,
                     html_url="https://github.com/ownerA/repoA/issues/803")
database.set_meta("repo", "")
rebuilt = database.bind_repo("ownerB/repoB")
check("没记录 + 证据指向别的仓库：清空并重建",
      rebuilt["changed"] is True and rebuilt["cleared"] == 1, rebuilt)

# ⑤ 没有记录、也没有证据（html_url 为空）、但缓存非空 → 无法确认归属，清掉
database.upsert_task(804, "没有 html_url 的老缓存", "", "todo", assignee=ME)
database.set_meta("repo", "")
unknown = database.bind_repo("ownerC/repoC")
check("无法确认归属：宁可从 GitHub 重拉（清空）",
      unknown["changed"] is True and unknown["cleared"] == 1, unknown)

print()
print("[10] 组长 vs 组员：分派权限")
print("=" * 68)
# 造一个组长账号（用第三个代号，避免和 ME/OTHER 冲突）
LEAD = "小刚"
LEAD_NAME = assignee_display(LEAD) or LEAD
database.set_user_role(ME, "member")
database.set_user_role(LEAD, "lead")

lead_client = server.app.test_client()
lead_client.post("/login", data={"username": LEAD_NAME, "password": "123456"})

database.upsert_task(901, "组员的任务", "", "todo", assignee=ME)
database.upsert_task(902, "组长的任务", "", "todo", assignee=LEAD)

check("组员自己：/api/me 的 allow_assign_to_others=False",
      client.get("/api/me").get_json()["allow_assign_to_others"] is False)
check("组长：/api/me 的 allow_assign_to_others=True",
      lead_client.get("/api/me").get_json()["allow_assign_to_others"] is True)
check("组长：is_lead / can_merge_pr 都是 True",
      lead_client.get("/api/me").get_json()["is_lead"] is True
      and lead_client.get("/api/me").get_json()["can_merge_pr"] is True)

check("组员把别人的任务派给别人 → 403",
      client.put("/api/tasks/902/assignee", json={"assignee": OTHER}).status_code == 403)
check("组长把别人的任务派给第三人 → 200",
      lead_client.put("/api/tasks/901/assignee", json={"assignee": OTHER}).status_code == 200)
check("派完之后负责人确实变了", database.get_task_by_issue(901)["assignee"] == OTHER)
check("组长可以把任务收回给自己 → 200",
      lead_client.put("/api/tasks/901/assignee", json={"assignee": LEAD}).status_code == 200)
check("组长可以推进别人的状态 → 200",
      lead_client.put("/api/tasks/901", json={"status": "doing"}).status_code == 200)
check("组长可以替别人建任务（直接派活）→ 201",
      lead_client.post("/api/tasks", json={"title": "组长派的活", "assignee": ME}).status_code == 201)
check("组员替别人建任务仍然 → 403",
      client.post("/api/tasks", json={"title": "x", "assignee": OTHER}).status_code == 403)
check("组长拿到的卡片 can_edit 全为真（任何卡片都能拖）",
      all(t.get("can_edit") for t in lead_client.get("/api/tasks").get_json()
          if t["issue_number"] in (901, 902)))
check("组员仍然不能推进别人的任务 → 403",
      client.put("/api/tasks/902", json={"status": "doing"}).status_code == 403)

print()
print("[11] 分支 / PR：谁能做、组长放行与合并")
print("=" * 68)
github_client.create_branch = lambda branch, from_branch="main": {
    "name": branch, "created": True, "sha": "0" * 40}
github_client.create_pull_request = lambda head, base="main", title="", body="", draft=False: {
    "number": 77, "state": "open", "title": title, "url": "https://example/pr/77",
    "head": head, "base": base, "merged": False, "draft": False, "created": True}
github_client.post_issue_comment = lambda number, body: {"id": 1}
github_client.merge_pull_request = lambda number, method="squash": {
    "merged": True, "message": "Pull Request successfully merged", "sha": "abc"}
# 合并前会现读一次 Issue 评论里的最新审核结论；这里给一个「已过审」的桩
github_client.get_issue_comments = lambda number: [{"body": "占位", "id": 1}]
github_client.parse_review_from_comments = lambda comments: {
    "passed": True, "score": 90, "comment": "实现没问题", "text": "报告"}
# 合并后的收尾（标 done + 关 Issue）也是看板写的，同样打桩
github_client.set_issue_status = lambda number, status: {"number": number}
github_client.update_issue = lambda number, **kwargs: {"number": number}

database.upsert_task(903, "带 PR 的任务", "", "doing", assignee=ME)
database.upsert_task(904, "别人的 PR 任务", "", "doing", assignee=OTHER)

check("组员给自己的任务建分支 → 200",
      client.post("/api/tasks/903/branch", json={"name": "task/903-fix"}).status_code == 200)
check("分支记到了卡片上", database.get_task_by_issue(903)["branch"] == "task/903-fix")
check("组员给别人的任务建分支 → 403",
      client.post("/api/tasks/904/branch", json={}).status_code == 403)
check("组长给别人的任务建分支 → 200",
      lead_client.post("/api/tasks/904/branch", json={}).status_code == 200)

check("组员给自己的任务提 PR → 200",
      client.post("/api/tasks/903/pr", json={"head": "task/903-fix"}).status_code == 200)
pr_task = database.get_task_by_issue(903)
check("PR 编号/状态记到了卡片上",
      pr_task["pr_number"] == 77 and pr_task["pr_state"] == "open", pr_task["pr_state"])
check("新 PR 默认「未放行」", pr_task["pr_allowed"] is False)
check("组员不能合并 → 403",
      client.post("/api/tasks/903/pr/merge", json={}).status_code == 403)
check("组员也不能放行 → 403",
      client.post("/api/tasks/903/pr/allow", json={}).status_code == 403)
check("组长没放行就合并 → 403（必须先放行）",
      lead_client.post("/api/tasks/903/pr/merge", json={}).status_code == 403)
check("组长放行 → 200", lead_client.post("/api/tasks/903/pr/allow", json={}).status_code == 200)
check("放行状态记下来了，且记了是谁放的",
      database.get_task_by_issue(903)["pr_allowed"] is True
      and database.get_task_by_issue(903)["pr_allowed_by"] == LEAD)
check("放行后组长合并 → 200",
      lead_client.post("/api/tasks/903/pr/merge", json={}).status_code == 200)
check("合并后 PR 状态是 merged",
      database.get_task_by_issue(903)["pr_state"] == "merged")

print()
print("[12] 合并闸门：Agent 过审 + 组长放行，两条都满足才合并；合并后自动进 Done")
print("=" * 68)
github_client.create_branch = lambda branch, from_branch="main": {
    "name": branch, "created": True, "sha": "0" * 40}
github_client.create_pull_request = lambda head, base="main", title="", body="", draft=False: {
    "number": 88, "state": "open", "title": title, "url": "https://example/pr/88",
    "head": head, "base": base, "merged": False, "draft": False, "created": True}
github_client.post_issue_comment = lambda number, body: {"id": 1}
github_client.merge_pull_request = lambda number, method="squash": {
    "merged": True, "message": "ok", "sha": "abc"}

# 这两步是「合并后收尾」：看板自己把 Issue 标 done 并关闭
finalize_calls: list[tuple] = []
github_client.set_issue_status = lambda number, status: (
    finalize_calls.append(("label", number, status)) or {"number": number})
github_client.update_issue = lambda number, **kw: (
    finalize_calls.append(("state", number, kw.get("state"))) or {"number": number})

# 审核报告的解析结果由这个桩控制（模拟 Issue 评论里最新的 Agent 报告）
verdict = {"value": None}
github_client.get_issue_comments = lambda number: [{"body": "占位", "id": 1}]
github_client.parse_review_from_comments = lambda comments: verdict["value"]

database.upsert_task(905, "待合并的任务", "", "doing", assignee=ME)
lead_client.post("/api/tasks/905/branch", json={"name": "task/905-fix"})
client.post("/api/tasks/905/pr", json={"head": "task/905-fix"})

verdict["value"] = None
check("还没有 Agent 报告时：组长合并 → 403",
      lead_client.post("/api/tasks/905/pr/merge", json={}).status_code == 403)
check("先放行", lead_client.post("/api/tasks/905/pr/allow", json={}).status_code == 200)
check("放行了但还没有 Agent 报告：合并仍然 → 403",
      lead_client.post("/api/tasks/905/pr/merge", json={}).status_code == 403)

verdict["value"] = {"passed": False, "score": 40, "comment": "有内存泄漏", "text": "报告"}
check("Agent 没过审：合并不了 → 403",
      lead_client.post("/api/tasks/905/pr/merge", json={}).status_code == 403)
check("提示里说清了是审核没过",
      "审核" in (lead_client.post("/api/tasks/905/pr/merge", json={}).get_json() or {}).get("error", ""))
check("组员依然不能合并 → 403",
      client.post("/api/tasks/905/pr/merge", json={}).status_code == 403)

verdict["value"] = {"passed": True, "score": 92, "comment": "没问题", "text": "报告"}
check("Agent 过审 + 已放行：合并 → 200",
      lead_client.post("/api/tasks/905/pr/merge", json={}).status_code == 200)
check("合并后 PR 状态是 merged",
      database.get_task_by_issue(905)["pr_state"] == "merged")
check("合并后看板自己把 Issue 标成 done 标签",
      ("label", 905, "done") in finalize_calls, finalize_calls)
check("合并后看板自己关闭了 Issue",
      ("state", 905, "closed") in finalize_calls, finalize_calls)
check("合并后本地卡片状态就是 done（不用等下次同步）",
      database.get_task_by_issue(905)["status"] == "done", database.get_task_by_issue(905)["status"])

print()
print("[13] 补收尾接口（已经合过、但还赖在 Doing 的卡片）")
database.upsert_task(906, "早就合过但没收尾", "", "doing", assignee=ME)
database.update_task_pr(906, pr_number=99, pr_state="merged", pr_head="task/906")
finalize_calls.clear()
check("组员不能补收尾 → 403",
      client.post("/api/tasks/906/pr/finalize", json={}).status_code == 403)
check("组长补收尾 → 200",
      lead_client.post("/api/tasks/906/pr/finalize", json={}).status_code == 200)
check("补收尾确实写了 done 标签与 closed",
      ("label", 906, "done") in finalize_calls and ("state", 906, "closed") in finalize_calls,
      finalize_calls)
check("补收尾后本地状态是 done", database.get_task_by_issue(906)["status"] == "done")

print()
print("[14] Issue 关闭即 Done（标签说什么都不重要）")
check("已关闭 + 还挂着 doing 标签 → done",
      github_client.status_from_labels({"state": "closed", "labels": [{"name": "doing"}]}) == "done")
check("已关闭 + 还挂着 todo 标签 → done",
      github_client.status_from_labels({"state": "closed", "labels": [{"name": "todo"}]}) == "done")
check("开着 + doing 标签 → doing",
      github_client.status_from_labels({"state": "open", "labels": [{"name": "doing"}]}) == "doing")
check("开着 + 没标签 → todo",
      github_client.status_from_labels({"state": "open", "labels": []}) == "todo")

print()
print("[15] 卡片上直接提交代码（纯文本编辑器）")
print("=" * 68)
commit_calls: list[dict] = []
github_client.commit_files = lambda branch, files, message, create_from="": (
    commit_calls.append({"branch": branch, "files": files, "message": message,
                         "create_from": create_from})
    or {"commit": "d" * 40, "url": "https://example.test/commit/" + "d" * 40,
        "branch": branch, "files": [f["path"] for f in files], "message": message}
)
github_client.get_file_content = lambda path, ref: {
    "path": path, "content": "print('hi')\n", "sha": "abc", "size": 12}

# 907：组员自己的任务；908：别人的任务
database.upsert_task(907, "要提交代码的任务", "", "doing", assignee=ME)
database.upsert_task(908, "别人的任务", "", "doing", assignee=OTHER)
database.update_task_pr(907, pr_number=None, pr_state="", pr_url="", pr_head="",
                        pr_allowed=0, pr_allowed_by="")

resp = client.post("/api/tasks/907/code", json={
    "files": [{"path": "recon/knn.py", "content": "def knn():\n    return 1\n"}],
    "message": "feat: 实现 KNN",
})
check("组员给自己的任务提交代码 → 200", resp.status_code == 200,
      f"HTTP {resp.status_code} {resp.get_data(as_text=True)[:160]}")
check("提交调用带了分支与文件",
      bool(commit_calls) and commit_calls[-1]["branch"] == "task/907"
      and commit_calls[-1]["files"][0]["path"] == "recon/knn.py",
      str(commit_calls[-1] if commit_calls else None))
check("提交信息自动补上了 Issue 编号（否则 Agent 不会审）",
      commit_calls and "(#907)" in commit_calls[-1]["message"], str(commit_calls[-1]))
check("分支不存在时从主干创建",
      commit_calls and commit_calls[-1]["create_from"] == "main", str(commit_calls[-1]))
check("提交后本地记下了分支", (database.get_task_by_issue(907) or {}).get("branch", "").startswith("task/907"))
check("提交后自动提了 PR（默认打开）",
      (database.get_task_by_issue(907) or {}).get("pr_number") == 88,
      str((database.get_task_by_issue(907) or {}).get("pr_number")))

check("组员给别人的任务提交代码 → 403",
      client.post("/api/tasks/908/code",
                  json={"files": [{"path": "a.py", "content": "x"}]}).status_code == 403)
check("组长可以替别人提交 → 200",
      lead_client.post("/api/tasks/908/code",
                       json={"files": [{"path": "a.py", "content": "x"}]}).status_code == 200)

check("路径带 .. → 400",
      client.post("/api/tasks/907/code",
                  json={"files": [{"path": "../evil.py", "content": "x"}]}).status_code == 400)
check("绝对路径 → 400",
      client.post("/api/tasks/907/code",
                  json={"files": [{"path": "/etc/passwd", "content": "x"}]}).status_code == 400)
check("空内容 → 400",
      client.post("/api/tasks/907/code",
                  json={"files": [{"path": "a.py", "content": "   "}]}).status_code == 400)
check("没有文件 → 400", client.post("/api/tasks/907/code", json={"files": []}).status_code == 400)
check("两个文件同名 → 400",
      client.post("/api/tasks/907/code", json={"files": [
          {"path": "a.py", "content": "1"}, {"path": "a.py", "content": "2"}]}).status_code == 400)
check("多文件一次提交：两个文件都在同一个 commit 里",
      client.post("/api/tasks/907/code", json={"files": [
          {"path": "a.py", "content": "1"}, {"path": "recon/b.py", "content": "2"}]}).status_code == 200
      and len(commit_calls[-1]["files"]) == 2, str(commit_calls[-1]))

# 大小上限（临时把上限调小，验证拦住 + 明确提示）
original_limit = server.CODE_MAX_FILE_BYTES
server.CODE_MAX_FILE_BYTES = 10
try:
    check("单文件超过上限 → 400",
          client.post("/api/tasks/907/code", json={
              "files": [{"path": "big.py", "content": "x" * 50}]}).status_code == 400)
finally:
    server.CODE_MAX_FILE_BYTES = original_limit

print()
print("[16] 把仓库里的文件读进编辑器")
resp = client.get("/api/tasks/907/file?path=recon/knn.py")
check("读文件 → 200", resp.status_code == 200, resp.get_data(as_text=True)[:120])
check("返回了文件内容", resp.get_json().get("content") == "print('hi')\n", str(resp.get_json())[:120])
check("路径非法 → 400", client.get("/api/tasks/907/file?path=../x").status_code == 400)
check("未登录不能读文件", server.app.test_client().get(
    "/api/tasks/907/file?path=a.py").status_code == 401)

print()
print("=" * 68)
print("清理测试数据")
shutil.rmtree(WORKDIR, ignore_errors=True)

if FAILURES:
    print(f"❌ 失败 {len(FAILURES)} 项：")
    for item in FAILURES:
        print("   -", item)
    sys.exit(1)
print("✅ 后端安全与权限自检全部通过")
