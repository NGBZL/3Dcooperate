# 项目看板（GitHub Issues 版）

一个用来追踪三维重建项目进度的轻量级看板：**数据源是 GitHub Issues**，本地用 SQLite 做缓存，
不需要额外搭服务器，GitHub 仓库本身就是远程存储。

看板有两个**互相独立**的维度，可以自由组合：

| 维度 | 取值 | 存储位置 | 视图 |
|------|------|----------|------|
| 完成状态 | `todo` / `doing` / `done` | Issue **标签** | 状态视图（三列） |
| 负责人 | 代号：小明、小红、小刚、小丽、小强、小美 + 未分配（界面上显示本地真名） | Issue **正文标记** | 人员视图（七列） |

在此之上还带一个 **🤖 Agent 代码审核**：有人 push 到 `main` 时自动调用 LLM 审核 diff，
过审就把关联 Issue 标记为 `done` 并关闭，不过审只留评语。

---

## 目录结构

```
kanban/
├── server.py                 # Flask 后端（REST API + 页面渲染）
├── github_client.py          # GitHub API 封装（重试、限流、负责人解析、分支/提交收集）
├── database.py               # SQLite 操作（建表 / 增删改查 / 轻量迁移）
├── config.py                 # 配置读取、人员名单、环境变量校验
├── requirements.txt          # Python 依赖
├── .env.example              # 看板配置模板（复制成 .env）
├── assignees.local.json      # 负责人「代号 -> 真名」映射（本地专用，已 gitignore）
├── tools/
│   ├── check_names.py        # 推送前自检：真名有没有混进 Git
│   └── test_review_parse.py  # 审核报告解析自检（离线，改动报告模板后必跑）
├── .githooks/
│   └── pre-commit            # 可选的提交前守卫（默认未启用，见下文）
├── .gitignore
├── templates/
│   └── index.html            # 看板页面
├── static/
│   ├── style.css             # 样式（参考 Trello）
│   └── app.js                # 前端逻辑（原生 JS）
└── agent/                    # 🤖 代码审核 Agent
    ├── __init__.py
    ├── config.py             # Agent 配置（.env 加载、阈值、超时）
    ├── llm_client.py         # LLM 调用（chat / chat_json，兼容 DeepSeek、OpenAI）
    ├── prompt.py             # 提示词与报告模板
    ├── reviewer.py           # 审核核心逻辑
    ├── run.py                # 入口（Actions / 本地命令行）
    ├── selftest.py           # 离线自检（不需要 API Key）
    ├── .env.example          # Agent 配置模板（复制成 agent/.env）
    └── .env                  # 你的 LLM Key（已被 .gitignore 忽略）
└── .github/workflows/
    └── agent-review.yml      # push 到 main 时触发审核
```

---

# 一、看板

## 功能特性

| 功能 | 说明 |
|------|------|
| 📊 状态视图 | Todo / Doing / Done 三列，颜色区分（黄 / 蓝 / 绿） |
| 👥 人员视图 | 固定 6 人各一列 + 1 列「未分配」，列头紫色系且每人色相不同 |
| 👤 负责人徽章 | 每张卡片显示负责人，**点击即可快速切换**（弹出选择器，输入序号或姓名） |
| 🖱️ 拖拽改属性 | 状态视图拖到某列 = 改状态；人员视图拖到某人列 = 改负责人 |
| 📱 移动端可用 | 响应式布局；触屏用卡片上的 `←` `→` 按钮（状态视图切状态，人员视图切负责人） |
| 🔄 一键同步 | 从 GitHub 拉取全部 Issue，含**从正文解析负责人** |
| ➕ 新建任务 | 同时在 GitHub 创建 Issue，自动打状态标签并把负责人写进正文 |
| 🌿 分支 / 提交关联 | 自动识别分支名中的 Issue 编号，并汇总提交信息里的 `#编号` |
| 🤖 卡片显示 AI 评语 | 同步时读回 Agent 的审核报告，卡片上显示通过/不通过徽章，点击展开评语 |
| 🏷️ 标签自愈 | 首次使用时自动创建 `todo` / `doing` / `done` 三个标签 |
| 🛡️ 健壮性 | 网络失败重试 3 次、Token 失效明确提示、数据库异常写日志、旧库自动迁移 |
| 🔍 搜索过滤 | 按标题 / 描述 / **负责人** / Issue 编号本地过滤 |

