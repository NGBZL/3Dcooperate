#!/usr/bin/env python3
"""
真实姓名泄漏自检
----------------
用途：在 push 之前确认「真实姓名没有进入 Git」。

真名来源：本地 `assignees.local.json`（代号 -> 真名，已被 .gitignore 忽略）。
GitHub 上只应出现代号（小明/小红/…），真名只允许留在本地。

检查三处：
    1. 已跟踪文件的内容（git ls-files）
    2. 暂存区的改动（git diff --cached）
    3. 所有提交信息（git log --all --format=%B）

用法：
    python tools/check_names.py            # 完整检查（推送前跑一次）
    python tools/check_names.py --staged   # 只查暂存区（pre-commit 钩子用）
    python tools/check_names.py --hook     # 由 .githooks/pre-commit 调用（等价 --staged 加提交信息）
    python tools/check_names.py --quiet    # 只在发现问题时输出

退出码：0 = 干净；1 = 发现真实姓名或敏感文件；2 = 环境问题（不在 git 仓库等）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# 明确不允许提交的文件（正则，按名字匹配）
SENSITIVE_PATTERNS = (
    ".env",
    "agent/.env",
    "assignees.local.json",
)

# 忽略的目录/文件类型（二进制、缓存等不做内容检查）
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".whl", ".so", ".dll", ".exe", ".bin", ".pcd", ".ply", ".obj", ".glb",
    ".npy", ".npz", ".pt", ".pth", ".onnx", ".db", ".db-wal", ".db-shm",
}


def git(*args: str) -> tuple[int, str]:
    """
    执行 git 命令。

    必须显式指定 encoding="utf-8"：Windows 上 subprocess 的 text=True 默认按本地
    编码（GBK/cp936）解码，而 git 输出的是 UTF-8，中文会被解成乱码导致漏检。

    :param args: git 子命令参数
    :return: (退出码, 标准输出文本)
    """
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout


def find_repo_root() -> Path | None:
    """定位仓库根目录。"""
    code, out = git("rev-parse", "--show-toplevel")
    return Path(out.strip()) if code == 0 and out.strip() else None


def load_real_names(aliases_path: Path) -> list[str]:
    """
    从 assignees.local.json 读取真实姓名列表。

    :param aliases_path: 映射文件路径
    :return: 真名列表（去重、保持原顺序）
    """
    if not aliases_path.is_file():
        return []
    try:
        with aliases_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []

    names: list[str] = []
    for key, value in data.items():
        if str(key).startswith("_"):
            continue
        if isinstance(value, str) and value.strip() and value.strip() not in names:
            names.append(value.strip())
    return names


def scan_text(text: str, names: list[str], where: str) -> list[str]:
    """
    在一段文本里查找真名。

    :param text: 待检查文本
    :param names: 真名列表
    :param where: 位置描述（用于输出）
    :return: 问题描述列表
    """
    problems: list[str] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for name in names:
            if name in line:
                problems.append(f"{where}:{line_no} 出现「{name}」-> {line.strip()[:100]}")
    return problems


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    # Windows 控制台默认 GBK，✅/⚠️ 这类字符会让 print 抛 UnicodeEncodeError，
    # 结果工具明明检查通过了却以退出码 1 结束（还甩一段 traceback），所以先把输出切成 UTF-8。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - 老版本 Python / 特殊 stdout
        pass

    parser = argparse.ArgumentParser(description="检查真实姓名是否泄漏进 Git")
    parser.add_argument("--staged", action="store_true", help="只检查暂存区改动")
    parser.add_argument("--hook", action="store_true", help="由 pre-commit 钩子调用")
    parser.add_argument("--quiet", action="store_true", help="只在有问题时输出")
    parser.add_argument("--aliases", default="", help="真名映射文件路径（默认自动定位）")
    args = parser.parse_args(argv)

    root = find_repo_root()
    if root is None:
        print("✗ 当前目录不在 Git 仓库中", file=sys.stderr)
        return 2

    aliases_path = Path(args.aliases) if args.aliases else root / "assignees.local.json"
    names = load_real_names(aliases_path)

    if not names:
        if not args.quiet:
            print(f"ℹ️ 未读到真名映射（{aliases_path} 不存在或为空），无需检查")
        return 0

    problems: list[str] = []

    def say(message: str) -> None:
        if not args.quiet:
            print(message)

    say(f"真名条目：{len(names)} 条（来自 {aliases_path.name}）")
    say("-" * 56)

    # ---------- 1. 敏感文件是否被跟踪 ----------
    code, tracked_out = git("ls-files")
    tracked = [line for line in tracked_out.splitlines() if line.strip()]
    for path in tracked:
        if path in SENSITIVE_PATTERNS or path.endswith((".db", ".db-wal", ".db-shm", ".log")):
            problems.append(f"敏感文件已被 Git 跟踪：{path}")

    # ---------- 2. 已跟踪文件的内容 ----------
    if not args.staged and not args.hook:
        say(f"检查已跟踪文件：{len(tracked)} 个")
        for path in tracked:
            if Path(path).suffix.lower() in SKIP_SUFFIXES:
                continue
            try:
                text = (root / path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            problems.extend(scan_text(text, names, path))

    # ---------- 3. 暂存区改动 ----------
    code, staged_diff = git("diff", "--cached", "-U0")
    if code == 0 and staged_diff.strip():
        say("检查暂存区改动")
        problems.extend(scan_text(staged_diff, names, "暂存区"))
        for path in SENSITIVE_PATTERNS:
            if f"b/{path}" in staged_diff:
                problems.append(f"敏感文件被加入暂存区：{path}")

    # ---------- 4. 提交信息 ----------
    code, messages = git("log", "--all", "--format=%H%n%B")
    if code == 0 and messages.strip():
        say("检查提交信息")
        problems.extend(scan_text(messages, names, "提交信息"))

    say("-" * 56)
    if problems:
        print(f"❌ 发现 {len(problems)} 处真实姓名/敏感文件问题：")
        for item in problems:
            print("   -", item)
        print()
        print("处理方式：")
        print("  · 文件内容：把真名替换成代号（小明/小红/…），真名只留在 assignees.local.json")
        print("  · 提交信息：改写提交信息（git commit --amend）后再推送")
        print("  · 敏感文件：git rm --cached <文件>，并确认 .gitignore 已覆盖")
        return 1

    say("✅ 未发现真实姓名泄漏")
    return 0


if __name__ == "__main__":
    sys.exit(main())
