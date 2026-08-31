const API_BASE_URL = "/api/v1";

// ==========================================
// 1. 核心 HTTP 拦截器 (自动带 Token)
// ==========================================
async function fetchWithAuth(url, options = {}) {
    const token = localStorage.getItem('jwt_token');
    if (!token) throw new Error("未登录");

    const headers = { 'Accept': 'application/json', 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}`, ...options.headers };
    const response = await fetch(url, { ...options, headers });

    if (response.status === 401 || response.status === 403) {
        const data = await response.json().catch(() => ({}));
        if (response.status === 401) { logout(); throw new Error("登录已过期"); }
        throw new Error(data.detail || "权限不足");
    }
    return response;
}

// ==========================================
// 2. 登录与登出逻辑
// ==========================================
async function handleLogin() {
    const user = document.getElementById("login-user").value;
    const pass = document.getElementById("login-pass").value;
    const errDiv = document.getElementById("login-error");

    if (!user || !pass) return errDiv.textContent = "账号密码不能为空";
    errDiv.textContent = "正在验证...";

    try {
        const res = await fetch(`${API_BASE_URL}/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: user, password: pass })
        });

        if (!res.ok) throw new Error("账号或密码错误");
        const data = await res.json();

        if (data.role !== 'admin') {
            localStorage.removeItem('jwt_token');
            localStorage.removeItem('current_user');
            localStorage.removeItem('user_role');
            throw new Error("云端前端页面当前仅支持管理员登录");
        }

        localStorage.setItem('jwt_token', data.access_token);
        localStorage.setItem('current_user', data.username);
        localStorage.setItem('user_role', data.role);

        initializeDashboard();
    } catch (error) { errDiv.textContent = error.message; }
}

function logout() {
    localStorage.clear();
    window.ipToDeviceMap = {};
    document.getElementById("login-overlay").style.display = "flex";
    document.getElementById("grafana-frame").src = "";
    document.getElementById("admin-panel-btn").style.display = "none";
}

// ==========================================
// 3. 后台管理面板逻辑 (Admin CRUD)
// ==========================================
let systemDevicesMap = {};

function openAdminModal() {
    document.getElementById("admin-modal-overlay").style.display = "flex";
    refreshAllAdminData();
}

function closeAdminModal() {
    document.getElementById("admin-modal-overlay").style.display = "none";
    loadSystemDevices();
}

async function refreshAllAdminData() {
    document.getElementById("add-user-msg").textContent = "";
    document.getElementById("add-dev-msg").textContent = "";
    await fetchDevices();
    await fetchUsers();
}

async function fetchDevices() {
    const tbody = document.getElementById("device-table-body");
    try {
        const res = await fetchWithAuth(`${API_BASE_URL}/system/devices`);
        const devices = await res.json();
        tbody.innerHTML = ''; systemDevicesMap = {};
        devices.forEach(d => {
            systemDevicesMap[d.id] = d.name;
            tbody.innerHTML += `<tr>
                <td style="font-weight:bold">${d.id}</td><td>${d.name}</td><td><code>${d.value}</code></td>
                <td>${d.id !== 'cloud' ? `<span class="delete-btn" onclick="deleteDevice('${d.id}')">下线</span>` : '<span style="color:#666">保留</span>'}</td>
            </tr>`;
        });
    } catch (error) {}
}

async function createNewDevice() {
    const id = document.getElementById("new-dev-id").value;
    const name = document.getElementById("new-dev-name").value;
    const val = document.getElementById("new-dev-value").value;
    // 👇 获取选择的类型
    const type = document.getElementById("new-dev-type").value;
    const msg = document.getElementById("add-dev-msg");

    if (!id || !name || !val) return msg.innerHTML = '<span style="color:red">所有字段必填</span>';
    try {
        // 👇 body 中增加 device_type: type
        await fetchWithAuth(`${API_BASE_URL}/system/devices`, { method: 'POST', body: JSON.stringify({ id, name, value: val, device_type: type }) });
        msg.innerHTML = '<span style="color:var(--accent-green)">✅ 资产录入成功</span>';
        // 清空表单
        document.getElementById("new-dev-id").value = "";
        document.getElementById("new-dev-name").value = "";
        document.getElementById("new-dev-value").value = "";
        refreshAllAdminData();
    } catch (e) { msg.innerHTML = `<span style="color:red">❌ ${e.message}</span>`; }
}

