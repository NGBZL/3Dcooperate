/**
 * 看板前端逻辑（纯原生 JS，无任何框架）
 * ------------------------------------------------------------
 * 两个互相独立的维度：
 *   * 状态维度：todo / doing / done  → 状态视图（三列）
 *   * 人员维度：固定 6 人 + 未分配   → 人员视图（7 列）
 *
 * 职责：
 *   1. 从后端 /api/tasks 读取任务并按当前视图渲染看板
 *   2. 拖拽卡片改状态（状态视图）或改负责人（人员视图）；移动端用 ← → 按钮
 *   3. 点击卡片上的负责人徽章，用 prompt 快速切换负责人
 *   4. 新建任务、从 GitHub 同步、把任务从看板移除（可恢复）
 *   5. 所有 API 失败都会在顶部状态条给出明确错误提示
 */

'use strict';

/* ============================================================
   常量
   ============================================================ */
const API_BASE = '';                        // 与后端同源，无需前缀
const STATUSES = ['todo', 'doing', 'done']; // 状态顺序（状态视图的列顺序）
const STATUS_TEXT = { todo: 'Todo', doing: 'Doing', done: 'Done' };

// 负责人「代号」名单的兜底值（若 /api/assignees 不可用，页面也能正常显示）
// 注意：这里是代号，不是真实姓名。真名只由后端 /api/assignees 的 display_names 下发，
// 所以任何写到 GitHub 上的值都只是代号。
const FALLBACK_ASSIGNEES = ['小明', '小红', '小刚', '小丽', '小强', '小美'];
const FALLBACK_UNASSIGNED = '未分配';

// 人员视图列头的色相（紫色系，与后端 config.ASSIGNEE_HUES 保持一致）
const ASSIGNEE_HUES = [265, 280, 250, 292, 272, 305];
const UNASSIGNED_HUE = 238;

/* ============================================================
   状态
   ============================================================ */
let tasks = [];                                   // 全部任务（后端返回，不含已移除的）
let hiddenTasks = [];                             // 已从看板移除的任务（用于提示与恢复）
let keyword = '';                                 // 搜索关键字
let searchActive = -1;                            // 搜索结果里当前高亮的第几项
let searchHits = [];                              // 当前匹配到的任务
let currentView = 'status';                       // 'status' | 'assignee'
let assignees = FALLBACK_ASSIGNEES.slice();       // 负责人代号列表（提交给后端的值）
let displayNames = {};                            // 代号 -> 本地真名（仅用于显示）
let aliasesConfigured = false;                    // 是否配置了本地真名映射
let unassignedLabel = FALLBACK_UNASSIGNED;        // “未分配”的显示名
let draggingIssue = null;                         // 当前拖拽中的 Issue 编号
let busy = false;                                 // 是否有正在进行的写操作
let statusTimer = null;                           // 状态条自动清除定时器
let me = null;                                    // 当前登录用户 {username, display_name, ...}
let doneRequiresAgent = true;                     // 「完成」是否只能由 Agent 写入
let doneIsFinal = true;                          // 已完成的任务是否不允许再改动（终态）
let allowAssignToOthers = false;                  // 是否允许把任务指派给别人（组长恒为 true）
let isLead = false;                              // 当前登录者是不是组长
let canMergePr = false;                          // 能否放行 / 合并 PR（组长）
// 卡片编辑器的上限（与服务端 config 保持一致，超出时前端就拦住）
const CODE_MAX_FILE_BYTES = 300000;
const CODE_MAX_TOTAL_BYTES = 1000000;

/* ============================================================
   DOM 引用
   ============================================================ */
const $ = (id) => document.getElementById(id);

const el = {
    statusBar: $('status-bar'),
    board: $('board'),
    emptyHint: $('empty-hint'),
    repoName: $('repo-name'),
    search: $('search-input'),
    btnNew: $('btn-new'),
    btnSync: $('btn-sync'),
    btnCancel: $('btn-cancel'),
    btnConfirm: $('btn-confirm'),
    modal: $('modal'),
    inputTitle: $('input-title'),
    inputDesc: $('input-desc'),
    inputStatus: $('input-status'),
    inputAssignee: $('input-assignee'),
    columnTemplate: $('column-template'),
    cardTemplate: $('card-template'),
    lastUpdate: $('last-update'),
    healthLink: $('health-link'),
    viewHint: $('view-hint'),
    accessInfo: $('access-info'),
    accessUrl: $('access-url'),
    btnCopyUrl: $('btn-copy-url'),
    btnAccessHelp: $('btn-access-help'),
    searchResults: $('search-results'),
    hiddenInfo: $('hidden-info'),
    hiddenCount: $('hidden-count'),
    btnRestoreAll: $('btn-restore-all'),
    realtimeInfo: $('realtime-info'),
    realtimeText: $('realtime-text'),
    realtimeSep: $('realtime-sep'),
    userChip: $('user-chip'),
    userName: $('user-name'),
    defaultPwdHint: $('default-pwd-hint'),
    btnChangePassword: $('btn-change-password'),
    btnLogout: $('btn-logout'),
    pwdModal: $('pwd-modal'),
    pwdOld: $('input-old-pwd'),
    pwdNew: $('input-new-pwd'),
    pwdNew2: $('input-new-pwd2'),
    btnPwdCancel: $('btn-pwd-cancel'),
    btnPwdConfirm: $('btn-pwd-confirm'),
    // 分支 / PR 面板
    prModal: $('pr-modal'),
    prTaskLine: $('pr-task-line'),
    prBranch: $('pr-branch'),
    prBase: $('pr-base'),
    prStatus: $('pr-status'),
    btnPrClose: $('btn-pr-close'),
    btnBranch: $('btn-branch'),
    btnPrOpen: $('btn-pr-open'),
    btnPrAllow: $('btn-pr-allow'),
    btnPrMerge: $('btn-pr-merge'),
    btnPrFinalize: $('btn-pr-finalize'),
    prForce: $('pr-force'),
    prForceLine: $('pr-force-line'),
    // 代码编辑器
    codeModal: $('code-modal'),
    codeTaskLine: $('code-task-line'),
    codeFileList: $('code-file-list'),
    codePath: $('code-path'),
    codeContent: $('code-content'),
    codeMessage: $('code-message'),
    codeOpenPr: $('code-open-pr'),
    codeSizeTip: $('code-size-tip'),
    codeFileInput: $('code-file-input'),
    btnCodeAdd: $('btn-code-add'),
    btnCodePick: $('btn-code-pick'),
    btnCodeLoad: $('btn-code-load'),
    btnCodeClear: $('btn-code-clear'),
    btnCodeCancel: $('btn-code-cancel'),
    btnCodeSubmit: $('btn-code-submit'),
};

/* ============================================================
   通用工具
   ============================================================ */
/**
 * 在顶部状态条显示提示信息。
 * @param {string} message 提示内容
 * @param {'info'|'success'|'error'|'loading'} type 提示类型
 * @param {number} timeout 自动清除时间（毫秒）；error 类型默认不自动清除
 */
function showStatus(message, type = 'info', timeout = 0) {
    el.statusBar.textContent = message;
    el.statusBar.className = 'status-bar ' + type;

    if (statusTimer) {
        clearTimeout(statusTimer);
        statusTimer = null;
    }
    if (type !== 'error') {
        statusTimer = setTimeout(() => {
            el.statusBar.textContent = '';
            el.statusBar.className = 'status-bar';
        }, timeout || 4000);
    }
}

/**
 * 统一的 API 调用封装：自动解析 JSON、自动提示错误。
 * @param {string} path 接口路径
 * @param {RequestInit} options fetch 配置
 * @returns {Promise<any>} 解析后的响应体
 */
async function api(path, options = {}) {
    let response;
    try {
        response = await fetch(API_BASE + path, {
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',   // 带上登录 Cookie
            ...options,
        });
    } catch (networkError) {
        showStatus('无法连接后端服务，请确认 python server.py 正在运行', 'error');
        throw networkError;
    }

    // 登录失效：直接回登录页，别让用户对着一个空看板发呆
    if (response.status === 401) {
        window.location.href = '/login';
        throw new Error('请先登录');
    }

    let data = null;
    try {
        data = await response.json();
    } catch (parseError) {
        // 后端返回了非 JSON（例如 500 错误页）
        data = null;
    }

    if (!response.ok) {
        const message = (data && (data.error || data.message)) || `请求失败（HTTP ${response.status}）`;
        showStatus(message, 'error');
        throw new Error(message);
    }
    return data;
}

/** 把字符串安全地写入节点文本，避免 XSS。 */
function setText(node, text) {
    node.textContent = text == null ? '' : String(text);
}

/** 当前登录者的登录名（代号）；未就绪时返回空串。 */
function myUsername() {
    return (me && me.username) ? me.username : '';
}

/** 当前登录者的显示名（本地真名）。 */
function myDisplayName() {
    return (me && me.display_name) ? me.display_name : '我';
}

/** 任务是否属于自己（负责人代号等于自己的登录名）。 */
function isMyTask(task) {
    return Boolean(myUsername()) && String(task.assignee || '') === myUsername();
}

/** 任务是否未分配。 */
function isUnassigned(task) {
    return !String(task.assignee || '');
}

/**
 * 能否操作这个任务。
 *
 * 后端会在每条任务上算好 ``can_edit``（它已经考虑组长的权限），这里优先用它，
 * 保证前后端规则一致；拿不到该字段时才回退到本地判断 —— 回退里同样要认组长。
 */
function canEdit(task) {
    if (typeof task.can_edit === 'boolean') return task.can_edit;
    return isLead || isMyTask(task) || isUnassigned(task);
}

