"""
SQLite 数据库操作模块
--------------------
职责：
1. 建表 / 旧表结构自动升级（轻量迁移，含 assignee 列）
2. 任务（对应 GitHub Issue）的增删改查
3. 所有数据库异常统一记录日志后再向上抛出，由 Flask 层转成 JSON 错误

表结构 tasks：
    id            INTEGER  主键，自增
    issue_number  INTEGER  GitHub Issue 编号（唯一）
    title         TEXT     标题
    description   TEXT     描述
    status        TEXT     状态：todo / doing / done
    assignee      TEXT     负责人（固定 6 人之一；空串表示未分配）
    branch        TEXT     关联分支名（可为空）
    commits       TEXT     关联提交列表（JSON 字符串，扩展字段）
    html_url      TEXT     Issue 网页地址（扩展字段）
    state         TEXT     Issue 状态：open / closed（扩展字段）
    review_score  INTEGER  Agent 审核评分 0~100（无审核结果时为 NULL）
    review_passed INTEGER  审核是否通过：1=通过 / 0=不通过 / NULL=无结论
    review_comment TEXT    Agent 评语（含改进建议，空串表示没有）
    updated_at    TEXT     最后更新时间（本地 ISO 时间）

两个维度互相独立：status 与 assignee 分开存储、分开更新。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Optional

from config import DB_PATH, DEFAULT_ASSIGNEE, DEFAULT_STATUS, STATUSES, normalize_assignee

logger = logging.getLogger(__name__)

# 建表语句：新库直接按最新结构创建
_CREATE_TASKS_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number      INTEGER UNIQUE,
    title             TEXT NOT NULL,
    description       TEXT DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'todo',
    assignee          TEXT DEFAULT '',
    assignee_username TEXT DEFAULT '',
    branch            TEXT,
    commits           TEXT DEFAULT '[]',
    html_url          TEXT DEFAULT '',
    state             TEXT DEFAULT 'open',
    review_score      INTEGER DEFAULT NULL,
    review_passed     INTEGER DEFAULT NULL,
    review_comment    TEXT DEFAULT '',
    updated_at        TEXT
)
"""

# 登录用户表
# 说明：username 用「代号」（小明…），真实姓名只在本地 assignees.local.json 里，
# 由界面层翻译，绝不写进数据库、更不会进 GitHub。
_CREATE_USERS_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    display_name  TEXT DEFAULT '',
    password_hash TEXT NOT NULL,
    created_at    TEXT,
    last_login_at TEXT
)
"""

# 操作日志表：谁在什么时候把哪个 Issue 改成了什么（含被拒绝的尝试）
_CREATE_LOGS_SQL = """
CREATE TABLE IF NOT EXISTS operation_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT,
    issue_number INTEGER,
    action       TEXT NOT NULL,
    old_value    TEXT DEFAULT '',
    new_value    TEXT DEFAULT '',
    result       TEXT DEFAULT 'ok',
    detail       TEXT DEFAULT '',
    timestamp    TEXT
)
"""

# 看板自己的元信息表（key/value）：目前只用来记住「本地缓存属于哪个 GitHub 仓库」
# 为什么要记：tasks 表以 issue_number 为主键，而 Issue 编号只在**单个仓库内**唯一。
# 换了任务仓库（例如把任务从 A 仓迁到 B 仓）如果不清缓存，B 仓的 #1 会覆盖 A 仓的 #1，
# 更糟的是看板后续会把状态/负责人写回 B 仓那个编号完全不相干的 Issue 上。
_CREATE_META_SQL = """
CREATE TABLE IF NOT EXISTS board_meta (
    key   TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
)
"""

# 旧库缺失的列及其定义，用于自动升级（ALTER TABLE ADD COLUMN）
# 新增字段只需要加到这里，老库启动时会自动补齐，无需删库重建
_COLUMN_MIGRATIONS: dict[str, str] = {
    "assignee": "TEXT DEFAULT ''",
    "assignee_username": "TEXT DEFAULT ''",
    "commits": "TEXT DEFAULT '[]'",
    "html_url": "TEXT DEFAULT ''",
    "state": "TEXT DEFAULT 'open'",
    # 1 = 已被「从看板移除」，同步不会把它拉回来（GitHub 上的 Issue 不受影响）
    "hidden": "INTEGER DEFAULT 0",
    # Agent 审核结果（旧库在这里被自动补齐）
    "review_score": "INTEGER DEFAULT NULL",
    "review_passed": "INTEGER DEFAULT NULL",
    "review_comment": "TEXT DEFAULT ''",
}

# 常用索引
_CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_issue ON tasks(issue_number)",
    "CREATE INDEX IF NOT EXISTS idx_logs_time ON operation_logs(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_logs_user ON operation_logs(username)",
)


def now_iso() -> str:
    """返回当前本地时间的 ISO 字符串（秒级精度），用作 updated_at。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """
    数据库连接上下文管理器。

    用法::

        with get_conn() as conn:
            conn.execute(...)

    正常退出自动 commit，出现异常自动 rollback 并记录日志，最后一定关闭连接。
    """
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        # WAL 模式：读写并发更友好
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error as exc:
        logger.error("打开数据库失败（路径：%s）：%s", DB_PATH, exc)
        raise

    try:
        yield conn
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        logger.exception("数据库操作失败，已回滚：%s", exc)
        raise
    except Exception:
        conn.rollback()
        logger.exception("处理数据时发生异常，已回滚")
        raise
    finally:
        conn.close()


