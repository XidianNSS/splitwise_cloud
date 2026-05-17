# 设备优先方案代码修改实施计划

本文档用于指导后续代码修改，目标是把当前项目从：

- 本地普通用户账号 + `/api/v1/auth/exchange`
- `openwebui_user_id -> 本地 User`
- `users.allowed_devices`

迁移到：

- 普通用户直接使用 OpenWebUI token
- 后端按请求来源 IP 识别边端设备
- 当前阶段固定使用本机云端设备 `10.144.144.2`（设备 `cloud`）
- 后端通过临时 session 串起一次完整使用
- 云端前端页面仅管理员可用

本文档聚焦于：

1. 当前代码现状
2. 需要修改的具体模块
3. 分阶段实施顺序
4. 每一步的落地要求
5. 兼容与回归验证策略

---

## 1. 目标与边界

## 1.1 目标

本次改造的核心目标：

1. 普通用户不再依赖本地 `users` 账号体系
2. 普通用户不再调用 `/api/v1/auth/exchange`
3. 普通用户直接带 OpenWebUI token 调用后端
4. 后端通过请求来源 IP 自动识别边端设备
5. 后端通过临时 session 保持一次完整使用的上下文
6. 云端设备当前固定为本机设备 `cloud`
7. 云端前端页面只保留管理员入口和监控功能

## 1.2 不在第一阶段解决的问题

这些内容不属于第一阶段最小闭环：

- OpenWebUI 用户轻量档案管理
- 多云端设备选择能力
- 普通用户的审计后台页面
- 独立的 `GET /api/v1/devices/cloud-options`
- 代理/NAT/统一网关场景下的边端设备识别

## 1.3 已融入代码增强的处理原则

最近已经融入的内部增强应直接作为第一阶段实施基础保留：

- `backend/app/db/database.py`
  - SQLite `timeout=30`
  - `pool_pre_ping=True`
  - `expire_on_commit=False`
  - `session_scope()`
- `backend/app/services/network_probe.py`
  - 网络探测模块化
  - TTL 缓存
  - 并发限制
- `backend/app/services/prometheus_metrics.py`
  - Prometheus 查询模块化
  - 查询缓存
  - 并发查询
- `backend/app/core/config.py`
  - `PROMETHEUS_QUERY_TIMEOUT`
  - `PROMETHEUS_CACHE_SECONDS`
  - `NETWORK_PROBE_CACHE_SECONDS`
  - `NETWORK_MAX_CONCURRENT_PROBES`
  - `ADMIN_USERNAME`
  - `ADMIN_PASSWORD`
- `backend/app/models/models.py`
  - 管理员初始化已改为优先读取环境变量

这意味着后续实施时应遵循：

1. 不要把网络探测和 Prometheus 查询逻辑重新塞回 `schedule.py`
2. 不要回退管理员初始化逻辑到写死账号密码
3. 新增表、会话和迁移代码时，优先复用 `SessionLocal` / `session_scope()` 的现有封装

---

## 2. 当前代码现状

## 2.1 当前认证链路

当前普通用户链路是：

1. 边端前端拿到 OpenWebUI token
2. 调用 `/api/v1/auth/exchange`
3. 后端在 [auth.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/auth.py) 中：
   - 验签 OpenWebUI token
   - 解析 `openwebui_user_id`
   - 查本地 `User.openwebui_user_id`
   - 再签发内部 JWT
4. 后续所有普通用户接口都只认内部 JWT

受影响代码：

- [auth.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/auth.py)
- [deps.py](/home/nss-d/splitwise_cloud/backend/app/api/deps.py)
- [security.py](/home/nss-d/splitwise_cloud/backend/app/core/security.py)

## 2.2 当前普通用户与设备绑定方式

当前普通用户调度依赖：

- [models.py](/home/nss-d/splitwise_cloud/backend/app/models/models.py)
  - `User.openwebui_user_id`
  - `User.allowed_devices`
