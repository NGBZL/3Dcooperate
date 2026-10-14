"""用 GitHub API 推送当前提交（git push 连不上 github.com 时的备用通道）。

背景：这台机器有时能访问 api.github.com、却连不上 github.com（git push 会报
`Connection was reset` / `Could not connect to server`）。这时可以用本脚本走
Git Data API 推送：blob → tree → commit → 更新 ref。

它会让远端提交与本地**完全一致**（同样的 tree、parent、author/committer、提交信息），
所以生成的 SHA 就是本地那个 SHA —— 推完 `git status` 显示与 origin 同步，不会有分叉。

用法::

    python tools/push_via_api.py                # 推送当前分支到同名远端分支
    python tools/push_via_api.py --branch main  # 指定分支
    python tools/push_via_api.py --dry-run      # 只报告会做什么，不写远端

前提：`.env` 里的 GITHUB_TOKEN 对该仓库有 Contents: Read and write 权限。
"""

from __future__ import annotations

import argparse
import base64
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402  (复用 .env 里的 GITHUB_TOKEN / 仓库地址)

API = config.GITHUB_API_BASE
TIMEOUT = 60
RETRIES = 4


def run_git(*args: str, binary: bool = False) -> Any:
    """
    执行 git 命令。

    :param args: git 子命令与参数
    :param binary: True 时返回原始字节（读 blob 用），否则返回 UTF-8 文本
    :return: 命令输出
    :raises RuntimeError: git 返回非 0
    """
    proc = subprocess.run(["git", "-c", "core.quotePath=false", *args],
                          capture_output=True, timeout=120)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
        raise RuntimeError(f"git {' '.join(args)} 失败：{detail}")
    if binary:
        return proc.stdout
    return proc.stdout.decode("utf-8", "replace")


def remote_repo() -> str:
    """
    从 origin 的 URL 推断 ``owner/repo``。

    :return: 形如 NGBZL/3Dcooperate
    """
    url = run_git("remote", "get-url", "origin").strip()
    match = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?$", url)
    if not match:
        raise RuntimeError(f"看不懂 origin 地址：{url}")
    return match.group(1)


def identity(line: str) -> dict[str, str]:
    """
    把 ``名字 <邮箱> 1750000000 +0800`` 拆成 GitHub API 要的结构。

    :param line: commit 对象里的 author/committer 行（去掉前缀后的内容）
    :return: {"name", "email", "date"}（date 为带时区的 ISO 8601）
    """
    match = re.match(r"^(.*) <(.*)> (\d+) ([+-]\d{4})$", line)
    if not match:
        raise RuntimeError(f"看不懂的身份行：{line}")
    name, email, stamp, tz = match.groups()
    offset = timedelta(hours=int(tz[:3]), minutes=int(tz[3:]) * (1 if tz[0] == "+" else -1))
    when = datetime.fromtimestamp(int(stamp), tz=timezone.utc).astimezone(timezone(offset))
    return {"name": name, "email": email, "date": when.isoformat()}


