# 项目代码清理指导文档

本文档用于指导当前云端后端项目的下一轮代码收口，重点目标是：

- 清理已经废弃但还残留在代码中的旧流程
- 清理已经不再启用但没有删除干净的接口、字段、schema、注释与前端逻辑
- 在不破坏当前主流程联调的前提下，让代码结构更干净、更容易维护

本文档不聚焦“新增功能”，而是聚焦“减法收口”。

## 当前完成状态总览

截至当前代码状态，本清理文档对应的实施进度如下：

- 第一批：`部分完成`
- 第二批：`已完成`
- 第三批：`已完成`
- 第四批：`已完成`
- 第五批：`已完成`
- 第 9.1 项旧 runtime 回调兼容入口：`已完成`

说明：

- “部分完成”表示主清理目标已基本落地，但仍保留了少量出于健壮性或兼容性考虑的保护性实现
- “部分完成”表示主清理目标已基本落地，但仍保留了少量出于健壮性考虑的保护性实现

---

## 1. 当前主流程基线

当前真正使用中的主流程是：

1. 边端前端使用 OpenWebUI token
2. 调用 `POST /api/v1/session/init`
3. 拿到 `session_id`
4. 使用：
   - `Authorization: Bearer <openwebui_token>`
   - `Session-Id: <session_id>`
   调用 `POST /api/v1/schedule/trigger`
5. 后续查询：
   - `GET /api/v1/schedule/tasks/{task_id}`
   - `GET /api/v1/schedule/tasks/{task_id}/strategy`
   - `GET /api/v1/schedule/tasks/{task_id}/stream?token=<openwebui_token>`

当前不再作为主流程使用的内容包括：

- `/api/v1/auth/exchange`
- `/api/v1/users/my_devices`
- `/api/v1/users/{username}/openwebui-binding`
- 本地普通用户账号体系

因此，后续清理应以“不影响以上主流程”为第一原则。

---

## 2. 清理目标

本轮代码清理的目标分为 4 类：

### 2.1 清理废弃接口

把已经明确废弃、且现在只是返回 `410 Gone` 的旧接口从正式代码中移除。

### 2.2 清理废弃 schema 与重复字段

把只为旧流程服务的请求/响应模型，以及已经被新字段取代的重复字段清掉。

### 2.3 清理旧注释、旧文案、旧前端逻辑

把已经和当前系统设计不一致的提示语、注释、前端展示逻辑清掉，避免误导协作者。

### 2.4 清理用户模型历史包袱

在确认不再需要后，逐步移除本地普通用户时代留下的字段与表意。

---

## 3. 推荐清理批次

建议按 5 个批次推进，每批都单独联调验证。

---

## 4. 第一批：删除明确废弃的旧接口

当前状态：`已完成`

这一批风险最低，优先处理。

### 4.1 清理项

删除以下接口及其实现：

- `POST /api/v1/auth/exchange`
- `GET /api/v1/users/my_devices`
- `PATCH /api/v1/users/{username}/openwebui-binding`

当前实际落地情况：

- `POST /api/v1/auth/exchange`：`已删除`
- `PATCH /api/v1/users/{username}/openwebui-binding`：`已删除`
- `GET /api/v1/users/my_devices`：`未再作为正式接口暴露，但保留了隐藏的 404 占位路由`

之所以保留 `my_devices` 的隐藏 404 占位，是为了避免该路径被 `/{username}` 动态路由误命中后退化成 `405`，属于保护性收口，不再出现在 OpenAPI 中。

### 4.2 涉及文件

- [backend/app/api/v1/auth.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/auth.py)
- [backend/app/api/v1/users.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/users.py)

### 4.3 清理原因

这些接口当前已经不是“兼容可用”，而是明确废弃，只返回 `410`。

继续保留它们的问题是：

- OpenAPI 路由面仍然暴露旧流程
- 后续协作者会误以为旧流程还可恢复
- schema 和依赖也会继续被拖着走

### 4.4 清理后验证

验证以下接口仍然正常：

1. `POST /api/v1/session/init`
2. `POST /api/v1/schedule/trigger`
3. `GET /api/v1/schedule/tasks/{task_id}`
4. `GET /api/v1/schedule/tasks/{task_id}/strategy`
5. `POST /api/v1/login`
6. `GET /api/v1/users`

---

## 5. 第二批：删除废弃 schema 与误导性输入字段

当前状态：`已完成`

这一批风险也较低，建议紧接着做。

### 5.1 清理项

删除只为旧接口服务的 schema：

- `TokenExchangeRequest`
- `UserOpenWebUIBindingUpdate`

当前实际落地情况：