/** 把 ISO 时间转成“x 分钟前”这类相对时间显示。 */
function timeAgo(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso.slice(0, 10);

    const diff = (Date.now() - date.getTime()) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
    if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`;
    return date.toLocaleDateString('zh-CN');
}

/** 取描述的第一行，作为紧凑展示。 */
function firstLine(text) {
    if (!text) return '';
    return String(text).split('\n').find((line) => line.trim()) || '';
}

/**
 * 把任务的负责人规整为代号，或空串（未分配）。
 * 后端返回的 assignee 字段始终是代号。
 */
function safeAssignee(task) {
    const code = task && task.assignee ? String(task.assignee) : '';
    return assignees.includes(code) ? code : '';
}

/**
 * 负责人展示名：代号 -> 本地真名（没配置映射时原样显示代号）。
 * 只影响界面显示，提交给后端的仍然是代号。
 * @param {string} code 负责人代号
 * @returns {string} 界面上显示的名字
 */
function assigneeLabel(code) {
    if (!code) return unassignedLabel;
    return displayNames[code] || code;
}

/* ============================================================
   数据加载
   ============================================================ */
/** 加载负责人代号名单、本地真名映射与各自的计数。 */
async function loadAssignees() {
    try {
        const data = await api('/api/assignees');
        if (Array.isArray(data.assignees) && data.assignees.length) {
            assignees = data.assignees.slice();
        }
        if (data.display_names && typeof data.display_names === 'object') {
            displayNames = data.display_names;
        }
        aliasesConfigured = Boolean(data.aliases_configured);
        if (data.unassigned_label) {
            unassignedLabel = data.unassigned_label;
        }
    } catch (error) {
        // 使用兜底名单，不影响页面可用性
    }
    fillAssigneeOptions();
}

/**
 * 填充新建任务弹窗中的负责人下拉框（value 是代号，显示的是真名）。
 *
 * 默认只给两个选择：**我自己** 与 **未分配**（不能替别人建任务）。
 * 如果配置允许指派给别人，则列出全部人选。
 */
function fillAssigneeOptions() {
    const kept = el.inputAssignee.value;
    el.inputAssignee.textContent = '';

    const unassignedOption = document.createElement('option');
    unassignedOption.value = '';
    setText(unassignedOption, unassignedLabel + '（让大家自己认领）');
    el.inputAssignee.appendChild(unassignedOption);

    const mine = myUsername();
    assignees
        .filter((code) => canAssignTo(code) || code === kept)
        .forEach((code) => {
            const option = document.createElement('option');
            option.value = code;                       // 提交给后端的是代号
            setText(option, code === mine
                ? `我自己（${assigneeLabel(code)}）`
                : assigneeLabel(code));                // 界面显示真名
            el.inputAssignee.appendChild(option);
        });

    el.inputAssignee.value = assignees.includes(kept) || kept === '' ? kept : '';
}

/** 从后端加载任务列表并渲染（silent=true 时不显示“暂无任务”提示）。 */
async function loadTasks(silent = false) {
    try {
        // 连「已从看板移除」的一起取回来：用于页脚提示与一键恢复，
        // 但看板本身只渲染未移除的（不传这个参数时后端也会过滤）
        const data = await api('/api/tasks?include_hidden=1');
        const all = Array.isArray(data) ? data : [];
        hiddenTasks = all.filter((task) => task.hidden);
        tasks = all.filter((task) => !task.hidden);
        render();
        renderHiddenInfo();
        el.lastUpdate.textContent = '最后更新：' + new Date().toLocaleTimeString('zh-CN');
        // 空看板的引导语只在 #empty-hint 里出现一次，状态条不再重复提示
    } catch (error) {
        // 错误提示已在 api() 中处理
    }
}

/** 更新页脚「已从看板移除 N 条 · 全部恢复」的显示。 */
function renderHiddenInfo() {
    el.hiddenInfo.hidden = hiddenTasks.length === 0;
    setText(el.hiddenCount, hiddenTasks.length);
}

/** 一键恢复全部被移除的任务。 */
async function restoreAllHidden() {
    try {
        const data = await api('/api/tasks/restore-all', { method: 'POST' });
        showStatus(data.message || '已全部恢复', 'success');
        await loadTasks(true);
    } catch (error) {
        // 错误提示已在 api() 中处理
    }
}

/**
 * 复制文本到剪贴板。
 *
 * 必须分三级降级：`navigator.clipboard` 只在**安全上下文**（https 或 localhost）下可用，
 * 而组员是用 http://192.168.x.x:5000 打开的，那里它是 undefined —— 只有一级实现的话，
 * 复制按钮在组员手机上一定失效。
 *
 * @param {string} text 要复制的文本
 * @returns {Promise<boolean>} 是否复制成功
 */
async function copyText(text) {
    // 1) 现代 API（安全上下文才有）
    if (navigator.clipboard && window.isSecureContext) {
        try {
            await navigator.clipboard.writeText(text);
            return true;
        } catch (error) {
            // 落到下一级
        }
    }

    // 2) 老办法 execCommand（http 页面可用的兜底）
    try {
        const area = document.createElement('textarea');
        area.value = text;
        area.setAttribute('readonly', '');
        area.style.position = 'fixed';
        area.style.top = '-1000px';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.select();
        area.setSelectionRange(0, area.value.length);
        const ok = document.execCommand('copy');
        document.body.removeChild(area);
        if (ok) return true;
    } catch (error) {
        // 落到下一级
    }

    // 3) 都失败：让调用方提示用户手动复制
    return false;
}

/**
 * 加载并显示本机的局域网访问地址（组员用这个地址打开看板）。
 *
 * 所有「怎么连、连不上怎么办」的说明都写进访问地址旁边的圆圈 i（data-hint），
 * 页面上不再铺开这些文字。
 */
async function loadServerInfo() {
    if (!el.accessInfo) return;

    const help = el.btnAccessHelp;

    try {
        const info = await api('/api/server-info');

        if (info.local_only) {
            // 服务能跑，但没探测到局域网地址（多半是还没连热点 / Wi-Fi）
            el.accessInfo.classList.add('access-info-warn');
            setText(el.accessUrl, '未检测到局域网地址');
            el.accessUrl.removeAttribute('href');
            if (help) {
                help.dataset.hint = '请先让本机连上手机热点或 Wi-Fi，然后刷新页面。'
                    + (info.firewall_hint || '');
            }
        } else {
            setText(el.accessUrl, info.url);
            el.accessUrl.href = info.url;
            el.accessInfo.classList.remove('access-info-warn');
            if (help) {
                help.dataset.hint = [
                    `组员连同一个热点后，用浏览器打开：${info.url}`,
                    info.urls.length > 1
                        ? `备选地址（第一条连不上时换一个试试）：${info.urls.slice(1).join('、')}`
                        : '',
                    info.hostname_url ? `主机名地址：${info.hostname_url}` : '',
                    info.firewall_hint || '请确认组员与本机在同一个热点/Wi-Fi 下',
                ].filter(Boolean).join('\n');
            }
        }

        el.accessInfo.hidden = false;

        // 复制按钮
        el.btnCopyUrl.addEventListener('click', async () => {
            const text = info.local_only ? '' : info.url;
            if (!text) {
                showStatus('没有可复制的访问地址，请先连上热点/Wi-Fi', 'error');
                return;
            }
            const ok = await copyText(text);
            if (ok) {
                showStatus('已复制访问地址：' + text, 'success');
            } else {
                // 兜底失败：把地址选中，让用户自己按 Ctrl+C
                try {
                    const range = document.createRange();
                    range.selectNodeContents(el.accessUrl);
                    const selection = window.getSelection();
                    selection.removeAllRanges();
                    selection.addRange(range);
                } catch (error) {
                    // 忽略
                }
                showStatus('浏览器不允许自动复制，地址已选中，请按 Ctrl+C 手动复制', 'error');
            }
        });
    } catch (error) {
        // 接口挂了也不要影响看板主体功能
        el.accessInfo.classList.add('access-info-warn');
        setText(el.accessUrl, '无法获取访问地址');
        el.accessUrl.removeAttribute('href');
        el.accessInfo.hidden = false;
        if (help) {
            help.dataset.hint = '获取本机地址失败，可在本机执行 ipconfig 查看 IPv4 地址，'
                + '再用 http://那串地址:端口 打开看板。';
        }
    }
}

/** 读取后端配置信息，展示当前绑定的仓库。 */
async function loadHealth() {
    try {
        const data = await api('/api/health');
        if (data.repo) {
            el.repoName.textContent = data.repo + (data.token_configured ? '' : '（未配置 Token）');
        } else {
            el.repoName.textContent = '未配置 GitHub 仓库（请检查 .env）';
        }
        if (Array.isArray(data.config_errors) && data.config_errors.length) {
            showStatus(data.config_errors[0], 'error');
        }
    } catch (error) {
        // 忽略：任务列表加载时还会再次提示
    }
}

/**
 * 看板上要渲染的任务。
 *
 * 注意：搜索**不再过滤看板**（那样会让卡片突然消失，容易看漏），
 * 改成输入框下方的下拉结果 + 点击跳转高亮，见 renderSearchResults()。
 */
function visibleTasks() {
    return tasks;
}

/* ============================================================
   搜索：下拉结果 + 跳转高亮
   ============================================================ */
/**
 * 按关键字匹配任务（标题 / 描述 / 编号 / 负责人 / AI 评语）。
 * @param {string} key 关键字
 * @returns {Array<object>} 匹配到的任务
 */
function searchMatches(key) {
    const needle = key.toLowerCase();
    return tasks.filter((task) => {
        const code = safeAssignee(task);
        return (task.title || '').toLowerCase().includes(needle)
            || (task.description || '').toLowerCase().includes(needle)
            || String(task.issue_number || '').includes(needle)
            || assigneeLabel(code).toLowerCase().includes(needle)
            || code.toLowerCase().includes(needle)
            || String(task.review_comment || '').toLowerCase().includes(needle);
    });
}

/**
 * 把标题里命中的片段包成 <mark>，做出浏览器搜索那种高亮。
 * @param {string} text 原文
 * @param {string} key 关键字
 * @returns {DocumentFragment}
 */
function highlightMatch(text, key) {
    const fragment = document.createDocumentFragment();
    const source = String(text || '');
    const needle = String(key || '');
    if (!needle) {
        fragment.appendChild(document.createTextNode(source));
        return fragment;
    }

    const lowerSource = source.toLowerCase();
    const lowerNeedle = needle.toLowerCase();
    let cursor = 0;
    let index = lowerSource.indexOf(lowerNeedle);

    while (index !== -1) {
        if (index > cursor) {
            fragment.appendChild(document.createTextNode(source.slice(cursor, index)));
        }
        const mark = document.createElement('mark');
        setText(mark, source.slice(index, index + needle.length));
        fragment.appendChild(mark);
        cursor = index + needle.length;
        index = lowerSource.indexOf(lowerNeedle, cursor);
    }
    if (cursor < source.length) {
        fragment.appendChild(document.createTextNode(source.slice(cursor)));
    }
    return fragment;
}

/** 渲染搜索下拉面板。 */
function renderSearchResults() {
    const panel = el.searchResults;
    if (!panel) return;

    searchHits = keyword ? searchMatches(keyword) : [];
    panel.textContent = '';

    if (!keyword) {
        closeSearchResults();
        return;
    }

    if (!searchHits.length) {
        const empty = document.createElement('div');
        empty.className = 'search-empty';
        setText(empty, `没有匹配「${keyword}」的任务`);
        panel.appendChild(empty);
        panel.hidden = false;
        el.search.setAttribute('aria-expanded', 'true');
        searchActive = -1;
        return;
    }

    searchHits.forEach((task, index) => {
        const row = document.createElement('div');
        row.className = 'search-item' + (index === searchActive ? ' active' : '');
        row.setAttribute('role', 'option');
        row.dataset.index = String(index);

        const number = document.createElement('span');
        number.className = 'si-issue';
        setText(number, '#' + task.issue_number);
        row.appendChild(number);

        const title = document.createElement('span');
        title.className = 'si-title';
        title.appendChild(highlightMatch(task.title || '(无标题)', keyword));
        row.appendChild(title);

        const meta = document.createElement('span');
        meta.className = 'si-meta';
        setText(meta, `${assigneeLabel(safeAssignee(task))} · ${STATUS_TEXT[task.status] || task.status}`);
        row.appendChild(meta);

        row.addEventListener('mouseenter', () => {
            searchActive = index;
            markActiveResult();
        });
        row.addEventListener('click', () => jumpToTask(task.issue_number));
        panel.appendChild(row);
    });

    const foot = document.createElement('div');
    foot.className = 'search-foot';
    setText(foot, `找到 ${searchHits.length} 条`);   // 快捷键提示在搜索框旁的圆圈 i 里
    panel.appendChild(foot);

    panel.hidden = false;
    el.search.setAttribute('aria-expanded', 'true');
}

/** 只更新当前高亮项（避免整块重建）。 */
function markActiveResult() {
    const panel = el.searchResults;
    if (!panel) return;
    panel.querySelectorAll('.search-item').forEach((row) => {
        row.classList.toggle('active', Number(row.dataset.index) === searchActive);
    });
    const active = panel.querySelector('.search-item.active');
    if (active && active.scrollIntoView) {
        active.scrollIntoView({ block: 'nearest' });
    }
}

/** 关闭搜索下拉。 */
function closeSearchResults() {
    if (!el.searchResults) return;
    el.searchResults.hidden = true;
    el.search.setAttribute('aria-expanded', 'false');
    searchActive = -1;
}

/**
 * 跳到某张卡片：滚动到视野内并闪一下边框。
 * @param {number} issueNumber Issue 编号
 */
function jumpToTask(issueNumber) {
    closeSearchResults();
    const card = el.board.querySelector(`.card[data-issue="${issueNumber}"]`);
    if (!card) {
        showStatus(`没在当前看板上找到 #${issueNumber}（可能已被移除）`, 'error');
        return;
    }

    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.classList.remove('card-flash');
    // 强制重排，保证连续跳转同一条时动画会重新播放
    void card.offsetWidth;
    card.classList.add('card-flash');
    setTimeout(() => card.classList.remove('card-flash'), 1800);
    showStatus(`已定位到 #${issueNumber}`, 'success', 2500);
}