- [users.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/users.py)
  - `/users/my_devices`
  - 管理员创建用户时分配边端/云端设备
- [schedule.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/schedule.py)
  - 通过 `current_username -> User.allowed_devices` 找边端/云端设备

这是当前最需要被替换掉的链路。

补充说明：

- 当前 `schedule.py` 已经不再内嵌网络探测与 Prometheus 查询实现
- 它通过：
  - [network_probe.py](/home/nss-d/splitwise_cloud/backend/app/services/network_probe.py)
  - [prometheus_metrics.py](/home/nss-d/splitwise_cloud/backend/app/services/prometheus_metrics.py)
  获取边云环境指标

后续改造调度链路时，应只替换“设备来源”和“用户归属”逻辑，不应破坏这两个 service 模块的接入方式。

## 2.3 当前调度任务归属方式

当前 [models.py](/home/nss-d/splitwise_cloud/backend/app/models/models.py) 中：

- `ScheduleTask.username`

当前 [schedule.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/schedule.py) 中：

- `trigger`
- `tasks/{task_id}`
- `tasks/{task_id}/strategy`
- `tasks/{task_id}/stream`

都依赖：

- `current_username`
- `ScheduleTask.username`

这意味着当前任务归属是：

**本地 username 驱动**

需要迁移为：

**OpenWebUI 用户 ID 驱动**

## 2.4 当前云端前端页面状态

当前云端前端页面在 [frontend/dashboard.js](/home/nss-d/splitwise_cloud/frontend/dashboard.js) 中：

- 使用 `/login`
- 允许普通用户进入
- 调 `/users/my_devices`
- 展示普通用户设备视图和 Grafana 切换

这和新方案不一致，后续需要收口成：

- 云端前端页面仅管理员登录
- 普通用户不再走这套页面

---

## 3. 目标架构

第一阶段实施后的目标架构：

### 普通用户

1. OpenWebUI 登录后拿到 token
2. 调用：
   - `POST /api/v1/session/init`
3. 后端：
   - 验签 OpenWebUI token
   - 根据来源 IP 匹配边端设备
   - 创建 `edge_sessions`
   - 返回 `session_id` 和固定云端设备信息
4. 前端选择：
   - `model_type`
5. 前端调用：
   - `POST /api/v1/schedule/trigger`
   - 并带 `Session-Id`
6. 后端把本次选择写入 session 后继续原有调度链路

### 管理员

1. 继续走本地 `/api/v1/login`
2. 继续使用内部 JWT
3. 继续使用云端前端页面进行：
   - 设备资产管理
   - Grafana / Prometheus 监控
   - runtime 状态查看

---

## 4. 第一阶段要改的内容

## 4.1 数据模型改造

### 4.1.1 新增 `edge_sessions`

在 [models.py](/home/nss-d/splitwise_cloud/backend/app/models/models.py) 中新增：

### `EdgeSession`

建议字段：

- `id`
- `session_id`，唯一，字符串 UUID
- `openwebui_user_id`，索引
- `edge_device_id`，索引
- `edge_ip`
- `cloud_device_id`，固定为 `cloud`
- `model_type`，可空
- `status`
  - `active`
  - `expired`
  - `closed`
- `created_at`
- `updated_at`
- `expires_at`

用途：

- 保存一次完整普通用户使用会话
- 初始化识别边端后挂住上下文
- 初始化时写入固定 `cloud_device_id`
- trigger 时写入 `model_type`

### 4.1.2 扩展 `ScheduleTask`

建议在 [models.py](/home/nss-d/splitwise_cloud/backend/app/models/models.py) 中为 `ScheduleTask` 增加：

- `openwebui_user_id`，索引，可空
- `edge_session_id`，索引，可空

说明：

- 现有 `username` 字段先保留，兼容管理员/旧数据
- 新普通用户流程不再依赖 `username`
- 新普通用户流程按 `openwebui_user_id` 做任务归属

### 4.1.3 暂不移除 `User`

