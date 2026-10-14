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
├── server.py                 # Flask 后端（REST API + 页面渲染 + 登录/权限）
├── auth.py                   # 登录、Session、权限校验（也可当命令行用：list / reset）
├── github_client.py          # GitHub API 封装（重试、限流、负责人解析、分支/提交、审核报告解析）
├── database.py               # SQLite 操作（tasks / users / operation_logs，含轻量迁移）
├── config.py                 # 配置读取、人员名单、会话密钥、环境变量校验
├── requirements.txt          # Python 依赖
├── .env.example              # 看板配置模板（复制成 .env）
├── assignees.local.json      # 负责人「代号 -> 真名」映射（本地专用，已 gitignore）
├── secret.key                # 会话密钥（首次启动自动生成，已 gitignore）
├── tools/
│   ├── check_names.py        # 推送前自检：真名有没有混进 Git
│   └── test_review_parse.py  # 审核报告解析自检（离线，改动报告模板后必跑）
├── .githooks/
│   └── pre-commit            # 可选的提交前守卫（默认未启用，见下文）
├── .gitignore
├── templates/
│   ├── index.html            # 看板页面
│   └── login.html            # 登录页
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
| 📊 状态视图 | Todo / Doing / Done 三列，低饱和配色区分（淡赭石 / Tiffany 蓝 / 灰绿），列头带同色小圆点 |
| 👥 人员视图 | 固定 6 人各一列 + 1 列「未分配」，列头紫色系且每人色相不同 |
| 👤 负责人徽章 | 每张卡片显示负责人，**点击即可快速切换**（弹出选择器，输入序号或姓名） |
| 🖱️ 拖拽改属性 | 状态视图拖到某列 = 改状态；人员视图拖到某人列 = 改负责人 |
| 📱 移动端可用 | 响应式布局；触屏用卡片上的 `←` `→` 按钮（状态视图切状态，人员视图切负责人） |
| 🔄 一键同步 | 从 GitHub 拉取全部 Issue，含**从正文解析负责人** |
| ⚡ 实时刷新 | WebSocket 通道：别人改状态 / 认领 / 新建 / 移除 / 同步时，自己的看板立刻更新，页脚还能看到谁在线 |
| ➕ 新建任务 | 同时在 GitHub 创建 Issue，自动打状态标签并把负责人写进正文 |
| 🌿 分支 / 提交关联 | 自动识别分支名中的 Issue 编号，并汇总提交信息里的 `#编号` |
| 🤖 卡片显示 AI 评语 | 同步时读回 Agent 的审核报告，卡片上显示通过/不通过徽章，点击展开评语 |
| 🏷️ 标签自愈 | 首次使用时自动创建 `todo` / `doing` / `done` 三个标签 |
| 🛡️ 健壮性 | 网络失败重试 3 次、Token 失效明确提示、数据库异常写日志、旧库自动迁移 |
| 🔍 搜索下拉 | 输入即在搜索框下弹出匹配结果（像浏览器搜索），**不过滤、不隐藏看板卡片**，点结果或按 Enter 跳到卡片并高亮闪烁 |

## 视觉风格（低饱和 · 稳重淡雅 · Apple 弹窗动效）

界面刻意做得安静：颜色低饱和、层次靠留白与描边、**说明文字一律收进圆圈 i 的悬浮气泡**。
所有设计量都在 `static/style.css` 顶部的 `:root` 里，改一处即可全站生效。

| 设计量 | 取值 | 说明 |
|--------|------|------|
| 强调色 `--accent` | `#0abab5`（Tiffany 蓝） | 品牌色，只用在焦点环、选中态等「需要被注意」的地方 |
| 深强调 `--accent-deep` | `#0d8f8b` | 按钮底色、链接文字，保证白底上的对比度 |
| 浅底色 `--accent-soft` | `#e6f5f4` | 悬停行、拖拽高亮列 |
| 状态色 | Todo `#b5945f` / Doing `#4fa8a3` / Done `#7fa98f` | 三者饱和度接近，靠**色相**区分而不是靠刺眼程度 |
| 人员视图 | `hsl(var(--hue) 26% 94%)` 底 + `24% 38%` 字 | 低饱和紫灰系，每人一个色相，由 JS 注入 `--hue` |
| 中性色 | 文字 `#34383f`、次要 `#6b7280`、弱化 `#9aa1aa`、描边 `#e5e7eb` | 冷灰系，卡片白底浮在浅灰页面上 |
| 缓动 `--ease` | `cubic-bezier(.32, .72, 0, 1)` | Apple 那种「先快后慢、落位不弹跳」的手感 |
| 时长 `--dur` / `--dur-fast` | `.28s` / `.16s` | 弹窗、下拉用前者；按钮与悬停用后者 |

**弹窗（Apple 风格）**：遮罩 `rgba(28,32,38,.26)` + `backdrop-filter: blur(12px)`，
对话框圆角 20px、`scale(.94) → 1` 淡入；关闭时反向 `scale(.97)` 淡出。
关闭动画由 JS 的 `dismissModal()` 先加 `.closing`，180ms 后再摘掉 `.active`
（时长与 CSS 里的 `dialog-out` 对齐；动画期间重新打开不会被上一次的定时器收掉）。

**圆圈 i（所有帮助提示的家）**：组件就是 `.hint`，用法只有一行：

```html
<button class="hint hint-end" type="button" aria-label="搜索用法"
        data-hint="输入即在下方弹出匹配结果……↑ ↓ 选择，Enter 跳转，Esc 关闭。"></button>
```