/* ============================================================
   渲染：视图分发
   ============================================================ */
/** 统一入口：按当前视图渲染看板。 */
function render() {
    el.board.dataset.view = currentView;

    if (currentView === 'assignee') {
        renderAssigneeView();
        el.viewHint.textContent = '当前视图：人员（6 人 + ' + unassignedLabel + '）';
    } else {
        renderStatusView();
        el.viewHint.textContent = '当前视图：状态（todo / doing / done）';
    }
}

/** 状态视图：三列（Todo / Doing / Done）。 */
function renderStatusView() {
    const columns = STATUSES.map((status) => ({
        key: status,
        kind: 'status',
        // Done 列在样式上加了斜纹与圆圈 i（见 .col-locked）；说明写在 hint 里
        title: STATUS_TEXT[status],
        hint: status === 'done' && doneRequiresAgent
            ? '任务只能由 Agent 审核通过后自动移入，不能手动拖进来；'
              + (doneIsFinal ? '进来之后在界面上也不能再移出去（避免绕过审核）。' : '')
            : '',
        className: `col-${status}` + (status === 'done' && doneRequiresAgent ? ' col-locked' : ''),
        hue: null,
        tasks: [],
    }));

    const list = visibleTasks();
    list.forEach((task) => {
        const status = STATUSES.includes(task.status) ? task.status : 'todo';
        const column = columns.find((item) => item.key === status);
        column.tasks.push(task);
    });

    drawBoard(columns, list);
}

/** 人员视图：6 列（每人一列）+ 1 列「未分配」（列头显示本地真名，key 仍是代号）。 */
function renderAssigneeView() {
    const columns = assignees.map((code, index) => ({
        key: code,                                   // 拖拽/接口都使用代号
        kind: 'assignee',
        title: assigneeLabel(code),                  // 列头显示真名
        className: 'col-person',
        hue: ASSIGNEE_HUES[index % ASSIGNEE_HUES.length],
        tasks: [],
    }));

    // 最后一列固定为「未分配」，其 key 为空串
    columns.push({
        key: '',
        kind: 'assignee',
        title: unassignedLabel,
        className: 'col-person col-unassigned',
        hue: UNASSIGNED_HUE,
        tasks: [],
    });

    const list = visibleTasks();
    const unassignedColumn = columns[columns.length - 1];

    list.forEach((task) => {
        const owner = safeAssignee(task);
        const column = columns.find((item) => item.kind === 'assignee' && item.key === owner) || unassignedColumn;
        column.tasks.push(task);
    });

    drawBoard(columns, list);
}

/**
 * 把列定义渲染成真实 DOM。
 * @param {Array<object>} columns 列定义（含 tasks）
 * @param {Array<object>} list 当前可见任务（用于空状态提示）
 */
function drawBoard(columns, list) {
    el.board.textContent = '';

    columns.forEach((column) => {
        const node = el.columnTemplate.content.firstElementChild.cloneNode(true);

        column.className.split(' ').forEach((name) => node.classList.add(name));
        node.dataset.kind = column.kind;
        node.dataset.key = column.key;
        if (column.hue != null) {
            node.style.setProperty('--hue', column.hue);
        }

        setText(node.querySelector('.column-title'), column.title);
        setText(node.querySelector('.count'), column.tasks.length);

        // 列说明统一收进列头的圆圈 i（原来是挂在列头上的 title 提示）
        const columnHelp = node.querySelector('.column-help');
        if (columnHelp && column.hint) {
            columnHelp.dataset.hint = column.hint;
            columnHelp.hidden = false;
        }

        const listEl = node.querySelector('.card-list');
        column.tasks.forEach((task) => listEl.appendChild(buildCard(task)));

        // 列底部的「＋ 添加任务」：带入该列的默认状态 / 负责人（默认给自己）
        node.querySelector('.add-inline').addEventListener('click', () => {
            openModal({
                status: column.kind === 'status' ? column.key : 'todo',
                assignee: column.kind === 'assignee' ? column.key : myUsername(),
            });
        });

        el.board.appendChild(node);
    });

    // 空状态提示（搜索不再过滤看板，所以这里只看有没有任务）
    if (!tasks.length) {
        el.emptyHint.hidden = false;
        el.emptyHint.textContent = '还没有任何任务，点击「同步」从 GitHub 拉取 Issues，'
            + '或点击「新建任务」创建第一个任务。';
    } else {
        el.emptyHint.hidden = true;
    }
}

/**
 * 渲染卡片上的 Agent 审核徽章与可展开的 AI 评语。
 *
 * 三种状态（徽章前的圆点由 CSS 按类名着色，不用 emoji）：
 *   · passed=true  → 灰绿徽章「审核通过 · 88」
 *   · passed=false → 暗红徽章「审核不通过 · 45」
 *   · passed=null 且没有评语 → 整块隐藏（解析失败时的降级表现）
 *   · passed=null 但有评语 → 中性徽章「审核未完成」（Agent 自己没跑成时的情况）
 *
 * @param {HTMLElement} node 卡片节点
 * @param {object} task 任务数据
 */
function renderReview(node, task) {
    const wrap = node.querySelector('.card-review');
    const badge = node.querySelector('.review-badge');
    const commentEl = node.querySelector('.review-comment');
    if (!wrap || !badge || !commentEl) return;

    // review_passed 后端已统一成 true / false / null（这里同时容忍 1 / 0 的老数据）
    const raw = task.review_passed;
    const passed = (raw === true || raw === 1) ? true
        : (raw === false || raw === 0) ? false
            : null;

    const score = typeof task.review_score === 'number' ? task.review_score : null;
    const comment = String(task.review_comment || '').trim();

    // 既没有结论也没有评语 → 不显示徽章
    if (passed === null && !comment) return;

    let variant = 'unknown';
    let text = '审核未完成';
    if (passed === true) {
        variant = 'pass';
        text = '审核通过' + (score === null ? '' : ` · ${score}`);
    } else if (passed === false) {
        variant = 'fail';
        text = '审核不通过' + (score === null ? '' : ` · ${score}`);
    }

    wrap.hidden = false;
    badge.classList.add(variant);
    setText(badge, text);

    if (!comment) {
        // 只有结论、没有评语：徽章不可点击
        badge.classList.add('review-badge-static');
        badge.title = 'Agent 审核结果';
        return;
    }

    // 有评语：点击徽章展开 / 收起
    setText(commentEl, comment);
    commentEl.title = '来自 Agent 的审核评语';
    badge.classList.add('review-badge-toggle');
    badge.title = '点击查看 AI 评语';
    badge.addEventListener('click', (event) => {
        event.stopPropagation();          // 避免触发卡片上的其它交互
        const expanded = !commentEl.hidden;
        commentEl.hidden = expanded;
        badge.setAttribute('aria-expanded', expanded ? 'false' : 'true');
        badge.title = expanded ? '点击查看 AI 评语' : '点击收起 AI 评语';
    });
}

/**
 * 根据 task 对象克隆并填充一张卡片。
 * @param {object} task 任务数据
 * @returns {HTMLElement} 卡片节点
 */
