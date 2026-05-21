# splitwise_cloud 开发指导文档：Runtime Slot 生命周期与并发控制

## 0. 文档目的

本文档面向 `splitwise_cloud` 后端控制器开发者，说明在当前 Runtime Slot 生命周期与并发控制方案中，**后端控制器需要实现什么、改造哪些模块、如何与 ModelSplit 推理服务对接**。

本方案解决两个问题：

1. **模型长期驻留内存 / 显存**：当前 prefill / decode runtime 加载模型后长期常驻，缺少按会话、请求和空闲状态释放模型资源的机制。
2. **一个云端设备无法服务多个边端设备**：当前 scheduler 主要向固定 edge/cloud runtime 下发 `/load_strategy`，无法复用云端 hot model，也无法管理多个云端 slot。

后端控制器的核心职责是：

```text
管理谁在用模型、用哪个 runtime slot、何时可复用、何时该释放。
```

ModelSplit runtime 的职责是：

```text
按后端指令加载模型、执行完整性校验、提供推理、暴露 runtime_state、执行卸载。
```

---

## 1. splitwise_cloud 在本方案中的职责边界

### 1.1 splitwise_cloud 需要负责

```text
1. 管理 EdgeSession 租约；
2. 管理 RuntimeSlot 状态；
3. 管理 RuntimeBinding 绑定关系；
4. 决定是否复用已有 cloud slot；
5. 决定是否下发新的 /load_strategy；
6. 维护 slot active_session_count；
7. 从 ModelSplit /runtime_state 获取 active_request_count；
8. 定期执行 idle unload；
9. 通过 scheduler 中转 cloud confirmation；
10. 在任务 ready 前确认 edge/cloud slot 状态一致。
```

### 1.2 splitwise_cloud 第一阶段不负责

```text
1. 不在后端估算 active_request_count；
2. 不直接管理 ModelSplit 内部 executor / adapter；
3. 不在第一阶段做多个 cloud slot；
4. 不改 gRPC proto；
5. 不做同模型不同 partition_digest 的共享；
6. 不维护 runtime 内部 session lease；
7. 不同时实现 cloud 直连 edge 与 scheduler 中转两条 confirmation 链路。
```

---

## 2. MVP 范围：splitwise_cloud 第一版必须完成什么

MVP 必须收紧范围，只做以下内容。

### 2.1 后端数据模型

MVP 需要改造或新增：

```text
EdgeSession：增加 last_active_at / lease_expires_at / status
RuntimeSlot：新增表
RuntimeBinding：新增表
ScheduleTask：增加 runtime_binding_id / edge_slot_id / cloud_slot_id / reuse_existing_slot
```

### 2.2 后端服务

MVP 需要新增：

```text
SessionLeaseService
RuntimeSlotService
RuntimeBindingService
SlotReaper
RuntimeConfirmationService
```

### 2.3 后端接口

MVP 需要新增：

```text
POST /api/v1/session/heartbeat
POST /api/v1/session/close
GET  /api/v1/runtime/slots
POST /api/v1/runtime/slots/{slot_id}/unload
POST /api/v1/runtime/confirmation/cloud
```

### 2.4 与 ModelSplit runtime 的交互

MVP 依赖 ModelSplit runtime 提供：

```text
GET  /runtime_state
POST /unload_model
POST /load_strategy
GET  /integrity
runtime callback
```

### 2.5 MVP 不做

```text
不做多 cloud slot 池；
不做单进程多 RuntimeLifecycle；
不改 gRPC proto；
不做同模型不同 partition_digest 共享；
不做 edge 多 slot；
不实现 cloud 直连 edge confirmation。
```

MVP 目标只有两个：

```text
1. 模型不再无限期常驻；
2. 后端开始用 RuntimeSlot / RuntimeBinding 接管当前单 slot 的资源状态。
```

---

## 3. 数据模型设计

### 3.1 EdgeSession

用于表达用户 / 边端会话租约。

建议字段：

```text
session_id
user_id
edge_device_id
status: active / closing / expired / closed
model_type
current_task_id
last_active_at
lease_expires_at
bound_edge_slot_id
bound_cloud_slot_id
created_at
updated_at
```

字段说明：

| 字段 | 说明 |
|---|---|
| `last_active_at` | 最近一次会话活跃时间，由 heartbeat / trigger / 推理请求刷新 |
| `lease_expires_at` | 会话租约过期时间 |
| `status` | 会话状态，用于 reaper 判断是否释放 binding |
| `bound_edge_slot_id` | 当前会话绑定的边端 slot |
| `bound_cloud_slot_id` | 当前会话绑定的云端 slot |