- `TokenExchangeRequest`：`已删除`
- `UserOpenWebUIBindingUpdate`：`已删除`
- `UserCreate`：`已收紧为仅允许创建 admin`

收紧当前已经被收口的 schema：

- `UserCreate`

建议把 `UserCreate` 从：

- `username`
- `password`
- `role`
- `allowed_devices`
- `openwebui_user_id`

收口为当前真实需要的最小字段，例如：

- `username`
- `password`

如仍保留 `role`，则只接受 `admin`。

### 5.2 涉及文件

- [backend/app/schemas/schemas.py](/home/nss-d/splitwise_cloud/backend/app/schemas/schemas.py)
- [backend/app/api/v1/users.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/users.py)
- [backend/app/api/v1/auth.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/auth.py)

### 5.3 清理原因

当前 schema 层还在向阅读者暗示：

- 还存在 token exchange 输入模型
- 还存在普通用户 OpenWebUI 绑定更新
- 还存在可自由指定 `allowed_devices` 的账号创建方式

这些都与当前系统设计不一致。

### 5.4 清理后验证

验证管理员创建账号仍然正常：

1. `POST /api/v1/users` 可创建管理员
2. 新管理员可通过 `/api/v1/login` 登录
3. `/api/v1/users` 返回结果仍正确

---

## 6. 第三批：清理重复字段与旧状态写入

当前状态：`已完成`

这一批开始进入“数据模型收口”，风险中等。

### 6.1 清理项

重点检查并清理：

- `ScheduleTask.username`

当前任务创建时同时写入：

- `username=current_openwebui_user_id`
- `openwebui_user_id=current_openwebui_user_id`

但代码中已经没有地方再通过 `ScheduleTask.username` 读取任务归属。

因此建议分两步：

### 第一步

先停止继续写入 `ScheduleTask.username`

当前状态：`已完成`

### 第二步

确认没有任何代码依赖该字段后，再决定是否：

- 保留字段但视为废弃
- 或在后续迁移中删除字段

当前状态：`已完成`

说明：

- 当前代码已经停止写入 `ScheduleTask.username`
- SQLAlchemy 模型中已经移除该字段
- SQLite 数据库中的 `schedule_tasks.username` 列已经完成物理删除
- 迁移前已生成数据库备份，迁移后已完成主链路联调验证

### 6.2 涉及文件

- [backend/app/models/models.py](/home/nss-d/splitwise_cloud/backend/app/models/models.py)
- [backend/app/api/v1/schedule.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/schedule.py)

### 6.3 清理原因

重复字段会导致：

- 阅读成本提高
- 后续维护者误以为本地 username 仍参与调度
- 数据语义模糊

### 6.4 清理后验证

验证任务主链路仍然正常：

1. `session/init`
2. `trigger`
3. `tasks`
4. `strategy`
5. SSE

同时确认数据库中新增任务记录仍然完整可用。

---

## 7. 第四批：清理前端中的旧用户视角逻辑

当前状态：`已完成`

这一批主要是云端前端页面收口。

### 7.1 清理项

检查并重构管理员大屏中的以下逻辑：

- `window.boundDeviceMap`
- “当前账号绑定设备信息不完整”
- “边端 + 云端绑定组合”推导可用模型

当前实际落地情况：

- `window.boundDeviceMap`：`已移除`
- “当前账号绑定设备信息不完整”提示：`已移除`
- 模型展示逻辑：`已改为系统全局 runtime 视角`
- 页面标题“我的可用大模型 (部署状态)”：`已改为系统协同模型状态`

### 7.2 涉及文件

- [frontend/dashboard.js](/home/nss-d/splitwise_cloud/frontend/dashboard.js)

### 7.3 当前问题

当前云端前端页面已经是管理员专用页面，但部分逻辑仍然沿用“单个用户绑定一组边云设备”的旧思路。

这会带来两个问题：

1. 逻辑语义已经和管理员视角不匹配
2. 多 edge / 多 cloud 场景下会产生任意选中的展示结果

### 7.4 推荐调整方向

管理员页面应围绕以下视角构建：

- 系统全量设备
- 系统全量 runtime 状态
- 系统级可用模型分布

而不是：

- 某个用户绑定的 edge/cloud 对

### 7.5 清理后验证

验证云端前端页面：

1. 管理员登录正常
2. 设备列表正常
3. Grafana 切换正常
4. 模型状态展示逻辑不再依赖“用户绑定设备”

---

## 8. 第五批：清理用户模型历史字段

当前状态：`部分完成`

这一批风险相对最高，建议最后做。

### 8.1 候选清理项

重点评估以下字段是否还需要保留：

- `User.openwebui_user_id`
- `User.allowed_devices`