def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    """把 sqlite Row 转成普通 dict，并规范化 commits / assignee / 审核结果字段。"""
    task = dict(row)

    # commits 字段存的是 JSON 字符串，这里解析成列表
    raw = task.get("commits")
    if isinstance(raw, str):
        try:
            task["commits"] = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            logger.warning("任务 #%s 的 commits 字段不是合法 JSON，已忽略", task.get("issue_number"))
            task["commits"] = []
    elif raw is None:
        task["commits"] = []

    # assignee 兜底：老库中可能是 NULL
    if task.get("assignee") is None:
        task["assignee"] = ""

    # assignee_username：与 assignee（代号）保持一致，老数据缺这一列时自动补上
    if not task.get("assignee_username"):
        task["assignee_username"] = task.get("assignee") or ""

    # hidden：是否已从看板移除（同步不会把它拉回来）
    task["hidden"] = bool(task.get("hidden"))

    # ---- Agent 审核结果：统一成 JSON 友好的三态 ----
    # review_passed：True / False / None（None 表示没有结论，前端不显示通过/不通过徽章）
    passed = task.get("review_passed")
    if passed is None:
        task["review_passed"] = None
    else:
        task["review_passed"] = bool(passed)

    score = task.get("review_score")
    if score is not None:
        try:
            task["review_score"] = int(score)
        except (TypeError, ValueError):
            logger.warning("任务 #%s 的 review_score 不是整数：%r", task.get("issue_number"), score)
            task["review_score"] = None

    if not isinstance(task.get("review_comment"), str):
        task["review_comment"] = ""

    return task


def init_db() -> None:
    """
    初始化数据库：建表、补列、建索引。

    可以在每次启动时安全地重复调用（幂等）。
    旧库（例如没有 assignee 列的库）会自动 ALTER TABLE 补齐，无需删库重建。
    """
    with get_conn() as conn:
        conn.execute(_CREATE_TASKS_SQL)
        conn.execute(_CREATE_USERS_SQL)
        conn.execute(_CREATE_LOGS_SQL)
        conn.execute(_CREATE_META_SQL)

        # ---- 轻量迁移：为已存在的旧表补齐新增列 ----
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        for column, definition in _COLUMN_MIGRATIONS.items():
            if column in existing:
                continue
            logger.info("检测到旧表缺少列 %s，正在自动补齐", column)
            try:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError as exc:
                # 并发启动 / PRAGMA 读到旧快照时可能重复执行，列已存在属于正常情况
                if "duplicate column" in str(exc).lower():
                    logger.info("列 %s 已存在，跳过迁移", column)
                else:
                    raise

        # 老库里的任务：assignee_username 与 assignee 保持一致（历史数据补一次）
        conn.execute(
            "UPDATE tasks SET assignee_username = assignee "
            "WHERE COALESCE(assignee_username, '') = '' AND COALESCE(assignee, '') != ''"
        )

        for sql in _CREATE_INDEX_SQL:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError as exc:
                logger.warning("创建索引失败（可忽略）：%s", exc)

    logger.info("数据库初始化完成：%s", DB_PATH)


# ============================================================
# 看板元信息 / 仓库切换
# ============================================================
def get_meta(key: str, default: str = "") -> str:
    """
    读一条看板元信息。

    :param key: 键名
    :param default: 不存在时返回的默认值
    :return: 值
    """
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM board_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    """写一条看板元信息（存在则覆盖）。"""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO board_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def count_tasks() -> int:
    """任务缓存条数（含已从看板移除的）。"""
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]