状态流转：

```text
active -> closing -> closed
active -> expired -> closed
```

---

### 3.2 RuntimeSlot

`RuntimeSlot` 是后端管理 runtime 资源的核心对象，表示某台设备上的一个可调度模型运行槽位。

建议字段：

```text
slot_id
device_id
role: edge / cloud
slot_index
control_url
grpc_target

state: free / loading / ready / draining / unloading / failed / needs_reconcile

model_type
model_artifact_digest
server_param_digest
partition_digest
strategy_id
task_id

active_session_count
active_request_count
max_concurrent_requests

reserved_gpu_mem_mb
estimated_model_mem_mb

integrity_status: unknown / healthy / unhealthy
confirmation_status: none / pending / passed / failed

last_used_at
idle_deadline
created_at
updated_at
```

字段说明：

| 字段 | 说明 |
|---|---|
| `control_url` | 后端调用该 slot 的 `/load_strategy`、`/unload_model`、`/runtime_state` 的 HTTP 地址 |
| `grpc_target` | edge prefill 连接 cloud decode slot 的 gRPC 地址 |
| `active_session_count` | 后端维护，表示多少会话正在绑定该 slot |
| `active_request_count` | runtime 权威维护，通过 `/runtime_state` 获取 |
| `integrity_status` | 完整性校验是否健康 |
| `confirmation_status` | cloud confirmation 是否已通过 |

注意：

```text
active_request_count 只能以 runtime_state 返回值为准，splitwise_cloud 不应自行估算。
```

---

### 3.3 RuntimeBinding

`RuntimeBinding` 表示某个会话绑定到了哪对 edge/cloud slot。

建议字段：

```text
binding_id
session_id
task_id
model_type
edge_slot_id
cloud_slot_id
partition_digest
status: active / released
created_at
released_at
```

作用：

```text
1. 支持一个 session 切换模型；
2. 支持 session 重新调度；
3. 支持多个 session 共享同一个 cloud slot；
4. 支持后续根据 session_id 路由推理请求。
```

---

### 3.4 ScheduleTask 需要扩展

建议增加：

```text
runtime_binding_id
edge_slot_id
cloud_slot_id
reuse_existing_slot
runtime_route
partition_digest
server_param_digest
```

其中：

```text
reuse_existing_slot = true
```

表示本次任务没有重新加载 cloud runtime，而是复用了已有 ready slot。

---

## 4. API 改造要求

### 4.1 session heartbeat

```text
POST /api/v1/session/heartbeat
```

请求示例：

```json
{
  "session_id": "sess-xxx"
}
```

后端行为：

```text
1. 校验 session 存在且未关闭；
2. 更新 last_active_at；
3. 重新计算 lease_expires_at；
4. 若有 active RuntimeBinding，可同步更新对应 slot.last_used_at。
```

---

### 4.2 session close

```text
POST /api/v1/session/close
```

请求示例：

```json
{
  "session_id": "sess-xxx",
  "reason": "user_close"
}
```

后端行为：

```text
1. 将 EdgeSession 标记为 closing / closed；
2. 释放 RuntimeBinding；
3. 减少相关 RuntimeSlot.active_session_count；
4. 更新 slot.last_used_at；
5. 由 SlotReaper 后续判断是否卸载模型。
```

不要在 close 时立即卸载模型。连续聊天场景下，模型可保温一段时间。

---

### 4.3 runtime slots 查询

```text
GET /api/v1/runtime/slots
```

用途：

```text
调试和管理当前 slot 状态。
```

建议返回：

```json
[
  {
    "slot_id": "cloud-slot-0",
    "device_id": "cloud-1",
    "role": "cloud",
    "state": "ready",
    "model_type": "Llama-3.2-3B",
    "partition_digest": "sha256:...",
    "active_session_count": 1,
    "active_request_count": 0,
    "integrity_status": "healthy",
    "confirmation_status": "passed"
  }
]
```

---

### 4.4 手动卸载 slot

```text
POST /api/v1/runtime/slots/{slot_id}/unload
```

后端行为：

```text
1. 查询 slot；
2. 调用 slot.control_url + /runtime_state；
3. 若 active_request_count > 0，拒绝卸载或进入 draining；
4. 调用 slot.control_url + /unload_model；
5. 成功后更新 slot.state = free；
6. 失败后更新 slot.state = failed 或 needs_reconcile。
```

---

### 4.5 cloud confirmation 中转接口

第一阶段默认只实现 scheduler 中转。

```text
POST /api/v1/runtime/confirmation/cloud
```