function buildCard(task) {
    const node = el.cardTemplate.content.firstElementChild.cloneNode(true);
    const status = STATUSES.includes(task.status) ? task.status : 'todo';
    const owner = safeAssignee(task);
    const editable = canEdit(task);
    const mine = isMyTask(task);

    node.classList.add('status-' + status);
    node.dataset.issue = String(task.issue_number);
    node.dataset.status = status;

    // 别人的任务：置灰、不可拖拽（后端同样会拦截，这里只是别让人白拖一场）
    if (!editable) {
        node.classList.add('card-locked');
        node.draggable = false;
        node.dataset.locked = '1';
    }

    // Issue 编号（可点击跳转到 GitHub）
    const issueLink = node.querySelector('.issue-num');
    setText(issueLink, '#' + task.issue_number);
    if (task.html_url) {
        issueLink.href = task.html_url;
        issueLink.title = '在 GitHub 中打开 Issue #' + task.issue_number;
    } else {
        issueLink.removeAttribute('href');
    }

    // 状态徽章
    const badge = node.querySelector('.badge');
    badge.classList.add('badge-' + status);
    setText(badge, STATUS_TEXT[status]);

    // 负责人徽章（点击弹出选择器；别人的任务不给点）
    const ownerBadge = node.querySelector('.assignee-badge');
    ownerBadge.classList.add(owner ? 'assignee-badge-named' : 'assignee-badge-empty');
    ownerBadge.dataset.assignee = owner;
    if (editable) {
        ownerBadge.title = `负责人：${assigneeLabel(owner)}（点击切换）`;
    } else {
        ownerBadge.title = `负责人：${assigneeLabel(owner)}（别人的任务，只有本人能改）`;
        ownerBadge.classList.add('assignee-badge-readonly');
    }
    setText(ownerBadge, assigneeLabel(owner) + (mine ? '（我）' : ''));
    ownerBadge.addEventListener('click', (event) => {
        event.stopPropagation();
        if (!editable) {
            showStatus(`这是 ${assigneeLabel(owner)} 的任务，只有本人能改`, 'error');
            return;
        }
        showAssigneePicker(task);
    });

    // 标题与描述
    setText(node.querySelector('.card-title'), task.title || '(无标题)');
    setText(node.querySelector('.card-desc'), firstLine(task.description));

    // Agent 审核结果（徽章 + 可展开的 AI 评语）
    renderReview(node, task);

    // PR：有 PR 就显示编号与状态（open / draft / 待放行 / 已放行 / merged）
    renderPrChip(node, task);

    // 「代码」按钮：打开纯文本编辑器，直接把代码提交到分支
    const codeBtn = node.querySelector('.code-btn');
    if (codeBtn) {
        codeBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            openCodeEditor(task);
        });
    }

    // 「PR」按钮：打开分支 / PR 面板（建分支、提 PR、组长放行与合并）
    const prBtn = node.querySelector('.pr-btn');
    if (prBtn) {
        prBtn.addEventListener('click', (event) => {
            event.stopPropagation();
            openPrPanel(task);
        });
    }

    // 分支与提交（同步时抓取，可能为空）
    const meta = node.querySelector('.card-meta');
    const commits = Array.isArray(task.commits) ? task.commits : [];
    if (task.branch || commits.length) {
        meta.hidden = false;

        if (task.branch) {
            const chip = document.createElement('span');
            chip.className = 'branch-chip';
            chip.title = '关联分支：' + task.branch;
            setText(chip, task.branch);
            meta.appendChild(chip);
        }

        commits.slice(0, 2).forEach((commit) => {
            const row = document.createElement('div');
            row.className = 'commit';

            const sha = document.createElement('code');
            setText(sha, commit.sha || '');
            if (commit.url) {
                const link = document.createElement('a');
                link.href = commit.url;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                link.appendChild(sha);
                row.appendChild(link);
            } else {
                row.appendChild(sha);
            }

            const message = document.createElement('span');
            message.className = 'commit-msg';
            message.title = commit.message || '';
            setText(message, commit.message || '');
            row.appendChild(message);

            meta.appendChild(row);
        });
    }

    // 时间
    setText(node.querySelector('.card-time'), timeAgo(task.updated_at));

    // ← → 按钮：状态视图里切换状态，人员视图里切换负责人（移动端没有拖拽时的替代方案）
    const moveLeft = node.querySelector('.move-left');
    const moveRight = node.querySelector('.move-right');

    if (currentView === 'assignee') {
        // 人员视图里 ← → 只在「允许的目标」之间移动：
        // 默认配置下就是「认领给自己」↔「放回未分配」，不会把任务推给别人
        const order = assignees.concat(['']).filter(canAssignTo);
        if (!order.length) order.push(myUsername() || '');
        const index = Math.max(0, order.indexOf(owner));
        moveLeft.disabled = !editable || index === 0;
        moveRight.disabled = !editable || index === order.length - 1;
        moveLeft.title = index > 0 ? `认领给自己（${assigneeLabel(order[index - 1])}）` : '';
        moveRight.title = index < order.length - 1
            ? `改为 ${assigneeLabel(order[index + 1])}` : '';
        moveLeft.addEventListener('click', () => assignTask(task.issue_number, order[index - 1]));
        moveRight.addEventListener('click', () => assignTask(task.issue_number, order[index + 1]));
    } else {
        const index = STATUSES.indexOf(status);
        // 已完成的任务是终态：连「退回上一列」都不给，避免绕过 Agent 审核
        const doneLocked = status === 'done' && doneIsFinal;
        moveLeft.disabled = !editable || index === 0 || doneLocked;
        if (doneLocked) {
            moveLeft.title = '已完成的任务不能再改动（Done 是 Agent 审核通过的结果）';
        }
        // 右移按钮：不能移到 done（done 只能由 Agent 写入），所以 Done 列前一列禁用右移
        const rightTarget = STATUSES[index + 1];
        moveRight.disabled = !editable || index === STATUSES.length - 1
            || (rightTarget === 'done' && doneRequiresAgent);
        if (rightTarget === 'done' && doneRequiresAgent) {
            moveRight.title = '不能手动完成：请提交代码等待 Agent 审核';
        }
        moveLeft.addEventListener('click', () => moveTask(task.issue_number, STATUSES[index - 1]));
        moveRight.addEventListener('click', () => moveTask(task.issue_number, STATUSES[index + 1]));
    }

    // 从看板移除（只隐藏本地缓存，不动 GitHub；只有自己的或未分配的任务能移除）
    const deleteBtn = node.querySelector('.delete-btn');
    if (!editable) {
        deleteBtn.disabled = true;
        deleteBtn.title = '只有负责人本人能移除';
    }
    deleteBtn.addEventListener('click', () => {
        if (!editable) {
            showStatus(`这是 ${assigneeLabel(owner)} 的任务，只有本人能移除`, 'error');
            return;
        }
        if (confirm(`确定把任务 #${task.issue_number} 从看板移除吗？\n（GitHub 上的 Issue 不受影响，页脚「全部恢复」可以找回）`)) {
            deleteTask(task.issue_number);
        }
    });

    // 拖拽（桌面端；别人的任务不可拖，已完成的任务也不可拖）
    // 已完成的任务直接去掉 draggable：连「拖起来」的手感都不给，避免误以为能移出去
    const doneLocked = task.status === 'done' && doneIsFinal;
    if (doneLocked) {
        node.draggable = false;
        node.classList.add('card-final');
        node.title = '已完成的任务不能再改动（Done 是 Agent 审核通过的结果）';
    }
    node.addEventListener('dragstart', (event) => {
        if (doneLocked) {
            event.preventDefault();
            showStatus('已完成的任务不能再改动：Done 是 Agent 审核通过的结果', 'error');
            return;
        }
        if (!editable) {
            event.preventDefault();
            showStatus(`这是 ${assigneeLabel(owner)} 的任务，只有本人能拖动`, 'error');
            return;
        }
        draggingIssue = task.issue_number;
        node.classList.add('dragging');
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', String(task.issue_number));
    });
    node.addEventListener('dragend', () => {
        draggingIssue = null;
        node.classList.remove('dragging');
        clearDropHighlight();
    });

    return node;
}

/* ============================================================
   卡片上的纯文本编辑器：直接提交代码到分支
   ============================================================ */
let codeTask = null;            // 当前正在编辑的任务
let codeFiles = [];             // [{path, content}]
let codeIndex = 0;              // 左边列表里选中的第几个文件
let codeDirty = false;          // 内容有没有未同步到 codeFiles 的改动

/** 草稿在 localStorage 里的键（每个任务一份，刷新不丢）。 */
function codeDraftKey(issueNumber) {
    return `kanban.codeDraft.${issueNumber}`;
}

/** 把当前编辑框里的内容同步回 codeFiles。 */
function syncCodeEditor() {
    const item = codeFiles[codeIndex];
    if (!item || !el.codeContent) return;
    item.path = el.codePath.value.trim();
    item.content = el.codeContent.value;
    codeDirty = false;
}

/** 存草稿（防止贴了一大段代码刷新就没了）。 */
function saveCodeDraft() {
    if (!codeTask) return;
    syncCodeEditor();
    try {
        window.localStorage.setItem(codeDraftKey(codeTask.issue_number), JSON.stringify({
            files: codeFiles,
            message: el.codeMessage ? el.codeMessage.value : '',
            openPr: el.codeOpenPr ? el.codeOpenPr.checked : true,
            index: codeIndex,
        }));
    } catch (error) {
        // 隐私模式 / 超出配额：存不了就算了，不影响提交
    }
}

/** 读草稿；没有草稿时返回 null。 */
function loadCodeDraft(issueNumber) {
    try {
        const raw = window.localStorage.getItem(codeDraftKey(issueNumber));
        if (!raw) return null;
        const data = JSON.parse(raw);
        if (!data || !Array.isArray(data.files) || !data.files.length) return null;
        return data;
    } catch (error) {
        return null;
    }
}

/** 清掉草稿。 */
function clearCodeDraft(issueNumber) {
    try {
        window.localStorage.removeItem(codeDraftKey(issueNumber));
    } catch (error) {
        // 忽略
    }
}

/** 字节数（用 TextEncoder，跟后端算的一致）。 */
function byteLength(text) {
    try {
        return new TextEncoder().encode(text).length;
    } catch (error) {
        return String(text).length;
    }
}

/** 人类可读的大小。 */
function humanSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

/** 重新渲染左边的文件列表。 */
function renderCodeFileList() {
    if (!el.codeFileList) return;
    el.codeFileList.textContent = '';
    codeFiles.forEach((item, index) => {
        const li = document.createElement('li');
        li.className = 'code-file-item' + (index === codeIndex ? ' active' : '');
        li.title = item.path || '（还没填路径）';

        const name = document.createElement('span');
        name.className = 'code-file-name';
        setText(name, item.path || '（还没填路径）');
        li.appendChild(name);

        const size = document.createElement('span');
        size.className = 'code-file-size';
        setText(size, humanSize(byteLength(item.content || '')));
        li.appendChild(size);

        const remove = document.createElement('button');
        remove.className = 'code-file-del';
        remove.type = 'button';
        remove.title = '从这次提交里去掉';
        setText(remove, '×');
        remove.addEventListener('click', (event) => {
            event.stopPropagation();
            codeFiles.splice(index, 1);
            if (!codeFiles.length) codeFiles.push({ path: '', content: '' });
            codeIndex = Math.min(codeIndex, codeFiles.length - 1);
            showCodeFile(codeIndex);
            saveCodeDraft();
        });
        li.appendChild(remove);

        li.addEventListener('click', () => {
            syncCodeEditor();
            codeIndex = index;
            showCodeFile(index);
            saveCodeDraft();
        });

        el.codeFileList.appendChild(li);
    });
}

/** 把第 index 个文件显示到右边编辑区。 */
function showCodeFile(index) {
    const item = codeFiles[index] || { path: '', content: '' };
    if (el.codePath) el.codePath.value = item.path || '';
    if (el.codeContent) el.codeContent.value = item.content || '';
    codeDirty = false;
    renderCodeFileList();
    updateCodeSizeTip();
}

/** 更新右下角的大小提示 + 提交按钮可用性。 */
function updateCodeSizeTip() {
    if (!el.codeSizeTip) return;
    const current = codeFiles[codeIndex] || { content: '' };
    const size = byteLength(el.codeContent ? el.codeContent.value : current.content || '');
    const total = codeFiles.reduce((sum, item) => sum + byteLength(item.content || ''), 0);
    setText(el.codeSizeTip, `当前文件 ${humanSize(size)} · 本次提交共 ${codeFiles.length} 个文件 ${humanSize(total)}`);

    const tooBig = size > CODE_MAX_FILE_BYTES || total > CODE_MAX_TOTAL_BYTES;
    if (el.btnCodeSubmit) el.btnCodeSubmit.disabled = tooBig;
    if (tooBig) {
        showStatus(`文件太大：单个文件上限 ${humanSize(CODE_MAX_FILE_BYTES)}，`
            + `单次提交上限 ${humanSize(CODE_MAX_TOTAL_BYTES)}`, 'error');
    }
}

/** 打开代码编辑器。 */
function openCodeEditor(task) {
    codeTask = task;
    if (!el.codeModal) return;

    setText(el.codeTaskLine, `#${task.issue_number} ${task.title || ''}`.trim()
        + (task.branch ? ` · 分支 ${task.branch}` : ''));

    const draft = loadCodeDraft(task.issue_number);
    if (draft) {
        codeFiles = draft.files.map((item) => ({
            path: item.path || '', content: item.content || '',
        }));
        codeIndex = Math.min(draft.index || 0, codeFiles.length - 1);
        if (el.codeMessage) el.codeMessage.value = draft.message || '';
        if (el.codeOpenPr) el.codeOpenPr.checked = draft.openPr !== false;
    } else {
        codeFiles = [{ path: '', content: '' }];
        codeIndex = 0;
        if (el.codeMessage) el.codeMessage.value = defaultCommitMessage(task);
        if (el.codeOpenPr) el.codeOpenPr.checked = true;
    }
    if (codeIndex < 0) codeIndex = 0;
    showCodeFile(codeIndex);
    showModal(el.codeModal);
}

/** 默认提交信息：带上 Issue 编号，Agent 才会审核。 */
function defaultCommitMessage(task) {
    const title = String(task.title || '').trim() || `任务 ${task.issue_number}`;
    return `feat: ${title} (#${task.issue_number})`;
}

/** 添加一个空文件。 */
function addCodeFile(path = '', content = '') {
    syncCodeEditor();
    codeFiles.push({ path, content });
    codeIndex = codeFiles.length - 1;
    showCodeFile(codeIndex);
    saveCodeDraft();
    if (el.codePath) el.codePath.focus();
}