## 安装与运行

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

> 建议使用虚拟环境：
>
> ```bash
> python -m venv .venv
> # Windows
> .venv\Scripts\activate
> # macOS / Linux
> source .venv/bin/activate
> ```

### 2. 配置

```bash
copy .env.example .env      # Windows
cp .env.example .env        # macOS / Linux
```

`.env` 内容：

```
GITHUB_TOKEN=ghp_你的token
GITHUB_REPO=你的用户名/仓库名
```

### 3. 获取 GitHub Token

1. 打开 GitHub → 右上角头像 → **Settings** → **Developer settings** → **Personal access tokens**
2. 推荐创建 **Fine-grained token**（可精确到单个仓库），权限勾选：
   - **Issues**：Read and write（读写任务、改标签、改正文、评论、关闭）
   - **Contents**：Read（读取分支与提交）
   - **Metadata**：Read（必选，GitHub 强制）
3. 生成后复制到 `.env` 的 `GITHUB_TOKEN`（注意不要带空格或换行）

### 4. 自检（可选但强烈建议）

```bash
python github_client.py     # 校验 Token / 仓库 / 标签
python database.py          # 初始化 SQLite，并打印各状态 / 各负责人任务数
```

### 5. 启动

```bash
python server.py
```

浏览器打开 <http://localhost:5000>

启动日志里会直接打印组员可用的地址，例如：

```
INFO 本机访问：http://127.0.0.1:5000
INFO 组员访问：http://192.168.43.144:5000
INFO   （页面顶部也会显示这个地址，并可一键复制）
```

## 让组员在同一个热点上访问

服务监听的是 `0.0.0.0`，所以**只要在同一个热点/Wi-Fi 下，组员用手机或电脑打开这个地址就能看看板**，
不需要额外部署、不需要任何脚本：

1. 本机连上手机热点（或让组员连你的热点 / 同一个 Wi-Fi）
2. 启动 `python server.py`
3. **看板页面顶部会自动显示**「🔗 组员访问 http://192.168.43.144:5000」，点 📋 即可复制发群里
4. 组员用浏览器打开这个地址即可（手机浏览器直接输入也行）

地址是后端 `/api/server-info` 实时探测的：优先取「默认路由出口」那块网卡的地址
（多网卡/VPN 时会返回多个备选，页面标题里有列表），并带主机名地址。没连上网时页面会提示
「未检测到局域网地址」。

### 组员打不开？按这个顺序排查

| 症状 | 原因 | 处理 |
|------|------|------|
| 本机能开，别人打不开 | **Windows 防火墙拦截**（最常见，占九成） | 管理员 PowerShell 执行一次：<br>`New-NetFirewallRule -DisplayName "Kanban 5000" -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow` |
| 地址不对 | 有多块网卡（VPN / 虚拟网卡 / 有线+无线） | 把鼠标悬停在页面顶部地址上，标题里会列出全部备选地址；换一个试 |
| 显示「未检测到局域网地址」 | 本机没连热点/Wi-Fi，或没拿到 DHCP | 先连上热点再刷新页面 |
| 手机连不上但电脑能连 | 手机连的是移动数据而不是热点 | 关掉移动数据、确认连的是同一个热点 |
| 端口冲突 | 5000 被占用 | 换个端口启动：`set PORT=8000 && python server.py`（页面会自动显示新端口） |

> 页面上的 ⓘ 按钮也会把防火墙命令直接显示在状态栏里，方便现场排查。

## 使用流程

### 状态视图（默认）

1. 启动服务，点击「🔄 同步」把 GitHub 上的 Issues 拉到看板
2. 点击「➕ 新建任务」创建任务（会在 GitHub 上真实创建 Issue）
3. 拖动卡片改变状态，或在手机上点卡片上的 `←` `→`
4. 每次状态变化都会同步修改 GitHub Issue 的 `todo` / `doing` / `done` 标签

### 人员视图