第一阶段不删除 `User` 表，也不删除 `allowed_devices` 字段。

原因：

- 管理员登录仍然依赖 `users`
- 现有管理端仍然依赖 `users`
- 数据迁移需要分步完成

但第一阶段开始：

- 普通用户主流程不再依赖 `User`
- 普通用户主流程不再依赖 `allowed_devices`

### 4.1.4 数据库迁移要求

需要在 [models.py](/home/nss-d/splitwise_cloud/backend/app/models/models.py) 的轻量迁移逻辑中补充：

1. 新表 `edge_sessions`
2. `schedule_tasks.openwebui_user_id`
3. `schedule_tasks.edge_session_id`

并保证：

- 老数据库可自动补齐字段
- 旧任务数据不被破坏
- 继续兼容当前管理员初始化逻辑
- 不破坏 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 的环境变量优先级

补充要求：

- 如果需要单独做数据库辅助操作，可优先复用 [database.py](/home/nss-d/splitwise_cloud/backend/app/db/database.py) 中现有的 `session_scope()`
- 不需要替换掉当前 `SessionLocal` 用法，只需要在新增批量迁移或初始化逻辑时优先沿用现有封装

---

## 4.2 认证与依赖注入改造

## 4.2.1 保留管理员认证

保留现状：

- [auth.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/auth.py)
  - `/login`
- [deps.py](/home/nss-d/splitwise_cloud/backend/app/api/deps.py)
  - `get_current_user`
  - `get_current_admin`

它们继续服务：

- 管理员云端前端
- 管理端接口

## 4.2.2 新增普通用户 OpenWebUI token 依赖

在 [deps.py](/home/nss-d/splitwise_cloud/backend/app/api/deps.py) 中新增独立依赖，不要复用管理员内部 JWT 依赖。

建议新增：

### `get_current_openwebui_payload`

职责：

- 从 `Authorization: Bearer <openwebui_token>` 取 token
- 调 [security.py](/home/nss-d/splitwise_cloud/backend/app/core/security.py) 的 `decode_openwebui_access_token`
- 返回解析后的 payload

### `get_current_openwebui_user_id`

职责：

- 从 OpenWebUI payload 中提取用户唯一 ID
- 不再转换为本地 username

## 4.2.3 新增来源 IP 提取与边端设备识别辅助函数

建议在：

- `deps.py`
  或
- 新的 service/helper 文件

中新增：

### `resolve_edge_device_by_request_ip(request, db)`

职责：

1. 读取 `request.client.host`
2. 遍历 `devices` 表中的边端设备
3. 用当前已有的 `extract_ip(Device.value)` 逻辑匹配
4. 返回：
   - `edge_device_id`
   - `edge_ip`
   - 对应 `Device`

要求：

- 第一阶段默认只认直连来源 IP
- 找不到匹配边端时，返回明确 403/400
- 错误信息要清楚说明“来源 IP 未匹配到任何边端设备”

---

## 4.3 新增会话初始化接口

建议新增文件：

- `backend/app/api/v1/session.py`

也可以暂时放在 [auth.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/auth.py) 中，但从长期维护看，更建议单独成路由文件。

### 新接口

```http
POST /api/v1/session/init
Authorization: Bearer <openwebui_token>
```

### 接口职责

1. 验签 OpenWebUI token
2. 解析 `openwebui_user_id`
3. 根据来源 IP 匹配边端设备
4. 创建 `edge_sessions`
5. 返回：
   - `session_id`
   - `openwebui_user_id`
   - 当前识别出的边端设备信息
   - 固定云端设备信息

### 建议返回结构

```json
{
  "session_id": "uuid",
  "openwebui_user_id": "ow-user-x",
  "edge_device": {
    "id": "edge_A",
    "name": "边端设备 A"
  },
  "cloud_device": {
    "id": "cloud",
    "name": "云端总枢纽"
  }
}
```

### 设计要求