/** 从本地选择 / 拖进来的文件读成文本。 */
async function addLocalFiles(fileList) {
    const items = Array.from(fileList || []);
    if (!items.length) return;
    syncCodeEditor();
    if (codeFiles.length === 1 && !codeFiles[0].path && !codeFiles[0].content) codeFiles = [];

    for (const file of items) {
        try {
            const text = await file.text();
            // 已知的不对劲内容给个提示（二进制文件读出来会是乱码）
            if (text.includes('\u0000')) {
                showStatus(`${file.name} 看起来是二进制文件，纯文本编辑器处理不了`, 'error');
                continue;
            }
            codeFiles.push({ path: file.name, content: text });
        } catch (error) {
            showStatus(`读取 ${file.name} 失败：${error.message}`, 'error');
        }
    }
    codeIndex = Math.max(0, codeFiles.length - 1);
    if (!codeFiles.length) codeFiles = [{ path: '', content: '' }];
    showCodeFile(codeIndex);
    saveCodeDraft();
}

/** 从仓库读一个文件进编辑器（改已有文件时用）。 */
async function loadCodeFileFromRepo() {
    if (!codeTask) return;
    const path = (el.codePath.value || '').trim();
    if (!path) {
        showStatus('先填仓库内路径，再点「读入」', 'error');
        return;
    }
    if (el.btnCodeLoad) el.btnCodeLoad.disabled = true;
    try {
        const data = await api(`/api/tasks/${codeTask.issue_number}/file?path=${encodeURIComponent(path)}`);
        syncCodeEditor();
        const item = codeFiles[codeIndex] || { path, content: '' };
        item.path = data.path || path;
        item.content = data.content || '';
        codeFiles[codeIndex] = item;
        showCodeFile(codeIndex);
        saveCodeDraft();
        showStatus(data.message || '已读入', data.exists === false ? 'info' : 'success');
    } catch (error) {
        // api() 已经提示过
    } finally {
        if (el.btnCodeLoad) el.btnCodeLoad.disabled = false;
    }
}

/** 提交代码。 */
async function submitCode() {
    if (!codeTask) return;
    syncCodeEditor();

    const files = codeFiles
        .map((item) => ({ path: (item.path || '').trim(), content: item.content || '' }))
        .filter((item) => item.path || item.content.trim());
    if (!files.length) {
        showStatus('还没有内容可以提交：粘贴代码或选择本地文件', 'error');
        return;
    }
    const missing = files.find((item) => !item.path);
    if (missing) {
        showStatus('有文件还没填仓库内路径（例如 recon/knn.py）', 'error');
        return;
    }

    if (el.btnCodeSubmit) {
        el.btnCodeSubmit.disabled = true;
        el.btnCodeSubmit.textContent = '提交中…';
    }
    try {
        const data = await api(`/api/tasks/${codeTask.issue_number}/code`, {
            method: 'POST',
            body: JSON.stringify({
                files,
                message: (el.codeMessage.value || '').trim(),
                open_pr: el.codeOpenPr ? el.codeOpenPr.checked : true,
            }),
        });
        clearCodeDraft(codeTask.issue_number);
        codeFiles = [{ path: '', content: '' }];
        codeIndex = 0;
        dismissModal(el.codeModal);
        showStatus(data.message || '提交成功', 'success');
        await loadTasks(true);
        // 提交会顺手提 PR，把卡片上的 PR 徽章刷新出来
        if (data.pr_number) {
            const fresh = tasks.find((item) => item.issue_number === codeTask.issue_number);
            if (fresh) Object.assign(fresh, {
                pr_number: data.pr_number, pr_state: data.pr_state || 'open',
                pr_url: data.pr_url || '', branch: data.branch,
            });
            render();
        }
    } catch (error) {
        // api() 已经提示过；草稿留着，内容不丢
    } finally {
        if (el.btnCodeSubmit) {
            el.btnCodeSubmit.disabled = false;
            el.btnCodeSubmit.textContent = '提交到分支';
        }
    }
}

/* ============================================================
   分支 / PR
   ============================================================ */
/** PR 状态的显示文案与配色类。 */
const PR_STATE_TEXT = {
    open: '待审核',
    draft: '草稿',
    merged: '已合并',
    closed: '已关闭',
};

/**
 * 渲染卡片上的 PR 徽章（没有 PR 时整块隐藏）。
 *
 * 状态由两部分决定：GitHub 的 PR 状态 + 本地「组长是否已放行」。
 */
function renderPrChip(node, task) {
    const wrap = node.querySelector('.card-pr');
    const chip = node.querySelector('.pr-chip');
    if (!wrap || !chip) return;

    if (!task.pr_number) {
        wrap.hidden = true;
        return;
    }

    const state = task.pr_state || 'open';
    let text = `PR #${task.pr_number}`;
    let variant = state;
    if (state === 'open' && task.pr_allowed) {
        variant = 'allowed';
        text += ' 已放行';
    } else if (state === 'open') {
        text += ' 待放行';
    } else {
        text += ' ' + (PR_STATE_TEXT[state] || state);
    }

    wrap.hidden = false;
    chip.className = 'pr-chip pr-' + variant;
    setText(chip, text);
    chip.title = `PR #${task.pr_number}（${PR_STATE_TEXT[state] || state}）`
        + (task.pr_allowed_by ? ` · 已由 ${assigneeLabel(task.pr_allowed_by)} 放行` : '')
        + ' · 点击打开分支/PR 面板';

    chip.onclick = (event) => {
        event.stopPropagation();
        if (task.pr_url) {
            // 直接点开 GitHub 上的 PR，省一步
            window.open(task.pr_url, '_blank', 'noopener');
        } else {
            openPrPanel(task);
        }
    };
}

/** 当前正在操作 PR 的任务。 */
let prTask = null;

/** 打开分支 / PR 面板。 */
function openPrPanel(task) {
    prTask = task;
    if (!el.prModal) return;

    setText(el.prTaskLine, `#${task.issue_number} ${task.title || ''}`.trim());
    el.prBranch.value = task.branch || defaultBranchName(task);
    el.prBase.value = task.pr_base || 'main';
    renderPrPanelState(task);

    // 只有组长看得到「允许合并 / 合并」
    [el.btnPrAllow, el.btnPrMerge].forEach((button) => {
        if (button) button.hidden = !canMergePr;
    });

    showModal(el.prModal);
}

/** 刷新面板里的状态说明与按钮可用性。 */
function renderPrPanelState(task) {
    const lines = [];
    if (task.branch) {
        lines.push(`分支：<code>${escapeHtml(task.branch)}</code>`);
    } else {
        lines.push('还没有分支：点「建分支」会基于目标分支创建。');
    }

    const mergedNotDone = task.pr_number && task.pr_state === 'merged' && task.status !== 'done';

    if (task.pr_number) {
        const state = PR_STATE_TEXT[task.pr_state] || task.pr_state || '未知';
        lines.push(`PR：<a href="${task.pr_url || '#'}" target="_blank" rel="noopener noreferrer">`
            + `#${task.pr_number}</a>（${state}${task.pr_allowed ? ' · 已放行' : ' · 待组长放行'}）`);
        if (task.pr_state === 'merged' && task.status === 'done') {
            lines.push('已合并：Issue 已关闭，卡片在 Done 列。');
        }
    } else {
        lines.push('还没有 PR：把代码推到分支后点「提 PR」。');
    }

    // 合并前的闸门：组长放行 + Agent 过审，两条都满足才能合
    if (task.pr_number && task.pr_state !== 'merged') {
        lines.push('合并需要两个条件都满足：<b>组长放行</b> + <b>Agent 审核通过</b>；'
            + '合并后看板会自动把 Issue 标成 done 并关闭，卡片进 Done。');
    }
    if (mergedNotDone) {
        lines.push('这个 PR 已经合并，但 Issue 还没标成 done（卡片还赖在 Doing）：'
            + '点「标记完成并关闭 Issue」补一次收尾。');
    }
    lines.push(canMergePr
        ? '你是组长：可以放行、合并、补收尾。'
        : '放行、合并、补收尾只有组长能做；提 PR 后等组长处理。');

    if (el.prStatus) {
        el.prStatus.innerHTML = lines.join('<br>');   // 内容全部来自后端字段/常量，已转义
    }

    if (el.btnBranch) el.btnBranch.disabled = false;
    if (el.btnPrOpen) el.btnPrOpen.disabled = Boolean(task.pr_number && task.pr_state !== 'closed');
    if (el.btnPrAllow) {
        el.btnPrAllow.disabled = !task.pr_number || task.pr_state === 'merged' || task.pr_allowed;
    }
    if (el.btnPrMerge) {
        el.btnPrMerge.disabled = !task.pr_number || task.pr_state === 'merged' || !task.pr_allowed;
    }
    // 「标记完成并关闭 Issue」只在「已合并但还没进 Done」且是组长时出现
    if (el.btnPrFinalize) el.btnPrFinalize.hidden = !(canMergePr && mergedNotDone);
    if (el.prForceLine) el.prForceLine.hidden = !canMergePr || !task.pr_number;
}

