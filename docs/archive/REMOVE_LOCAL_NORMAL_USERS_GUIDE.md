# 去除本地普通用户账号的修改指导文档

本文档用于指导“去除本地普通用户账号”这项收口改造，目标是：

- 云端后端不再维护“本地普通用户账号”这一整套体系
- 云端后端只保留管理员本地账号
- 普通用户不再通过云端前端页面登录、查看设备或管理权限
- 普通用户仅以 OpenWebUI 外部身份的形式存在于：
  - 会话
  - 调度任务
  - 可选审计记录

## 当前实施状态

当前代码已经完成了第一轮落地，现状如下：

- `POST /api/v1/session/init` 已经启用，普通用户通过 OpenWebUI token 初始化会话
- `POST /api/v1/schedule/trigger`、`GET /tasks/{task_id}`、`GET /tasks/{task_id}/strategy` 已经改为直接使用 OpenWebUI token
- `POST /api/v1/auth/exchange` 已废弃，当前返回 `410 Gone`
- `GET /api/v1/users/my_devices` 已废弃，当前返回 `410 Gone`
- 本地默认普通用户 `userA` 初始化逻辑已删除
- 云端前端页面已收口为管理员页面
- 历史普通用户数据已清理，当前数据库只保留管理员本地账号

本文档以下内容更适合作为“设计说明 + 回归检查清单”。

本文档只描述：

1. 为什么可以去除本地普通用户账号
2. 去除后哪些能力要保留
3. 需要修改哪些代码
4. 推荐的实施顺序
5. 每一步改完后如何验证

---

## 1. 目标结论

本次改造完成后，系统中“用户”分成两类：

### 1.1 管理员

管理员继续保留本地账号体系：

- 使用 `/api/v1/login`
- 使用内部 JWT
- 进入云端前端页面
- 管理设备、查看 Grafana、查看 runtime 状态

### 1.2 普通用户

普通用户不再作为本地账号存在：

- 不存在于本地 `users` 表中
- 不再通过 `/api/v1/login` 登录
- 不再调用 `/api/v1/auth/exchange`
- 不再进入云端前端页面
- 不再使用 `/api/v1/users/my_devices`

普通用户只通过：

- OpenWebUI token
- 请求来源 IP
- `edge_sessions`
- `schedule_tasks`

参与调度流程。

---

## 2. 去除本地普通用户账号后，哪些能力仍然要保留

去除本地普通用户账号，不代表“普通用户身份”完全消失。

仍然需要保留以下能力：

### 2.1 普通用户身份识别

普通用户仍然需要通过 OpenWebUI token 被识别。

保留：

- `openwebui_user_id`
- OpenWebUI token 验签
- 基于 token 的会话归属
- 基于 token 的任务归属

### 2.2 边端设备识别

普通用户调度仍然需要识别当前是哪台边端设备。

保留：

- 按请求来源 IP 匹配 `devices` 表
- 从 `devices` 表中找到边端设备和边端 IP

### 2.3 固定云端设备

当前阶段仍然保留：

- `cloud`
- `10.144.144.2`

### 2.4 会话与任务归属

保留：

- `edge_sessions.openwebui_user_id`
- `schedule_tasks.openwebui_user_id`
- `schedule_tasks.edge_session_id`

这些字段不是“本地普通用户账号”，而是普通用户外部身份的轻量记录。

---

## 3. 可以删除或收口的内容

## 3.1 可以删除的普通用户能力

### 后端接口

可以删除或废弃：

- `POST /api/v1/auth/exchange`
- `GET /api/v1/users/my_devices`
- `POST /api/v1/users` 中创建普通用户的能力
- `PATCH /api/v1/users/{username}/openwebui-binding`
  - 对普通用户的用途

### 数据初始化

可以删除：

- `userA` 初始化
- 任何默认普通用户初始化逻辑

### 云端前端页面

可以删除或禁用：

- 普通用户登录入口
- 普通用户设备选择与设备展示
- 普通用户查看 Grafana 大盘
- 普通用户沙盘页面视角