- 第一阶段直接在这里返回固定云端设备信息
- 第一阶段不需要新增独立的 `GET /api/v1/devices/cloud-options`
- 如果 session 还没过期且用户/IP 一致，可考虑复用已有 active session；否则新建

---

## 4.4 调度接口改造

## 4.4.1 修改 `EdgeTriggerRequest`

在 [schemas.py](/home/nss-d/splitwise_cloud/backend/app/schemas/schemas.py) 中把：

```python
class EdgeTriggerRequest(BaseModel):
    model_type: str
```

第一阶段继续保持为：

- `model_type`

## 4.4.2 trigger 鉴权方式调整

当前 [schedule.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/schedule.py) 的：

- `POST /trigger`

使用的是：

- `current_username: str = Depends(get_current_user)`

需要改成：

- OpenWebUI token 依赖
- `Session-Id`

### trigger 新职责

1. 校验 OpenWebUI token
2. 读取 `Session-Id`
3. 找到 active `edge_sessions`
4. 校验：
   - session 对应的 `openwebui_user_id` 与当前 token 一致
   - 当前请求来源 IP 与 `session.edge_ip` 一致
5. 确认 session 中固定云端设备仍为 `cloud`
6. 把 `model_type` 写入 session
7. 创建调度任务
8. 启动异步 `process_schedule_task`

## 4.4.3 `process_schedule_task` 的设备来源调整

当前 [schedule.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/schedule.py) 的 `process_schedule_task` 通过：

- `username -> User.allowed_devices`

找到边端和云端设备。

需要改成：

- 通过 `edge_session_id` 找 `EdgeSession`
- 直接取：
  - `edge_device_id`
  - `cloud_device_id`（固定为 `cloud`）

但需要明确保留：

- [network_probe.py](/home/nss-d/splitwise_cloud/backend/app/services/network_probe.py) 的使用
- [prometheus_metrics.py](/home/nss-d/splitwise_cloud/backend/app/services/prometheus_metrics.py) 的使用
- 当前 `MODEL_REGISTRY`、策略下发、runtime 回调聚合逻辑

也就是说，这一步主要改：

- 设备来源
- 任务归属
- session 关联

而不是重写环境指标采集实现。

后续：

- 查 `devices`
- 查 Prometheus
- 查 runtime

都沿着这两个设备 ID 继续做。

### 这一步需要删除的旧依赖

在 `process_schedule_task` 中移除：

- `db.query(User).filter(User.username == username)...`
- `user.allowed_devices`

## 4.4.4 任务归属校验调整

以下接口目前按 `ScheduleTask.username == current_username` 判权：

- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/strategy`
- `GET /tasks/{task_id}/stream`

第一阶段需要改成：

- `ScheduleTask.openwebui_user_id == current_openwebui_user_id`

说明：

- 第一阶段不强制这些任务查询接口再带 `Session-Id`
- 仍然以：
  - `task_id`
  - `openwebui_token`
  做归属校验

---

## 4.5 schema 改造

在 [schemas.py](/home/nss-d/splitwise_cloud/backend/app/schemas/schemas.py) 里新增：

### 第一阶段必须新增

- `SessionInitResponse`
- `SessionInitEdgeDeviceInfo`
- `SessionInitCloudDeviceInfo`
- 可选 `EdgeSessionHeader` 对应说明性 schema（如果需要）

### 第一阶段必须修改

- `EdgeTriggerRequest`
- 如有需要，新增普通用户任务接口的响应文档说明

### 第一阶段保留不动

- runtime 相关 schema
- 策略相关 schema
- 管理员登录相关 schema

---

## 4.6 云端前端页面改造

目标：

- 云端前端页面只给管理员使用

受影响文件：

- [frontend/dashboard.html](/home/nss-d/splitwise_cloud/frontend/dashboard.html)
- [frontend/dashboard.js](/home/nss-d/splitwise_cloud/frontend/dashboard.js)
- [frontend/dashboard.css](/home/nss-d/splitwise_cloud/frontend/dashboard.css)

### 第一阶段具体要求

1. 保留管理员登录流程：
   - `/api/v1/login`
2. 普通用户不再是云端前端页面支持对象
3. 移除/禁用普通用户依赖：
   - `loadMyDevices()`
   - `/users/my_devices`
   - 普通用户设备指标视图
4. `initializeDashboard()` 只按管理员视角初始化页面

### 建议实现方式

- 最小改法：保留登录框，但非 admin 用户直接拒绝进入云端前端
- 更干净的改法：前端 UI 直接明确“此页面仅管理员使用”

---

## 4.7 用户接口改造

受影响文件：

- [users.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/users.py)

### 第一阶段

保留管理员相关接口：

- `GET /users`
- `POST /users`
- `PATCH /users/{username}/openwebui-binding`
- `DELETE /users/{username}`

原因：

- 管理端现有功能先不动
- `users` 表仍用于管理员

### 第一阶段需要收口的接口

- `GET /users/my_devices`

建议：

- 直接标注为废弃
  或
- 云端前端不再调用它

如果短期不删代码，也要把它从普通用户主流程中移除。

---

## 4.8 设备接口改造

受影响文件：

- [devices.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/devices.py)

### 第一阶段

管理员设备接口全部保留：

- `GET /system/devices`
- `POST /system/devices`
- `DELETE /system/devices/{device_id}`
- `GET /system/devices/prometheus/targets/{job_type}`

### 第一阶段不强制新增

- `GET /api/v1/devices/cloud-options`

原因：

- 第一阶段当前固定使用本机云端设备，不需要额外拉取云端设备列表

### 第二阶段可新增

如果以后想支持多云端选择，再增加该接口。

---

## 5. 分阶段实施顺序

## 5.1 第一阶段：最小可用闭环

建议再拆成 4 个更小的落地批次，每完成一批就联调一次。

### 阶段 1A：认证入口能力

目标：

- 先让后端能直接识别 OpenWebUI token
- 先把普通用户入口能力建起来
- 先不切换真实调度主流程

主要修改：

- [security.py](/home/nss-d/splitwise_cloud/backend/app/core/security.py)
- [deps.py](/home/nss-d/splitwise_cloud/backend/app/api/deps.py)
- [schemas.py](/home/nss-d/splitwise_cloud/backend/app/schemas/schemas.py)
- 新增 `backend/app/api/v1/session.py`
  或在 [auth.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/auth.py) 中临时增加

完成：

- 新增 OpenWebUI token 依赖
- 跑通 `POST /api/v1/session/init` 的最小骨架
- 能完成：
  - token 验签
  - `openwebui_user_id` 解析
  - 基础响应返回

完成后联调：

- 只测 `POST /api/v1/session/init`
- 再跑一次最小语法检查

### 阶段 1B：边端设备识别与临时 session

目标：

- 把“本次使用属于哪台边端设备”稳定记录下来
- 建立一次完整使用的临时上下文

主要修改：

- [models.py](/home/nss-d/splitwise_cloud/backend/app/models/models.py)
- [deps.py](/home/nss-d/splitwise_cloud/backend/app/api/deps.py)
- `backend/app/api/v1/session.py`
- [schemas.py](/home/nss-d/splitwise_cloud/backend/app/schemas/schemas.py)

完成：

- 新增 `EdgeSession`
- 补轻量迁移逻辑
- `session/init` 中按来源 IP 匹配边端设备
- `session/init` 创建 session 并返回：
  - `session_id`
  - `openwebui_user_id`
  - 边端设备信息
  - 固定云端设备信息

完成后联调：

- 测 `POST /api/v1/session/init`
- 确认能返回 `session_id`
- 确认能识别边端设备
- 确认错误 IP 会正确报错

### 阶段 1C：调度主链路切换

目标：

- 真正把普通用户调度切到新路径
- 让 session 成为调度上下文来源

主要修改：

- [schedule.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/schedule.py)
- [schemas.py](/home/nss-d/splitwise_cloud/backend/app/schemas/schemas.py)
- [models.py](/home/nss-d/splitwise_cloud/backend/app/models/models.py)

完成：

- `trigger` 改用 OpenWebUI token + `Session-Id`
- `trigger` 第一阶段只传：
  - `model_type`
- `process_schedule_task` 改从 session 取设备
- 任务查询接口改按 `openwebui_user_id` 判权
- `ScheduleTask` 扩展与新归属模型对齐

完成后联调：

- 跑完整最小链路：
  - `session/init`
  - `trigger`
  - 策略回调
  - runtime 下发
  - 任务完成

### 阶段 1D：旧链路收口与页面清理

目标：

- 让普通用户只走新入口
- 管理员页面职责收敛
- 文档与调试脚本同步

主要修改：

- [frontend/dashboard.js](/home/nss-d/splitwise_cloud/frontend/dashboard.js)
- [frontend/dashboard.html](/home/nss-d/splitwise_cloud/frontend/dashboard.html)
- [users.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/users.py)
- 文档与 mock 文件

完成：

- 普通用户入口收掉
- 普通用户 `my_devices` 逻辑移除
- 页面只保留管理员使用
- 边端前端对接文档与 mock 同步到新流程

完成后联调：

- 验证管理员页面不受影响
- 验证普通用户新链路仍然可用
- 验证旧链路如保留时不会干扰新链路

## 5.2 第二阶段：增强项

第二阶段再补：

- `openwebui_principals`
- 多云端设备选择能力
- 禁用/审计能力
- 清理 `users.allowed_devices` 历史依赖

---

## 6. 兼容策略

## 6.1 第一阶段建议保留但废弃的旧接口

为了平滑迁移，第一阶段建议：

- 保留 `/api/v1/auth/exchange`
  - 但标记为旧链路兼容接口
- 保留 `/users/my_devices`
  - 但从前端主流程移除

等边端前端和云端前端都切换完成后，再考虑物理删除。

## 6.2 管理员链路不改

第一阶段不要动管理员核心链路：

- `/login`
- `get_current_admin`
- 设备管理
- 监控页面

这样可以把风险集中在普通用户新链路上。

---

## 7. 验证与回归要求

第一阶段建议按 1A -> 1B -> 1C -> 1D 逐段验证，每完成一段就跑一轮。

## 7.1 普通用户新链路

### 阶段 1A 完成后

1. `POST /api/v1/session/init`
   - token 合法
   - 能正确解析 `openwebui_user_id`

### 阶段 1B 完成后

1. `POST /api/v1/session/init`
   - 来源 IP 能匹配边端设备
   - 返回 `session_id`
   - 返回边端设备信息和固定云端设备信息

### 阶段 1C 完成后

1. `POST /api/v1/schedule/trigger`
   - 能通过 `Session-Id` 成功触发
2. `GET /tasks/{task_id}`
   - 用 OpenWebUI token 能查询自己的任务
3. `GET /tasks/{task_id}/strategy`
   - 用 OpenWebUI token 能取回策略
4. `GET /tasks/{task_id}/stream`
   - 用 OpenWebUI token 能持续拉流

### 阶段 1D 完成后

1. 普通用户不再依赖旧页面入口
2. 管理员页面仍可正常使用

## 7.2 异常场景

1. OpenWebUI token 无效
2. 来源 IP 未匹配到边端设备
3. session 不存在
4. session 已过期
5. token 用户与 session 用户不一致
6. token 用户与 task owner 不一致

## 7.3 管理员链路

1. `/login` 仍然可用
2. 设备管理页可用
3. Grafana 监控页可用
4. runtime 状态页可用

---

## 8. 一句话实施建议

这次代码改造最重要的原则是：

**先在不破坏管理员功能的前提下，把普通用户链路从“本地用户驱动”切成“OpenWebUI token + 边端来源 IP + 临时 session 驱动”。**

第一阶段只做最小闭环，先跑通；
第二阶段再补用户档案、偏好和审计。