async function deleteDevice(id) {
    if (!confirm(`警告：确定删除物理设备【${id}】吗？这会同步更新管理员设备列表与监控视图。`)) return;
    try { await fetchWithAuth(`${API_BASE_URL}/system/devices/${id}`, { method: 'DELETE' }); refreshAllAdminData(); } catch (e) { alert("删除失败: " + e.message); }
}

async function fetchUsers() {
    const tbody = document.getElementById("user-table-body");
    try {
        const res = await fetchWithAuth(`${API_BASE_URL}/users`);
        const users = await res.json();
        tbody.innerHTML = '';
        users.forEach(u => {
            const scopeHtml = '<span class="tag edge">全量设备</span>';
            tbody.innerHTML += `<tr>
                <td style="font-weight:bold; color:var(--text-bright);">${u.username}</td>
                <td>${u.role === 'admin' ? '🛡️ Admin' : '👤 User'}</td>
                <td>${scopeHtml}</td>
                <td>${u.username !== 'admin' ? `<span class="delete-btn" onclick="deleteUser('${u.username}')">删除</span>` : '<span style="color:#666">不可操作</span>'}</td>
            </tr>`;
        });
    } catch (error) { tbody.innerHTML = `<tr><td colspan="4" style="color:red">加载失败: ${error.message}</td></tr>`; }
}

async function createNewUser() {
    const username = document.getElementById("new-username").value;
    const password = document.getElementById("new-password").value;
    const msgDiv = document.getElementById("add-user-msg");

    if (!username || !password) return msgDiv.innerHTML = '<span style="color:red">账号和密码必填</span>';

    msgDiv.innerHTML = '创建中...';
    try {
        await fetchWithAuth(`${API_BASE_URL}/users`, {
            method: 'POST',
            body: JSON.stringify({
                username,
                password,
            })
        });
        msgDiv.innerHTML = '<span style="color:var(--accent-green)">✅ 创建成功！</span>';
        document.getElementById("new-username").value = "";
        document.getElementById("new-password").value = "";
        fetchUsers();
    } catch (error) {
        msgDiv.innerHTML = `<span style="color:red">❌ ${error.message}</span>`;
    }
}

async function deleteUser(username) {
    if (!confirm(`警告：确定要永久删除账号【${username}】吗？`)) return;
    try { await fetchWithAuth(`${API_BASE_URL}/users/${username}`, { method: 'DELETE' }); fetchUsers(); } catch (error) { alert("删除失败: " + error.message); }
}

// ==========================================
// 4. 大屏设备控制与侧边栏路由
// ==========================================
window.ipToDeviceMap = {}; // 🌟 新增：用于全局缓存 IP 到设备名称的映射字典

async function loadSystemDevices() {
    const selector = document.getElementById("custom-device-selector");
    selector.innerHTML = '<option value="">加载设备列表中...</option>';
    try {
        const response = await fetchWithAuth(`${API_BASE_URL}/system/devices`);
        const devices = await response.json();
        selector.innerHTML = '';
        window.ipToDeviceMap = {};
        devices.forEach(device => {
            const option = document.createElement("option");
            option.value = device.value;
            option.textContent = device.name;
            selector.appendChild(option);

            // 1. 核心逻辑：自动从 value 中提取真实 IP (现在会自动提取出 10.144.144.2)
            const ipMatch = device.value.match(/(?:\d{1,3}\.){3}\d{1,3}/);
            if (ipMatch) {
                window.ipToDeviceMap[ipMatch[0]] = device.name;
            }

            // 2. 兼容逻辑：仅保留针对本地测试的环回地址兼容，去掉硬编码的物理 IP
            if (device.id === 'cloud' || device.value.includes('127.0.0.1') || device.value.includes('localhost')) {
                window.ipToDeviceMap['127.0.0.1'] = device.name;
                window.ipToDeviceMap['localhost'] = device.name;
            }
        });
        switchGrafanaDevice();
    } catch (error) { selector.innerHTML = '<option value="">🚨 获取设备失败</option>'; }
}

function switchGrafanaDevice() {
    const val = document.getElementById("custom-device-selector").value;
    if (!val) return;
    const ip = val.split(":")[0];
    const isAscend = val.includes(":9500");
    const dashboardId = isAscend ? "addfqnr/e0d73c7" : "ad9hqhg/b9a97b3";
    const grafanaBaseUrl = `${window.location.protocol}//${window.location.hostname}:3001`;

    document.getElementById("grafana-frame").src =
        `${grafanaBaseUrl}/d/${dashboardId}?orgId=1&from=now-6h&to=now&timezone=browser&refresh=auto&kiosk&var-device=${encodeURIComponent(ip)}`;
}
//新增判断是否是昇腾，若是昇腾，则展示grafana中昇腾专属dashborad，若不是，则展示用户监控大屏dashboard