cloud slot 加载完成后回调 scheduler。

请求示例：

```json
{
  "task_id": "task-xxx",
  "cloud_slot_id": "cloud-slot-0",
  "model_type": "Llama-3.2-3B",
  "server_param_digest": "sha256:...",
  "partition_digest": "sha256:...",
  "ready": true,
  "timestamp": "2026-05-15T10:00:00Z",
  "nonce": "..."
}
```

后端行为：

```text
1. 校验 task_id；
2. 校验 cloud_slot_id；
3. 校验 model_type；
4. 比对 server_param_digest；
5. 比对 partition_digest；
6. 更新 RuntimeSlot.integrity_status；
7. 更新 RuntimeSlot.confirmation_status；
8. 如果 edge 也 ready，则创建 RuntimeBinding；
9. 将 ScheduleTask 标记为 ready_for_chat。
```

---

## 5. 服务层实现要求

### 5.1 SessionLeaseService

职责：

```text
1. 创建 EdgeSession；
2. 刷新 heartbeat；
3. 关闭 session；
4. 扫描过期 session；
5. 将过期 session 标记为 expired / closed。
```

核心方法建议：

```python
create_session(...)
heartbeat(session_id: str)
close_session(session_id: str, reason: str)
expire_stale_sessions(now)
```

---

### 5.2 RuntimeSlotService

职责：

```text
1. 查询可复用 cloud slot；
2. 分配 free slot；
3. 更新 slot 状态；
4. 更新 active_session_count；
5. 根据 runtime_state 回填 active_request_count；
6. 标记 slot failed / needs_reconcile。
```

核心方法建议：

```python
find_reusable_cloud_slot(model_type, server_param_digest, partition_digest)
allocate_cloud_slot(model_type, partition_digest)
allocate_edge_slot(edge_device_id)
mark_loading(slot_id, task_id)
mark_ready(slot_id, digests)
mark_unloading(slot_id)
mark_free(slot_id)
mark_failed(slot_id, reason)
refresh_runtime_state(slot_id)
```

cloud slot 复用条件必须包含：

```text
model_type 相同
server_param_digest 相同
partition_digest 相同
slot.state == ready
slot.integrity_status == healthy
slot.confirmation_status == passed
slot.active_request_count < slot.max_concurrent_requests
slot.state 不属于 failed / needs_reconcile / draining / unloading
```

---

### 5.3 RuntimeBindingService

职责：

```text
1. 创建 session -> edge/cloud slot 的绑定；
2. 释放 binding；
3. 根据 session_id 查询当前绑定；
4. 在绑定创建 / 释放时维护 active_session_count。
```

核心方法建议：

```python
create_binding(session_id, task_id, edge_slot_id, cloud_slot_id, partition_digest)
release_binding(binding_id, reason)
get_active_binding(session_id)
```

---

### 5.4 SlotReaper

职责：

```text
1. 扫描过期 session；
2. 释放过期 session 对应 binding；
3. 找到满足 idle 条件的 slot；
4. 调用 runtime /runtime_state；
5. active_request_count == 0 时调用 /unload_model；
6. 更新 slot 状态。
```

卸载条件：

```text
active_session_count == 0
active_request_count == 0
now - last_used_at > idle_timeout
slot.state == ready
```

注意：

```text
active_request_count 必须来自 runtime_state。
```

---

### 5.5 RuntimeConfirmationService

职责：

```text
1. 接收 cloud confirmation；
2. 校验 task / slot / digest；
3. 更新 slot integrity / confirmation 状态；
4. 推进 ScheduleTask 状态；
5. 在 edge/cloud 都 ready 后建立 RuntimeBinding。
```

---

## 6. schedule_orchestrator 改造要求

### 6.1 trigger 时先查 slot，再决定是否加载

当前不要直接创建策略任务后无条件下发 `/load_strategy`。

推荐流程：

```text
1. 校验 session；
2. 刷新 session lease；
3. 生成或复用 PartitionConfig；
4. 计算 partition_digest；
5. 查询是否存在可复用 cloud slot；
6. 若存在可复用 cloud slot：
   - 绑定现有 cloud slot；
   - 根据 edge 状态决定是否加载 edge；
   - cloud 侧不再下发 /load_strategy；
7. 若不存在可复用 cloud slot：
   - 分配 free cloud slot；
   - 创建加载任务；
   - 下发 cloud slot /load_strategy；
8. 下发 edge /load_strategy 时带 runtime_route；
9. 等待 edge/cloud ready 和 confirmation；
10. 创建 RuntimeBinding；
11. task -> ready_for_chat。
```