1. 点击顶部「👥 人员视图」，看板变成 6 人列 + 1 列「未分配」
2. **拖拽**卡片到某人的列 = 把负责人改成他；拖到「未分配」列 = 取消分配
3. 也可以**点击卡片上的负责人徽章**（👤 姓名），在弹出的选择器里输入序号或姓名
4. 每次改动都会重写 GitHub Issue 正文里的 `**负责人**: 姓名` 行（正文其余内容原样保留）

### 卡片上的 AI 评语

Agent 审核完会把报告留在 Issue 评论里，看板「🔄 同步」时会把它读回本地并显示在卡片上：

| 卡片上的表现 | 含义 |
|--------------|------|
| `✅ 审核通过 · 88 分`（绿） | 最新一份报告判定通过 |
| `❌ 审核不通过 · 45 分`（红） | 最新一份报告判定不通过 |
| `⚠️ 审核未完成`（黄） | 最新一份报告说“这次没审成”（比如 LLM Key 失效），此时不给通过/不通过结论 |
| 没有任何徽章 | 该 Issue 还没有审核报告，或报告格式解析不出来（降级：宁可不显示也不显示错的） |

**点击徽章**即可展开 / 收起评语（评语里包含模型给的分点评语与「💡 改进建议」清单，最多 190px 高，可滚动）。
搜索框也能搜评语内容，例如搜「空指针」能找出被评审提到过这个问题的任务。

实现要点：

- 解析逻辑在 `github_client.parse_review_from_comments()`，取**最新一份**能解析出结论的报告
  （同一张 Issue 每次 push 都会多一份报告，所以从最新往前找，解析不了的直接跳过）
- 评论抓取有成本控制：只对**有评论的 Issue**发请求，单次同步最多抓 `SYNC_REVIEW_BUDGET`（默认 50）个；
  评论超过一页时直接跳到最后一页取最新的，最多 2 次请求
- 同步是**整组覆盖**：一旦解析到新报告，评分/结论/评语三列一起更新，
  所以新报告若没有分数（例如“审核未完成”），旧的 88 分不会残留造成前后矛盾
- 没解析到报告时（例如 `?with_reviews=0` 的加速同步）**保留库里已有结果**，不会被清空
- ⚠️ 解析规则与 `agent/prompt.py` 的 `REPORT_TEMPLATE` 是**一对耦合关系**：
  改报告模板必须同步改解析，并跑一次 `python tools/test_review_parse.py`

### 负责人：GitHub 只存「代号」，本地显示「真名」

涉及真人姓名时，**真实姓名绝不出现在 GitHub 上**。做法是分两层：

| 层 | 存的值 | 存在哪 |
|----|--------|--------|
| GitHub（Issue 正文、评论、提交、仓库文件） | **代号**：小明 / 小红 / 小刚 / 小丽 / 小强 / 小美 | 公开仓库 |
| 本地看板界面 | **真名** | 只存在本地 `assignees.local.json` |

```
# assignees.local.json（已被 .gitignore 忽略，请勿提交）
{
  "小明": "张三",
  "小红": "李四"
}
```

- 看板界面（人员视图列头、卡片徽章、下拉框、搜索、提示消息）显示的都是**真名**
- 拖拽 / 提交给后端时用的仍是**代号**，所以写进 GitHub Issue 的永远只是代号
- 从 GitHub 同步回来的也是代号，界面再翻译成真名
- 改完 `assignees.local.json` **刷新页面即可生效**（后端按文件修改时间自动重载，无需重启）
- 删掉该文件：界面直接显示代号，功能不受影响（`GET /api/assignees` 的 `aliases_configured` 会变成 `false`）
- 后端接口对真名也友好：`PUT /api/tasks/1/assignee` 传 `小明` 或真名都能正确落库，落库值始终是代号

创建或修改负责人时，看板会把这一行写进 Issue 正文的**第一行**：

```
**负责人**: 小明

（这里是原本的任务描述）
```

同步时用正则 `\*\*负责人\*\*[:：]\s*(\S+)` 解析（全角/半角冒号都支持），并且：

- 解析出的这一行会从任务描述里剔除，卡片上只显示真正的描述
- 写成 `未分配` / `未指派` / `none` / `-` 都视为未分配
- **不在代号名单里的名字**会被归为「未分配」并在日志中告警（日志里也只记代号）
- 正文里没有这一行时，同步会**保留本地已有的负责人**，不会把手工指派冲掉