def infer_cached_repo() -> str:
    """
    从缓存里的 ``html_url`` 反推「这批任务其实是哪个仓库的」。

    为什么要反推：``board_meta`` 里的仓库名是「上一次运行时写下的」，
    如果看板中途换过仓库、或者先前的版本没写过 meta，光看 meta 并不可靠；
    而每个任务的 html_url（``https://github.com/owner/repo/issues/12``）是硬证据。

    :return: 唯一推断出的 ``owner/repo``（小写）；没有 url、或混杂多个仓库时返回空串
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT html_url FROM tasks WHERE COALESCE(html_url, '') != '' LIMIT 50"
        ).fetchall()

    found: set[str] = set()
    for row in rows:
        match = re.search(r"github\.com/([^/\s]+/[^/\s]+)/issues/\d+", row["html_url"] or "")
        if match:
            found.add(match.group(1).lower())
    return found.pop() if len(found) == 1 else ""


def clear_tasks() -> int:
    """
    清空任务缓存（**不动**用户、也不动操作日志）。

    用在换任务仓库时：缓存是 GitHub 的副本，清掉之后同步一次就能重建，
    而用户/密码/日志属于看板自己的数据，必须留着。

    :return: 清掉的任务条数
    """
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        conn.execute("DELETE FROM tasks")
    logger.warning("已清空任务缓存：%d 条（换任务仓库后需要重新同步）", count)
    return count


def bind_repo(repo: str) -> dict[str, Any]:
    """
    把本地缓存与「当前任务仓库」绑定，必要时清空旧缓存。

    判断依据有两份，**证据优先**：
      1. ``board_meta.repo``：上次运行时记下的仓库（可能缺失或过时）
      2. 缓存里每个任务的 ``html_url``：这批任务到底属于哪个仓库（硬证据）

    规则：
      * 记下的仓库变了 → 清空
      * html_url 明确指向**别的**仓库 → 清空（meta 不可信时以证据为准）
      * 从没记过、也推断不出来、但缓存里已有数据 → 清空（无法确认归属，
        宁可从 GitHub 重拉，也不能拿旧编号去写新仓库的 Issue）
      * 其余情况（同仓库 / 空库）→ 保持不动

    :param repo: 当前配置的任务仓库，形如 ``owner/name``
    :return: {"repo", "previous", "inferred", "changed", "cleared", "reason"}
    """
    previous = get_meta("repo", "")
    inferred = infer_cached_repo()
    cached = count_tasks()
    target = (repo or "").lower()

    reason = ""
    if inferred:
        # html_url 是硬证据：和当前仓库一致就保留（哪怕 meta 记录过时/写错），
        # 指向别的仓库就清掉
        if inferred != target:
            reason = f"缓存里的任务其实属于 {inferred}，不是当前仓库 {repo}"
    elif previous and previous.lower() != target:
        reason = f"配置的仓库从 {previous} 换成了 {repo}"
    elif not previous and cached:
        reason = "缓存里已有任务，但既没有仓库记录、也推断不出归属"

    if reason:
        cleared = clear_tasks()
        set_meta("repo", repo)
        logger.warning("重建缓存：%s（已清空 %d 条旧任务）", reason, cleared)
        return {"repo": repo, "previous": previous, "inferred": inferred,
                "changed": True, "cleared": cleared, "reason": reason}

    set_meta("repo", repo)
    return {"repo": repo, "previous": previous, "inferred": inferred,
            "changed": False, "cleared": 0, "reason": ""}


# ============================================================
# 用户（登录）相关
# ============================================================
def get_user(username: str) -> Optional[dict[str, Any]]:
    """
    按登录名查用户。

    :param username: 登录名（代号，如「小美」）
    :return: 用户字典；不存在时返回 None
    """
    if not username:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict[str, Any]]:
    """列出全部用户（按 id 升序）。"""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id ASC").fetchall()
    return [dict(row) for row in rows]


def create_user(
    username: str,
    password_hash: str,
    display_name: str = "",
) -> dict[str, Any]:
    """
    新建用户。

    :param username: 登录名（代号）
    :param password_hash: 已经哈希过的密码（**不要传明文**）
    :param display_name: 显示名；留空表示由本地真名映射表解析
    :return: 新用户字典
    """
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, created_at) "
            "VALUES (?, ?, ?, ?)",
            (username, display_name, password_hash, now_iso()),
        )
    user = get_user(username)
    assert user is not None
    return user


def set_user_password(username: str, password_hash: str) -> bool:
    """
    更新用户密码哈希。

    :param username: 登录名
    :param password_hash: 新的密码哈希
    :return: 是否命中该用户
    """
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (password_hash, username),
        )
        return cursor.rowcount > 0


def touch_last_login(username: str) -> None:
    """记录最近一次登录时间（登录成功时调用）。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET last_login_at = ? WHERE username = ?",
            (now_iso(), username),
        )