/** 极简 HTML 转义（面板里用 innerHTML 拼小段说明时用）。 */
function escapeHtml(text) {
    return String(text == null ? '' : text)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

/** 默认分支名（与后端 _branch_name_for 保持一致）。 */
function defaultBranchName(task) {
    const slug = String(task.title || '')
        .replace(/[^0-9A-Za-z]+/g, '-').replace(/^-+|-+$/g, '').toLowerCase().slice(0, 24)
        .replace(/^-+|-+$/g, '');
    return slug ? `task/${task.issue_number}-${slug}` : `task/${task.issue_number}`;
}

/** 等 PR 相关操作结束后刷新数据。 */
async function refreshAfterPrAction(message) {
    showStatus(message, 'success');
    await loadTasks(true);
    const fresh = tasks.find((item) => item.issue_number === prTask?.issue_number);
    if (fresh && el.prModal && el.prModal.classList.contains('active')) {
        prTask = fresh;
        renderPrPanelState(fresh);
    }
}

/** 建分支。 */
async function createBranchForTask() {
    if (!prTask) return;
    const name = el.prBranch.value.trim() || defaultBranchName(prTask);
    const base = el.prBase.value.trim() || 'main';
    if (el.btnBranch) el.btnBranch.disabled = true;
    try {
        const data = await api(`/api/tasks/${prTask.issue_number}/branch`, {
            method: 'POST',
            body: JSON.stringify({ name, from: base }),
        });
        await refreshAfterPrAction(data.message || `已创建分支 ${name}`);
    } catch (error) {
        // api() 已经提示过
    } finally {
        if (el.btnBranch) el.btnBranch.disabled = false;
    }
}

/** 提 PR。 */
async function openPrForTask() {
    if (!prTask) return;
    const head = el.prBranch.value.trim() || prTask.branch || defaultBranchName(prTask);
    const base = el.prBase.value.trim() || 'main';
    if (el.btnPrOpen) el.btnPrOpen.disabled = true;
    try {
        const data = await api(`/api/tasks/${prTask.issue_number}/pr`, {
            method: 'POST',
            body: JSON.stringify({ head, base }),
        });
        await refreshAfterPrAction(data.message || '已提交 PR');
    } catch (error) {
        // api() 已经提示过
    } finally {
        if (el.btnPrOpen) el.btnPrOpen.disabled = false;
    }
}

/**
 * 包一层「点一次只发一次」：请求进行中把按钮禁用掉。
 *
 * 之前允许/合并按钮在请求飞行中还是可点的，手快点两下就会发两次
 * （PR 下会出现两条「组长已放行」评论）。
 */
async function withBusy(button, label, task) {
    if (!button) return;
    button.disabled = true;
    const original = button.textContent;
    if (label) button.textContent = label;
    try {
        await task();
    } finally {
        button.textContent = original;
        // 面板可能已经刷新过，按最新状态再决定禁用与否
        if (prTask && el.prModal && el.prModal.classList.contains('active')) {
            renderPrPanelState(prTask);
        } else {
            button.disabled = false;
        }
    }
}

/** 组长：放行 PR。 */
async function allowPrForTask() {
    if (!prTask) return;
    await withBusy(el.btnPrAllow, '放行中…', async () => {
        try {
            const data = await api(`/api/tasks/${prTask.issue_number}/pr/allow`, { method: 'POST' });
            await refreshAfterPrAction(data.message || '已放行');
        } catch (error) {
            // api() 已经提示过
        }
    });
}

/** 组长：合并 PR（必须先放行；Agent 过审是另一道闸门，可在面板上强制越过）。 */
async function mergePrForTask() {
    if (!prTask) return;
    const force = Boolean(el.prForce && el.prForce.checked);
    const note = force ? '\n\n已勾选「强制合并」：会忽略 Agent 的结论。' : '';
    if (!confirm(`确定合并 PR #${prTask.pr_number} 吗？\n`
        + '（合并后看板会把 Issue 标成 done 并关闭，卡片进 Done）' + note)) {
        return;
    }
    await withBusy(el.btnPrMerge, '合并中…', async () => {
        try {
            const data = await api(`/api/tasks/${prTask.issue_number}/pr/merge`, {
                method: 'POST',
                body: JSON.stringify({ force }),
            });
            await refreshAfterPrAction(data.message || '已合并');
        } catch (error) {
            // api() 已经提示过
        }
    });
}

/** 组长：给「已合并但还没进 Done」的卡片补一次收尾。 */
async function finalizePrForTask() {
    if (!prTask) return;
    await withBusy(el.btnPrFinalize, '处理中…', async () => {
        try {
            const data = await api(`/api/tasks/${prTask.issue_number}/pr/finalize`, { method: 'POST' });
            await refreshAfterPrAction(data.message || '已标记完成');
        } catch (error) {
            // api() 已经提示过
        }
    });
}

/* ============================================================
   写操作：改状态 / 改负责人 / 新建 / 删除 / 同步
   ============================================================ */
/** 清除所有列的高亮。 */
function clearDropHighlight() {
    el.board.querySelectorAll('.column.drop-active').forEach((column) => {
        column.classList.remove('drop-active');
    });
}

/**
 * 发送一次 PUT 更新（乐观更新 + 失败回滚）。
 * @param {string} url 接口地址
 * @param {object} payload 请求体
 * @param {object} optimistic {rollback} 失败时的回滚函数
 * @param {string} successText 成功提示
 */
async function sendUpdate(url, payload, optimistic, successText) {
    if (busy) return;
    busy = true;
    try {
        await api(url, { method: 'PUT', body: JSON.stringify(payload) });
        showStatus(successText, 'success');
        await loadTasks(true);
    } catch (error) {
        // 回滚界面，让看板与后端保持一致
        if (optimistic && typeof optimistic.rollback === 'function') {
            optimistic.rollback();
        }
        render();
    } finally {
        busy = false;
    }
}

/**
 * 修改任务状态（状态视图拖拽 / ← → 按钮）。
 *
 * 规则：
 *   1. 不能把任务移进 Done —— 完成只能由 Agent 审核通过后写入
 *   2. **已完成的任务不能再移出去** —— Done 是 Agent 审核的结果，能拖出来就等于绕过审核
 *   3. 别人的任务不能动（后端也会 403）
 *   4. **未分配的任务：任何人把它拖到别的列，就等于认领给自己并推进**
 *      —— 一次请求同时带上 assignee=自己 和 status，后端允许这样「认领并移动」；
 *      认领之后这张卡就归自己了，别人再想动会被 403 拒绝。
 *
 * @param {number} issueNumber Issue 编号
 * @param {string} newStatus 目标状态
 * @returns {boolean} 是否真正发起了请求
 */
function moveTask(issueNumber, newStatus) {
    if (!STATUSES.includes(newStatus)) return false;

    const task = tasks.find((item) => item.issue_number === issueNumber);
    if (!task || task.status === newStatus) return false;

    // ① 防「假完成」
    if (newStatus === 'done' && doneRequiresAgent) {
        showStatus('不能手动完成：请提交代码等待 Agent 审核，审核通过后会自动移到 Done', 'error');
        return false;
    }

    // ② 完成是终态：已 done 的任务不能再被拖出去（后端也会 403，这里只是先提示）
    if (task.status === 'done' && doneIsFinal) {
        showStatus('已完成的任务不能再改动：Done 是 Agent 审核通过的结果', 'error');
        return false;
    }

    // ③ 未分配 → 这次移动顺带认领给自己
    const unassigned = !safeAssignee(task);
    const me = myUsername();
    if (unassigned && !me) {
        showStatus('没有登录信息，无法认领这个任务，请刷新页面重试', 'error');
        return false;
    }

    // ④ 别人的任务：组长可以推着走（替团队收尾），组员不行
    if (!unassigned && !isMyTask(task) && !isLead) {
        const owner = safeAssignee(task);
        showStatus(`这是 ${assigneeLabel(owner)} 的任务，只有本人或组长能改`, 'error');
        return false;
    }

    const previous = { status: task.status, assignee: safeAssignee(task) };
    task.status = newStatus;
    if (unassigned) task.assignee = me;             // 乐观更新：卡片立刻落到自己名下
    render();
    showStatus(unassigned
        ? `正在认领 #${issueNumber} 并移动到 ${STATUS_TEXT[newStatus]}…`
        : `正在把 #${issueNumber} 移动到 ${STATUS_TEXT[newStatus]}…`, 'loading');

    sendUpdate(
        `/api/tasks/${issueNumber}`,
        unassigned ? { assignee: me, status: newStatus } : { status: newStatus },
        {
            rollback: () => {
                task.status = previous.status;
                task.assignee = previous.assignee;
            },
        },
        unassigned
            ? `#${issueNumber} 已认领给你并移动到 ${STATUS_TEXT[newStatus]}（GitHub 已同步）`
            : `#${issueNumber} 已更新为 ${STATUS_TEXT[newStatus]}（GitHub 标签已同步）`
    );
    return true;
}

/**
 * 修改任务负责人（人员视图拖拽 / ← → 按钮 / 徽章选择器）。
 *
 * 规则：
 *   * **组长**：可以把任何卡片指派给任何人（分派任务）
 *   * 组员：只能认领给自己，或把自己名下的放回「未分配」；不能把活塞给别人
 *
 * @param {number} issueNumber Issue 编号
 * @param {string} newAssignee 目标负责人；空串表示未分配
 * @returns {boolean} 是否真正发起了请求
 */
function assignTask(issueNumber, newAssignee) {
    const task = tasks.find((item) => item.issue_number === issueNumber);
    if (!task) return false;

    const target = assignees.includes(newAssignee) ? newAssignee : '';
    const previous = safeAssignee(task);
    if (previous === target) return false;

    if (!isLead && previous && !isMyTask(task)) {
        showStatus(`这是 ${assigneeLabel(previous)} 的任务，只有本人或组长能改负责人`, 'error');
        return false;
    }

    if (!canAssignTo(target)) {
        showStatus(`不能把任务指派给 ${assigneeLabel(target)}：只有组长能分派，组员只能自己认领`, 'error');
        return false;
    }

    task.assignee = target;
    render();
    const verb = isLead ? '分派给' : '指派给';
    showStatus(`正在把 #${issueNumber} ${verb} ${assigneeLabel(target)}…`, 'loading');

    sendUpdate(
        `/api/tasks/${issueNumber}/assignee`,
        { assignee: target },
        { rollback: () => { task.assignee = previous; } },
        target
            ? (isLead
                ? `#${issueNumber} 已分派给 ${assigneeLabel(target)}（GitHub 正文已同步）`
                : `#${issueNumber} 已认领给你（GitHub 正文已同步）`)
            : `#${issueNumber} 已放回「未分配」，等本人认领`
    );
    return true;
}

/**
 * 用 prompt 弹出负责人选择器。
 *
 * 默认只列出「可以选的目标」：认领给自己、或放回未分配（不能指派给别人）。
 * 支持输入序号或姓名；直接回车 = 未分配；点“取消”则不改动。
 *
 * @param {object} task 任务数据
 */
function showAssigneePicker(task) {
    const current = safeAssignee(task);
    // 只保留允许的目标；把当前负责人也带上，方便显示默认值
    const options = assignees.concat(['']).filter((code) => canAssignTo(code) || code === current);
    if (!options.length) options.push(myUsername() || '');

    const menu = options
        .map((name, index) => `${index + 1}. ${assigneeLabel(name)}`)
        .join('\n');

    const hint = allowAssignToOthers
        ? ''
        : '\n\n注意：只能认领给自己，或放回「未分配」让别人自己认领。';

    const input = prompt(
        `切换 #${task.issue_number} 的负责人\n\n${menu}\n\n` +
        '输入序号或姓名；直接回车 = 未分配；点“取消”则不改动。' + hint,
        assigneeLabel(current)
    );

    if (input === null) return;  // 用户取消

    const text = input.trim();

    // 空输入 → 未分配
    if (!text) {
        assignTask(task.issue_number, '');
        return;
    }

    // 纯数字 → 按序号选择
    if (/^\d+$/.test(text)) {
        const index = Number(text) - 1;
        if (index >= 0 && index < options.length) {
            assignTask(task.issue_number, options[index]);
        } else {
            showStatus(`序号 ${text} 超出范围（1-${options.length}）`, 'error');
        }
        return;
    }

    // 姓名（控制台里的代号或界面上的真名都接受）
    if (text === unassignedLabel || text.toLowerCase() === 'none') {
        assignTask(task.issue_number, '');
        return;
    }

    const matched = assignees.find((code) => code === text || assigneeLabel(code) === text);
    if (matched) {
        assignTask(task.issue_number, matched);   // 提交给后端的是代号
        return;
    }

    const optionsText = assignees.map((code) => assigneeLabel(code)).join('、');
    showStatus(`「${text}」不在负责人名单中，可选：${optionsText}、${unassignedLabel}`, 'error');
}

/** 新建任务：调用后端创建 GitHub Issue。 */
async function createTask() {
    const title = el.inputTitle.value.trim();
    const description = el.inputDesc.value.trim();
    const status = el.inputStatus.value;
    const assignee = el.inputAssignee.value;

    if (!title) {
        showStatus('标题不能为空', 'error');
        el.inputTitle.focus();
        return;
    }

    el.btnConfirm.disabled = true;
    el.btnConfirm.textContent = '创建中…';
    try {
        const data = await api('/api/tasks', {
            method: 'POST',
            body: JSON.stringify({ title, description, status, assignee }),
        });
        closeModal();
        showStatus(data.message || '任务创建成功', 'success');
        await loadTasks(true);
    } catch (error) {
        // 错误提示已显示
    } finally {
        el.btnConfirm.disabled = false;
        el.btnConfirm.textContent = '创建';
    }
}

/** 把任务从看板移除（只隐藏本地记录，GitHub 上的 Issue 不受影响，可恢复）。 */
async function deleteTask(issueNumber) {
    try {
        const data = await api(`/api/tasks/${issueNumber}`, { method: 'DELETE' });
        showStatus(data.message || `已把 #${issueNumber} 从看板移除`, 'success');
        await loadTasks(true);
    } catch (error) {
        // 错误提示已显示
    }
}

/** 从 GitHub 同步最新数据。 */
async function syncTasks() {
    if (busy) return;
    busy = true;
    el.btnSync.disabled = true;
    const originalText = el.btnSync.textContent;
    el.btnSync.textContent = '同步中…';
    showStatus('正在从 GitHub 同步 Issues（含负责人、分支与提交，可能需要几秒）…', 'loading');

    try {
        const data = await api('/api/sync', { method: 'POST' });
        showStatus(data.message || '同步完成', 'success');
        await loadTasks(true);
    } catch (error) {
        // 错误提示已显示
    } finally {
        busy = false;
        el.btnSync.disabled = false;
        el.btnSync.textContent = originalText;
    }
}

/* ============================================================
   视图切换
   ============================================================ */
/**
 * 切换视图（状态视图 / 人员视图）。
 * @param {'status'|'assignee'} view 目标视图
 */
function setView(view) {
    if (view !== 'status' && view !== 'assignee') return;
    if (view === currentView) return;

    currentView = view;

    document.querySelectorAll('.btn-view').forEach((button) => {
        button.classList.toggle('active', button.dataset.view === view);
    });

    render();
    // 拖拽语义等说明都在视图切换旁的圆圈 i 里，这里只报一句结果
    showStatus(view === 'assignee' ? '已切换到人员视图' : '已切换到状态视图', 'info');
}

/* ============================================================
   弹窗控制（Apple 风格：打开淡入放大，关闭反向淡出）
   ============================================================ */
const MODAL_CLOSE_MS = 180;      // 与 style.css 里 dialog-out 的时长保持一致

/** 打开弹窗：清掉可能还在跑的关闭定时器，避免刚打开又被收起。 */
function showModal(box) {
    clearTimeout(box._closeTimer);
    box.classList.remove('closing');
    box.classList.add('active');
    box.setAttribute('aria-hidden', 'false');
}

/** 关闭弹窗：先播关闭动画，动画结束再真正隐藏。 */
function dismissModal(box) {
    if (!box || !box.classList.contains('active')) return;
    box.classList.add('closing');
    box.setAttribute('aria-hidden', 'true');
    box._closeTimer = setTimeout(() => box.classList.remove('active', 'closing'), MODAL_CLOSE_MS);
}

/**
 * 打开新建任务弹窗。
 * @param {{status?: string, assignee?: string}} preset 预设的状态 / 负责人
 */
function openModal(preset = {}) {
    const status = STATUSES.includes(preset.status) ? preset.status : 'todo';
    const wanted = assignees.includes(preset.assignee) ? preset.assignee : '';
    // 不能指派给别人：预设目标不合法时退回「我自己」
    const assignee = canAssignTo(wanted) ? wanted : (myUsername() || '');

    el.inputTitle.value = '';
    el.inputDesc.value = '';
    el.inputStatus.value = status;
    el.inputAssignee.value = assignee;

    showModal(el.modal);
    setTimeout(() => el.inputTitle.focus(), 30);
}

function closeModal() {
    dismissModal(el.modal);
}

/* ============================================================
   修改密码
   ============================================================ */
function openPwdModal() {
    el.pwdOld.value = '';
    el.pwdNew.value = '';
    el.pwdNew2.value = '';
    showModal(el.pwdModal);
    setTimeout(() => el.pwdOld.focus(), 30);
}

function closePwdModal() {
    dismissModal(el.pwdModal);
}

/** 提交修改密码（校验两次输入一致，其余交给后端）。 */
async function submitPasswordChange() {
    const oldPassword = el.pwdOld.value;
    const newPassword = el.pwdNew.value;
    const repeat = el.pwdNew2.value;
    const minLength = (me && me.min_password_length) || 6;

    if (!oldPassword) {
        showStatus('请输入原密码', 'error');
        return;
    }
    if (newPassword.length < minLength) {
        showStatus(`新密码至少 ${minLength} 位`, 'error');
        return;
    }
    if (newPassword !== repeat) {
        showStatus('两次输入的新密码不一致', 'error');
        return;
    }

    el.btnPwdConfirm.disabled = true;
    try {
        const data = await api('/api/change-password', {
            method: 'POST',
            body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
        });
        closePwdModal();
        if (me) me.is_default_password = false;
        showStatus(data.message || '密码已更新', 'success');
    } catch (error) {
        // 错误提示已在 api() 中显示
    } finally {
        el.btnPwdConfirm.disabled = false;
    }
}

/* ============================================================
   当前登录用户
   ============================================================ */
/** 加载当前登录用户，填充右上角信息。 */
async function loadMe() {
    const info = await api('/api/me');   // 401 时 api() 会自动跳登录页
    me = info;
    doneRequiresAgent = info.done_requires_agent !== false;
    doneIsFinal = info.done_is_final !== false;
    allowAssignToOthers = info.allow_assign_to_others === true;
    isLead = info.is_lead === true;
    canMergePr = info.can_merge_pr === true;

    // 界面上不展示代号（太难听），只显示真实姓名；组长多一个标记
    setText(el.userName, (info.display_name || '我') + (isLead ? '（组长）' : ''));
    el.userChip.hidden = false;
    el.userChip.title = `已登录：${info.display_name || '我'}${isLead ? '（组长）' : ''}`;
    el.userChip.classList.toggle('user-chip-lead', isLead);

    // 还在用初始密码 → 在用户名旁边挂一个提醒态的圆圈 i（不再往状态条里塞提示）
    if (el.defaultPwdHint) {
        if (info.is_default_password) {
            el.defaultPwdHint.dataset.hint = '你还在使用初始密码。为了别人不能拿你的名义改任务，'
                + '建议点右边的「修改密码」换一个只有你知道的密码。';
            el.defaultPwdHint.hidden = false;
        } else {
            el.defaultPwdHint.hidden = true;
        }
    }
}

/** 能否管理这张卡（组长可以管任何卡片；组员只管自己的/未分配的）。 */
function canManageTask(task) {
    return isLead || isMyTask(task) || !safeAssignee(task);
}

/**
 * 判断某个负责人是否是「可以指派的目标」。
 * 默认只允许认领给自己，或把自己名下的放回「未分配」。
 * @param {string} target 目标负责人代号（空串 = 未分配）
 * @returns {boolean} 是否允许
 */
function canAssignTo(target) {
    if (allowAssignToOthers) return true;
    if (!target) return true;                    // 放回未分配池
    return target === myUsername();              // 只能认领给自己
}

/* ============================================================
   事件绑定
   ============================================================ */
// 拖拽：用事件委托绑定在 board 上（列每次渲染都会重建）
el.board.addEventListener('dragover', (event) => {
    const column = event.target.closest ? event.target.closest('.column') : null;
    if (!column) return;

    // Done 列不接受拖入（完成只能由 Agent 写），连高亮都不给
    if (column.dataset.kind === 'status' && column.dataset.key === 'done' && doneRequiresAgent) {
        event.dataTransfer.dropEffect = 'none';
        return;
    }

    // 别人的列也不接受拖入：不能把任务塞给别人
    if (column.dataset.kind === 'assignee' && !canAssignTo(column.dataset.key)) {
        event.dataTransfer.dropEffect = 'none';
        return;
    }

    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';

    el.board.querySelectorAll('.column.drop-active').forEach((item) => {
        if (item !== column) item.classList.remove('drop-active');
    });
    column.classList.add('drop-active');
});

el.board.addEventListener('dragleave', (event) => {
    const column = event.target.closest ? event.target.closest('.column') : null;
    if (column && !column.contains(event.relatedTarget)) {
        column.classList.remove('drop-active');
    }
});

el.board.addEventListener('drop', (event) => {
    const column = event.target.closest ? event.target.closest('.column') : null;
    if (!column) return;

    event.preventDefault();
    clearDropHighlight();

    const raw = event.dataTransfer.getData('text/plain') || draggingIssue;
    const issueNumber = parseInt(raw, 10);
    if (!Number.isFinite(issueNumber)) return;

    // 人员视图 → 改负责人；状态视图 → 改状态
    if (column.dataset.kind === 'assignee') {
        // 别人的列：直接拒绝，不打电话给后端
        if (!canAssignTo(column.dataset.key)) {
            const who = column.dataset.key ? assigneeLabel(column.dataset.key) : unassignedLabel;
            showStatus(`不能把任务指派给 ${who}：只有本人能认领任务`, 'error');
            return;
        }
        assignTask(issueNumber, column.dataset.key);
        return;
    }

    // 防「假完成」：Done 列不接受任何拖入
    if (column.dataset.key === 'done' && doneRequiresAgent) {
        const task = tasks.find((item) => item.issue_number === issueNumber);
        if (!task || task.status !== 'done') {
            showStatus('不能手动完成：请提交代码等待 Agent 审核，审核通过后会自动移到 Done', 'error');
            return;
        }
    }

    moveTask(issueNumber, column.dataset.key);
});

// 视图切换按钮
document.querySelectorAll('.btn-view').forEach((button) => {
    button.addEventListener('click', () => setView(button.dataset.view));
});

// 顶栏按钮
el.btnNew.addEventListener('click', () => openModal({ status: 'todo', assignee: myUsername() }));
el.btnSync.addEventListener('click', syncTasks);
el.btnCancel.addEventListener('click', closeModal);
el.btnConfirm.addEventListener('click', createTask);

// 修改密码
el.btnChangePassword.addEventListener('click', openPwdModal);
el.btnPwdCancel.addEventListener('click', closePwdModal);
el.btnPwdConfirm.addEventListener('click', submitPasswordChange);
el.pwdModal.addEventListener('click', (event) => {
    if (event.target.dataset.closePwd) closePwdModal();
});
// 密码弹窗里按回车直接提交
[el.pwdOld, el.pwdNew, el.pwdNew2].forEach((input) => {
    input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') submitPasswordChange();
    });
});