> 为什么不直接用 GitHub 原生 assignee？因为原生 assignee 要求对方是仓库协作者且必须有 GitHub 账号。
> 这份名单是项目内部人员，所以按需求用正文标记实现，`github_client.py` 中已注明。

### 改动 Agent 报告格式后：跑一遍解析自检

卡片上的 AI 评语靠解析 Issue 评论得到，解析规则和 `agent/prompt.py` 的报告模板是绑定的：

```bash
python tools/test_review_parse.py     # 离线，不需要 Token；覆盖真实格式/旧格式/降级/取最新
```

用例覆盖：Agent 现在产出的表格格式、`**评分**: 85` 的简化写法、报告里混着引用块、
“审核未完成”的降级报告、多条报告取最新、普通聊天评论、空评论、超长评语等。

### 推送前自检：真名有没有混进 Git

真名一旦进了提交历史，就必须重写历史才能清掉，所以推之前先跑一次：

```bash
python tools/check_names.py            # 完整检查：已跟踪文件 + 暂存区 + 所有提交信息
python tools/check_names.py --staged   # 只查暂存区（快）
```

它会检查三处并在发现问题时列出「文件:行号」，退出码 1：

1. 已跟踪文件的内容里是否出现真名
2. 暂存区改动里是否出现真名，以及有没有把 `.env` / `assignees.local.json` / `*.db` / `*.log` 加入暂存区
3. 所有提交信息里是否出现真名（**提交信息是最容易漏的地方**）

想让它自动拦截，可以启用可选的 pre-commit 守卫：

```bash
git config core.hooksPath .githooks     # 启用
git config --unset core.hooksPath       # 关闭
```

> ⚠️ 这个 hook 需要 Git 自带的 `sh` 可用。若提交时报 `couldn't create signal pipe`
> 之类的错误，说明你的环境 `sh` 起不来，请关掉它，改用上面的 `python tools/check_names.py`。
> 临时跳过一次提交：`git commit --no-verify`。

## 看板 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/tasks` | 获取本地全部任务（支持 `?status=todo`、`?assignee=小明`、`?assignee=未分配`、`?q=关键字`） |
| POST | `/api/tasks` | 新建任务，同时在 GitHub 创建 Issue（可带 `assignee`） |
| PUT | `/api/tasks/<issue_number>` | 更新状态和/或负责人（字段可选传，只传哪个就改哪个） |
| PUT | `/api/tasks/<issue_number>/assignee` | **仅更新负责人**（人员视图拖拽 / 徽章选择器调用） |
| DELETE | `/api/tasks/<issue_number>` | 从本地缓存删除（不影响 GitHub） |
| POST | `/api/sync` | 从 GitHub 同步（`?with_meta=0` 跳过分支/提交、`?with_reviews=0` 跳过审核报告，可加速） |
| GET | `/api/assignees` | 负责人名单与各自任务数 |
| GET | `/api/health` | 健康检查（`?deep=1` 会真实请求 GitHub 校验 Token） |
| GET | `/api/server-info` | 本机局域网访问地址（ip / port / url / urls / hostname / 防火墙提示） |

```bash
# 新建任务（带负责人）
curl -X POST http://localhost:5000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"title":"完成点云配准模块","description":"ICP + 粗配准","status":"todo","assignee":"小明"}'

# 改负责人 / 取消分配
curl -X PUT http://localhost:5000/api/tasks/12/assignee -H "Content-Type: application/json" -d '{"assignee":"小红"}'
curl -X PUT http://localhost:5000/api/tasks/12/assignee -H "Content-Type: application/json" -d '{"assignee":"未分配"}'
```

## 数据库表结构

```sql
CREATE TABLE tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number   INTEGER UNIQUE,   -- GitHub Issue 编号
    title          TEXT NOT NULL,
    description    TEXT,             -- Issue 正文（不含“负责人”行）
    status         TEXT DEFAULT 'todo',   -- todo / doing / done
    assignee       TEXT DEFAULT '',       -- 负责人代号；空串 = 未分配
    branch         TEXT,             -- 关联分支名
    commits        TEXT DEFAULT '[]',     -- 关联提交（JSON 字符串）
    html_url       TEXT DEFAULT '',       -- Issue 网页地址
    state          TEXT DEFAULT 'open',   -- open / closed
    review_score   INTEGER DEFAULT NULL,  -- Agent 审核评分 0~100
    review_passed  INTEGER DEFAULT NULL,  -- 1=通过 / 0=不通过 / NULL=无结论
    review_comment TEXT DEFAULT '',       -- Agent 评语（含改进建议）
    updated_at     TEXT
);
```