### 用户权限模型

可以停止依赖：

- `users.allowed_devices`
- `openwebui_user_id -> 本地 User`

---

## 3.2 不能删除的内容

### `users` 表本身

不能删，因为管理员仍然依赖它。

### 管理员登录链路

必须保留：

- `/api/v1/login`
- `get_current_user`
- `get_current_admin`

### `devices` 表

必须保留，因为：

- 设备管理依赖它
- Grafana / Prometheus 监控依赖它
- 边端设备识别依赖它
- runtime 注册与调度依赖它

---

## 4. 受影响的核心文件

## 4.1 后端模型与初始化

### [backend/app/models/models.py](/home/nss-d/splitwise_cloud/backend/app/models/models.py)

当前已完成：

- 删除 `userA` 初始化
- 管理员初始化继续保留
- `User` 表继续保留，但只作为管理员表使用

可选进一步收口：

- 后续可考虑限制 `User.role` 只允许 `admin`
- 但第一步不建议直接改数据库约束

---

## 4.2 认证接口

### [backend/app/api/v1/auth.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/auth.py)

需要修改：

- 保留 `/login`
- 废弃或删除 `/auth/exchange`

当前状态：

- 已保留 `/auth/exchange` 路径，但明确返回 `410 Gone`
- 已提示“普通用户请改走 /api/v1/session/init”
- 后续等所有调用方完全迁移后，可再物理删除

---

## 4.3 普通用户接口

### [backend/app/api/v1/users.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/users.py)

当前已完成：

- 删除 `GET /my_devices`
- 禁止创建普通用户
- 删除或废弃普通用户 OpenWebUI 绑定更新逻辑

当前状态：

- `GET /api/v1/users` 仅返回管理员账号
- `POST /api/v1/users` 只允许创建管理员
- `GET /api/v1/users/my_devices` 已返回 `410 Gone`
- 普通用户 OpenWebUI 绑定更新接口已废弃

#### 保留管理员相关接口

保留：

- `GET /api/v1/users`
- `DELETE /api/v1/users/{username}`

但它们只面向管理员账号数据。

#### 修改创建用户接口

`POST /api/v1/users`

当前已改为：

- 只允许创建管理员
- 如果 `role != admin`，直接报错

#### 删除普通用户设备逻辑

当前已删除或停止依赖：

- `allowed_devices` 对普通用户的前端校验逻辑依赖
- `/my_devices`

---

## 4.4 依赖注入与权限模型

### [backend/app/api/deps.py](/home/nss-d/splitwise_cloud/backend/app/api/deps.py)

当前状态：

- 管理员链路使用内部 JWT
- 普通用户链路使用 OpenWebUI token

这一层不需要大改，只需要确认：

- 管理员依赖继续保留
- 普通用户不再被映射为本地 username

---

## 4.5 云端前端页面

### [frontend/dashboard.js](/home/nss-d/splitwise_cloud/frontend/dashboard.js)
### [frontend/dashboard.html](/home/nss-d/splitwise_cloud/frontend/dashboard.html)

当前已完成：

- 完全去除普通用户页面行为
- 去除对 `/users/my_devices` 的依赖
- 去除普通用户设备大盘展示逻辑

当前已收口的关键旧残留是：

- `loadMyDevices()`
- `/api/v1/users/my_devices`
- 普通用户角色分支

改造目标：

- 云端前端页面只接受管理员登录
- 管理员直接基于 `devices` 表查看监控

也就是说，前端设备展示要从：

- “我的设备”

改成：

- “系统设备”

---

## 4.6 文档与 mock

### [EDGE_FRONTEND_INTEGRATION.md](/home/nss-d/splitwise_cloud/EDGE_FRONTEND_INTEGRATION.md)

当前已同步更新：

- 普通用户不再走 `/auth/exchange`
- 普通用户不再走云端前端页面
- 普通用户只走：
  - `POST /api/v1/session/init`
  - `POST /api/v1/schedule/trigger`