def main(argv: Optional[list[str]] = None) -> int:
    """
    程序入口。

    :param argv: 命令行参数（默认取 sys.argv）
    :return: 进程退出码
    """
    parser = argparse.ArgumentParser(description="用 GitHub API 推送当前分支（git 连不上时的备用通道）")
    parser.add_argument("--branch", default="", help="要推送的分支（默认当前分支）")
    parser.add_argument("--remote", default="origin", help="远端名（默认 origin）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写远端")
    args = parser.parse_args(argv)

    repo = remote_repo()
    branch = args.branch.strip() or run_git("rev-parse", "--abbrev-ref", "HEAD").strip()
    local_sha = run_git("rev-parse", "HEAD").strip()

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "kanban-api-push",
    })

    def call(method: str, endpoint: str, **kwargs: Any) -> requests.Response:
        """带重试的 API 调用（网络抖动时重来）。"""
        import time

        last: Optional[requests.Response] = None
        for attempt in range(1, RETRIES + 1):
            try:
                resp = session.request(method, f"{API}{endpoint}", timeout=TIMEOUT, **kwargs)
            except requests.RequestException as exc:
                print(f"    网络异常（{attempt}/{RETRIES}）：{type(exc).__name__}")
            else:
                if resp.status_code < 400:
                    return resp
                last = resp
                print(f"    HTTP {resp.status_code}（{attempt}/{RETRIES}）：{resp.text[:120]}")
            time.sleep(2 * attempt)
        if last is None:
            raise RuntimeError("API 一直连不上")
        return last

    raw = run_git("cat-file", "commit", local_sha)
    tree_sha = re.search(r"^tree ([0-9a-f]{40})$", raw, re.M).group(1)
    parent_match = re.search(r"^parent ([0-9a-f]{40})$", raw, re.M)
    parent_sha = parent_match.group(1) if parent_match else ""
    author = identity(re.search(r"^author (.+)$", raw, re.M).group(1))
    committer = identity(re.search(r"^committer (.+)$", raw, re.M).group(1))
    message = raw.split("\n\n", 1)[1]

    remote_ref = call("GET", f"/repos/{repo}/git/ref/heads/{branch}")
    remote_sha = remote_ref.json()["object"]["sha"] if remote_ref.status_code == 200 else ""

    print(f"仓库   ：{repo}")
    print(f"分支   ：{branch}")
    print(f"本地   ：{local_sha[:8]}")
    print(f"远端   ：{remote_sha[:8] if remote_sha else '(分支不存在)'}")
    if remote_sha == local_sha:
        print("✅ 远端已经和本地一致，不用推")
        return 0

    if parent_sha:
        parent = call("GET", f"/repos/{repo}/git/commits/{parent_sha}")
        if parent.status_code != 200:
            print(f"❌ 远端没有父提交 {parent_sha[:8]}：先把历史推上去再试")
            return 2
        base_tree = parent.json()["tree"]["sha"]
    else:
        base_tree = ""

    if remote_sha and parent_sha and remote_sha != parent_sha:
        print(f"⚠️  远端 {remote_sha[:8]} 不是本地父提交 {parent_sha[:8]}："
              f"说明远端有本地没有的提交，推完会覆盖它（脚本用 force 更新 ref）")

    changed_raw = run_git("diff", "--name-only", "-z", parent_sha or "HEAD", local_sha) if parent_sha else ""
    # -z + core.quotePath=false：中文文件名 / 特殊字符不会被 git 转义成 \345\220\257 这种
    changed = [item for item in changed_raw.split("\0") if item.strip()]
    print(f"改动   ：{len(changed)} 个文件 {changed if changed else ''}")
    if args.dry_run:
        print("（--dry-run：到此为止，什么都没写）")
        return 0

    entries = []
    for path in changed:
        # 内容取自 HEAD 的 blob（不是工作区文件）：避免 CRLF 之类的差异混进来
        content = run_git("cat-file", "blob", f"{local_sha}:{path}", binary=True)
        mode = run_git("ls-tree", "-z", local_sha, path).split()[0]
        blob = call("POST", f"/repos/{repo}/git/blobs", json={
            "content": base64.b64encode(content).decode("ascii"), "encoding": "base64",
        })
        if blob.status_code not in (200, 201):
            print(f"❌ 建 blob 失败：{path}")
            return 2
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob.json()["sha"]})
        print(f"    blob ok: {path}（{len(content)} 字节）")

    tree_payload: dict[str, Any] = {"tree": entries}
    if base_tree:
        tree_payload["base_tree"] = base_tree
    tree = call("POST", f"/repos/{repo}/git/trees", json=tree_payload)
    if tree.status_code not in (200, 201):
        print("❌ 建 tree 失败")
        return 2
    new_tree = tree.json()["sha"]
    print(f"tree   ：{new_tree[:8]}（本地 {tree_sha[:8]}）→ {'一致 ✅' if new_tree == tree_sha else '不一致 ⚠️'}")

    commit_payload: dict[str, Any] = {"message": message, "tree": new_tree,
                                      "author": author, "committer": committer}
    if parent_sha:
        commit_payload["parents"] = [parent_sha]
    commit = call("POST", f"/repos/{repo}/git/commits", json=commit_payload)
    if commit.status_code not in (200, 201):
        print("❌ 建 commit 失败")
        return 2
    new_sha = commit.json()["sha"]
    print(f"commit ：{new_sha[:8]} → {'与本地完全一致 ✅' if new_sha == local_sha else '与本地不同（内容一致）'}")

    if remote_sha:
        ref = call("PATCH", f"/repos/{repo}/git/refs/heads/{branch}",
                   json={"sha": new_sha, "force": True})
    else:
        ref = call("POST", f"/repos/{repo}/git/refs",
                   json={"ref": f"refs/heads/{branch}", "sha": new_sha})
    print(f"更新 {branch}：HTTP {ref.status_code}")
    if ref.status_code >= 400:
        print(ref.text[:200])
        return 2

    check = call("GET", f"/repos/{repo}/git/ref/heads/{branch}")
    final_sha = check.json()["object"]["sha"]
    print(f"远端现在：{final_sha[:8]} → {'✅ 与本地一致' if final_sha == local_sha else '⚠️ 与本地不一致'}")
    return 0 if final_sha == local_sha else 1


if __name__ == "__main__":
    sys.exit(main())