> **旧库无需删库重建**：启动时会读取 `PRAGMA table_info`，把缺失的列（`assignee`、`commits`、
> `html_url`、`state`、`review_score`、`review_passed`、`review_comment`）逐个
> `ALTER TABLE ADD COLUMN` 补齐；并用 `try/except` 兜住 “duplicate column name” 的情况，
> 重复启动、并发启动都不会报错。

---

# 二、🤖 Agent 代码审核

## 它做什么

| 职责 | 触发时机 | 动作 |
|------|----------|------|
| 审核代码 | 有人 push 到 `main` | 拉取本次提交的 diff，调用 LLM 审核 |
| 过审 | `score >= 70` | Issue 评论审核报告 → 标签改为 `done` → 关闭 Issue |
| 不过审 | `score < 70` | 只在 Issue 下留评语 + 改进建议，**Issue 状态不变** |

审核维度共 6 项：语法正确性、逻辑正确性、安全性、测试覆盖、代码风格、提交信息。

LLM 必须返回严格 JSON：

```json
{
    "passed": true,
    "score": 88,
    "comment": "详细评语（中文，300 字以内）",
    "suggestions": ["建议1", "建议2"]
}
```

**注意：最终是否通过以 `score >= 70` 为准。** 如果模型给的 `passed` 与 `score` 矛盾
（例如 `passed: true` 但 `score: 40`），Agent 会按分数判定并打印告警，避免误关 Issue。

## 快速开始

### 1. 配置 LLM Key（二选一）

**本地跑：** 编辑项目里已经建好的 `agent/.env`，只需要填一行：

```
LLM_API_KEY=sk-你的DeepSeek密钥
```

其余（`LLM_API_URL`、`LLM_MODEL`）已默认指向 DeepSeek，不用改。
根目录的 `.env` 里已有 `GITHUB_TOKEN` / `GITHUB_REPO`，Agent 会自动复用。

**在 GitHub Actions 跑：** 仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**：

| Secret | 是否必填 | 值 |
|--------|----------|-----|
| `LLM_API_KEY` | ✅ 必填 | 你的大模型 API Key（DeepSeek 形如 `sk-xxx`） |
| `LLM_API_URL` | 可选 | 默认 `https://api.deepseek.com/v1/chat/completions` |
| `LLM_MODEL` | 可选 | 默认 `deepseek-chat` |

`GITHUB_TOKEN` 由 GitHub Actions **自动注入**，不需要自己配置。

> ⚠️ `LLM_API_URL` / `LLM_MODEL` 若配成空值，Agent 会自动回落到默认值
> （Actions 里未设置的 Secret 会以空字符串注入，这一点已在 `agent/config.py` 里处理）。

### 2. 把 Agent 提交到被 push 的仓库

GitHub Actions 只认仓库根目录下的 `.github/workflows/`。所以要把 `agent/` 与 `.github/`
一起提交到**接收 push 的那个仓库**（也就是看板里 `GITHUB_REPO` 指向的仓库）：

```bash
git add agent .github requirements.txt README.md
git commit -m "chore: 接入 Agent 代码审核"
git push
```

如果看板项目和代码仓库是两个不同的仓库，把 `agent/`、`.github/` 复制到代码仓库根目录即可，
Agent 不依赖看板的任何文件（只用 `requests`）。

### 3. 在提交信息里关联 Issue

Agent 从 commit message 里提取 Issue 编号，支持这些写法（大小写不敏感）：

| 写法 | 例子 |
|------|------|
| close / closes / closed | `close #1`、`Closed: #1` |
| fix / fixes / fixed | `fixes #2`、`fix #2` |
| resolve / resolves / resolved | `resolved #3` |
| -ing 形式 | `closing #4`、`fixing #5`、`resolving #6` |
| 直接写编号 | `feat: 完成点云生成模块 #7` |