// 弹窗遮罩关闭
el.modal.addEventListener('click', (event) => {
    if (event.target.dataset.close) closeModal();
});

// 分支 / PR 面板
el.btnPrClose.addEventListener('click', () => dismissModal(el.prModal));
el.btnBranch.addEventListener('click', createBranchForTask);
el.btnPrOpen.addEventListener('click', openPrForTask);
el.btnPrAllow.addEventListener('click', allowPrForTask);
el.btnPrMerge.addEventListener('click', mergePrForTask);
el.btnPrFinalize.addEventListener('click', finalizePrForTask);
el.prModal.addEventListener('click', (event) => {
    if (event.target.dataset.closePr) dismissModal(el.prModal);
});

// 代码编辑器
el.codeModal.addEventListener('click', (event) => {
    if (event.target.dataset.closeCode) {
        syncCodeEditor();
        saveCodeDraft();
        dismissModal(el.codeModal);
    }
});
el.btnCodeAdd.addEventListener('click', () => addCodeFile());
el.btnCodePick.addEventListener('click', () => el.codeFileInput.click());
el.codeFileInput.addEventListener('change', async () => {
    await addLocalFiles(el.codeFileInput.files);
    el.codeFileInput.value = '';           // 允许再选同一个文件
});
el.btnCodeLoad.addEventListener('click', loadCodeFileFromRepo);
el.btnCodeCancel.addEventListener('click', () => {
    syncCodeEditor();
    saveCodeDraft();
    dismissModal(el.codeModal);
});
el.btnCodeSubmit.addEventListener('click', submitCode);
el.btnCodeClear.addEventListener('click', () => {
    if (!codeTask) return;
    clearCodeDraft(codeTask.issue_number);
    codeFiles = [{ path: '', content: '' }];
    codeIndex = 0;
    el.codeMessage.value = defaultCommitMessage(codeTask);
    showCodeFile(0);
    showStatus('草稿已清空', 'info');
});