说明写在 `data-hint` 里，气泡由 CSS `content: attr(data-hint)` 渲染：
鼠标悬停或键盘 focus 都会出现，**不需要 JS**，也天然对读屏友好（按钮带 `aria-label`）。
`hint-start` / `hint-end` 让气泡向左或向右对齐，避免顶出屏幕；`hint-warn` 是提醒态（暖黄）。
目前挂了 7 处，页面上不再有任何铺开的说明段落：

| 位置 | 说明内容 |
|------|----------|
| 顶栏「组员访问」旁 | 怎么让组员连上、备选地址、主机名地址、防火墙放行命令 |
| 顶栏「修改密码」旁（仅用初始密码时出现，暖黄） | 提醒尽快改掉初始密码 |
| 视图切换按钮旁 | 两个视图的拖拽语义（拖到某列改状态 / 拖到某人列改负责人） |
| 搜索框旁 | 匹配范围与 `↑` `↓` / `Enter` / `Esc` 快捷键（下拉底部只留「找到 N 条」） |
| 新建任务弹窗标题旁 | 会创建 GitHub Issue、正文写法、为什么不能直接选「完成」 |
| 修改密码弹窗标题旁 | 密码按哈希存在本机、忘了怎么重置 |
| Done 列头 | 「只能由 Agent 审核通过后自动移入，不能手动拖进来」 |

**图标原则**：不用 emoji。语义交给颜色圆点（列头、审核徽章）、左侧色条（卡片状态）、
圆圈 i（帮助提示）和 hairline 描边；顶栏左侧的看板标记是纯 CSS 画的
圆角方块 + 两条白色短杠，登录页复用同一套 `.logo`。

**其他细节**：顶栏半透明白 + 16px 模糊，内容滚到下面像隔了一层毛玻璃；
按钮按下 `scale(.97)`；卡片悬停上浮 1px；搜索下拉从右上角原地展开。
系统里开启「减弱动态效果」（`prefers-reduced-motion: reduce`）时，动画与位移会自动关掉。

**鼠标指针也是自绘的**：系统自带的 `grab`（白手）、`grabbing`、`not-allowed`（白圈）
在浅色看板上几乎看不见，所以用内联 SVG 画了两个指针，都带 1.7px 黑描边：

| 场景 | 指针 |
|------|------|
| 可拖拽的卡片（悬停） | 白底黑框的**四向移动箭头** |
| 正在拖拽（按住） | 同一形状，描边加粗到 2.6px，表示「抓起来了」 |
| 不可动的卡片 / 禁用按钮 | 白底黑框的**禁止符号**（圆圈加斜杠） |

写法是 `cursor: var(--cursor-move)`，变量里是
`url("data:image/svg+xml;utf8,<svg …>") 12 12, move` —— 括号里的原生关键字是兜底，
万一浏览器不支持 SVG 指针（比如老 Safari、iOS），自动退回系统指针，功能不受影响。

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
GITHUB_REPO=你的用户名/任务仓库名
```

> ⚠️ `GITHUB_REPO` 填的是**任务仓库**，不是看板代码仓库 —— 见下面的「仓库拆分」。

### 仓库拆分：看板代码 / 任务代码分开放

看板自己的代码和「项目的代码 + 任务」是**两个仓库**，别混在一起：

| 仓库 | 放什么 | 例子（本项目的实际配置） |
|------|--------|--------------------------|
| 看板代码仓库 | `server.py`、`static/`、`templates/`、`database.py`、`auth.py`、`realtime.py`、`agent/`、`tools/` | `NGBZL/3Dcooperate` |
| **任务仓库**（`GITHUB_REPO` 填这个） | 每张任务卡 = 一个 Issue；项目自己的分支、提交、PR | `NGBZL/3D-reconstruction-code` |

为什么要拆：看板仓库里是「看板这个工具」，任务仓库里是「被看板管理的项目」。
混在一起会让任务 Issue 和看板自身的提交记录互相干扰，也没法把仓库单独分享给组员。

**换任务仓库时会发生什么**：看板把本地 SQLite 当作 GitHub 的缓存，
而 **Issue 编号只在单个仓库内唯一**（A 仓的 #1 和 B 仓的 #1 是两件不相干的事）。
所以每次启动都会做一次「缓存归属体检」，**证据优先**：

1. `board_meta` 表里记着「上次用的是哪个仓库」（`repo` 键）
2. 每条缓存的 `html_url`（`https://github.com/owner/repo/issues/12`）是硬证据

只要出现下面任一情况，就**自动清空任务缓存**并打一条醒目日志（用户、密码、操作日志都保留）：

* 配置的 `GITHUB_REPO` 和记录里的仓库不一致
* 缓存里任务的 `html_url` 指向**别的**仓库（说明记录过时了，以证据为准）
* 从没记录过、又推断不出归属，但缓存里已经有任务（无法确认归属，宁可从 GitHub 重拉）

清完之后点一次「同步」即可从新仓库重新拉取。这样设计是因为：拿旧仓库的编号去写新仓库的
Issue，会把标签/负责人改到完全不相关的任务上 —— 宁可多同步一次，也不能写错仓库。

**Agent 审核栈要放在任务仓库里**：`.github/workflows/agent-review.yml` + `agent/` 必须存在于
**任务仓库**，因为它要审核的是推到任务仓库的代码、并关闭任务仓库里的 Issue。
同时别忘了在任务仓库的 `Settings → Secrets and variables → Actions` 里配 `LLM_API_KEY`
（Secret 不能跨仓库复制，需要在新仓库里再配一次）。

**Token 权限**：`GITHUB_TOKEN` 至少要能读写**任务仓库**的 Issues；
要用看板上的**分支 / PR** 功能，还要给 **Pull requests** 读写；
如果还想用 API 往仓库里推改动，再加 Contents 读写（改 workflow 还需要 Workflows 读写）。

> 少权限时的表现是明确的 403，不会被悄悄忽略：
> 建分支需要 **Contents: Read and write**，提 PR / 合并需要 **Pull requests: Read and write**。