---

### 6.2 RuntimeRoute 生成

`LoadStrategyRequest` 需要包含：

```python
class RuntimeRoute(BaseModel):
    edge_slot_id: str | None = None
    cloud_slot_id: str | None = None
    cloud_control_url: str | None = None
    cloud_decode_grpc_target: str | None = None
    scheduler_integrity_callback_url: str | None = None
```

对 edge 下发 `/load_strategy` 时必须包含：

```text
cloud_slot_id
cloud_decode_grpc_target
```

对 cloud slot 下发 `/load_strategy` 时必须包含：

```text
cloud_slot_id
scheduler_integrity_callback_url
```

第一阶段不下发 `edge_integrity_base_url`，因为 confirmation 默认走 scheduler 中转。

---

### 6.3 复用已有 cloud slot 时的处理

如果复用 cloud slot：

```text
1. 不向 cloud slot 下发 /load_strategy；
2. 不改变 cloud slot 当前模型；
3. 只增加 cloud slot active_session_count；
4. 仍需确保 edge 侧加载或绑定到该 cloud grpc_target；
5. 创建新的 RuntimeBinding。
```

---

### 6.4 分配新 cloud slot 时的处理

如果没有可复用 cloud slot：

```text
1. 查找 free cloud slot；
2. 若无 free slot，尝试驱逐 idle slot；
3. 若仍无资源，进入 slot 等待队列；
4. 分配 slot 后标记 loading；
5. 向该 slot.control_url 下发 /load_strategy；
6. 等待 runtime callback / cloud confirmation；
7. 成功后标记 ready；
8. 失败后标记 failed / needs_reconcile。
```

---

## 7. Runtime callback 与 confirmation 处理

### 7.1 runtime callback

后端收到 edge/cloud runtime callback 时，更新：

```text
ScheduleTask.edge_status / cloud_status
RuntimeSlot.state
RuntimeSlot.task_id
RuntimeSlot.model_type
RuntimeSlot.partition_digest
```

当状态为 ready 时，不应只看 callback ready，还要确认：

```text
slot.integrity_status == healthy
slot.confirmation_status == passed
```

cloud slot 尤其需要等待 confirmation。

---

### 7.2 cloud confirmation

cloud confirmation 是 cloud slot 对本次加载结果的摘要确认。

后端应校验：

```text
task_id 与当前 loading task 一致；
cloud_slot_id 与任务分配 slot 一致；
model_type 一致；
server_param_digest 与期望一致；
partition_digest 与任务 partition_digest 一致。
```

校验通过后：

```text
RuntimeSlot.integrity_status = healthy
RuntimeSlot.confirmation_status = passed
```

校验失败：

```text
RuntimeSlot.integrity_status = unhealthy
RuntimeSlot.confirmation_status = failed
ScheduleTask = failed
```

---

## 8. slot_reaper 详细逻辑

建议周期：

```text
每 30s 或 60s 执行一次
```

流程：

```text
1. expire_stale_sessions()
2. release expired session bindings
3. 对 active_session_count == 0 的 ready slot：
   a. 调用 /runtime_state
   b. 更新 active_request_count
   c. 如果 active_request_count > 0，跳过
   d. 如果 now - last_used_at <= idle_timeout，跳过
   e. 标记 slot.state = unloading
   f. 调用 /unload_model
   g. 成功 -> slot.state = free，清空模型字段
   h. 失败 -> slot.state = needs_reconcile
```

卸载成功后清空字段：

```text
model_type
model_artifact_digest
server_param_digest
partition_digest
strategy_id
task_id
integrity_status
confirmation_status
idle_deadline
```

---

## 9. 状态机建议

### 9.1 RuntimeSlot 状态机

```text
free
  -> loading
  -> ready
  -> unloading
  -> free

loading
  -> failed

ready
  -> draining
  -> unloading
  -> free

ready
  -> needs_reconcile

failed
  -> free 或 loading

needs_reconcile
  -> free 或 loading
```

### 9.2 EdgeSession 状态机

```text
active
  -> closing
  -> closed

active
  -> expired
  -> closed
```

### 9.3 RuntimeBinding 状态机

```text
active
  -> released
```

---

## 10. 恢复与 reconcile

后端重启后，需要执行恢复逻辑。

### 10.1 task 恢复

```text
stale loading task -> failed
stale waiting task -> queued 或 failed
```

### 10.2 session 恢复

```text
lease_expires_at < now 的 session -> expired / closed
释放对应 binding
```

### 10.3 slot 恢复

对非 free slot：