# ============================================================
# 操作日志
# ============================================================
def log_operation(
    username: str,
    action: str,
    issue_number: Optional[int] = None,
    old_value: str = "",
    new_value: str = "",
    result: str = "ok",
    detail: str = "",
) -> None:
    """
    记一条操作日志。

    写日志失败不应影响主流程，所以这里吞掉异常只记 warning。

    :param username: 操作者登录名（未登录时可为空）
    :param action: 动作，如 create_task / update_status / update_assignee / delete_task /
                   login / login_failed / logout / change_password / denied
    :param issue_number: 相关 Issue 编号
    :param old_value: 变更前的值
    :param new_value: 变更后的值
    :param result: ok / denied / error
    :param detail: 补充说明（例如拒绝原因）
    """
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO operation_logs "
                "(username, issue_number, action, old_value, new_value, result, detail, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (username, issue_number, action, str(old_value), str(new_value),
                 result, detail, now_iso()),
            )
    except Exception as exc:  # noqa: BLE001 - 日志失败不能影响业务
        logger.warning("写入操作日志失败：%s", exc)


def recent_logs(limit: int = 100, username: Optional[str] = None) -> list[dict[str, Any]]:
    """
    读取最近的操作日志（给审计/排查用）。

    :param limit: 最多返回多少条
    :param username: 只看某个人的操作；None 表示全部
    :return: 日志列表（新的在前）
    """
    sql = "SELECT * FROM operation_logs"
    params: list[Any] = []
    if username:
        sql += " WHERE username = ?"
        params.append(username)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 1000)))

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def get_all_tasks(include_hidden: bool = False) -> list[dict[str, Any]]:
    """
    获取任务，按 Issue 编号升序排列。

    :param include_hidden: 是否包含「已从看板移除」的任务（默认不包含）
    :return: 任务列表
    """
    sql = "SELECT * FROM tasks"
    if not include_hidden:
        sql += " WHERE COALESCE(hidden, 0) = 0"
    sql += " ORDER BY issue_number ASC"

    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [_row_to_task(row) for row in rows]


