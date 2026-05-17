# 设备优先方案简版实施计划

本文档是对当前“设备优先”改造方案的简化版说明，重点只回答三件事：

1. 具体要改哪些内容
2. 实施阶段怎么划分
3. 改完后的整体流程是什么样

本方案的目标是：

- 不再维护普通用户本地账号体系
- 不再手工做 `openwebui_user_id -> 本地用户` 绑定
- 普通用户直接使用 OpenWebUI token
- 边端设备由后端根据请求来源 IP 自动识别
- 当前阶段固定使用本机云端设备 `10.144.144.2`（设备 `cloud`）
- 云端前端页面只保留管理员使用

---

## 0. 与近期已融入代码的关系

当前代码里已经吸收了两类内部增强，这些增强**不会改变本方案方向**，但后续实施时应直接沿用，不要回退到旧写法：

- 数据库层已经增强：
  - `database.py` 中已有 `timeout=30`
  - `pool_pre_ping=True`
  - `expire_on_commit=False`
  - `session_scope()`
- 网络状态采集已经模块化：
  - `schedule.py` 已接入 `services/network_probe.py`
  - `services/prometheus_metrics.py`
  - 不应再把网络探测和 Prometheus 查询大段搬回 `schedule.py`
- 管理员初始化已经做了安全兼容：
  - `config.py` 已支持 `ADMIN_USERNAME`
  - `ADMIN_PASSWORD`
  - `models.py` 已优先读取环境变量初始化管理员

因此，这份方案后续要改的是：

- 普通用户认证与会话链路
- 边端设备识别与调度入口
- 普通用户页面职责

而不是推翻这些已经融入的底层增强。

---

## 1. 最终想要的系统形态

改造完成后，系统分成两条清晰的路径：

### 1.1 普通用户路径

普通用户只经过边端前端，不再进入云端前端页面。

普通用户侧流程只依赖：

- OpenWebUI token
- 后端按来源 IP 识别边端设备
- 后端默认使用本机云端设备 `10.144.144.2`
- 后端创建临时 session 维持一次完整使用

### 1.2 管理员路径

管理员继续通过云端前端页面登录。

管理员继续保留：

- 设备资产管理
- Grafana / Prometheus 监控查看
- runtime 状态查看

---

## 2. 具体要改哪些内容

## 2.1 认证方式修改

### 当前问题

- 普通用户需要先走 `/api/v1/auth/exchange`
- 后端还依赖本地普通用户账号
- 新 OpenWebUI 用户接入需要手工绑定

### 修改目标

- 普通用户不再调用 `/api/v1/auth/exchange`
- 普通用户直接带 OpenWebUI token 调后端
- 后端直接验签和解析 OpenWebUI token

### 保留不变

- 管理员继续使用本地 `/api/v1/login`
- 管理员继续使用内部 JWT

---

## 2.2 边端设备识别方式修改

### 当前问题

- 设备和用户绑定在一起
- 用户换 OpenWebUI 账号后还要改绑定

### 修改目标

- 后端不再根据本地普通用户推断边端设备
- 后端改为根据请求来源 IP 匹配 `devices` 表
- 前端不需要知道 `edge_device_id`
- 前端不需要发送设备 ID

### 前提

这个方案默认成立的前提是：

- 真正发起请求的那台设备，就是边端设备本机
- 没有代理、NAT、统一网关改写来源 IP

如果以后这个前提不成立，再升级为“显式设备 ID”方案。

---

## 2.3 普通用户会话方式修改

### 当前问题

- 前端的一次使用包括初始化、选模型、触发调度、看进度、看策略
- 如果没有统一上下文，后端每次都要重新推断，流程容易散

### 修改目标

- 在初始化时创建一次临时 session
- 用这次 session 串起整条使用流程

### 建议的临时会话内容

- `session_id`
- `openwebui_user_id`
- `edge_device_id`
- `edge_ip`
- `cloud_device_id`，固定为 `cloud`
- `model_type`，初始化时可为空
- `created_at`
- `expires_at`

---

## 2.4 云端设备来源方式修改

### 修改目标

- 云端设备不再由本地普通用户账号权限决定
- 当前阶段直接固定为本机云端设备：
  - `device_id = cloud`
  - `ip = 10.144.144.2`
- 前端暂时不承担云端设备选择职责

### 第一阶段建议

- `POST /api/v1/session/init` 初始化时直接把 `cloud_device_id` 写入 session
- `POST /api/v1/schedule/trigger` 第一阶段只需要提交：
  - `model_type`
- 后端调度时默认使用 `cloud` 这台云端设备

---

## 2.5 云端前端页面角色修改

### 修改目标

- 云端前端页面不再给普通用户使用
- 普通用户不再通过云端前端页面看设备指标
- 普通用户不再通过云端前端页面看 Grafana
- 云端前端页面只保留管理员使用

### 这意味着

普通用户相关页面/入口需要移除或禁用：

- 设备监控入口
- “我的设备”监控视图
- 普通用户设备指标展示逻辑

---

## 2.6 数据模型调整

### 继续保留

- `devices`
- `model_nodes`
- `schedule_tasks`
- `users`
  - 仅用于管理员

### 第一阶段新增

- `edge_sessions`
  - 用于维持一次完整普通用户使用会话

### 第二阶段可新增

- `openwebui_principals`
  - 轻量 OpenWebUI 用户档案表
  - 用于审计、禁用、统计

### 第一阶段不再依赖

- 本地普通用户账号
- `openwebui_user_id -> User`
- `users.allowed_devices`

---

## 3. 实施阶段划分

