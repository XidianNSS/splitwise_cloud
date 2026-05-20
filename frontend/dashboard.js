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
    document.getElementById("grafana-frame").src = `http://10.144.144.2:3000/d/${dashboardId}?orgId=1&from=now-6h&to=now&timezone=browser&refresh=auto&kiosk&var-device=${encodeURIComponent(ip)}`;
}
//新增判断是否是昇腾，若是昇腾，则展示grafana中昇腾专属dashborad，若不是，则展示用户监控大屏dashboard

function switchView(viewId, navElement) {
    document.querySelectorAll('.view-section').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
    document.getElementById(viewId).classList.add('active');
    navElement.classList.add('active');
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