```bash
git commit -m "feat: 完成点云生成模块 #1"
git commit -m "fix: 修复空指针导致的崩溃

补充了边界判断。

Closes #2"
```

> 一次提交可以关联多个 Issue（`fixes #1 and resolves #2`），Agent 会逐个处理。
> 提交信息里**没有** Issue 编号时会直接跳过，不评论、不改状态（不会打扰你）。

### 4. 本地测试（不污染 GitHub）

```bash
# ① 离线自检：验证正则、JSON 解析、降级、审核流程（不需要 API Key，秒级完成）
python -m agent.selftest

# ② 演练模式：真实调用 LLM 审核某个提交，但只把报告打印到屏幕，不写 GitHub
python -m agent.run --before <起始sha> --after <结束sha> --dry-run

# ③ 正式跑一次（会真的评论 / 关 Issue）
python -m agent.run --before <起始sha> --after <结束sha>

# 只审核某一个提交（不传 --before 即视为单提交审核）
python -m agent.run --after <commit_sha> --dry-run
```

拿 SHA 的办法：

```bash
git log --oneline -5          # 看最近几个提交
git rev-parse HEAD            # 当前提交
git rev-parse HEAD~1          # 上一个提交
```

`--dry-run` 也可以直接用环境变量打开：`AGENT_DRY_RUN=1`。

## 审核报告长什么样

```markdown
## 🤖 Agent 代码审核报告

| 项目 | 内容 |
|------|------|
| 审核结果 | ✅ 通过 |
| 综合评分 | `88 / 100`（通过线 70） |
| 变更规模 | 3 个文件，+120 / -8 |
| 关联提交 | `abc1234` feat: 完成点云生成模块 |

### 📝 评语
（模型给出的分点评语）

### 💡 改进建议
1. 建议给 register_point_cloud() 补充异常处理
2. 建议补充单元测试

> ✅ 已自动将 Issue 标记为 `done` 并关闭。

### 🔍 审核维度
语法正确性 · 逻辑正确性 · 安全性 · 测试覆盖 · 代码风格 · 提交信息
```

不通过时表格里是 ❌，末尾提示改为「Issue 状态保持不变，请修改后再次提交」。

## 行为细节（这些坑已经处理）

| 场景 | 行为 |
|------|------|
| LLM 返回的不是合法 JSON | **降级为不通过**（score=0），评论里附上原始输出片段，Issue 状态不变 |
| LLM 返回的 `score` 是字符串 / 越界 / 缺字段 | 自动转换、夹到 0~100、缺字段则降级不通过 |
| 模型的 `passed` 与 `score` 矛盾 | 以 `score >= 70` 为准，并打印告警 |
| LLM Key 无效 / 网络失败 / 限流 | 评论一条「审核未能完成」，**不改 Issue 状态**，Action 显示为失败（红叉）便于发现 |
| 同一个提交重复审核（Action 重跑） | 通过评论里的隐藏标记去重，**在调用 LLM 之前就短路**，不重复花 token；想强制重审加 `--force` |
| 只改了图片 / 点云文件 / 锁文件 | 视为无有效 diff，跳过审核（也不会多花一次请求去取原始 diff） |
| **GitHub 省略了 `files[].patch`** | 自动改用 `application/vnd.github.diff` 接口取原始 diff 再送审，**不会**误判成“没有可审核的代码”而跳过。diff 很大、或文件混入了异常换行符/编码时最容易触发这条路径 |
| diff 特别大 | 超出 `AGENT_DIFF_MAX_CHARS`（默认 12000 字符）后截断，并在提示词里告知模型「已截断」 |
| force-push 导致无法 compare | 自动退化为只审核最后一个提交 |
| 分支首次 push（before 全 0） | 自动改为单提交审核 |
| 标签 `done` 不存在 / `todo` 标签本来就没有 | 自动创建标签；删除不存在的标签返回 404 时静默忽略 |
| 一次提交关联多个 Issue | 逐个留评语、逐个按结论处理 |

## Token 消耗：什么时候会花钱

**不推送就完全不花钱。** 整个系统没有任何定时任务（没有 `schedule` / `cron`），
Agent 只在「push 到 `main`/`master`」或「你手动点 Actions 里的 Run workflow」时被唤起。