### [tests/mock_edge_client.py](/home/nss-d/splitwise_cloud/tests/mock_edge_client.py)

当前已经走新流程，一般不需要大改。

### 云端前端相关 mock/说明

当前应继续避免新增任何普通用户云端前端使用说明。

---

## 5. 对监控能力的影响

## 5.1 不会受影响的部分

以下核心监控能力不会因为删除本地普通用户账号而消失：

- `devices` 表
- 设备资产管理
- Prometheus target discovery
- Grafana iframe 监控
- runtime 注册状态查看

这些能力本质上依赖的是：

- `devices`
- `model_nodes`

而不是普通用户账号。

## 5.2 会受影响的部分

会消失的是：

- 普通用户登录云端前端后只看自己设备的大盘

因为这套逻辑本来就是：

- `users.allowed_devices`
- `/users/my_devices`

驱动的。

这正是本次要主动收掉的旧能力。

---

## 6. 推荐实施顺序

## 阶段 A：先停止“普通用户作为本地账号”继续扩张

目标：

- 不再创建新的普通用户账号
- 不再新增新的普通用户绑定

当前已完成：

- `POST /api/v1/users` 只允许创建管理员
- 管理界面不再鼓励创建普通用户

已验证重点：

- 管理员登录仍然正常
- 新普通用户链路完全不受影响

---

## 阶段 B：删除普通用户旧接口

目标：

- 从后端移除普通用户旧账号链路

当前已完成：

- 废弃 `/api/v1/auth/exchange`
- 删除 `/api/v1/users/my_devices`
- 废弃普通用户 OpenWebUI 绑定更新

已验证重点：

- 普通用户新链路：
  - `session/init`
  - `trigger`
  - `tasks`
  仍然正常
- 管理员接口不受影响

---

## 阶段 C：收口云端前端页面

目标：

- 云端前端页面彻底变为管理员页面

当前已完成：

- 删除普通用户设备视角
- 删除 `loadMyDevices()` 这条旧逻辑
- 监控页直接基于管理员可见的全量设备渲染

已验证重点：

- 管理员登录正常
- 设备列表、Grafana、runtime 状态正常

---

## 阶段 D：清理初始化数据与说明

目标：

- 去掉历史普通用户残留

当前已完成：

- 删除 `userA` 初始化
- 清理旧文档
- 清理和普通用户本地账号相关的说明
- 清理数据库中的历史普通用户记录

已验证重点：

- 新数据库初始化只生成管理员
- 普通用户仍可通过 OpenWebUI token 正常发起调度

---

## 7. 联调与回归验证清单

每一阶段完成后都建议检查：

### 7.1 普通用户新链路

验证：

1. `POST /api/v1/session/init`
2. `POST /api/v1/schedule/trigger`
3. `GET /api/v1/schedule/tasks/{task_id}`
4. `GET /api/v1/schedule/tasks/{task_id}/strategy`
5. `GET /api/v1/schedule/tasks/{task_id}/stream`

### 7.2 管理员链路

验证：

1. `/api/v1/login`
2. `/api/v1/users`
3. `/api/v1/system/devices`
4. `/api/v1/models/status`
5. 云端前端 Grafana 页面

### 7.3 runtime 对接

验证：

- `/api/v1/models/register`
- `/load_strategy`
- `/runtime_callback/edge`
- `/runtime_callback/cloud`

这一部分不应因为删除本地普通用户账号而受影响。

---

## 8. 最终状态总结

当前收口完成后的系统结构应为：

- 本地 `users` 表：只保留管理员
- 普通用户：不再是本地账号
- 普通用户身份：只存在于 OpenWebUI token、`edge_sessions`、`schedule_tasks`
- 云端前端页面：只保留管理员使用
- 设备与监控：完全围绕 `devices` 表运转

一句话总结：

**去除本地普通用户账号后，你的云端后端将变成“管理员本地管理系统 + 普通用户外部身份调度系统”的组合。**