## 3.1 第一阶段：最小可用闭环

这一阶段的目标是：

**先让“普通用户直接用 OpenWebUI token + 按来源 IP 识别边端设备 + 默认使用固定云端设备”整条链路跑通。**

### 第一阶段建议再拆成 4 个小阶段

#### 阶段 1A：认证入口能力

完成：

1. 普通用户不再走 `/api/v1/auth/exchange`
2. 普通用户侧接口支持 OpenWebUI token
3. 新增 `POST /api/v1/session/init` 的最小骨架
4. 先跑通：
   - token 验签
   - `openwebui_user_id` 解析
   - 基础返回结构

完成后联调：

- 只测 `POST /api/v1/session/init`
- 确认 OpenWebUI token 能被正确识别

#### 阶段 1B：边端设备识别与临时 session

完成：

1. 初始化时按来源 IP 匹配边端设备
2. 初始化时创建 `edge_sessions`
3. 初始化响应返回：
   - `session_id`
   - `openwebui_user_id`
   - 当前识别出的边端设备信息
   - 固定云端设备信息（`cloud / 10.144.144.2`）

完成后联调：

- 测 `POST /api/v1/session/init`
- 确认能返回 `session_id`
- 确认能识别边端设备
- 确认错误 IP 会正确报错

#### 阶段 1C：调度主链路切换

完成：

1. 第一阶段中，`POST /api/v1/schedule/trigger` 统一带：
   - `Session-Id`
2. `/api/v1/schedule/trigger` 第一阶段只传：
   - `model_type`
3. 后端在 trigger 受理时把：
   - `model_type`
   写入当前 session
4. 后端调度主链路改为优先从 session 取：
   - `edge_device_id`
   - 固定 `cloud_device_id`

完成后联调：

- 跑完整最小链路：
  - `session/init`
  - `trigger`
  - 策略回调
  - runtime 下发
  - 任务完成

#### 阶段 1D：旧链路收口与页面清理

完成：

1. 云端前端页面去掉普通用户入口，只保留管理员可见
2. 旧普通用户链路改为兼容保留但不再作为主流程
3. 文档与 mock 同步到新流程

完成后联调：

- 验证管理员页面不受影响
- 验证普通用户新链路仍然可用
- 验证旧链路如保留时不会干扰新链路

### 第一阶段不做的内容

- 不强制先上 `openwebui_principals`
- 不强制做默认云端设备偏好
- 不强制新增独立的 `GET /api/v1/devices/cloud-options`

### 为什么要这样拆

1. 每次改动的上下文更小
2. 每一小阶段完成后都能立即联调
3. 出错时更容易定位在哪一层
4. 不需要一次性同时修改认证、session、调度、前端清理全部内容

---

## 3.2 第二阶段：体验与管理增强

这一阶段的目标是：

**在第一阶段已经能稳定工作的基础上，再补体验和管理能力。**

### 第二阶段可以补的内容

1. `openwebui_principals`
2. 多云端设备选择能力
3. OpenWebUI 用户禁用/审计能力
4. 收口 `users.allowed_devices` 的历史用途
5. 如果以后需要多云端，再把云端设备列表拆成独立接口：
   - `GET /api/v1/devices/cloud-options`

---

## 4. 改造后的整体流程

## 4.1 普通用户完整流程

1. 用户在边端 OpenWebUI 登录
2. 边端前端拿到 `openwebui_token`
3. 前端调用：
   - `POST /api/v1/session/init`
   - `Authorization: Bearer <openwebui_token>`
4. 云端后端执行：
   - 验签 OpenWebUI token
   - 解析 `openwebui_user_id`
   - 根据请求来源 IP 匹配 `devices` 表，识别当前边端设备
   - 创建 `edge_sessions`
   - 返回：
     - `session_id`
     - `openwebui_user_id`
     - 边端设备信息
     - 固定云端设备信息
5. 前端保存 `session_id`
6. 用户选择：
   - `model_type`
7. 前端调用：
   - `POST /api/v1/schedule/trigger`
   - `Authorization: Bearer <openwebui_token>`
   - `Session-Id: <session_id>`
8. 后端把本次选择写入 session
9. 后端继续完成：
   - 指标采集
   - 26 维编码
   - 算法调用
   - 策略下发
   - runtime 加载进度聚合
10. 前端继续查看：
   - 任务状态
   - 切分策略
   - 加载进度

---

## 4.2 管理员完整流程

1. 管理员登录云端前端页面
2. 管理员使用本地登录与内部 JWT
3. 管理员继续查看：
   - 设备列表
   - Grafana / Prometheus 监控
   - runtime 状态
   - 设备管理信息

普通用户不再进入这一条路径。

---

## 5. 这个方案解决了什么问题

它直接解决了你当前最麻烦的几个问题：

1. 新 OpenWebUI 用户接入时，不再需要创建本地普通用户账号
2. 不再需要手工做 `openwebui_user_id -> 本地用户` 绑定
3. OpenWebUI 用户换号后，不再需要重绑设备
4. 后端完整能力不丢：
   - 设备表仍在
   - Prometheus 采集仍在
   - runtime 定位仍在
   - 切分策略下发仍在
5. 云端前端页面职责变清楚：
   - 管理员看监控
   - 普通用户只走边端前端

---

## 6. 一句话总结

这套方案的本质是：

**普通用户不再依赖本地账号，而是直接用 OpenWebUI token；边端设备由后端按来源 IP 自动识别；当前阶段云端固定使用本机 `10.144.144.2`；整次使用通过临时 session 串起来。**