def get_task_by_issue(issue_number: int) -> Optional[dict[str, Any]]:
    """
    根据 GitHub Issue 编号获取任务。

    :param issue_number: Issue 编号
    :return: 任务字典；不存在时返回 None
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE issue_number = ?", (issue_number,)
        ).fetchone()
    return _row_to_task(row) if row else None


def task_exists(issue_number: int) -> bool:
    """判断某个 Issue 编号是否已在本地缓存中。"""
    return get_task_by_issue(issue_number) is not None


def upsert_task(
    issue_number: int,
    title: str,
    description: str = "",
    status: str = DEFAULT_STATUS,
    assignee: Optional[str] = None,
    branch: Optional[str] = None,
    commits: Optional[list[dict[str, Any]]] = None,
    html_url: str = "",
    state: str = "open",
    review_score: Optional[int] = None,
    review_passed: Optional[bool] = None,
    review_comment: Optional[str] = None,
    review_replace: bool = False,
) -> dict[str, Any]:
    """
    插入或更新任务（按 issue_number 判定）。

    :param issue_number: GitHub Issue 编号
    :param title: 标题
    :param description: 描述（Markdown 原文，不含“负责人”行）
    :param status: 状态，必须是 todo/doing/done 之一，非法值会退回 todo
    :param assignee: 负责人；None 表示“本次不修改”，空串表示置为未分配，
                     非法人名会被规整成空串（未分配）
    :param branch: 关联分支名（None 表示“本次不修改”，空串表示清空）
    :param commits: 关联提交列表（None 表示“本次不修改”）
    :param html_url: Issue 网页地址
    :param state: Issue 状态 open/closed
    :param review_score: Agent 审核评分 0~100
    :param review_passed: 审核是否通过
    :param review_comment: Agent 评语
    :param review_replace: 是否“整组覆盖”审核三列。
                           True  —— 本次确实解析到了审核报告，三列按传入值写死
                                    （None 就是 NULL，用于“最新报告没有分数/没有结论”的场景，
                                     避免残留上一份报告的旧评分）
                           False —— 没有审核报告（例如 with_reviews=0 的加速同步），
                                    三列保持库中旧值不动
    :return: 写入后的任务字典
    """
    if status not in STATUSES:
        logger.warning("收到非法状态 %r，已按 %s 处理", status, DEFAULT_STATUS)
        status = DEFAULT_STATUS

    normalized_assignee: Optional[str] = None
    if assignee is not None:
        normalized_assignee = normalize_assignee(assignee)
        if assignee and not normalized_assignee:
            logger.warning("负责人 %r 不在名单中，已按未分配处理", assignee)

    commits_json = None if commits is None else json.dumps(commits, ensure_ascii=False)

    # ---- 审核结果：库中以 1 / 0 / NULL 三态存储 ----
    passed_value: Optional[int] = None
    if review_passed is not None:
        passed_value = 1 if review_passed else 0

    score_value: Optional[int] = None
    if review_score is not None:
        try:
            score_value = max(0, min(100, int(review_score)))
        except (TypeError, ValueError):
            logger.warning("审核评分不是整数：%r，已忽略", review_score)

    comment_value: Optional[str] = None
    if review_comment is not None:
        comment_value = str(review_comment)

    timestamp = now_iso()

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM tasks WHERE issue_number = ?", (issue_number,)
        ).fetchone()

        if row:
            # 审核三列有两种写法：
            #   review_replace=True  → 整组写死（None 就是 NULL），避免残留上一份报告的旧评分
            #   review_replace=False → COALESCE 保留旧值，供 with_reviews=0 的加速同步使用
            if review_replace:
                review_sql = ("review_score = ?, review_passed = ?, "
                              "review_comment = ?,")
                review_values: list[Any] = [
                    score_value,
                    passed_value,
                    comment_value if comment_value is not None else "",
                ]
            else:
                review_sql = ("review_score = COALESCE(?, review_score), "
                              "review_passed = COALESCE(?, review_passed), "
                              "review_comment = COALESCE(?, review_comment),")
                review_values = [score_value, passed_value, comment_value]

            conn.execute(
                f"""
                UPDATE tasks
                   SET title             = ?,
                       description       = ?,
                       status            = ?,
                       assignee          = COALESCE(?, assignee),
                       assignee_username = COALESCE(?, assignee_username),
                       branch            = COALESCE(?, branch),
                       commits           = COALESCE(?, commits),
                       html_url          = COALESCE(NULLIF(?, ''), html_url),
                       state             = COALESCE(NULLIF(?, ''), state),
                       {review_sql}
                       updated_at        = ?
                 WHERE issue_number = ?
                """,
                (title, description, status, normalized_assignee, normalized_assignee,
                 branch, commits_json,
                 html_url, state, *review_values, timestamp, issue_number),
            )
        else:
            conn.execute(
                """
                INSERT INTO tasks
                       (issue_number, title, description, status, assignee, assignee_username,
                        branch, commits, html_url, state, review_score, review_passed,
                        review_comment, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (issue_number, title, description, status,
                 normalize_assignee(assignee) if assignee is not None else DEFAULT_ASSIGNEE,
                 normalize_assignee(assignee) if assignee is not None else DEFAULT_ASSIGNEE,
                 branch, commits_json if commits_json is not None else "[]",
                 html_url, state, score_value, passed_value,
                 comment_value if comment_value is not None else "", timestamp),
            )

    logger.debug("任务 #%s 已写入本地库（status=%s, assignee=%s, 审核通过=%s 评分=%s）",
                 issue_number, status, normalized_assignee, passed_value, score_value)
    task = get_task_by_issue(issue_number)
    assert task is not None  # 刚写入，必然存在
    return task


def update_task_status(
    issue_number: int,
    status: str,
    assignee: Optional[str] = None,
) -> bool:
    """
    更新任务状态（可选同时更新负责人）。

    :param issue_number: Issue 编号
    :param status: 新状态
    :param assignee: 负责人；None 表示不改动负责人，空串表示置为未分配
    :return: 是否命中了记录（False 表示本地没有该任务）
    """
    if status not in STATUSES:
        raise ValueError(f"非法状态：{status}")

    fields = ["status = ?", "updated_at = ?"]
    values: list[Any] = [status, now_iso()]

    if assignee is not None:
        fields.append("assignee = ?")
        values.append(normalize_assignee(assignee))

    values.append(issue_number)

    with get_conn() as conn:
        cursor = conn.execute(
            f"UPDATE tasks SET {', '.join(fields)} WHERE issue_number = ?",
            values,
        )
        changed = cursor.rowcount > 0

    if not changed:
        logger.warning("尝试更新不存在的任务 #%s", issue_number)
    return changed