// 编辑区：路径换行 / 内容变化时同步回内存并存草稿（防刷新丢内容）
el.codePath.addEventListener('input', () => {
    const item = codeFiles[codeIndex];
    if (item) item.path = el.codePath.value.trim();
    renderCodeFileList();
    scheduleCodeDraftSave();
});
el.codeContent.addEventListener('input', () => {
    const item = codeFiles[codeIndex];
    if (item) item.content = el.codeContent.value;
    codeDirty = true;
    updateCodeSizeTip();
    scheduleCodeDraftSave();
});
el.codeMessage.addEventListener('input', scheduleCodeDraftSave);
if (el.codeOpenPr) el.codeOpenPr.addEventListener('change', scheduleCodeDraftSave);

// Tab 键在编辑区里插入四个空格（纯文本编辑器，不抢焦点）
el.codeContent.addEventListener('keydown', (event) => {
    if (event.key === 'Tab') {
        event.preventDefault();
        const area = el.codeContent;
        const start = area.selectionStart;
        const end = area.selectionEnd;
        area.value = area.value.slice(0, start) + '    ' + area.value.slice(end);
        area.selectionStart = area.selectionEnd = start + 4;
        const item = codeFiles[codeIndex];
        if (item) item.content = area.value;
        updateCodeSizeTip();
        scheduleCodeDraftSave();
    }
    // Ctrl/Cmd + Enter 直接提交
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        submitCode();
    }
});

// 把本地文件拖进编辑器窗口（VS Code 里导出、或直接拖文件）
['dragover', 'drop'].forEach((type) => {
    el.codeModal.addEventListener(type, async (event) => {
        if (!el.codeModal.classList.contains('active')) return;
        event.preventDefault();
        if (type === 'drop' && event.dataTransfer && event.dataTransfer.files) {
            await addLocalFiles(event.dataTransfer.files);
        }
    });
});

let codeDraftTimer = null;
/** 拖一下再存草稿，避免每敲一个字都写 localStorage。 */
function scheduleCodeDraftSave() {
    if (codeDraftTimer) clearTimeout(codeDraftTimer);
    codeDraftTimer = setTimeout(() => {
        codeDraftTimer = null;
        saveCodeDraft();
    }, 400);
}

// 键盘：Esc 关闭弹窗，弹窗内 Ctrl/Cmd + Enter 提交
document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
        if (el.modal.classList.contains('active')) closeModal();
        if (el.pwdModal.classList.contains('active')) closePwdModal();
        if (el.prModal.classList.contains('active')) dismissModal(el.prModal);
        if (el.codeModal.classList.contains('active')) {
            syncCodeEditor();
            saveCodeDraft();
            dismissModal(el.codeModal);
        }
    }
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && el.modal.classList.contains('active')) {
        createTask();
    }
});

// 搜索：输入即弹出匹配结果（不过滤看板，避免卡片突然消失）
let searchTimer = null;
el.search.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
        keyword = el.search.value.trim();
        searchActive = keyword ? 0 : -1;
        renderSearchResults();
    }, 120);
});

// 搜索框获得焦点且有内容时重新展开结果
el.search.addEventListener('focus', () => {
    if (el.search.value.trim()) {
        keyword = el.search.value.trim();
        renderSearchResults();
    }
});

// ↑↓ 选择、Enter 跳转、Esc 关闭
el.search.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
        closeSearchResults();
        el.search.blur();
        return;
    }
    if (!searchHits.length || el.searchResults.hidden) {
        if (event.key === 'Enter' && keyword) renderSearchResults();
        return;
    }
    if (event.key === 'ArrowDown') {
        event.preventDefault();
        searchActive = (searchActive + 1) % searchHits.length;
        markActiveResult();
    } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        searchActive = (searchActive - 1 + searchHits.length) % searchHits.length;
        markActiveResult();
    } else if (event.key === 'Enter') {
        event.preventDefault();
        const target = searchHits[searchActive] || searchHits[0];
        if (target) jumpToTask(target.issue_number);
    }
});

// 点击面板以外的地方收起结果
document.addEventListener('click', (event) => {
    if (!el.searchResults || el.searchResults.hidden) return;
    if (event.target === el.search || el.searchResults.contains(event.target)) return;
    closeSearchResults();
});

// 一键恢复被移除的任务
el.btnRestoreAll.addEventListener('click', restoreAllHidden);

// 服务状态
el.healthLink.addEventListener('click', async (event) => {
    event.preventDefault();
    try {
        const data = await api('/api/health?deep=1');
        const parts = [
            `状态：${data.status}`,
            `仓库：${data.repo || '未配置'}`,
            `Token：${data.token || '未配置'}`,
            `负责人：${(data.assignees || []).length} 人`,
            `真名映射：${data.aliases_configured ? '已配置' : '未配置（界面显示代号）'}`,
            `GitHub：${data.github ? (data.github.ok ? '可访问（' + (data.github.login || '') + '）' : data.github.message) : '未探测'}`,
        ];
        showStatus(parts.join(' · '), 'info', 12000);
    } catch (error) {
        // 错误提示已显示
    }
});

/* ============================================================
   实时通道（WebSocket）
   ------------------------------------------------------------
   作用：别人改任务（改状态 / 认领 / 新建 / 移除 / 同步）时，
   自己的看板立刻跟着刷新，不用等 30 秒轮询。

   设计取舍：
   * 事件里**不带任务数据**，只带「谁、改了哪张、做了什么」。
     收到后重新拉一次 /api/tasks：任务真值永远只有一份（SQLite + GitHub），
     不会出现「本地按事件打补丁」打错导致的状态漂移。
   * 断线自动重连（指数退避 + 抖动），重连不上就退回轮询，
     所以 WebSocket 挂了看板照样能用，只是回到 30 秒刷新一次。
   * 浏览器原生 WebSocket，不引第三方库（组员在没外网的热点里也能用）。
   ============================================================ */
const REALTIME = {
    ws: null,                 // 当前 WebSocket
    connected: false,         // 是否已连上
    retry: 0,                 // 连续失败次数（用于退避）
    timer: null,              // 重连定时器
    keepalive: null,          // 应用层 ping 定时器
    online: [],               // 在线的人（代号列表）
    closedByUs: false,        // 页面卸载时主动关闭，不再重连
    lastEventAt: 0,           // 最近一次收到事件的时间
};

/** 把代号列表翻成真名列表（没有映射时就是代号）。 */
function realtimeNames(codes) {
    return (codes || []).map((code) => assigneeLabel(code));
}

/** 更新页脚的实时状态：在线人数 + 姓名。 */
function renderRealtimeInfo() {
    const wrap = el.realtimeInfo;
    const sep = el.realtimeSep;
    if (!wrap) return;

    const names = realtimeNames(REALTIME.online);
    if (!REALTIME.connected && !names.length) {
        wrap.hidden = true;
        if (sep) sep.hidden = true;
        return;
    }

    wrap.hidden = false;
    if (sep) sep.hidden = false;
    wrap.classList.toggle('realtime-off', !REALTIME.connected);

    if (!REALTIME.connected) {
        setText(el.realtimeText, '实时连接断开，已退回轮询');
        return;
    }
    setText(el.realtimeText, names.length
        ? `实时已连接 · 在线 ${names.length} 人（${names.join('、')}）`
        : '实时已连接');
}

/** 收到 tasks_changed：重新拉一次列表，并温和地提示是谁改的。 */
async function handleRealtimeTasksChanged(message) {
    REALTIME.lastEventAt = Date.now();
    const who = message && message.by ? assigneeLabel(message.by) : '';
    const issue = message && message.issue ? `#${message.issue}` : '';
    const detail = (message && message.detail) || '';
    const parts = [who, issue, detail].filter(Boolean).join(' ');
    if (parts) {
        showStatus(`${parts}（已自动刷新）`, 'info', 4000);
    }
    if (!busy) await loadTasks(true);
}

/** 处理一条服务端事件。 */
function handleRealtimeMessage(raw) {
    let message = null;
    try {
        message = JSON.parse(raw);
    } catch (error) {
        return;                       // 不是 JSON 就忽略，不影响看板
    }
    if (!message || typeof message !== 'object') return;

    if (message.type === 'hello') {
        REALTIME.connected = true;
        REALTIME.retry = 0;
        renderRealtimeInfo();
    } else if (message.type === 'presence') {
        REALTIME.online = (message.users || []).map((item) => item.username);
        renderRealtimeInfo();
    } else if (message.type === 'tasks_changed') {
        handleRealtimeTasksChanged(message);
    }
    // pong / error 不需要处理
}

/** 建立连接（失败会自动重连）。 */
function connectRealtime() {
    if (!window.WebSocket) return;                 // 极老的浏览器：直接走轮询

    const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    let socket;
    try {
        socket = new WebSocket(`${scheme}//${window.location.host}/ws`);
    } catch (error) {
        scheduleRealtimeReconnect();
        return;
    }
    REALTIME.ws = socket;

    socket.addEventListener('open', () => {
        REALTIME.connected = true;
        REALTIME.retry = 0;
        renderRealtimeInfo();
        // 应用层保活：有些代理会掐掉长时间没有数据的连接
        clearInterval(REALTIME.keepalive);
        REALTIME.keepalive = setInterval(() => {
            if (socket.readyState === WebSocket.OPEN) {
                socket.send(JSON.stringify({ type: 'ping' }));
            }
        }, 25000);
    });

    socket.addEventListener('message', (event) => handleRealtimeMessage(event.data));

    socket.addEventListener('close', () => {
        REALTIME.connected = false;
        clearInterval(REALTIME.keepalive);
        renderRealtimeInfo();
        if (!REALTIME.closedByUs) scheduleRealtimeReconnect();
    });

    socket.addEventListener('error', () => {
        // 出错后浏览器一定会再触发 close，重连逻辑放在 close 里
        REALTIME.connected = false;
    });
}

/** 指数退避重连（1s、2s、4s…最多 30s，带随机抖动避免同时重连）。 */
function scheduleRealtimeReconnect() {
    clearTimeout(REALTIME.timer);
    REALTIME.retry += 1;
    const base = Math.min(1000 * (2 ** (REALTIME.retry - 1)), 30000);
    const delay = base + Math.random() * 400;
    REALTIME.timer = setTimeout(connectRealtime, delay);
}

/* ============================================================
   初始化
   ============================================================ */
async function init() {
    // 先确认登录状态：未登录会被 api() 直接送回登录页
    try {
        await loadMe();
    } catch (error) {
        return;   // 已经在跳转登录页了，不用继续
    }

    await loadAssignees();   // 拿到名单，保证人员视图与下拉框正确
    loadServerInfo();        // 顶部显示组员可用的局域网地址
    loadHealth();
    loadTasks();
    connectRealtime();       // 实时通道：别人改了立刻刷新
}

init();

// 轮询兜底：实时通道正常时只是每 2 分钟兜一次底（防漏事件），
// 断开时回到原来的 30 秒，保证 WebSocket 挂了功能也不受影响。
let pollTick = 0;
setInterval(() => {
    if (document.hidden || busy) return;
    pollTick += 1;
    if (REALTIME.connected && pollTick % 4 !== 0) return;
    loadTasks(true);
}, 30000);

// 离开页面时主动断开，服务端立刻把这个人从在线名单里摘掉
window.addEventListener('beforeunload', () => {
    REALTIME.closedByUs = true;
    if (REALTIME.ws) REALTIME.ws.close();
});
