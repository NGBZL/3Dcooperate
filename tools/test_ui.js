/**
 * 前端自检（可选）：在 jsdom 里真正跑一遍 `static/app.js`，
 * 用桩数据验证「搜索下拉 + 跳转高亮 + 从看板移除 / 恢复」这些纯前端行为。
 *
 * 它比 `node --check` 强的地方在于：会真的建 DOM、真的发事件、真的读回渲染结果，
 * 所以「下拉三层结构没显示」「点结果不跳转」「恢复后卡片没回来」这类问题都能被抓到。
 *
 * 用法（需要一次性装 jsdom，不装 JS 依赖也能正常跑后端）：
 *
 * ```bash
 * npm install jsdom          # 在项目根目录执行，会生成 node_modules/（已被 .gitignore 忽略）
 * node tools/test_ui.js
 * ```
 *
 * 不连真后端：fetch 全部换成桩数据，也不会写任何文件。
 */
const fs = require('fs');
const path = require('path');

/** jsdom 优先从项目根目录的 node_modules 找，找不到时给出可操作的提示。 */
function loadJsdom() {
    const candidates = [
        'jsdom',
        path.join(__dirname, '..', 'node_modules', 'jsdom'),
    ];
    for (const candidate of candidates) {
        try {
            return require(candidate);
        } catch (error) {
            // 试下一个位置
        }
    }
    console.error('✗ 没找到 jsdom。请先在项目根目录执行：npm install jsdom');
    process.exit(2);
}

const { JSDOM } = loadJsdom();

const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'templates', 'index.html'), 'utf8');
const appJs = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');

const TASKS = [
    {
        issue_number: 11, title: '完成点云配准模块', description: 'ICP 粗配准', status: 'todo',
        assignee: '小明', state: 'open', hidden: false, review_comment: '建议加空指针保护',
        review_verdict: 'pass', review_score: 88, branch: 'task/11-icp', commits: [],
        pr_number: 12, pr_state: 'open', pr_url: 'https://example/pr/12',
        pr_head: 'task/11-icp', pr_allowed: false, pr_allowed_by: '',
    },
    {
        issue_number: 12, title: '修复空指针崩溃', description: '配准里 null 判断', status: 'doing',
        assignee: '小红', state: 'open', hidden: false, review_comment: '', branch: '', commits: [],
    },
    {
        issue_number: 13, title: '写单元测试', description: '', status: 'todo',
        assignee: '', state: 'open', hidden: false, review_comment: '', branch: '', commits: [],
    },
    {
        issue_number: 14, title: '已移除的旧任务', description: '', status: 'done',
        assignee: '小明', state: 'closed', hidden: true, review_comment: '', branch: '', commits: [],
    },
];

const json = (data) => Promise.resolve({
    ok: true, status: 200, headers: { get: () => 'application/json' },
    json: () => Promise.resolve(data), text: () => Promise.resolve(JSON.stringify(data)),
});

// 桩后端：状态可变，这样「全部恢复」才能真的反映到下一次列表请求上
const state = { tasks: TASKS.map((t) => ({ ...t })), taskFetches: 0, writes: [], isLead: false };

// 用 dangerously + 注入 <script>：让 app.js 像在浏览器里那样以「经典脚本」执行，
// 这样它的顶层函数才是真正的全局函数（app.js 里有 'use strict'，
// 用 window.eval 的话函数会被关在 eval 自己的作用域里，测试就调不到 loadMe/render）。
const dom = new JSDOM(html, { runScripts: 'dangerously', url: 'http://127.0.0.1:5062/', pretendToBeVisual: true });
const { window } = dom;
const { document } = window;

// ---- 假 WebSocket：用来验证前端实时通道的接线（jsdom 本身没有 WebSocket）----
const sockets = [];
class FakeWebSocket {
    constructor(url) {
        this.url = url;
        this.readyState = 0;                 // CONNECTING
        this.sent = [];
        this.listeners = {};
        sockets.push(this);
    }
    addEventListener(type, fn) {
        (this.listeners[type] = this.listeners[type] || []).push(fn);
    }
    send(data) {
        this.sent.push(data);
    }
    close() {
        this.readyState = 3;
        this.emit('close', {});
    }
    // 下面几个是测试用的小工具
    emit(type, event) {
        (this.listeners[type] || []).forEach((fn) => fn(event));
    }
    open() {
        this.readyState = 1;
        this.emit('open', {});
    }
    push(message) {
        this.emit('message', { data: JSON.stringify(message) });
    }
    get lastSent() {
        return this.sent.length ? JSON.parse(this.sent[this.sent.length - 1]) : null;
    }
}
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSING = 2;
FakeWebSocket.CLOSED = 3;
window.WebSocket = FakeWebSocket;