### 8.2 关于 `User.openwebui_user_id`

当前普通用户已经不再映射到本地 `users` 表。

如果管理员账号也不需要和 OpenWebUI 身份绑定，那么可以继续清理：

- 模型字段
- 索引
- 轻量迁移补列逻辑
- `ow-admin` 回填逻辑
- 用户列表返回字段

当前实际落地情况：

- `User.openwebui_user_id` 模型字段：`已删除`
- `users.openwebui_user_id` 数据库列：`已物理删除`
- `ow-admin` 初始化 / 回填逻辑：`已删除`
- `create_user` 中对该字段的无效写入：`已删除`

说明：

- 这次清理仅针对 `User` 表上的本地管理员历史字段
- `EdgeSession.openwebui_user_id` 与 `ScheduleTask.openwebui_user_id` 仍然保留，继续作为外部身份归属字段使用

### 8.3 关于 `User.allowed_devices`

当前该字段已完成清理：

- `User.allowed_devices` 模型字段：`已删除`
- `users.allowed_devices` 数据库列：`已物理删除`
- 管理员列表接口：`已改为固定返回全量监控范围`
- 设备录入/删除时对该字段的同步维护逻辑：`已删除`
- 管理员前端页面：`已改为展示“全量设备”而非账号绑定设备列表`

说明：

- 当前云端后端仅保留管理员本地账号
- 管理员默认具备系统全量设备监控权限
- Prometheus target discovery 继续完全基于 `devices` 表，不依赖用户表中的设备字符串

### 8.4 涉及文件

- [backend/app/models/models.py](/home/nss-d/splitwise_cloud/backend/app/models/models.py)
- [backend/app/api/v1/users.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/users.py)
- [backend/app/api/v1/devices.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/devices.py)
- [frontend/dashboard.js](/home/nss-d/splitwise_cloud/frontend/dashboard.js)

### 8.5 清理后验证

这一批完成后重点验证：

1. 管理员登录与账号管理
2. 设备录入与删除
3. 云端前端设备展示
4. Prometheus target discovery

---

## 9. 可选清理项

以下项目可以放在上述 5 批之后处理：

### 9.1 删除旧 runtime 回调兼容入口

当前状态：`已完成`

已清理接口：

- `POST /api/v1/schedule/runtime_callback`

当前说明：

- 旧统一入口已删除
- 当前正式使用接口为：
  - `/api/v1/schedule/runtime_callback/edge`
  - `/api/v1/schedule/runtime_callback/cloud`

### 9.2 清理历史计划文档

当前 `next_stage_adjustment_plans` 中仍保留若干历史方案文档。

这些文档有价值，但建议后续做状态标注，例如：

- 已完成
- 已废弃
- 仅作历史记录

避免协作者把旧方案误当成当前实施标准。

---

## 10. 推荐执行顺序

建议严格按以下顺序推进：

1. 删除废弃接口
2. 删除废弃 schema，收紧输入模型
3. 清理 `ScheduleTask.username` 等重复状态
4. 清理云端前端旧用户视角逻辑
5. 最后评估并清理 `User.openwebui_user_id` / `allowed_devices`

这样可以把风险从低到高逐步推进。

---

## 11. 每一批的统一验证清单

每做完一批，都建议做一次完整联调：

### 11.1 普通用户调度主链路

1. `POST /api/v1/session/init`
2. `POST /api/v1/schedule/trigger`
3. `GET /api/v1/schedule/tasks/{task_id}`
4. `GET /api/v1/schedule/tasks/{task_id}/strategy`
5. `GET /api/v1/schedule/tasks/{task_id}/stream`

### 11.2 管理员主链路

1. `POST /api/v1/login`
2. `GET /api/v1/users`
3. `POST /api/v1/users`
4. `GET /api/v1/system/devices`
5. `POST /api/v1/system/devices`
6. `DELETE /api/v1/system/devices/{device_id}`

### 11.3 runtime / 算法服务对接

1. `POST /api/v1/models/register`
2. `POST /api/v1/schedule/strategy_callback`
3. `POST /api/v1/schedule/runtime_callback/edge`
4. `POST /api/v1/schedule/runtime_callback/cloud`

---

## 12. 最终目标状态

清理完成后，项目应达到如下状态：

- 普通用户旧流程代码不再残留在正式接口面上
- schema 层只描述当前真实可用的协议
- 调度任务数据模型不再保留无意义重复字段
- 云端前端页面完全匹配管理员视角
- 用户模型只保留当前真实仍在使用的字段

一句话总结：

**让当前项目的代码结构，真正只反映“现在还活着的流程”，而不是继续背着旧方案的历史包袱。**