def update_task_assignee(issue_number: int, assignee: str) -> bool:
    """
    仅更新任务负责人（不影响状态）。

    :param issue_number: Issue 编号
    :param assignee: 负责人；空串表示未分配
    :return: 是否命中了记录
    """
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE tasks SET assignee = ?, assignee_username = ?, updated_at = ? "
            "WHERE issue_number = ?",
            (normalize_assignee(assignee), normalize_assignee(assignee),
             now_iso(), issue_number),
        )
        changed = cursor.rowcount > 0

    if not changed:
        logger.warning("尝试修改不存在的任务 #%s 的负责人", issue_number)
    return changed


def update_task_meta(
    issue_number: int,
    branch: Optional[str] = None,
    commits: Optional[list[dict[str, Any]]] = None,
) -> None:
    """更新任务的分支 / 提交缓存信息（不改变状态、负责人与标题）。"""
    commits_json = None if commits is None else json.dumps(commits, ensure_ascii=False)

    with get_conn() as conn:
        conn.execute(
            """
            UPDATE tasks
               SET branch     = COALESCE(?, branch),
                   commits    = COALESCE(?, commits),
                   updated_at = ?
             WHERE issue_number = ?
            """,
            (branch, commits_json, now_iso(), issue_number),
        )


def delete_task(issue_number: int) -> bool:
    """
    从本地缓存中彻底删除任务（不会删除 GitHub 上的 Issue）。

    注意：日常「从看板移除」请用 hide_task()，否则下次同步会把它拉回来。

    :return: 是否真的删除了记录
    """
    with get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM tasks WHERE issue_number = ?", (issue_number,)
        )
        return cursor.rowcount > 0


def hide_task(issue_number: int, hidden: bool = True) -> bool:
    """
    把任务「从看板移除 / 恢复显示」。

    这是**本地意图**：置为 hidden 后，同步不会把它重新放回看板
    （GitHub 上的 Issue 完全不受影响）。

    :param issue_number: Issue 编号
    :param hidden: True 表示从看板移除，False 表示恢复
    :return: 是否命中了记录
    """
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE tasks SET hidden = ?, updated_at = ? WHERE issue_number = ?",
            (1 if hidden else 0, now_iso(), issue_number),
        )
        return cursor.rowcount > 0


def count_hidden() -> int:
    """统计「已从看板移除」的任务数量。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE COALESCE(hidden, 0) = 1"
        ).fetchone()
    return int(row["n"]) if row else 0


def delete_missing_issues(valid_issue_numbers: list[int]) -> int:
    """
    同步后清理本地“已从 GitHub 消失”的任务。

    仅当传入列表非空时才执行，避免同步失败（空列表）时误删全部数据。

    :param valid_issue_numbers: 本次同步中真实存在的 Issue 编号列表
    :return: 被清理的记录数
    """
    if not valid_issue_numbers:
        logger.warning("有效 Issue 列表为空，跳过本地清理以免误删数据")
        return 0

    placeholders = ",".join("?" for _ in valid_issue_numbers)
    with get_conn() as conn:
        cursor = conn.execute(
            f"DELETE FROM tasks WHERE issue_number NOT IN ({placeholders})",
            valid_issue_numbers,
        )
        removed = cursor.rowcount
    if removed:
        logger.info("同步清理：移除了 %d 条本地已不存在的任务", removed)
    return removed


def count_by_status() -> dict[str, int]:
    """统计各状态的任务数量，返回形如 {'todo': 3, 'doing': 1, 'done': 5} 的字典。"""
    result = {status: 0 for status in STATUSES}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()
    for row in rows:
        result[row["status"]] = row["n"]
    return result


def count_by_assignee() -> dict[str, int]:
    """
    统计各负责人的任务数量。

    :return: {负责人: 数量}，未分配任务的键为空串 ""
    """
    result: dict[str, int] = {}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT COALESCE(assignee, '') AS assignee, COUNT(*) AS n "
            "FROM tasks GROUP BY COALESCE(assignee, '')"
        ).fetchall()
    for row in rows:
        result[row["assignee"]] = row["n"]
    return result


if __name__ == "__main__":
    # 直接运行本文件时可以快速初始化并查看当前数据
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    init_db()
    print("各状态任务数：", count_by_status())
    print("各负责人任务数：", count_by_assignee())