// jsdom 没实现这些：滚动、以及 http 下不可用的剪贴板
window.Element.prototype.scrollIntoView = function scrollIntoView() {};
window.fetch = (url, options = {}) => {
    const target = String(url);
    const method = (options.method || 'GET').toUpperCase();
    if (method !== 'GET') {
        // 记下所有写请求，供拖拽相关的断言检查
        let body = null;
        try {
            body = options.body ? JSON.parse(options.body) : null;
        } catch (error) {
            body = options.body;
        }
        state.writes.push({ method, url: target, body });
    }
    if (target.includes('/api/me')) {
        return json({ username: '小明', display_name: '张三', is_default_password: true,
            min_password_length: 6, done_requires_agent: true, done_is_final: true,
            is_lead: state.isLead, can_merge_pr: state.isLead,
            allow_assign_to_others: state.isLead });
    }
    if (target.includes('/api/tasks/restore-all')) {
        state.tasks.forEach((t) => { t.hidden = false; });
        return json({ message: '已恢复全部任务' });
    }
    // PR 相关接口：只回消息，具体行为由断言检查请求
    if (/\/pr(\/allow|\/merge)?$/.test(target)) {
        return json({ message: 'PR 操作完成', pr_number: 12, allowed: true, merged: true });
    }
    if (/\/branch$/.test(target)) {
        return json({ message: '已创建分支', branch: 'task/13-fix', created: true });
    }
    if (target.includes('/api/tasks')) {
        state.taskFetches += 1;
        // 返回副本：前端会就地改任务对象（乐观更新），不能让桩数据被一起改掉
        return json(state.tasks.map((t) => ({ ...t })));
    }
    if (target.includes('/api/assignees')) {
        return json({ assignees: ['小明', '小红'], display_names: { 小明: '张三', 小红: '李四' },
            aliases_configured: true, unassigned_label: '未分配' });
    }
    if (target.includes('/api/server-info')) return json({ url: 'http://127.0.0.1:5062', local_only: false });
    if (target.includes('/api/health')) return json({ status: 'ok' });
    return json({});
};

const errors = [];
window.addEventListener('error', (e) => errors.push(String(e.message)));
window.onerror = (msg) => { errors.push(String(msg)); };