看板前端每 30 秒刷新一次，但那只是请求本机 Flask 的 `/api/tasks`（读 SQLite），
**不调用 LLM，也不消耗 GitHub API 额度**；GitHub 同步是你点「🔄 同步」时才发生。

一次 push 的调用次数与花费：

| 情况 | LLM 调用次数 |
|------|--------------|
| 提交信息里没有关联 Issue（没写 `#编号`） | **0**（不评论、不改状态） |
| 变更全是二进制 / 锁文件 / 被忽略的文件 | **0** |
| 这个提交已经审核过（Actions 重跑、重复推送同一个 SHA） | **0**（直接短路，见下文） |
| 正常提交，关联 1 个 Issue | 1 |
| 正常提交，关联 3 个 Issue | **1**（一次审核覆盖该提交关联的所有 Issue，不是每个 Issue 一次） |

想再省一点，有三个开关（都在 `.github/workflows/agent-review.yml` 里，按需打开）：

| 开关 | 效果 | 代价 |
|------|------|------|
| `paths-ignore: ["**.md", "docs/**"]` | 纯文档提交连 workflow 都不触发 | 这类提交不会被审核 |
| `concurrency.cancel-in-progress: true` | 连续快速推送时只审最新一次 | 被取消那次 push 的改动不会单独出报告 |
| `LLM_MAX_RETRIES: '1'` | LLM 失败不重试（默认 3 次） | 网络抖动时更容易失败，需手动重跑 |

另外 `AGENT_DIFF_MAX_CHARS`（默认 12000 字符）决定每次送给模型的 diff 上限，
调小可以直接降低单次花费，代价是超大提交只看得到前面一部分。

## Agent 可调参数

改 `agent/.env`（本地）或 Actions 的 Variables / Secrets：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_API_KEY` | 无（必填） | 大模型 API Key |
| `LLM_API_URL` | `https://api.deepseek.com/v1/chat/completions` | 接口地址，OpenAI 兼容 |
| `LLM_MODEL` | `deepseek-chat` | 模型名 |
| `AGENT_PASS_SCORE` | `70` | 通过线 |
| `AGENT_DIFF_MAX_CHARS` | `12000` | 送审 diff 总长度上限 |
| `AGENT_DIFF_MAX_CHARS_PER_FILE` | `4000` | 单个文件 diff 长度上限 |
| `AGENT_DRY_RUN` | `0` | 设为 1 则只打印报告 |
| `LLM_TIMEOUT` / `LLM_MAX_RETRIES` | `120` / `3` | LLM 超时与重试 |

## Agent 常见问题

**Q：Action 报 `LLM_API_KEY 未设置`？**
A：Secrets 里没配 `LLM_API_KEY`，或本地 `agent/.env` 没填。注意 Secret 名字大小写要一致。

**Q：Action 报 `403`？**
A：`permissions: issues: write` 是必须的（workflow 里已写好）；如果用了自己的 PAT，需要 Issues 读写权限。

**Q：为什么 push 了但什么都没发生（没有评论）？**
A：看 Action 日志里是不是 `⏭️ 审核跳过`——最常见原因是提交信息里没带 Issue 编号；
其次是把改动都提交到了非 `main` 分支（workflow 只监听 `main` / `master`）。

**Q：能审 Pull Request 吗？**
A：当前只监听 `push` 到 `main`/`master`。如果你们走 PR 流程，可以在 workflow 的 `on:` 里加
`pull_request: branches: [main]`，并把 `BEFORE_SHA`/`COMMIT_SHA` 换成 `${{ github.event.pull_request.base.sha }}`
与 `${{ github.event.pull_request.head.sha }}`。

**Q：审核结果会同步到看板吗？**
A：会。Agent 把 Issue 标签改成 `done` 并关闭后，看板下次点「🔄 同步」即可看到状态变为 Done
（关闭的 Issue 也会被视为 done）。Agent 不直接写看板的 SQLite，避免两个进程抢锁。

---

## 异常处理策略（看板 + Agent）