### 3. 获取 GitHub Token

1. 打开 GitHub → 右上角头像 → **Settings** → **Developer settings** → **Personal access tokens**
2. 推荐创建 **Fine-grained token**（可精确到单个仓库），权限勾选：
   - **Issues**：Read and write（读写任务、改标签、改正文、评论、关闭）
   - **Contents**：Read（读取分支与提交）——要用看板建分支则给 **Read and write**
   - **Pull requests**：Read and write（提 PR / 看 PR 状态 / 组长合并）
   - **Metadata**：Read（必选，GitHub 强制）
3. 生成后复制到 `.env` 的 `GITHUB_TOKEN`（注意不要带空格或换行）

### 4. 自检（可选但强烈建议）

```bash
python github_client.py     # 校验 Token / 仓库 / 标签
python database.py          # 初始化 SQLite，并打印各状态 / 各负责人任务数
python tools/test_auth.py   # 离线：登录 / 权限 / 移除恢复 / 同步只写变化（不动真库）
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

## 多用户登录与权限

6 个人各自在自己电脑上登录同一个看板，**只能操作自己的任务**，而且**不能手动把任务标成「完成」**。

### 登录

打开地址后先看到登录页，**输入你的真实姓名**（例如「张三」）：

> 姓名对照表在本地 `assignees.local.json`（代号 → 真名）。代号只是 GitHub/数据库内部用的标识，
> **不需要也不建议记住它**，登录框里也不再接受代号。
> 万一这个文件丢了/为空，登录会自动退化为「可以用代号登录」，避免整站进不去。

初始密码统一是 `123456`，**登录后请立刻点右上角 🔑 改掉**（还在用初始密码时页面会持续提醒）。
密码用 werkzeug 的 scrypt 哈希存储，数据库里没有明文；连续输错 5 次会锁定 60 秒。

有人忘记密码时，在本机跑命令行工具重置（不需要停服务）：

```bash
python auth.py list            # 看有哪些人、谁还在用初始密码
python auth.py reset 小美       # 重置为初始密码 123456
python auth.py reset 小美 新密码  # 重置为指定密码
```

登录态存在 Flask session 里，默认保持 12 小时（`SESSION_LIFETIME_HOURS` 可调）；
服务重启不会踢人下线，因为会话密钥持久化在本地 `secret.key`（已 gitignore，**不要提交**：
泄露了别人就能伪造登录态）。

### 权限规则

角色存在数据库里（`users.role`），两种：**组员 member**（默认）与**组长 lead**。
设置组长的命令（在本机执行，不需要重启服务）：

```bash
python auth.py role 小美 lead      # 设为组长（也可以写真名）
python auth.py role 小美 member    # 取消组长
python auth.py list                # 看每个人当前的角色
```

| 操作 | 组员 | 组长 |
|------|------|------|
| 查看所有人的任务 | ✅ | ✅ |
| 改状态（Todo/Doing）| 只有负责人本人；未分配的任务谁都能「拖走 = 认领并推进」 | **任何人的卡片都能推** |
| 认领任务 | ✅（拖动即认领） | ✅ |
| **把任务指派给别人** | ❌ 只能认领给自己 | ✅ **人员视图里想拖给谁就拖给谁**（含从别人手里拿走） |
| 把自己名下的任务放回「未分配」 | ✅ | ✅ |
| 新建任务 | 只能建给**自己**，或选「未分配」 | **可以直接建给别人**（派活） |
| 建分支 / 提 PR | 只能给自己的任务 | 任何人的任务 |
| **放行 PR / 合并 PR** | ❌ | ✅（合并前必须先放行） |
| 删除（从看板移除） | 负责人本人，或未分配的任务 | 任何任务 |
| 同步 GitHub | ✅（只读 GitHub，不改远端） | ✅ |
| 修改密码 | 只能改自己的 | 只能改自己的 |

* 别人的任务对**组员**是置灰、不可拖拽的；对**组长**是正常可拖的
* **未分配的任务：谁把它拖走，就等于认领给自己**
  - 状态视图：拖到任何一列（Todo / Doing）＝ 一次请求同时「认领给自己 + 改成那一列的状态」
  - 人员视图：拖到自己的列 = 认领；`←` 按钮也能一键认领
  - 认领之后这张卡就归本人了（组长除外），别人再想动会被 403 拒绝（也就是「移完之后不能动」）
  - 只改状态、不带认领的请求仍然被拒（提示先认领），防止绕过「谁的任务谁负责」
* **已完成的任务是终态**：连组长也不能把它拖出 Done（见下一节）
* 后端会独立校验一遍（返回 403），前端的限制只是提前告诉你，不是安全边界
* 想「所有人都是组长」？把 `.env` 里的 `ALLOW_ASSIGN_TO_OTHERS` 设为 `1` 即可（不推荐）

### 禁止「假完成」：Done 是单向门

**Done 列既不接受手动拖入，也不允许把任务移出去**：

- 不能拖进来：
  - 前端：拖到 Done 列会提示「不能手动完成：请提交代码等待 Agent 审核，审核通过后会自动移到 Done」，
    列的标题上带斜纹底色与圆圈 i，`→` 按钮也不会把你送进 Done
  - 后端：`PUT /api/tasks/<n>` 带 `status=done` 一律返回 **403**，GitHub 标签不会被改
  - 新建任务时也不能直接选 Done
  - 只有 **Agent 审核通过后写入 GitHub 标签**，同步回看板才显示在 Done 列
    （`DONE_REQUIRES_AGENT=0` 可以关掉这条限制，一般不要关）
- 也不能移出去（**终态**）：
  - 前端：已完成的任务 `draggable=false`（拖都拖不起来，鼠标是普通箭头），
    `←` 退回上一列的按钮置灰，卡片上写着「已完成的任务不能再改动」
  - 后端：`PUT /api/tasks/<n>` 想把 `done` 改成别的状态一律 **403**
    （**负责人本人也不行** —— 否则随手拖出来就等于绕过了 Agent 审核这道闸门）
  - 同步不受这条规则影响：Agent 关掉 Issue 后同步照样能把状态写成 `done`

确实需要返工（Agent 误判）时有两种办法：在 GitHub 上重新打开 Issue 并改回 `todo`/`doing` 标签再同步，
或者把 `.env` 里的 `DONE_IS_FINAL` 设为 `0`，负责人本人就能把完成的任务退回 Doing。

### 分支与 Pull Request（看板上直接提 PR、组长放行与合并）

每张卡片右下角都有一个 **PR** 按钮，打开「分支与 PR」面板：

| 步骤 | 谁来做 | 看板做了什么 |
|------|--------|--------------|
| ① 建分支 | 任务负责人本人 / 组长 | 基于 `main` 建 `task/<编号>-<英文 slug>`（重复点不会报错，已存在就直接用），并把分支名记到卡片上 |
| ② 推代码 | 负责人本人（本地 git） | 卡片在同步时会显示分支名与最近提交 |
| ③ 提 PR | 任务负责人本人 / 组长 | `POST /api/tasks/<n>/pr` → PR 标题带 `(#编号)`，正文自动写 `Closes #编号` |
| ④ Agent 审核 | 自动（`pull_request` 事件触发） | 在 **Issue** 下评论审核报告；**只审核不定案**：不打 done、不关 Issue |
| ⑤ 放行 | **只有组长** | 卡片徽章变「已放行」，并在 PR 下留言「组长已放行」 |
| ⑥ 合并 | **只有组长**（必须先放行 + Agent 过审） | 默认 squash 合并；**合并成功后看板立刻把 Issue 标成 `done` 并关闭**，卡片当场进 Done |

### 合并的两道闸门（为什么不会「没审就合」「合了却没完成」）

合并是**不可逆**的动作，所以两道闸门都放在合并**之前**，缺一个就 403：

1. **组长放行**：组员不能放行也不能合并；
2. **Agent 过审**：合并时会**现读** Issue 评论里最新的审核报告（不依赖本地缓存），
   报告是「不通过」或还没有报告 → 拦住，并把你该做什么写清楚。
   组长确实要先合，可以勾面板里的「**强制合并**」（会记进操作日志）。

合并**之后**由看板补齐最后一步 —— 这一步很关键，因为：

* 组员自己在 GitHub 上提的 PR 未必带 `Closes #编号`，合并后 Issue 还开着；
* Agent 在 PR 事件里是**只审核不定案**的，不会打 done 标签；
* 合并到主干那次 push 的提交信息往往是 `标题 (#PR编号)`，里面没有 Issue 编号，
  Agent 同样不会定案（而且 PR 编号会被 Agent 主动忽略，见下）。

所以 `POST /pr/merge` 成功后，看板自己执行「**标 done + 关 Issue**」，卡片立刻进 Done（不必等下次同步）。
写 GitHub 失败也不会影响「已经合并」这个事实，只会在返回消息里提示你重试。

**兜底 1：Issue 关闭即 Done。** 看板判定状态时，`closed` 的优先级**高于标签**：
一个 Issue 只要被关闭（不管是 Agent 关的、还是 PR 正文的 `Closes #N` 自动关的、
还是有人在网页上手动关的），卡片就进 Done —— 哪怕它还挂着 `todo` / `doing` 标签。
这就是「PR 合并了、卡片却赖在 Doing」的直接原因，已经修掉。

**兜底 2：补收尾按钮。** 同步时若发现「PR 已合并、卡片却还不是 Done」（历史遗留、
或者合并时写 GitHub 失败），日志里会点名；组长打开该卡片的 PR 面板会看到
「**标记完成并关闭 Issue**」按钮，点一下补齐。

**兜底 3：PR 换新。** 卡片上记着的 PR 如果被关掉（但没合并），而同一分支上又开了新 PR，
同步会自动改认新的那个，不会一直指着作废的 PR。

* 卡片上的 PR 徽章一眼能看出状态：`待放行` / `已放行` / `已审核`（open）/ `草稿` / `已合并` / `已关闭`，
  点徽章直接跳 GitHub 上的 PR
* 组员的 PR 会**卡在「待放行」**：自己不能放行、也不能合并（前端不显示这两个按钮，后端也会 403）
* 别人**直接在 GitHub 上提的 PR** 也能被认领到卡片上：同步时会按 head 分支名匹配（1 次列表请求 + 有 PR 的卡片各 1 次刷新，
  受 `PR_SYNC_BUDGET` 限制，默认 25）
* 合并方式由 `PR_MERGE_METHOD` 控制（默认 `squash`），主干分支名由 `DEFAULT_BRANCH` 控制（默认 `main`）
* **PR 编号不会污染审核**：squash 合并产生的提交信息形如 `feat: xxx (#12)`，其中 `#12` 是 PR 编号；
  Agent 会先判断编号到底是 Issue 还是 PR，是 PR 就跳过（不评论、不花 token）——
  GitHub 的 Actions 令牌通常也没有 PR 写权限，去评论只会 403

### 操作日志

所有写操作都会记进 `operation_logs` 表（`GET /api/logs`，支持 `?mine=1&limit=50`）：

| 记录内容 | 示例 |
|----------|------|
| 谁做的 | 小美（代号；真名不会写进日志和数据库） |
| 做了什么 | create_task / update_status / update_assignee / hide_task / restore_task / sync / sync_update / login / logout / change_password |
| 改了什么 | old_value → new_value |
| **被拒绝的尝试** | `result=denied` + 原因（例如「这是 小刚 的任务」），方便事后追责 |

## 使用流程

### 状态视图（默认）

1. 登录后点击「🔄 同步」把 GitHub 上的 Issues 拉到看板
2. 点击「➕ 新建任务」创建任务（会在 GitHub 上真实创建 Issue，负责人默认是你自己）
3. 拖动**自己的**卡片改变状态，或在手机上点卡片上的 `←` `→`
4. 每次状态变化都会同步修改 GitHub Issue 的 `todo` / `doing` / `done` 标签
5. 「完成」不由你决定：提交代码 → Agent 审核通过 → 自动进入 Done

### 人员视图

1. 点击顶部「👥 人员视图」，看板变成 6 人列 + 1 列「未分配」
2. **拖拽**卡片到某人的列 = 把负责人改成他；拖到「未分配」列 = 取消分配
3. 也可以**点击卡片上的负责人徽章**（👤 姓名），在弹出的选择器里输入序号或姓名
4. 每次改动都会重写 GitHub Issue 正文里的 `**负责人**: 姓名` 行（正文其余内容原样保留）

### 搜索：下拉跳转（不过滤看板）

搜索**不再过滤看板**。以前的做法是把不匹配的卡片藏起来，容易让人误以为任务丢了，所以改成浏览器那种交互：

1. 在搜索框里输入 → 框下立刻弹出匹配结果（标题、`#编号`、负责人、状态，命中的字用浅色 `<mark>` 高亮）
2. `↑` `↓` 选择，`Enter` 跳到当前项，`Esc` 关闭，点面板外面也会收起
3. 跳转时看板**自动滚到那张卡片并闪烁描边**，不会把别的卡片藏起来
4. 匹配范围：标题 / 描述 / Issue 编号 / 负责人真名与代号 / **AI 评语**；结果面板底部只显示「找到 N 条」，
   快捷键说明在搜索框旁的圆圈 i 里
5. 一张都没匹配到时面板显示「没有匹配「xxx」的任务」，看板本身依然是完整的

### 同步：谁覆盖谁（别再被「同步改回去」坑到）

`🔄 同步` 的方向是 **GitHub → 本地**，所以远端就是这几个字段的权威来源；
但同步**只写真正发生变化的行**，不会再每次把整库刷一遍：

| 字段 | 同步时以谁为准 | 说明 |
|------|----------------|------|
| 状态（todo/doing/done） | **GitHub** | 远端标签/关闭状态变了就跟着变，并在提示里说明原因 |
| 负责人 | **GitHub** | Issue 正文里的 `**负责人**` 行是权威值 |
| 标题 / Issue 状态 | **GitHub** | 你在网页上改了标题，同步后会更新 |
| AI 评语 / 评分 | **GitHub 评论** | 只在解析到新报告时覆盖整组，解析不到就保留本地 |
| 是否「已从看板移除」 | **本地** | 同步**永远不会**把被你移除的卡片拉回来（要恢复请点页脚「全部恢复」） |

- 每次同步的提示会写清楚：「更新 N，未变化 M」，以及**被 GitHub 改动的卡片清单**
  （例如 `#12 状态 doing → done；Issue 状态 open → closed`），不再出现“神不知鬼不觉被改回去”
- 每个远端驱动的改动都会记一条 `sync_update` 操作日志，事后可查
- 本地写操作（改状态/改负责人/新建）依旧是 **GitHub 优先**：先写远端成功，再写本地库；
  远端失败会直接报错，不会留下“本地以为改了、GitHub 上没改”的假象
- 想彻底删掉一张卡片（同步也不该再出现）只能在 GitHub 上关闭或删除 Issue；
  看板上的 × 只是**本地移除**，语义是「这张我暂时不想看」，随时可恢复

### 实时通道：WebSocket

看板之间用 WebSocket 保持同步：别人改状态、认领、新建、移除、点同步时，
**自己的看板立刻跟着变**，不用等轮询（页脚会显示 `● 实时已连接 · 在线 2 人（张三、李四）`）。

| 项目 | 做法 | 为什么 |
|------|------|--------|
| 协议 | 浏览器原生 `WebSocket`，端点 `GET /ws` | 不引 socket.io，**不需要 CDN**：组员在只有热点、没外网的手机上也能用 |
| 服务端实现 | `simple_websocket.Server(environ)`，Werkzeug 开发服务器直接跑 | 只多一个 `simple-websocket` 依赖，不用 gevent/eventlet，也不用换 WSGI 服务器 |
| 事件内容 | 只推「谁、改了哪张、做了什么」 | **不推任务数据**：任务真值只有一份（SQLite + GitHub），客户端收到后自己重拉 `/api/tasks`，不会出现两份状态打架 |
| 鉴权 | 握手**之前**校验 Flask session，未登录直接 401 | 不会「先建连接再踢人」；WS 通道**不接受任何写操作**，写操作一律走带权限校验的 HTTP 接口 |
| 线程安全 | 每条连接一个发送锁 | 广播可能同时来自多个 HTTP 请求线程，而 `send` 不是线程安全的 |
| 心跳 | 协议级 ping（25s）+ 客户端应用层 ping | 双方都能尽快发现断线；代理掐空闲连接也不怕 |
| 断线 | 客户端指数退避重连（1s→30s 带抖动），连不上就退回 30 秒轮询 | **WebSocket 挂了功能照样可用**，只是实时性差一点 |
| 真实姓名 | 事件里只有代号，真名由前端用自己的映射表翻译 | 跟「真名不出本地」的约定一致 |

事件格式（JSON 文本帧）：

```jsonc
// 服务端 → 客户端
{"type": "hello",         "protocol": 1, "you": {...}, "server_time": 1730000000.0}
{"type": "presence",      "count": 2, "online": 2, "users": [{"username": "小明", "connections": 1}]}
{"type": "tasks_changed", "action": "update_task", "issue": 12, "by": "小明", "detail": "状态=doing", "at": ...}
{"type": "pong",          "at": ...}
{"type": "error",         "message": "..."}

// 客户端 → 服务端
{"type": "ping"}     // 保活
{"type": "whoami"}   // 想重新拿一次 hello
```

事件由**服务端**在写操作成功后广播（新建 / 改状态 / 改负责人 / 移除 / 恢复 / 同步），
所以即使改动来自 GitHub 网页或 Agent，只要有人点了同步，其他人也会立刻看到。

实现上的两个坑（都已在代码注释里写明）：

1. **路由必须声明 `websocket=True`**（`@app.route("/ws", websocket=True)`）。
   握手请求带 `Connection: Upgrade` + `Upgrade: websocket`，Werkzeug 会把这次请求的
   url_scheme 认成 `ws`，只匹配声明了 `websocket=True` 的路由；不写这个参数会在
   **进入视图之前**就抛 `WebsocketMismatch`（表现为 400，从日志里看不出原因）。
2. **`app.run(..., threaded=True)`**：每条 WebSocket 连接要占一个线程，必须开多线程。

关掉实时通道也能跑：没装 `simple-websocket` 时 `/ws` 返回 503，前端自动退回轮询，
`/api/health` 的 `realtime.available` 会是 `false`。

### 帮助提示都在圆圈 i 里

页面上不铺开说明文字，所有「怎么用 / 怎么回事」都挂在对应控件旁边的圆圈 i 上，
鼠标悬停或键盘 Tab 聚焦即可看到（详见上面的「视觉风格」一节）。
唯一保留的整段文字是**空看板时的引导**：它本身就是「没有数据」的状态占位，
收进气泡反而会让空页面无从下手。

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

### 改完权限 / 登录 / 同步逻辑后：跑一遍安全自检

```bash
python tools/test_auth.py   # 离线，不需要 Token；用临时库，不动 kanban.db，也不写 GitHub
```

覆盖十二个部分，任何一条不过就以退出码 1 结束：

1. **路由审计**：所有 `/api/*`（除 `/api/server-info`）未登录必须 401，防止以后新增接口漏加登录校验
2. 登录（真名 / 代号拒绝 / 密码错误 / 锁定）与密码策略
3. 权限：改别人的状态、替别人建任务、把未分配的指派给他人，全部 403
4. 禁止「假完成」：手动改成 done 被拒
5. **Done 是终态**：已完成的卡片改回 doing/todo 一律 403（负责人本人也不行），但
   Agent 通过同步写 done 不受影响
6. 认领 / 放回未分配 / 改负责人
7. **未分配的任务**：只改状态被拒、想指派给别人被拒、「认领给自己 + 改状态」一次请求成功，
   认领之后别人再动一律 403
8. **从看板移除**：默认列表看不到、`include_hidden=1` 能看到、同步不会把它拉回来、恢复后又能看到
9. **同步只写真正的变化**：第二次同步更新数为 0、`updated_at` 不被刷新、远端变化被正确识别并写日志
10. 真名不会落进操作日志（只记代号）
11. **组长 vs 组员**：组长可以给别人分派、推别人的卡片、替别人建任务；组员这些操作仍然 403
12. **分支 / PR**：负责人本人或组长能建分支、提 PR；放行与合并只有组长能做，且必须先放行

### 改完实时通道后：跑一遍 WebSocket 自检

```bash
python tools/test_ws.py     # 离线，不需要 Token；用真服务器 + 真 WebSocket 连接，不动真库
```

它会在线程里起一个真实的 Werkzeug 服务器（数据库用副本），然后用
`simple_websocket.Client` 真的连上去，断言 26 项：未登录被拒、`hello` / `presence`、
第二条连接上线后在线数变化、**通过 HTTP 改一张卡片后另一个连接收到 `tasks_changed`**
（且事件里只有代号）、`ping`/`pong`、未知消息回 `error`、断开后在名单里回收、
`/api/health` 里的实时通道统计。

### 改完前端交互后：跑一遍界面自检（可选）

搜索下拉、点结果跳转、从看板移除 / 恢复、圆圈 i 的说明、弹窗开合动画、
实时通道的接线（连 `/ws`、在线名单、收到事件触发刷新、断线重连）这类纯前端行为，
光靠 `node --check` 是查不出来的。`tools/test_ui.js` 会在 jsdom 里真的加载
`templates/index.html` 并执行 `static/app.js`（fetch 与 WebSocket 全用桩，
不连后端、不写文件），然后模拟输入、`↑` `↓`、`Enter`、`Esc`、点外部、点恢复按钮、
点开/关闭弹窗、推一条 WebSocket 事件，断言下拉结构、`<mark>` 高亮、卡片闪烁、页脚计数、
每个圆圈 i 的 `data-hint` 是否齐全、`.active`/`.closing` 弹窗状态、在线名单与重连、
已完成的任务是否真的拖不动、未分配的卡片拖走时是否一次请求带上「认领 + 新状态」等 80 项：

```bash
npm install jsdom          # 只需一次（node_modules 已被 .gitignore 忽略）
node tools/test_ui.js
```

```bash
npm install jsdom          # 只需一次（node_modules 已被 .gitignore 忽略）
node tools/test_ui.js
```

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
| GET | `/login` | 登录页（唯一的匿名页面） |
| POST | `/login` | 提交登录（`username` 填**真实姓名**；代号已不接受） |
| GET | `/logout` | 登出 |
| GET | `/api/me` | 当前登录用户（前端据此显示与控权） |
| POST | `/api/change-password` | 修改自己的密码 |
| GET | `/api/logs` | 操作日志（`?limit=50&mine=1`） |
| GET | `/api/tasks` | 获取全部任务（支持 `?status=todo`、`?assignee=小明`、`?mine=1`、`?q=关键字`、`?include_hidden=1` 连已移除的一起返回） |
| POST | `/api/tasks` | 新建任务，同时在 GitHub 创建 Issue（负责人默认是自己；**不能建到 done**） |
| PUT | `/api/tasks/<issue_number>` | 更新状态和/或负责人（**只有负责人本人**；**不能改成 done**，**已 done 的也不能改成别的**） |
| PUT | `/api/tasks/<issue_number>/assignee` | 仅更新负责人（只能认领给自己，或把自己名下的放回未分配） |
| DELETE | `/api/tasks/<issue_number>` | **从看板移除**（本人或未分配）：只把本地记录标记为隐藏，GitHub 上的 Issue 不动，可恢复 |
| POST | `/api/tasks/<issue_number>/restore` | 恢复被移除的某张卡片（`PUT` 同样可用） |
| POST | `/api/tasks/restore-all` | 一键恢复全部被移除的卡片（页脚「全部恢复」按钮用的就是它） |
| POST | `/api/sync` | 从 GitHub 同步（`?with_meta=0` 跳过分支/提交、`?with_reviews=0` 跳过审核报告，可加速） |
| GET | `/api/assignees` | 负责人名单与各自任务数 |
| GET | `/api/health` | 健康检查（`?deep=1` 会真实请求 GitHub 校验 Token） |
| GET | `/api/server-info` | 本机局域网访问地址（**无需登录**，登录页也要显示它） |
| GET | `/ws` | **实时通道（WebSocket）**：推送 `tasks_changed` 与在线名单；未登录在握手前就 401 |

**所有 `/api/*` 都需要登录**（未登录返回 401，前端会自动跳登录页），只有三处例外：
登录页 `/login`、`/logout` 与 `/api/server-info`（登录页要显示局域网地址）；
`/ws` 同样要求登录，只是在**升级协议之前**就用 401 拒绝。
新增接口时记得加 `@auth.api_login_required`：`python tools/test_auth.py` 会自动审计所有路由，
漏加装饰器会直接判 FAIL。

```bash
# 先用真实姓名登录拿到 Cookie（张三 是占位示例，换成你自己的姓名）
curl -c cookie.txt -X POST http://localhost:5000/login \
  -d "username=张三&password=123456"

# 新建任务（负责人默认是自己）
curl -b cookie.txt -X POST http://localhost:5000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"title":"完成点云配准模块","description":"ICP + 粗配准","status":"todo"}'

# 改状态（只能改自己的；改成 done 会被 403，已 done 的改成别的也会被 403）
curl -b cookie.txt -X PUT http://localhost:5000/api/tasks/12 \
  -H "Content-Type: application/json" -d '{"status":"doing"}'

# 认领未分配的任务（只能填自己；填别人会 403）/ 把自己名下的放回未分配
curl -b cookie.txt -X PUT http://localhost:5000/api/tasks/12/assignee \
  -H "Content-Type: application/json" -d '{"assignee":"小美"}'
curl -b cookie.txt -X PUT http://localhost:5000/api/tasks/12/assignee \
  -H "Content-Type: application/json" -d '{"assignee":"未分配"}'

# 看自己的操作日志
curl -b cookie.txt "http://localhost:5000/api/logs?mine=1&limit=20"
```

## 数据库表结构

```sql
CREATE TABLE tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number      INTEGER UNIQUE,   -- GitHub Issue 编号
    title             TEXT NOT NULL,
    description       TEXT,             -- Issue 正文（不含“负责人”行）
    status            TEXT DEFAULT 'todo',   -- todo / doing / done
    assignee          TEXT DEFAULT '',       -- 负责人代号；空串 = 未分配
    assignee_username TEXT DEFAULT '',       -- 负责人登录名（当前等于 assignee，自动同步）
    branch            TEXT,             -- 关联分支名
    commits           TEXT DEFAULT '[]',     -- 关联提交（JSON 字符串）
    html_url          TEXT DEFAULT '',       -- Issue 网页地址
    state             TEXT DEFAULT 'open',   -- open / closed
    review_score      INTEGER DEFAULT NULL,  -- Agent 审核评分 0~100
    review_passed     INTEGER DEFAULT NULL,  -- 1=通过 / 0=不通过 / NULL=无结论
    review_comment    TEXT DEFAULT '',       -- Agent 评语（含改进建议）
    updated_at        TEXT
);

-- 登录用户：username 是**代号**，真名只存在本地 assignees.local.json 里
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,  -- 小明 / 小红 / …
    display_name  TEXT DEFAULT '',       -- 预留；当前由真名映射表解析
    password_hash TEXT NOT NULL,         -- werkzeug scrypt 哈希，无明文
    created_at    TEXT,
    last_login_at TEXT
);

-- 操作日志：谁、对哪个 Issue、做了什么、结果如何（含被拒绝的尝试）
CREATE TABLE operation_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT,
    issue_number INTEGER,
    action       TEXT NOT NULL,
    old_value    TEXT DEFAULT '',
    new_value    TEXT DEFAULT '',
    result       TEXT DEFAULT 'ok',      -- ok / denied / error
    detail       TEXT DEFAULT '',
    timestamp    TEXT
);
```

> **旧库无需删库重建**：启动时会读取 `PRAGMA table_info`，把缺失的列（`assignee`、
> `assignee_username`、`commits`、`html_url`、`state`、`review_score`、`review_passed`、
> `review_comment`）逐个 `ALTER TABLE ADD COLUMN` 补齐，并自动创建 `users` /
> `operation_logs` 两张表、把老任务的 `assignee_username` 补成 `assignee`；
> 并用 `try/except` 兜住 “duplicate column name” 的情况，重复启动、并发启动都不会报错。

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
| 关联到一个**不存在的 Issue 编号**（提交信息写错） | 只影响那个编号：其它 Issue 照常收报告、照常改状态；写错的编号会写进 Action 摘要并让运行变红，方便发现笔误 |

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
另外 PR 的评论/标签走的是 **Pull requests** 权限（Actions 默认令牌没有），所以 agent 会**跳过 PR 编号**、
只对 Issue 定案 —— 如果你看到「只关联到 Pull Request…跳过审核」，那是预期行为。

**Q：为什么 push 了但什么都没发生（没有评论）？**
A：看 Action 日志里是不是 `⏭️ 审核跳过`——最常见原因是提交信息里没带 Issue 编号；
其次是把改动都提交到了非 `main` 分支（`push` 事件只监听 `main` / `master`，
但 PR 事件会审核 PR 里的改动）。

**Q：能审 Pull Request 吗？**
A：能，而且默认就是按「PR 只审核不定案」的规则跑的（看板的分支/PR 流程依赖这一点）：

* `pull_request`（opened / synchronize / reopened / ready_for_review）触发审核，
  审核范围用 `base.sha ... head.sha`；
* 通过后**只在 Issue 下留报告**——不打 `done` 标签、不关闭 Issue（`AGENT_NO_CLOSE=1` 或
  `EVENT_NAME=pull_request` 都会打开这个模式）；
* 真正定案发生在 PR **合并到主干之后**：那次 `push` 事件按老规则处理（过审 → 打 done → 关闭 Issue），
  同时 PR 正文里的 `Closes #编号` 本身也会让 GitHub 自动关闭 Issue。

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

**Q：卡片上的 PR 徽章是怎么来的？**
A：两条路——① 从卡片上「提 PR」时直接记下编号；② 同步时拉一次 PR 列表（1 次请求），
按 **head 分支名**认领别人在 GitHub 上手动提的 PR。已经有 PR 的卡片每次同步会单独刷新它一次状态
（open / draft / merged / closed），受 `PR_SYNC_BUDGET`（默认 25）限制。
同一条分支有多个 PR 时优先取还开着的那个。

**Q：提 PR 报 403？**
A：`.env` 里那个 Token 缺 **Pull requests: Read and write** 权限。
建分支只需要 Contents 读写，提 PR / 合并需要 Pull requests 读写；
合并前还必须先由组长「放行」、且 Agent 过审（这是看板自己的闸门，和 GitHub 的分支保护无关）。

**Q：组长放行并合并了，卡片怎么没进 Done？**
A：按顺序排查——
① **PR 是否真的合并了**：看 PR 页面是不是还开着。看板的「合并」需要 Token 有
**Pull requests: Read and write**；缺权限时接口会返回 403（消息里写明），PR 其实没合。
② **Issue 是否还开着**：看板现在把「Issue 已关闭」直接当 Done（优先级高于标签），
所以合并后只要 Issue 关了就一定进 Done；去 GitHub 确认 Issue 状态。
③ **卡片上记的是不是那个 PR**：卡片记的 PR 被关掉、同一分支又开了新 PR 时，同步会自动改认新的；
手动在 GitHub 上换过分支名的话，点一次「同步」再看。
④ 确实漏了收尾 → 组长打开卡片 PR 面板点「标记完成并关闭 Issue」。

**Q：为什么改状态 / 改负责人也会失败？**
A：状态是 GitHub 标签、负责人是 GitHub 正文，都必须能写 GitHub 才生效（先改远端、成功后再落本地）。
Token 失效或断网时会明确报错，本地数据不会被改成不一致的状态。

**Q：卡片上的 × 会把 GitHub Issue 也删掉吗？**
A：不会。× 的语义是「从看板移除」，只把本地这条记录标记为隐藏；
GitHub 上的 Issue 一个字都不动，也**不会被同步拉回来**。
页脚会出现「已从看板移除 N 条 · 全部恢复」，点一下就能全部找回。
真要彻底消失，只能在 GitHub 上关闭或删除该 Issue。

**Q：关闭的 Issue 怎么处理？**
A：默认 `SYNC_ISSUE_STATE=all`，关闭的 Issue 会视为 `done` 保留在看板上；设为 `open` 则只同步未关闭的 Issue。

**Q：别人改了我这张卡，我要刷新页面吗？**
A：不用。看板和服务器之间有一条 WebSocket（`/ws`），别人改状态、认领、新建、移除、
点同步之后你这边会**自动刷新**，底部还会提示是谁改了什么。
页脚 `● 实时已连接 · 在线 N 人` 就是在告诉你这条通道是通的；
如果它变成「实时连接断开，已退回轮询」，说明 WebSocket 被网络掐了（有些代理会这样），
此时看板退化成每 30 秒拉一次，功能不受影响，网络恢复后会自动重连。

**Q：手机上看板能用吗？**
A：可以。窄屏下列会竖向堆叠，拖拽在触屏上不可用，请用卡片右下角的 `←` `→` 按钮：
状态视图里切换状态，人员视图里切换负责人（顺序为 6 人 + 未分配）。

---

## 技术栈

- 后端：Python 3.9+ / Flask / Flask-CORS / requests / **simple-websocket（实时通道）**
- 前端：原生 HTML + CSS + JavaScript（无任何框架，实时通道用浏览器原生 WebSocket）
- 存储：SQLite（本地缓存，支持旧库自动迁移）
- Agent：Python + requests（不引入 PyGithub），兼容 DeepSeek / OpenAI 协议
- 数据源：GitHub REST API v3 + GitHub Actions