// ==========================================
// 5. 云端运行态总览页面
// ==========================================
let runtimeRefreshTimer = null;
let runtimeLastOverview = null;

const PHASE_LABELS = {
    strategy: "切分策略计算",
    loading: "模型加载与完整性确认",
    completed: "加载完成，等待推理",
    failed: "失败"
};

const MODEL_STATE_LABELS = {
    empty: "未加载模型",
    loading: "模型加载中",
    ready: "模型已就绪"
};

const SLOT_STATE_LABELS = {
    free: "空闲",
    bound: "已绑定",
    retained: "模型保留中",
    unloading: "卸载中",
    needs_reconcile: "状态待校正"
};

const PROCESS_STATE_LABELS = {
    running: "进程运行中",
    stopped: "进程未启动",
    failed: "进程异常"
};

function escapeHtml(value) {
    if (value === null || value === undefined || value === "") return "-";
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function shortId(value, size = 8) {
    if (!value) return "-";
    const text = String(value);
    return text.length > size ? `${text.slice(0, size)}...` : text;
}

function formatTime(value) {
    if (!value) return "-";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString("zh-CN", { hour12: false });
}

function clampPercent(value) {
    const numeric = Number(value || 0);
    return Math.max(0, Math.min(100, numeric));
}

function statusTone(value) {
    if (["ready", "running", "completed", "free", "retained", "passed", true].includes(value)) return "ok";
    if (["loading", "accepted", "bound", "pending", "strategy", "running_loading", "running_strategy"].includes(value)) return "warn";
    if (["failed", "stopped", "needs_reconcile", false].includes(value)) return "bad";
    return "neutral";
}

function badge(label, value) {
    return `<span class="runtime-badge runtime-badge-${statusTone(value)}">${escapeHtml(label)}</span>`;
}

function renderRuntimeSummary(summary = {}) {
    const cards = [
        ["Cloud Slots", summary.cloud_slot_total, "登记的云端 decode slot"],
        ["Running", summary.cloud_slot_running, "正在运行的 decode_server"],
        ["Ready", summary.cloud_slot_ready, "模型已加载完成"],
        ["Bound", summary.cloud_slot_bound, "已绑定到边端 session"],
        ["Active Req", summary.active_request_total, "当前活跃推理请求"],
        ["Waiting", summary.waiting_task_total, "加载/等待队列任务"],
    ];
    document.getElementById("runtime-summary-grid").innerHTML = cards.map(([title, value, desc]) => `
        <div class="runtime-summary-card">
            <span>${escapeHtml(title)}</span>
            <strong>${escapeHtml(value ?? 0)}</strong>
            <small>${escapeHtml(desc)}</small>
        </div>
    `).join("");
}

function renderRuntimeAlerts(alerts = []) {
    const container = document.getElementById("runtime-alerts");
    const activeAlerts = alerts.filter(alert => alert.source !== "task");
    if (!activeAlerts.length) {
        container.innerHTML = `<div class="runtime-alert runtime-alert-ok">当前没有检测到运行态异常。</div>`;
        return;
    }
    container.innerHTML = activeAlerts.slice(0, 8).map(alert => `
        <div class="runtime-alert runtime-alert-${escapeHtml(alert.level || "warning")}">
            <strong>${escapeHtml(alert.level || "warning")}</strong>
            <span>${escapeHtml(alert.message)}</span>
            <code>${escapeHtml(alert.slot_id || alert.task_id || alert.source || "")}</code>
        </div>
    `).join("");
}

function renderCloudSlots(slots = []) {
    document.getElementById("runtime-slot-count").textContent = `${slots.length} slots`;
    const container = document.getElementById("runtime-cloud-slots");
    if (!slots.length) {
        container.innerHTML = `<div class="runtime-empty-card">暂无 cloud decode slot。</div>`;
        return;
    }
    container.innerHTML = slots.map(slot => {
        const runtimeState = slot.runtime_state || {};
        const ownerSession = slot.owner_session || {};
        const runtimeBadgeHtml = slot.process_state === "stopped"
            ? badge("服务未启动", "stopped")
            : runtimeState.ready === false && slot.model_state !== "ready" && slot.model_state !== "loading"
                ? badge("模型未加载", false)
                : "";
        return `
            <article class="runtime-slot-card runtime-slot-${statusTone(slot.slot_state)}">
                <div class="runtime-slot-topline">
                    <div>
                        <span class="runtime-section-label">slot #${escapeHtml(slot.slot_index)}</span>
                        <h4>${escapeHtml(slot.slot_id)}</h4>
                    </div>
                    ${badge(PROCESS_STATE_LABELS[slot.process_state] || slot.process_state, slot.process_state)}
                </div>
                <div class="runtime-badge-row">
                    ${slot.slot_state === "retained" && slot.model_state === "ready" ? "" : badge(MODEL_STATE_LABELS[slot.model_state] || slot.model_state, slot.model_state)}
                    ${badge(SLOT_STATE_LABELS[slot.slot_state] || slot.slot_state, slot.slot_state)}
                    ${runtimeBadgeHtml}
                </div>
                <dl class="runtime-kv">
                    <div><dt>模型</dt><dd>${escapeHtml(slot.model_type || runtimeState.model_type)}</dd></div>
                    <div><dt>使用边端</dt><dd>${escapeHtml(ownerSession.edge_device_name || ownerSession.edge_ip || slot.owner_session_id)}</dd></div>
                    <div><dt>Task</dt><dd title="${escapeHtml(slot.task_id || runtimeState.task_id)}">${escapeHtml(shortId(slot.task_id || runtimeState.task_id, 12))}</dd></div>
                    <div><dt>PID</dt><dd>${escapeHtml(slot.process_pid)}</dd></div>
                    <div><dt>HTTP</dt><dd>${escapeHtml(slot.control_url)}</dd></div>
                    <div><dt>gRPC</dt><dd>${escapeHtml(slot.grpc_target)}</dd></div>
                    <div><dt>活跃请求</dt><dd>${escapeHtml(runtimeState.active_request_count ?? slot.active_request_count)}</dd></div>
                    <div><dt>完整性</dt><dd>${escapeHtml(slot.integrity_status)} / ${escapeHtml(slot.confirmation_status)}</dd></div>
                </dl>
                ${slot.runtime_state_error ? `<div class="runtime-inline-error">${escapeHtml(slot.runtime_state_error)}</div>` : ""}
            </article>
        `;
    }).join("");
}

function progressBar(label, value) {
    const percent = clampPercent(value);
    return `
        <div class="runtime-progress">
            <div class="runtime-progress-meta"><span>${escapeHtml(label)}</span><strong>${percent}%</strong></div>
            <div class="runtime-progress-track"><div style="width:${percent}%"></div></div>
        </div>
    `;
}

function renderTaskTimeline(tasks = []) {
    const container = document.getElementById("runtime-task-timeline");
    const activeTasks = tasks.filter(task => ["accepted", "running"].includes(task.status));
    const recentFailedTasks = tasks.filter(task => task.status === "failed").slice(0, 3);
    if (!activeTasks.length) {
        const failedHtml = recentFailedTasks.length ? `
            <div class="runtime-history-block">
                <div class="runtime-history-title">最近失败记录</div>
                ${recentFailedTasks.map(task => `
                    <div class="runtime-history-item">
                        <span title="${escapeHtml(task.task_id)}">${escapeHtml(shortId(task.task_id, 12))}</span>
                        <small>${escapeHtml(task.message || task.error_detail)}</small>
                    </div>
                `).join("")}
            </div>
        ` : "";
        container.innerHTML = `<div class="runtime-empty-card">暂无运行中的调度任务。</div>${failedHtml}`;
        return;
    }
    container.innerHTML = activeTasks.slice(0, 8).map(task => `
        <article class="runtime-task-card">
            <div class="runtime-task-head">
                <div>
                    <span class="runtime-section-label">${escapeHtml(PHASE_LABELS[task.phase] || task.phase)}</span>
                    <h4 title="${escapeHtml(task.task_id)}">${escapeHtml(shortId(task.task_id, 14))}</h4>
                </div>
                ${badge(task.status, task.status)}
            </div>
            <p>${escapeHtml(task.message || task.error_detail)}</p>
            ${progressBar("总体进度", task.overall_progress)}
            <div class="runtime-progress-pair">
                ${progressBar("Edge 策略", task.edge_strategy_progress)}
                ${progressBar("Cloud 策略", task.cloud_strategy_progress)}
                ${progressBar("Edge 完整性", task.edge_integrity_progress)}
                ${progressBar("Cloud 完整性", task.cloud_integrity_progress)}
                ${progressBar("Edge 加载", task.edge_runtime_load_progress)}
                ${progressBar("Cloud 加载", task.cloud_runtime_load_progress)}
            </div>
            <div class="runtime-task-foot">
                <span>${escapeHtml(task.session?.edge_device_name || task.edge_device?.name || task.edge_slot_id)}</span>
                <span>${escapeHtml(formatTime(task.updated_at))}</span>
            </div>
            ${task.error_detail ? `<div class="runtime-inline-error">${escapeHtml(task.error_detail)}</div>` : ""}
        </article>
    `).join("");
}

function renderRuntimeBindings(bindings = []) {
    const tbody = document.getElementById("runtime-binding-table");
    if (!bindings.length) {
        tbody.innerHTML = `<tr><td colspan="6">暂无运行时绑定。</td></tr>`;
        return;
    }
    tbody.innerHTML = bindings.slice(0, 12).map(binding => `
        <tr>
            <td title="${escapeHtml(binding.session_id)}">${escapeHtml(shortId(binding.session_id, 12))}</td>
            <td>${escapeHtml(binding.session?.edge_device_name || binding.session?.edge_ip)}</td>
            <td>${escapeHtml(binding.edge_slot_id)}</td>
            <td>${escapeHtml(binding.cloud_slot_id)}</td>
            <td title="${escapeHtml(binding.task_id)}">${escapeHtml(shortId(binding.task_id, 12))}</td>
            <td>${badge(binding.status, binding.status)}</td>
        </tr>
    `).join("");
}

function renderRuntimeOverview(data) {
    runtimeLastOverview = data;
    renderRuntimeSummary(data.summary);
    renderRuntimeAlerts(data.alerts);
    renderCloudSlots(data.cloud_slots);
    renderTaskTimeline(data.recent_tasks);
    renderRuntimeBindings(data.bindings);
    const generatedAt = data.generated_at ? formatTime(data.generated_at) : "未知时间";
    document.getElementById("runtime-refresh-status").textContent = `最后刷新: ${generatedAt}`;
}

async function refreshRuntimeOverview() {
    const status = document.getElementById("runtime-refresh-status");
    if (!status) return;
    status.textContent = "正在刷新...";
    try {
        const response = await fetchWithAuth(`${API_BASE_URL}/admin/runtime/overview`);
        const data = await response.json();
        renderRuntimeOverview(data);
    } catch (error) {
        status.textContent = `刷新失败: ${error.message}`;
        if (!runtimeLastOverview) {
            document.getElementById("runtime-summary-grid").innerHTML = `<div class="runtime-empty-card">运行态接口暂不可用：${escapeHtml(error.message)}</div>`;
        }
    }
}

function startRuntimeAutoRefresh() {
    if (runtimeRefreshTimer) return;
    refreshRuntimeOverview();
    runtimeRefreshTimer = window.setInterval(refreshRuntimeOverview, 5000);
}

function stopRuntimeAutoRefresh() {
    if (!runtimeRefreshTimer) return;
    window.clearInterval(runtimeRefreshTimer);
    runtimeRefreshTimer = null;
}

function switchView(viewId, navElement) {
    document.querySelectorAll('.view-section').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
    document.getElementById(viewId).classList.add('active');
    navElement.classList.add('active');
    if (viewId === "view-sandbox") {
        startRuntimeAutoRefresh();
    } else {
        stopRuntimeAutoRefresh();
    }
}

function initializeDashboard() {
    const role = localStorage.getItem('user_role');
    if (role !== 'admin') {
        logout();
        const errDiv = document.getElementById("login-error");
        errDiv.textContent = "云端前端页面当前仅支持管理员登录";
        return;
    }

    document.getElementById("login-overlay").style.display = "none";
    document.getElementById("login-error").textContent = "";

    const username = localStorage.getItem('current_user');
    document.getElementById("current-user-display").textContent = `👤 在线身份: ${username.toUpperCase()}`;

    if (role === 'admin') {
        document.getElementById("admin-panel-btn").style.display = "inline-block";
    } else {
        document.getElementById("admin-panel-btn").style.display = "none";
    }

    loadSystemDevices();
}

// ==========================================
// 6. 全局初始化触发器
// ==========================================
if (localStorage.getItem('jwt_token')) {
    initializeDashboard();
}