```text
1. 调用 slot.control_url /runtime_state；
2. 若 runtime 不可达：slot.state = needs_reconcile；
3. 若 runtime ready 且 digest 与 DB 一致：slot.state = ready；
4. 若 runtime ready 但 digest 不一致：slot.state = needs_reconcile；
5. 若 runtime not ready：slot.state = free 或 needs_reconcile。
```

---

## 11. 配置项建议

建议新增配置：

```text
SESSION_LEASE_TIMEOUT_SECONDS=1800
RUNTIME_SLOT_IDLE_TIMEOUT_SECONDS=300
RUNTIME_SLOT_REAPER_INTERVAL_SECONDS=60
RUNTIME_STATE_TIMEOUT_MS=3000
RUNTIME_UNLOAD_TIMEOUT_MS=30000
RUNTIME_SLOT_MAX_CONCURRENT_REQUESTS=1
RUNTIME_CONFIRMATION_TOKEN=...
```

说明：

```text
RUNTIME_SLOT_MAX_CONCURRENT_REQUESTS 第一阶段建议为 1，后续再按显存和吞吐测试放大。
```

---

## 12. 测试清单

### 12.1 session lease

```text
session/init 创建 active session
heartbeat 刷新 last_active_at / lease_expires_at
close 释放 binding
lease 超时后 reaper 自动释放 binding
```

### 12.2 runtime slot

```text
创建 cloud-slot-0 / edge-slot-0
trigger 后 slot 从 free -> loading -> ready
runtime_state 能回填 active_request_count
runtime_state 不可达时 slot -> needs_reconcile
```

### 12.3 unload

```text
active_session_count > 0 时不卸载
active_request_count > 0 时不卸载
idle_timeout 未到时不卸载
idle_timeout 到期后调用 /unload_model
unload 成功后 slot -> free
unload 失败后 slot -> needs_reconcile
```

### 12.4 confirmation

```text
cloud confirmation task_id 不一致 -> failed
cloud confirmation slot_id 不一致 -> failed
server_param_digest 不一致 -> failed
partition_digest 不一致 -> failed
confirmation 通过 -> integrity_status=healthy / confirmation_status=passed
```

### 12.5 slot 复用

```text
同 model_type + 同 server_param_digest + 同 partition_digest + healthy + passed -> 可复用
partition_digest 不同 -> 不复用
slot failed / needs_reconcile -> 不复用
slot active_request_count >= max_concurrent_requests -> 不复用
```

---

## 13. splitwise_cloud 开发者实施顺序

推荐按以下顺序开发：

### Step 1：数据模型和迁移

```text
扩展 EdgeSession
扩展 ScheduleTask
新增 RuntimeSlot
新增 RuntimeBinding
```

### Step 2：基础服务

```text
SessionLeaseService
RuntimeSlotService
RuntimeBindingService
```

### Step 3：API

```text
heartbeat
close
runtime slots 查询
runtime slot unload
cloud confirmation
```

### Step 4：orchestrator 改造

```text
trigger 时先查 RuntimeSlot
生成 runtime_route
下发 load_strategy 到选定 slot
复用 slot 时不再重复下发 cloud load_strategy
```

### Step 5：reaper

```text
扫描 expired session
释放 binding
调用 runtime_state
触发 unload_model
```

### Step 6：recovery

```text
服务启动时 reconcile task / session / slot
```

### Step 7：同模型同策略 cloud slot 复用

```text
实现 find_reusable_cloud_slot()
实现 active_session_count 维护
实现 max_concurrent_requests 限制
```

---

## 14. 与 ModelSplit 团队的接口约定

splitwise_cloud 需要等待或推动 ModelSplit 提供以下能力：

```text
1. GET /runtime_state
2. POST /unload_model
3. LoadStrategyRequest.runtime_route
4. edge prefill 使用 runtime_route.cloud_decode_grpc_target
5. cloud runtime 向 scheduler 回调 confirmation
6. runtime_state 返回 active_request_count
```

其中，`active_request_count` 必须由 ModelSplit runtime 维护。

---

## 15. 最终结论

对 splitwise_cloud 开发者来说，本方案第一阶段的重点不是“马上实现多模型并发”，而是：

```text
1. 把会话租约管起来；
2. 把 runtime slot 状态管起来；
3. 把 session 到 edge/cloud slot 的 binding 管起来；
4. 基于 runtime_state 判断是否真的空闲；
5. 在 idle 后调用 /unload_model 释放模型；
6. 让后端具备后续复用 cloud hot slot 的基础。
```

一句话：

```text
splitwise_cloud 要从“策略下发器”升级为“模型运行资源管理器”。
```