let ok = 0;
let bad = 0;
function check(name, cond, extra = '') {
    if (cond) { ok += 1; console.log('  PASS  ' + name); }
    else { bad += 1; console.log('  FAIL  ' + name + (extra ? '  -> ' + extra : '')); }
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * 模拟一次拖拽：卡片上 dragstart → 列上 dragover / drop。
 *
 * jsdom 没有真的拖拽，也没有 DataTransfer，所以这里手搓一个最小实现，
 * 让 app.js 里的事件委托逻辑能照常跑（它只用 setData/getData/dropEffect）。
 */
function dragCardTo(card, column) {
    if (!card || !column) return false;
    const store = {};
    const dataTransfer = {
        effectAllowed: '',
        dropEffect: '',
        setData: (key, value) => { store[key] = value; },
        getData: (key) => store[key] || '',
    };
    ['dragstart'].forEach((type) => {
        const event = new window.Event(type, { bubbles: true, cancelable: true });
        event.dataTransfer = dataTransfer;
        card.dispatchEvent(event);
    });
    ['dragover', 'drop'].forEach((type) => {
        const event = new window.Event(type, { bubbles: true, cancelable: true });
        event.dataTransfer = dataTransfer;
        column.dispatchEvent(event);
    });
    return true;
}
const input = document.getElementById('search-input');
const panel = document.getElementById('search-results');
const press = (key) => input.dispatchEvent(new window.KeyboardEvent('keydown', { key, bubbles: true }));

(async () => {
    // 在 window 里执行 app.js（等价于浏览器加载脚本）
    // 把 app.js 作为真实脚本注入页面（等价于浏览器加载 <script src="...">）
    const appScript = document.createElement('script');
    appScript.textContent = appJs;
    document.body.appendChild(appScript);
    await wait(300);   // 等 init() 里的几个 await 走完 + 搜索防抖 120ms

    check('脚本执行没有抛错', errors.length === 0, errors.join(' | '));
    check('空搜索时下拉是隐藏的', panel.hidden === true);
    check('看板渲染出 3 张卡片（不含被移除的）',
        document.querySelectorAll('#board .card').length === 3,
        String(document.querySelectorAll('#board .card').length));
    check('被移除的卡片不在看板上', !document.querySelector('.card[data-issue="14"]'));

    // —— 页脚「已从看板移除」——
    const hiddenInfo = document.getElementById('hidden-info');
    check('页脚提示已显示', hiddenInfo.hidden === false);
    check('页脚数量正确', document.getElementById('hidden-count').textContent === '1',
        document.getElementById('hidden-count').textContent);

    // —— 搜索：输入即弹下拉，且看板卡片不变 ——
    input.value = '空指针';
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
    await wait(250);

    check('输入后下拉展开', panel.hidden === false);
    check('匹配到 2 条（#11 评语 + #12 标题）', panel.querySelectorAll('.search-item').length === 2,
        String(panel.querySelectorAll('.search-item').length));
    check('命中的字被 <mark> 高亮', panel.querySelectorAll('mark').length >= 1
        && panel.querySelector('mark').textContent === '空指针');
    check('第一项默认高亮', panel.querySelector('.search-item').classList.contains('active'));
    check('底部有统计行', /找到 2 条/.test(panel.querySelector('.search-foot').textContent),
        panel.querySelector('.search-foot').textContent);
    check('搜索没有过滤看板（3 张还在）', document.querySelectorAll('#board .card').length === 3);
    const issues = Array.from(panel.querySelectorAll('.search-item .si-issue')).map((n) => n.textContent);
    check('评语命中的 #11 也在结果里', issues.includes('#11'), issues.join(','));
    check('标题命中的 #12 也在结果里', issues.includes('#12'), issues.join(','));

    // —— 键盘：↓ 之后 Enter 跳转 ——
    input.value = '配准';
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
    await wait(250);
    const count = panel.querySelectorAll('.search-item').length;
    check('「配准」匹配到 2 条（#11/#12；已移除的 #14 不算）', count === 2, String(count));

    press('ArrowDown');
    check('↓ 之后第二项高亮',
        panel.querySelectorAll('.search-item')[1].classList.contains('active'));
    const secondIssue = panel.querySelectorAll('.search-item')[1].querySelector('.si-issue').textContent;

    press('Enter');
    await wait(60);
    check('Enter 后下拉收起', panel.hidden === true);
    const flashed = document.querySelector('.card.card-flash');
    check('Enter 后目标卡片闪烁高亮', flashed !== null);
    check('闪烁的是选中的那一张', flashed && '#' + flashed.dataset.issue === secondIssue,
        flashed ? flashed.dataset.issue : 'none');
    check('状态条提示已定位', /已定位到 #/.test(document.getElementById('status-bar').textContent),
        document.getElementById('status-bar').textContent);

    // —— Esc 关闭 ——
    input.value = '配准';
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
    await wait(250);
    check('再次输入重新展开', panel.hidden === false);
    press('Escape');
    check('Esc 关闭下拉', panel.hidden === true);

    // —— 无匹配 ——
    input.value = 'zzz-不存在';
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
    await wait(250);
    check('无匹配时面板仍展开', panel.hidden === false);
    check('无匹配时显示空提示',
        panel.querySelector('.search-empty') !== null
        && /没有匹配/.test(panel.querySelector('.search-empty').textContent));

    // —— 点击面板外收起 ——
    document.body.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    check('点击面板外收起', panel.hidden === true);

    // —— 一键恢复被移除的卡片 ——
    document.getElementById('btn-restore-all').click();
    await wait(200);
    check('恢复后页脚提示消失', document.getElementById('hidden-info').hidden === true);
    check('恢复后 4 张卡片都在看板上', document.querySelectorAll('#board .card').length === 4,
        String(document.querySelectorAll('#board .card').length));

    // —— 圆圈 i：帮助提示都收在 data-hint 里，页面上不再铺开 ——
    // 先重新搜一次，让结果面板里带上 .search-foot
    input.value = '配准';
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
    await wait(250);

    const hints = [
        ['访问地址', document.getElementById('btn-access-help')],
        ['视图用法', document.getElementById('view-help')],
        ['搜索用法', document.getElementById('search-help')],
        ['新建任务', document.getElementById('modal-help')],
        ['修改密码', document.getElementById('pwd-help')],
    ];
    hints.forEach(([name, node]) => {
        check(`${name}有圆圈 i`, node !== null && node.classList.contains('hint'));
        check(`${name}的说明写在 data-hint 里`,
            node !== null && String(node.dataset.hint || '').length > 10,
            node ? node.dataset.hint : 'missing');
    });
    check('搜索快捷键提示已从结果面板移走（底部只剩条数）',
        panel.querySelector('.search-foot') !== null
        && !/↑↓/.test(panel.querySelector('.search-foot').textContent),
        panel.querySelector('.search-foot') ? panel.querySelector('.search-foot').textContent : '');
    check('搜索圆圈 i 的说明里含快捷键',
        /↑/.test(document.getElementById('search-help').dataset.hint));

    // Done 列的说明挂在列头圆圈 i 上（原来挂在列头 title）
    const doneHead = document.querySelector('.column.col-done .column-head');
    const doneHelp = document.querySelector('.column.col-done .column-help');
    check('Done 列头有圆圈 i', doneHelp !== null && !doneHelp.hidden);
    check('Done 列说明含「Agent」', doneHelp && /Agent/.test(doneHelp.dataset.hint || ''));
    check('Done 列头不再用 title 塞说明', doneHead && !doneHead.title);
    check('其它列没有多余的圆圈 i',
        Array.from(document.querySelectorAll('.column-help')).filter((n) => !n.hidden).length === 1);

    // 初始密码提醒：以提醒态圆圈 i 的形式挂在用户名旁（不再进状态条）
    const pwdHint = document.getElementById('default-pwd-hint');
    check('初始密码提醒显示为圆圈 i', pwdHint !== null && !pwdHint.hidden
        && pwdHint.classList.contains('hint-warn'));
    check('初始密码提醒的说明非空', String(pwdHint.dataset.hint || '').length > 10);
    check('状态条不再重复初始密码提示',
        !/初始密码/.test(document.getElementById('status-bar').textContent),
        document.getElementById('status-bar').textContent);

    // —— 弹窗（Apple 风格）：打开加 .active，关闭先 .closing 播动画再真正隐藏 ——
    const modal = document.getElementById('modal');
    document.getElementById('btn-new').click();
    await wait(60);
    check('打开弹窗后有 .active', modal.classList.contains('active'));
    check('打开弹窗后 aria-hidden=false', modal.getAttribute('aria-hidden') === 'false');
    check('打开弹窗时没有 .closing', !modal.classList.contains('closing'));

    modal.querySelector('.modal-mask').click();
    check('关闭时先加 .closing（播关闭动画）',
        modal.classList.contains('closing') && modal.classList.contains('active'));
    check('关闭时 aria-hidden 立刻置 true', modal.getAttribute('aria-hidden') === 'true');
    await wait(300);
    check('关闭动画结束后彻底隐藏',
        !modal.classList.contains('active') && !modal.classList.contains('closing'));

    // 刚关完立刻再打开：不能被上一次的关闭定时器收掉
    document.getElementById('btn-new').click();
    await wait(260);
    check('关闭动画期间重新打开仍然是打开的', modal.classList.contains('active'));
    modal.querySelector('.modal-mask').click();
    await wait(300);

    // —— 实时通道（WebSocket）：接线、在线名单、事件触发刷新、断线重连 ——
    check('页面启动后主动连了 /ws', sockets.length === 1 && /\/ws$/.test(sockets[0].url),
        sockets.length ? sockets[0].url : '没有建连接');
    check('用的是 ws:// 协议', sockets.length === 1 && sockets[0].url.startsWith('ws://'),
        sockets.length ? sockets[0].url : '');

    sockets[0].open();
    await wait(30);
    const rtInfo = document.getElementById('realtime-info');
    check('连上后页脚显示实时状态', rtInfo.hidden === false
        && /实时已连接/.test(document.getElementById('realtime-text').textContent),
        document.getElementById('realtime-text').textContent);

    sockets[0].push({
        type: 'presence', count: 2,
        users: [{ username: '小明', connections: 1 }, { username: '小红', connections: 1 }],
    });
    const rtText = document.getElementById('realtime-text').textContent;
    check('在线名单显示真名与人数', /在线 2 人/.test(rtText) && /张三/.test(rtText) && /李四/.test(rtText),
        rtText);
    check('页脚实时状态不显示代号', !/小明|小红/.test(rtText), rtText);

    const fetchesBefore = state.taskFetches;
    sockets[0].push({ type: 'tasks_changed', action: 'update_task', issue: 12,
        by: '小红', detail: '状态=doing' });
    await wait(150);
    check('收到 tasks_changed 会重新拉任务',
        state.taskFetches > fetchesBefore, `${fetchesBefore} -> ${state.taskFetches}`);
    const bar = document.getElementById('status-bar').textContent;
    check('底部状态条说明是谁改了什么', /李四/.test(bar) && /#12/.test(bar) && /状态=doing/.test(bar), bar);

    // 保活：每 25 秒发一次 ping（这里直接手动触发一次，验证格式对）
    sockets[0].open();
    await wait(30);
    check('连上后没有异常报错（保持 socket 打开）', errors.length === 0, errors.join(' | '));

    // 断线 → 自动重连
    sockets[0].close();
    await wait(50);
    check('断开后页脚提示已退回轮询',
        /退回轮询/.test(document.getElementById('realtime-text').textContent),
        document.getElementById('realtime-text').textContent);
    await wait(1600);
    check('断开后会自动重连（新建了连接）', sockets.length === 2, `sockets=${sockets.length}`);
    if (sockets[1]) sockets[1].open();

    // —— 未分配的任务：拖到别的列 = 认领给自己并推进（状态视图）——
    const unassignedCard = document.querySelector('.card[data-issue="13"]');
    const doingColumn = document.querySelector('.column.col-doing');
    check('未分配的卡片可以拖（draggable != false）',
        unassignedCard !== null && unassignedCard.draggable !== false);

    const writesBefore = state.writes.length;
    dragCardTo(unassignedCard, doingColumn);
    await wait(120);
    const claimWrite = state.writes.slice(writesBefore)
        .find((w) => w.method === 'PUT' && /\/api\/tasks\/13$/.test(w.url));
    check('拖到 Doing 列会发一次 PUT /api/tasks/13', claimWrite !== undefined,
        JSON.stringify(state.writes.slice(writesBefore)));
    check('这一次请求同时带上了「认领给自己」和「新状态」',
        claimWrite && claimWrite.body && claimWrite.body.assignee === '小明'
        && claimWrite.body.status === 'doing',
        claimWrite ? JSON.stringify(claimWrite.body) : '没有请求');
    check('状态条提示说的是「认领并移动」',
        /认领/.test(document.getElementById('status-bar').textContent),
        document.getElementById('status-bar').textContent);

    // —— 人员视图：拖到自己的列 = 认领；拖到别人的列 = 被拒 ——
    document.getElementById('view-assignee').click();
    await wait(60);
    const myColumn = document.querySelector('.column[data-key="小明"]');
    const otherColumn = document.querySelector('.column[data-key="小红"]');
    const unassignedColumn = document.querySelector('.column.col-unassigned');
    check('人员视图里有「未分配」列', unassignedColumn !== null);
    check('人员视图里没有别人的列时也有自己的列', myColumn !== null && otherColumn !== null);

    const cardInUnassigned = unassignedColumn
        ? unassignedColumn.querySelector('.card[data-issue="13"]') : null;
    check('未分配的卡片在「未分配」列里', cardInUnassigned !== null);

    const writesBefore2 = state.writes.length;
    dragCardTo(cardInUnassigned, myColumn);
    await wait(120);
    const assignWrite = state.writes.slice(writesBefore2)
        .find((w) => w.method === 'PUT' && /\/api\/tasks\/13\/assignee$/.test(w.url));
    check('拖到自己的列会认领（PUT .../assignee）', assignWrite !== undefined,
        JSON.stringify(state.writes.slice(writesBefore2)));
    check('认领的目标是自己', assignWrite && assignWrite.body && assignWrite.body.assignee === '小明',
        assignWrite ? JSON.stringify(assignWrite.body) : '没有请求');

    const writesBefore3 = state.writes.length;
    dragCardTo(cardInUnassigned, otherColumn);
    await wait(120);
    check('拖到别人的列被拒绝，不会发请求',
        state.writes.length === writesBefore3,
        JSON.stringify(state.writes.slice(writesBefore3)));

    document.getElementById('view-status').click();
    await wait(60);

    // —— 组长：可以给任何人分派（组员做不到）——
    // 现状：我是 小明（组员），#12 是 小红 的
    document.getElementById('view-assignee').click();
    await wait(60);
    const otherCard = document.querySelector('.card[data-issue="12"]');
    check('组员看到的别人卡片是锁定的', otherCard !== null
        && otherCard.classList.contains('card-locked'));

    await wait(250);   // 等上一段（认领 #13）的异步写入彻底落定，避免污染计数
    const writesBefore4 = state.writes.length;
    dragCardTo(otherCard, document.querySelector('.column[data-key="小明"]'));
    await wait(150);
    check('组员把别人的任务拖给自己 → 被拒（不发请求）',
        !state.writes.slice(writesBefore4).some((w) => /\/api\/tasks\/12/.test(w.url)),
        JSON.stringify(state.writes.slice(writesBefore4)));

    // 切成组长（重新拉一次 /api/me）
    state.isLead = true;
    await window.loadMe();
    window.render();
    await wait(60);
    check('组长登录后用户名旁边带「组长」标记',
        /组长/.test(document.getElementById('user-name').textContent),
        document.getElementById('user-name').textContent);

    const otherCardAsLead = document.querySelector('.card[data-issue="12"]');
    check('组长看到的别人卡片不再锁定（可拖）', otherCardAsLead !== null
        && !otherCardAsLead.classList.contains('card-locked')
        && otherCardAsLead.draggable !== false);

    const writesBefore5 = state.writes.length;
    dragCardTo(otherCardAsLead, document.querySelector('.column[data-key="小明"]'));
    await wait(150);
    const leadAssign = state.writes.slice(writesBefore5)
        .find((w) => w.method === 'PUT' && /\/api\/tasks\/12\/assignee$/.test(w.url));
    check('组长把别人的任务分派给自己 → 发请求', leadAssign !== undefined,
        JSON.stringify(state.writes.slice(writesBefore5)));
    check('分派目标是组长选的（小明）',
        leadAssign && leadAssign.body && leadAssign.body.assignee === '小明',
        leadAssign ? JSON.stringify(leadAssign.body) : '没有请求');
    check('提示语用的是「分派」',
        /分派/.test(document.getElementById('status-bar').textContent),
        document.getElementById('status-bar').textContent);

    document.getElementById('view-status').click();
    await wait(60);

    // —— 卡片显示 PR + 分支 / PR 面板 ——
    const prChip = document.querySelector('.card[data-issue="11"] .pr-chip');
    check('有 PR 的卡片显示 PR 徽章', prChip !== null
        && /PR #12/.test(prChip.textContent), prChip ? prChip.textContent : '没有徽章');
    check('未放行时徽章写「待放行」', prChip !== null && /待放行/.test(prChip.textContent),
        prChip ? prChip.textContent : '');
    check('卡片上也有分支名',
        /task\/11-icp/.test(document.querySelector('.card[data-issue="11"] .branch-chip')?.textContent || ''),
        document.querySelector('.card[data-issue="11"] .branch-chip')?.textContent || '无');
    check('没有 PR 的卡片不显示 PR 徽章',
        document.querySelector('.card[data-issue="13"] .card-pr').hidden === true);

    // 组长打开面板：允许/合并按钮可见，合并按钮要求先放行
    document.querySelector('.card[data-issue="11"] .pr-btn').click();
    await wait(40);
    const prModal = document.getElementById('pr-modal');
    check('PR 按钮能打开分支 / PR 面板', prModal.classList.contains('active'));
    check('组长能看到「允许合并」按钮', document.getElementById('btn-pr-allow').hidden === false);
    check('组长能看到「合并」按钮', document.getElementById('btn-pr-merge').hidden === false);
    check('未放行时「合并」是禁用的', document.getElementById('btn-pr-merge').disabled === true);
    check('面板里说明了要组长放行',
        /组长/.test(document.getElementById('pr-status').textContent));
    check('分支名已预填到输入框',
        /task\/11-icp/.test(document.getElementById('pr-branch').value),
        document.getElementById('pr-branch').value);

    // 点「允许合并」→ 发请求
    const writesBefore6 = state.writes.length;
    document.getElementById('btn-pr-allow').click();
    await wait(150);
    check('点「允许合并」会调用 /pr/allow',
        state.writes.slice(writesBefore6).some((w) => /\/api\/tasks\/11\/pr\/allow$/.test(w.url)),
        JSON.stringify(state.writes.slice(writesBefore6)));
    check('组长能看到「强制合并」勾选项',
        document.getElementById('pr-force-line').hidden === false);

    // —— 已合并但没收尾：组长能看到「标记完成并关闭 Issue」 ——
    const stubTask11 = state.tasks.find((t) => t.issue_number === 11);
    stubTask11.pr_state = 'merged';
    await window.loadTasks(true);
    window.render();
    await wait(60);
    document.querySelector('.card[data-issue="11"] .pr-btn').click();
    await wait(40);
    check('已合并但没进 Done：出现「标记完成并关闭 Issue」',
        document.getElementById('btn-pr-finalize').hidden === false);
    check('面板里提示要补一次收尾',
        /补一次收尾/.test(document.getElementById('pr-status').textContent),
        document.getElementById('pr-status').textContent);
    const writesBefore7 = state.writes.length;
    document.getElementById('btn-pr-finalize').click();
    await wait(150);
    check('点它会调用 /pr/finalize',
        state.writes.slice(writesBefore7).some((w) => /\/api\/tasks\/11\/pr\/finalize$/.test(w.url)),
        JSON.stringify(state.writes.slice(writesBefore7)));
    stubTask11.pr_state = 'open';           // 还原，后面还有别的小节要看卡片

    document.getElementById('btn-pr-close').click();
    await wait(260);
    check('面板可以关闭', !prModal.classList.contains('active'));

    // 组员打开同一面板：看不到允许 / 合并
    state.isLead = false;
    await window.loadMe();
    window.render();
    await wait(60);
    document.querySelector('.card[data-issue="11"] .pr-btn').click();
    await wait(40);
    check('组员看不到「允许合并」', document.getElementById('btn-pr-allow').hidden === true);
    check('组员看不到「合并」', document.getElementById('btn-pr-merge').hidden === true);
    check('组员也看不到「标记完成并关闭 Issue」',
        document.getElementById('btn-pr-finalize').hidden === true);
    check('组员也看不到「强制合并」勾选项',
        document.getElementById('pr-force-line').hidden === true);
    check('组员面板里提示只有组长能做', /只有组长/.test(document.getElementById('pr-status').textContent));
    document.getElementById('btn-pr-close').click();
    await wait(260);

    // —— 已完成的任务：终态，界面上拖不动、也退不回上一列 ——
    const doneCard = document.querySelector('.card[data-issue="14"]');
    check('已完成的任务渲染成不可拖拽（draggable=false）',
        doneCard !== null && doneCard.draggable === false,
        doneCard ? String(doneCard.draggable) : '找不到 #14');
    check('已完成的任务带 card-final 标记',
        doneCard !== null && doneCard.classList.contains('card-final'));
    const doneLeftBtn = doneCard ? doneCard.querySelector('.move-left') : null;
    check('已完成的任务「← 退回上一列」按钮被禁用',
        doneLeftBtn !== null && doneLeftBtn.disabled === true);
    // #13 是「未分配」且未完成 → 按规则可以认领后推进，所以仍然可拖
    const movableCard = document.querySelector('.card[data-issue="13"]');
    check('未完成且可操作的任务仍然可以拖（draggable=true）',
        movableCard !== null && movableCard.draggable !== false,
        movableCard ? String(movableCard.draggable) : '找不到 #13');
    check('已完成的任务即使是自己的也不可拖（#14 负责人是自己）',
        doneCard !== null && doneCard.draggable === false);

    // Done 列的圆圈 i 说明里要写清「进不来也出不去」
    const doneHintText = document.querySelector('.column.col-done .column-help');
    check('Done 列说明同时讲了「不能拖进来」和「不能再移出去」',
        doneHintText !== null
        && /不能手动拖进来/.test(doneHintText.dataset.hint || '')
        && /不能再移出去/.test(doneHintText.dataset.hint || ''),
        doneHintText ? doneHintText.dataset.hint : 'missing');

    check('全程没有 JS 报错', errors.length === 0, errors.join(' | '));

    console.log(`\n合计：${ok} 通过，${bad} 失败`);
    window.close();
    process.exit(bad ? 1 : 0);
})();