| 场景 | 行为 |
|------|------|
| Token 无效 / 过期 | 返回 401 并提示检查 `GITHUB_TOKEN` / Secrets |
| 权限不足 / 限流 | 403 区分“限流”与“权限不足”，限流时提示预计恢复时间 |
| 网络超时 / 5xx | 自动重试 3 次，退避 2s / 4s，仍失败返回中文错误 |
| 负责人不在名单 | 接口返回 400 并列出可选名单；同步解析到名单外的名字则归为未分配并告警 |
| GitHub 写失败 | 先改 GitHub 再改本地，失败时**本地数据保持不变**，前端拖拽自动回滚 |
| LLM 输出解析失败 | 降级为不通过，评论附原始输出片段 |
| 审核报告解析失败（看板侧） | 不写审核字段、卡片不显示徽章（宁可不显示也不显示错的） |
| 评论接口读失败 | 只记警告并跳过该 Issue，不影响整体同步 |
| 数据库异常 | 回滚事务 + `logging.exception` 记录到 `kanban.log` |
| 前端请求失败 | 顶部状态条红色提示，界面回滚到后端真实状态 |

---

## 常见问题

**Q：怎么增删改人员名单？**
A：两步——
① 代号名单：改 `config.py` 里的 `ASSIGNEES`（顺序即人员视图的列顺序），并同步调整
`static/app.js` 顶部的 `FALLBACK_ASSIGNEES`；列头颜色由 `config.py` 的 `ASSIGNEE_HUES` 与
前端同名常量控制，加人时补一个色相即可。
② 真名映射：在 `assignees.local.json` 里补一行 `"代号": "真名"`（该文件不会被提交）。

**Q：真实姓名会不会被提交到 GitHub？**
A：不会。GitHub 上只有代号（小明/小红/…）：Issue 正文标记、Agent 评论、日志、仓库文件都只用代号；
真名只存在本地 `assignees.local.json`（已 gitignore）与浏览器里。**请不要手动把这个文件加进 Git**
（`git add -f assignees.local.json` 会绕过忽略规则）。

**Q：卡片上没有审核徽章？**
A：按顺序排查——
① 该 Issue 还没有 Agent 报告（没 push 过、或提交信息里没带 Issue 编号）；
② 同步时带了 `?with_reviews=0`；③ 报告格式解析不出来（此时**故意不显示**，避免显示错误结论，
可看 `kanban.log` 里的“像审核报告但解析不出结论”告警并跑 `python tools/test_review_parse.py`）；
④ 该 Issue 一条评论都没有（看板不会为它发请求，这也是一种加速）。

**Q：点「同步」后卡片没有分支和提交？**
A：分支通过**名字里的 Issue 编号**匹配（如 `12-fix`、`issue/12`、`feature/12-xxx`）；提交通过**提交信息里的 `#12`** 匹配。
都不符合时自然为空。另外为保护 API 额度，单次同步最多为 25 个任务查询分支提交，可用 `SYNC_BRANCH_COMMIT_BUDGET` 调整。

**Q：为什么改状态 / 改负责人也会失败？**
A：状态是 GitHub 标签、负责人是 GitHub 正文，都必须能写 GitHub 才生效（先改远端、成功后再落本地）。
Token 失效或断网时会明确报错，本地数据不会被改成不一致的状态。

**Q：删除按钮会把 GitHub Issue 也删掉吗？**
A：不会。删除只清理本地缓存，下次同步该 Issue 会重新出现。彻底移除请在 GitHub 上关闭或删除 Issue。

**Q：关闭的 Issue 怎么处理？**
A：默认 `SYNC_ISSUE_STATE=all`，关闭的 Issue 会视为 `done` 保留在看板上；设为 `open` 则只同步未关闭的 Issue。

**Q：手机上看板能用吗？**
A：可以。窄屏下列会竖向堆叠，拖拽在触屏上不可用，请用卡片右下角的 `←` `→` 按钮：
状态视图里切换状态，人员视图里切换负责人（顺序为 6 人 + 未分配）。

---

## 技术栈

- 后端：Python 3.9+ / Flask / Flask-CORS / requests
- 前端：原生 HTML + CSS + JavaScript（无任何框架）
- 存储：SQLite（本地缓存，支持旧库自动迁移）
- Agent：Python + requests（不引入 PyGithub），兼容 DeepSeek / OpenAI 协议
- 数据源：GitHub REST API v3 + GitHub Actions
