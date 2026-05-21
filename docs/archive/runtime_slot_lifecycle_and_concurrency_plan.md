# 云边协同推理 Runtime Slot 生命周期与并发控制方案（最终版）

## 0. 方案定位

本方案用于解决当前 `ModelSplit + splitwise_cloud` 架构中的两个核心问题：

1. **模型资源长期驻留内存 / 显存**  
   当前 prefill / decode runtime 在加载模型后，除非收到新策略或进程退出，否则模型长期常驻，缺少按会话、请求和空闲状态释放资源的机制。

2. **一个云端设备无法同时服务多个边端设备**  
   当前 scheduler 基本按固定 edge/cloud runtime 下发 `/load_strategy`，cloud `decode_server` 内部也是单 `RuntimeLifecycle`。新任务可能覆盖旧任务正在使用的模型实例，无法按需为新的边端需求启动独立 cloud decode 服务，也无法在同一云端设备上动态管理多个模型实例。

本方案的核心思想是：

```text
splitwise_cloud 后端负责资源所有权：
  Session Lease + Runtime Slot + Runtime Binding + Slot Reaper

ModelSplit runtime 负责执行：
  load_strategy / 推理 / integrity / unload_model / runtime_state

短期用“后端 slot 状态接管 + 单 cloud slot + idle unload”完成 MVP；
中期用“按需启动多 decode_server 进程 cloud slot”实现多边端隔离服务；
长期再评估是否演进到“单进程多 slot manager”。
```

---

## 1. MVP 范围：第一版真正要上线的内容

为避免第一版被多 slot、多模型、动态进程管理等长期目标拖复杂，MVP 范围必须收紧。

### 1.1 MVP 要做

#### ModelSplit runtime 侧

```text
POST /unload_model
GET  /runtime_state
RuntimeLifecycle.unload_current_runtime()
active_request_count
last_used_at
draining 状态增强
unload 时释放 executor / adapter / KV cache / CUDA cache
```

#### splitwise_cloud 后端侧

```text
EdgeSession 增加：
  last_active_at
  lease_expires_at
  status: active / closing / expired / closed

新增：
  RuntimeSlot 表
  RuntimeBinding 表
  SessionLeaseService
  RuntimeSlotService
  SlotReaper

支持：
  session heartbeat
  session close
  单 cloud slot 状态接管
  idle unload
```

#### 路由与状态

```text
GET /runtime_state 由 runtime 返回当前真实状态；
后端以 runtime_state 作为 runtime 当前 active_request_count / ready / draining 的权威来源；
slot_reaper 根据后端 session 状态 + runtime_state 判断是否卸载。
```

### 1.2 MVP 不做

```text
不做多 cloud slot 池
不做单进程多 RuntimeLifecycle
不改 gRPC proto
不支持多个 active session 共享同一 cloud executor
不做 edge 侧多 slot 池
不做复杂 strategy_registry
不做 cloud 直连 edge 与 scheduler 中转双路径并存
```

### 1.3 MVP 目标

MVP 的目标只有两个：

```text
1. 模型不再无限期常驻内存 / 显存；
2. 后端开始用 RuntimeSlot / RuntimeBinding 接管当前单 slot 的资源状态，
   为后续按需启动多 cloud slot 和多边端隔离服务打基础。
```

---

## 2. 现有问题根因

### 2.1 模型长期驻留的根因

当前 runtime 只有：

```text
load_strategy
load_model
switch model
shutdown
```

但没有：

```text
这个模型实例现在被谁占用？
哪个会话还在使用？
什么时候可以释放？
无人使用后是否保温一段时间？
```

因此模型一旦加载成功，就会长期占用内存 / 显存。

### 2.2 云端不能多边端隔离服务的根因

当前 scheduler 主要管理的是：

```text
策略计算任务
edge/cloud 加载进度
runtime callback
```

但没有管理：

```text
云端当前有哪些 hot model slot
哪个 slot 已经 ready
哪个 slot 可分配给新会话
哪个 slot 正在被哪个 session 使用
哪个 slot 可以卸载或停止进程
```

同时，当前 ModelSplit `decode_server` 是单 `RuntimeLifecycle`：

```text
一个 decode_server 进程
  -> 一个当前 adapter
  -> 一个当前 executor
  -> 一份当前 PartitionConfig
```

所以，新的边端任务如果直接向同一个 cloud runtime 下发 `/load_strategy`，就可能覆盖旧任务使用中的模型实例。

---

## 3. 优化后的总体架构

建议引入三类核心资源对象：

```text
EdgeSession      用户 / 边端会话租约
RuntimeSlot      某台设备上的一个可调度 runtime 槽位
RuntimeBinding   某个会话绑定到哪对 edge/cloud slot
```

### 3.1 职责划分

```text
splitwise_cloud 后端：
  - 管理 session lease
  - 管理 runtime slot
  - 管理 runtime binding
  - 选择可用 cloud slot 或按需启动新的 cloud decode 服务
  - 决定是否下发 load_strategy
  - 触发空闲卸载
  - 维护 slot / binding 状态

ModelSplit runtime：
  - 接收 load_strategy
  - 加载模型并执行完整性校验
  - 提供推理服务
  - 暴露 runtime_state / integrity
  - 维护 active_request_count
  - 接收 unload_model 并安全释放资源
```

### 3.2 第一阶段不把 session lease 下沉到 runtime

第一阶段中，**会话租约只由 splitwise_cloud 后端维护**。

不建议在 runtime 侧新增完整的：

```text
/lease/acquire
/lease/release
/lease/heartbeat
```

原因：

```text
1. Session lease 是业务会话概念，更适合由后端统一管理；
2. Runtime 只需要知道自己是否 ready、是否 draining、是否有 active request；
3. 避免后端和 runtime 同时维护两套会话状态；
4. 降低 MVP 实现复杂度。
```

### 3.3 推荐短期部署形态

MVP 阶段默认只运行一个 cloud `decode_server`：

```text
cloud-slot-0:
  control_url = http://cloud:9113
  grpc_target = cloud:51163
```

进入多 cloud slot 阶段后，仍然不建议一开始预启动大量 decode 服务，而是采用**按需启动**：

```text
默认只有 cloud-slot-0；
当新的边端会话到来且已有 cloud slot 都被 active RuntimeBinding 占用时，
splitwise_cloud 才启动新的 decode_server 进程，并注册为新的 RuntimeSlot。
```

例如按需扩展后：

```text
cloud-slot-0:
  control_url = http://cloud:9113
  grpc_target = cloud:51163

cloud-slot-1:
  control_url = http://cloud:9123
  grpc_target = cloud:51173
```

回收时分两级：

```text
第一步：调用 /unload_model，释放模型显存；
第二步：如果该额外 decode_server 长时间空闲，再停止 decode_server 进程。
```

这样既避免模型长期占用显存，又避免频繁启停进程导致调度复杂。

优点：

```text
不需要立即修改 gRPC proto；
不需要大改 decode_server 的单 RuntimeLifecycle 结构；
不同模型实例之间进程隔离，释放显存和故障隔离更简单；
后端可以按需启动 / 停止 cloud decode 服务。
```

### 3.4 Docker 容器化管理 cloud decode_server（推荐实现）

按需启动多个 cloud `decode_server` 时，推荐将每个 `decode_server` 封装为 Docker 容器，并将“一个 cloud slot”落地为“一个 decode_server 容器”。这样 `splitwise_cloud` 不需要人工 SSH 到云端主机启动脚本，也不需要手动 kill 进程，而是通过受控的容器管理层完成启动、健康检查、注册、卸载和停止。

推荐关系：

```text
cloud-slot-0 -> modelsplit-decode-cloud-slot-0 容器
cloud-slot-1 -> modelsplit-decode-cloud-slot-1 容器
cloud-slot-2 -> modelsplit-decode-cloud-slot-2 容器
```

第一阶段默认只启动 `cloud-slot-0` 对应的容器。后续如果新的边端会话到来，且已有 cloud slot 都被 active `RuntimeBinding` 占用，则由 `splitwise_cloud` 触发启动新的 decode 容器，并注册为新的 `RuntimeSlot`。

#### 3.4.1 同机部署与跨机部署

如果 `splitwise_cloud` 与 cloud `decode_server` 在同一台云端主机上，可以在 `splitwise_cloud` 内部实现：

```text
DockerDecodeServerProcessManager
```

它通过本机 Docker Engine / Docker SDK 启动和停止 decode 容器。

如果 `splitwise_cloud` 与 cloud `decode_server` 不在同一台主机上，推荐在每台云端算力机上部署一个轻量的：

```text
CloudNodeAgent
```

此时调用链为：

```text
splitwise_cloud
  -> CloudNodeAgent 内部管理 API
  -> Docker Engine
  -> modelsplit-decode 容器
```

`splitwise_cloud` 不直接操作远端 Docker socket，也不通过 SSH 执行启动脚本，而是调用 CloudNodeAgent 的受控 API。

#### 3.4.2 容器镜像原则

`modelsplit-decode` 镜像只封装代码和运行依赖，不把模型权重打进镜像。模型目录通过只读 volume 挂载：

```text
宿主机：/data/modelsplit/aloepri
容器内：/models/aloepri
挂载方式：read-only
```

原因：

```text
1. 模型文件很大，不适合反复打入镜像；
2. 多个 decode 容器可以共享同一份只读模型目录；
3. 模型更新不需要重新构建镜像；
4. 可以降低镜像体积和发布成本。
```

容器应至少接收这些环境变量：

```text
RUNTIME_SLOT_ID=cloud-slot-1
PORT=9113
DECODE_GRPC_PORT=51163
MODEL_REGISTRY_PATH=/app/configs/model_registry.json
```

宿主机端口由 `splitwise_cloud` 或 CloudNodeAgent 分配，例如：

```text
cloud-slot-1:
  host_http_port = 9123
  host_grpc_port = 51173
  container_http_port = 9113
  container_grpc_port = 51163
```

#### 3.4.3 容器启动流程

按需启动新 cloud slot 时：

```text
1. RuntimeSlotService 判断需要新的 cloud slot；
2. 检查设备级显存预算和端口池；
3. 分配 slot_id、host_http_port、host_grpc_port、gpu_device；
4. 调用 DockerDecodeServerProcessManager 或 CloudNodeAgent.start_slot；
5. 启动 modelsplit-decode 容器；
6. 等待 GET /health 通过；
7. 写入 RuntimeSlot.control_url / grpc_target / container_name / process_state；
8. RuntimeSlot.state = free；
9. scheduler 向该 slot 下发 /load_strategy；
10. 该 slot ready 后创建 RuntimeBinding。
```

容器启动示例，仅作为实现参考：

```bash
docker run -d \\
  --name modelsplit-decode-cloud-slot-1 \\
  --gpus '"device=0"' \\
  -p 9123:9113 \\
  -p 51173:51163 \\
  -e RUNTIME_SLOT_ID=cloud-slot-1 \\
  -e PORT=9113 \\
  -e DECODE_GRPC_PORT=51163 \\
  -e MODEL_REGISTRY_PATH=/app/configs/model_registry.json \\
  -v /data/modelsplit/aloepri:/models/aloepri:ro \\
  -v /data/modelsplit/logs/cloud-slot-1:/app/logs \\
  modelsplit-decode:latest
```

实际代码中不应散落 shell 命令，而应封装在 `DockerDecodeServerProcessManager` 或 CloudNodeAgent 内。

#### 3.4.4 容器回收流程

回收分两级：

```text
第一级：调用 /unload_model
  释放模型显存、KV cache、executor、adapter；
  decode_server 容器仍然运行。

第二级：停止 decode 容器
  对按需启动的额外 slot，如果长时间没有 session、没有 active request、没有模型驻留，
  则 docker stop / docker rm，释放进程和端口资源。
```

建议判断条件：

```text
active_session_count == 0
active_request_count == 0
slot.state == free 或 ready_without_model
now > process_idle_deadline
slot_id != cloud-slot-0
```

默认 `cloud-slot-0` 可以作为基础服务常驻；按需启动的 `cloud-slot-1+` 可在长时间空闲后停止容器。

#### 3.4.5 CloudNodeAgent 建议接口

如果采用跨主机部署，CloudNodeAgent 建议提供：

```text
POST /agent/slots/start
POST /agent/slots/{slot_id}/stop
GET  /agent/slots/{slot_id}
GET  /agent/slots/{slot_id}/health
GET  /agent/slots
```

`start` 请求示例：

```json
{
  "slot_id": "cloud-slot-1",
  "image": "modelsplit-decode:latest",
  "gpu_device": "0",
  "host_http_port": 9123,
  "host_grpc_port": 51173,
  "model_volume": "/data/modelsplit/aloepri",
  "log_dir": "/data/modelsplit/logs/cloud-slot-1"
}
```

返回示例：

```json
{
  "slot_id": "cloud-slot-1",
  "container_name": "modelsplit-decode-cloud-slot-1",
  "control_url": "http://cloud-host:9123",
  "grpc_target": "cloud-host:51173",
  "process_state": "running"
}
```

---

---

## 4. 核心数据模型

### 4.1 EdgeSession

用于表达用户 / 边端会话的租约状态。

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

接口：

```text
POST /api/v1/session/init
POST /api/v1/session/heartbeat
POST /api/v1/session/close
```

说明：

```text
heartbeat 刷新租约，避免用户仍在对话时被回收；
close 显式释放当前会话绑定；
lease_expires_at 用于处理浏览器关闭、网络断开等没有显式 close 的情况。
```

---

### 4.2 RuntimeSlot

表示某台设备上的一个可调度模型运行槽位。

建议字段：

```text
slot_id
device_id
role: edge / cloud
slot_index
control_url
grpc_target
process_mode: local_process / docker / k8s
process_id
process_state: not_started / starting / running / stopping / stopped / failed
container_name
container_image
gpu_device
host_http_port
host_grpc_port
container_http_port
container_grpc_port
started_at
stopped_at
process_idle_deadline

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

说明：

```text
control_url 用于后端调用 /load_strategy、/unload_model、/runtime_state；
grpc_target 用于 edge prefill runtime 连接对应 cloud decode slot；
active_session_count 由后端维护，表示有多少会话绑定该 slot；
active_request_count 由 runtime 自身维护，并通过 /runtime_state 对外返回；
integrity_status / confirmation_status 用于避免分配到完整性状态存疑的 slot；
process_mode / container_name / host_http_port / host_grpc_port 用于 Docker 容器化管理 decode_server 进程。
```

---

### 4.3 RuntimeBinding

建议新增独立绑定表，不要只把 slot id 塞进 session 表。

字段：

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
支持一个 session 切换模型；
支持 session 重新调度；
记录一个 session 独占绑定到哪一个 cloud slot；
支持后续按照 session_id 路由推理请求。
```

---

## 5. ModelSplit runtime 侧改造

### 5.1 新增卸载接口

在 `prefill_server` 和 `decode_server` 中新增：

```text
POST /unload_model
GET  /runtime_state
```

`POST /unload_model` 请求示例：

```json
{
  "task_id": "xxx",
  "slot_id": "cloud-slot-0",
  "reason": "idle_timeout"
}
```

---

### 5.2 RuntimeLifecycle 增加安全卸载

建议新增：

```python
async def unload_current_runtime(self, reason: str) -> None:
    ...
```

基本流程：

```text
1. 设置 draining = true，不再接收新请求；
2. 等待 active_request_count == 0；
3. 清理 KV cache / session store；
4. 关闭 executor；
5. adapter.unload()；
6. torch.cuda.empty_cache()；
7. is_ready = false；
8. 清空 current_task_id / current_model_type / digest / integrity 状态；
9. draining = false。
```

---

### 5.3 active_request_count 的权威来源

第一阶段中，`active_request_count` 必须由 **ModelSplit runtime 自身维护**，后端不应自行估算，coordinator 也不应推测。

建议实现方式：

```text
RuntimeLifecycle.use_executor() 进入时：active_request_count += 1
RuntimeLifecycle.use_executor() 退出时：active_request_count -= 1
```

`GET /runtime_state` 返回当前值：

```json
{
  "ready": true,
  "draining": false,
  "active_request_count": 0,
  "last_used_at": "..."
}
```

后端的 `slot_reaper` 只能基于 `/runtime_state` 返回的 `active_request_count` 决定是否允许卸载：

```text
active_request_count == 0
```

这样可以避免：

```text
scheduler 自己估算正在执行的请求数；
coordinator 根据请求发出情况推测请求是否结束；
后端和 runtime 状态不一致导致误卸载。
```

---

### 5.4 runtime_state 输出

`GET /runtime_state` 建议返回：

```json
{
  "node_role": "cloud",
  "ready": true,
  "draining": false,
  "task_id": "xxx",
  "model_type": "Llama-3.2-3B",
  "server_param_digest": "sha256:...",
  "partition_digest": "sha256:...",
  "integrity_verified": true,
  "confirmation_passed": true,
  "active_request_count": 0,
  "last_used_at": "..."
}
```

---

## 6. 调度与 cloud slot 分配策略

### 6.1 新会话调度流程

```text
1. 前端调用 /session/init
2. 后端创建 EdgeSession
3. 前端调用 /schedule/trigger(model_type)
4. 后端刷新 session lease
5. 后端生成当前会话所需的 PartitionConfig
6. 后端计算 partition_digest
7. 后端选择 edge slot
8. 后端选择空闲 cloud slot，必要时按需启动新的 decode_server
9. 生成 RuntimeRoute
10. 下发 edge /load_strategy
11. 下发 cloud slot /load_strategy
12. edge/cloud ready 后创建 RuntimeBinding
13. ScheduleTask 进入 ready_for_chat
14. 后续推理请求按 RuntimeBinding 路由
15. heartbeat 刷新 lease
16. close 或 lease timeout 后释放 binding
17. slot 空闲超时后 reaper 调用 /unload_model
```

---

### 6.2 cloud slot 分配原则：不复用 active 模型实例

本方案不再保留“多个边端会话 active 复用同一个 cloud 模型实例”的能力。原因是不同边端设备资源不同，动态生成出的切分策略通常不同，复用命中率低；同时 active 复用会引入 session / KV cache 隔离、并发调度和故障影响范围等额外复杂度。

第一阶段和中期阶段均采用更保守的策略：

```text
一个 active RuntimeBinding 独占一个 cloud slot。
只要 cloud slot 已经绑定 active session，就不再分配给新的边端会话。
```

新的边端会话到来时，后端按以下顺序处理：

```text
1. 查找 state == free 的 cloud slot；
2. 如果存在 free slot，则使用该 slot 下发 /load_strategy；
3. 如果不存在 free slot，但设备资源允许，则按需启动新的 decode_server，并注册为新的 cloud slot；
4. 如果资源不足，则尝试回收 idle slot；
5. 仍无资源时，进入等待队列或返回资源不足。
```

因此，`model_type`、`server_param_digest`、`partition_digest` 不再作为“复用 active cloud slot”的条件，而只作为当前绑定 slot 的状态记录、完整性确认和调试字段。

### 6.3 不保留云端模型实例 active 复用能力

本方案取消原先的“同模型 + 同 partition_digest 复用 cloud hot instance”设计。

取消原因：

```text
1. 不同边端设备资源不同，切分策略完全相同的概率较低；
2. active 复用需要严格保证 session_id / KV cache / decode state 隔离；
3. 当前阶段的核心目标是资源释放和独立调度，而不是最大化 hot model 复用；
4. 一个 active cloud slot 独占一个 RuntimeBinding，故障边界和排查路径更清晰。
```

如果未来确实需要 active 模型实例复用，应作为新的专项能力重新设计，至少需要补充：

```text
session_id 强制路由；
KV cache / decode state 严格隔离；
请求级并发控制；
策略路由与 coverage 校验；
异常 session 的局部清理能力。
```

## 7. 边端 slot 策略

第一阶段和第二阶段中，边端策略必须保守：

```text
每台边端设备默认只允许 1 个 active edge slot。
```

边端第一阶段目标是：

```text
会话租约
idle unload
cloud binding
接收 runtime_route.cloud_decode_grpc_target
```

暂不做：

```text
边端多 slot 池
边端同时加载多个模型
边端多会话多模型复杂路由
```

原因：

```text
当前真实价值密度最高的是云端资源回收、按需启动和独立绑定；
边端通常服务本地前端，资源更弱，不适合第一阶段引入多模型常驻；
先保持一台边端设备一个 active edge slot，可以显著降低路由和资源管理复杂度。
```

---

## 8. 按需 cloud slot 路由改造

这是按需启动多个 cloud decode 服务后，边端能否连接正确 cloud slot 的关键。

### 8.1 LoadStrategyRequest 增加 runtime_route

建议新增：

```python
class RuntimeRoute(BaseModel):
    edge_slot_id: str | None = None
    cloud_slot_id: str | None = None
    cloud_control_url: str | None = None
    cloud_decode_grpc_target: str | None = None
    scheduler_integrity_callback_url: str | None = None
```

并扩展：

```python
class LoadStrategyRequest(BaseModel):
    task_id: str
    model_type: str
    decision: StrategyDecision
    runtime_route: RuntimeRoute | None = None
```

第一阶段不建议把 `edge_integrity_base_url` 作为默认链路字段，因为完整性确认默认走 scheduler 中转。

---

### 8.2 edge prefill 不能只读全局 DECODE_GRPC_TARGET

当前单 cloud slot 模式下，edge 可以从环境变量读取：

```text
DECODE_GRPC_TARGET
```

按需启动多 cloud slot 后必须改为：

```text
优先使用 LoadStrategyRequest.runtime_route.cloud_decode_grpc_target；
没有则 fallback 到 DECODE_GRPC_TARGET。
```

否则 edge 永远只能连接一个固定 cloud decode runtime。

---

## 9. 完整性 confirmation 拓扑

### 9.1 第一阶段默认只实现 scheduler 中转

第一阶段完整性 confirmation 默认只实现一条主链路：

```text
cloud slot -> scheduler runtime confirmation endpoint
scheduler 更新 task / slot 状态
edge 从 scheduler task 状态获知确认结果，或由 scheduler 通知 edge
```

第一阶段不同时支持：

```text
cloud 直连 edge
scheduler 中转
```

原因：

```text
双路径会增加实现复杂度；
双路径会增加排查复杂度；
真实部署中 edge 可能在 NAT 后，cloud 未必能直连 edge；
scheduler 中转更符合后端统一资源状态管理的目标。
```

因此，第一阶段推荐新增：

```text
POST /api/v1/runtime/confirmation/cloud
POST /api/v1/runtime/slots/start
POST /api/v1/runtime/slots/{slot_id}/stop

# 如果采用 CloudNodeAgent，以下为云端节点内部管理 API：
POST /agent/slots/start
POST /agent/slots/{slot_id}/stop
GET  /agent/slots/{slot_id}
GET  /agent/slots/{slot_id}/health
GET  /agent/slots
```

cloud slot 加载完成后，将以下信息回调 scheduler：

```json
{
  "task_id": "xxx",
  "cloud_slot_id": "cloud-slot-0",
  "model_type": "Llama-3.2-3B",
  "server_param_digest": "sha256:...",
  "partition_digest": "sha256:...",
  "ready": true
}
```

scheduler 收到后：

```text
1. 比对 cloud confirmation 与 task / slot 期望状态；
2. 更新 RuntimeSlot.integrity_status / confirmation_status；
3. 若 edge 也 ready，则建立 RuntimeBinding；
4. 将 ScheduleTask 标记为 ready_for_chat。
```

### 9.2 cloud 直连 edge 仅作为后续内网优化

如果后续确认部署环境中 cloud 可以直接访问 edge HTTP 控制面，可以增加：

```text
cloud -> edge /integrity/cloud_confirmation
```

但这不进入 MVP。

---

## 10. 并发控制

### 10.1 slot 级请求并发

每个 cloud slot 配置：

```text
max_concurrent_requests
active_request_count
```

runtime 侧建议增加 semaphore：

```python
self._request_semaphore = asyncio.Semaphore(max_concurrent_requests)
```

在 `use_executor()` 中获取 semaphore，避免同一个模型实例被过多请求打爆。

---

### 10.2 设备级显存预算

后端维护设备级资源状态：

```text
total_gpu_mem_mb
reserved_gpu_mem_mb
available_gpu_mem_mb
```

每个 slot 记录：

```text
estimated_model_mem_mb
reserved_gpu_mem_mb
```

加载新模型前判断：

```text
sum(ready/loading slot reserved_mem) + new_model_estimated_mem <= device_budget
```

不足时：

```text
优先驱逐 idle slot；
仍不足则排队。
```

---

### 10.3 请求路由必须有 session_id / task_id

如果同一 edge 上未来可能同时存在多个 session 或多个 cloud binding，推理请求必须能定位到对应绑定。

建议：

```text
OpenAI API 请求带 session_id；
或 Header: X-Session-ID；
或 coordinator 根据当前 active binding 查询 task_id。
```

短期若一台 edge 只允许一个 active session，可以暂时简化；长期必须补 `session_id -> RuntimeBinding` 查询。

---

## 11. 空闲回收策略

### 11.1 释放触发

支持三类释放：

```text
显式 close：
  用户主动结束会话

租约超时：
  heartbeat 中断，session 过期

slot idle timeout：
  active_session_count == 0
  active_request_count == 0
  now - last_used_at > idle_timeout
```

---

### 11.2 slot_reaper 流程

```text
1. 扫描过期 EdgeSession；
2. 将过期 session 标记 expired / closed；
3. 释放 RuntimeBinding；
4. 更新 RuntimeSlot.active_session_count；
5. 找到满足 idle 条件的 ready slot；
6. 调用 /runtime_state 确认 active_request_count == 0；
7. slot.state = unloading；
8. 调用 runtime /unload_model；
9. 成功后 slot.state = free；
10. 失败则 slot.state = failed / needs_reconcile。
```

---

### 11.3 不建议单次问答后立即卸载

推荐：

```text
lease_timeout_seconds: 10 ~ 30 分钟
idle_timeout_seconds: 2 ~ 10 分钟
```

这样能在连续聊天体验和显存释放之间取得平衡。

---

## 12. 后端服务改造清单

### 12.1 数据层

新增或扩展：

```text
EdgeSession:
  last_active_at
  lease_expires_at
  bound_edge_slot_id
  bound_cloud_slot_id

ScheduleTask:
  runtime_binding_id
  spawned_cloud_slot / allocated_cloud_slot_id
  edge_slot_id
  cloud_slot_id

RuntimeSlot:
  新表

RuntimeBinding:
  新表
```

---

### 12.2 API 层

新增：

```text
POST /api/v1/session/heartbeat
POST /api/v1/session/close
GET  /api/v1/runtime/slots
POST /api/v1/runtime/slots/{slot_id}/unload
POST /api/v1/runtime/confirmation/cloud
POST /api/v1/runtime/slots/start
POST /api/v1/runtime/slots/{slot_id}/stop

# 如果采用 CloudNodeAgent，以下为云端节点内部管理 API：
POST /agent/slots/start
POST /agent/slots/{slot_id}/stop
GET  /agent/slots/{slot_id}
GET  /agent/slots/{slot_id}/health
GET  /agent/slots
```

---

### 12.3 服务层

新增：

```text
session_lease_service.py
runtime_slot_service.py
runtime_binding_service.py
slot_reaper.py
decode_server_process_manager.py
docker_decode_server_process_manager.py
cloud_node_agent_client.py
```

改造：

```text
schedule_orchestrator.py:
  trigger 时先查是否有空闲 slot；没有则通过 RuntimeSlotService 按需申请新的 cloud slot
  选定 slot 后进入策略计算 / load_strategy
  根据选定 slot 生成 runtime_route

runtime_slot_service.py:
  分配 slot_id / 端口 / GPU；
  调用 DockerDecodeServerProcessManager 或 CloudNodeAgentClient 启动容器；
  维护 RuntimeSlot.process_state / control_url / grpc_target；

decode_server_process_manager.py:
  定义 start_slot / stop_slot / inspect_slot / health_check 抽象接口；
  Docker 实现负责 docker run / docker stop / docker rm；

schedule_queue.py:
  区分策略计算队列与 slot 等待队列

runtime_callback / runtime_confirmation:
  更新 slot 状态、task 状态、binding 状态

schedule_recovery.py:
  重启后 reconcile stale task / stale session / stale slot
```

---

### 12.4 Docker 容器化改造清单

后续代码开发时，Docker 化管理 cloud `decode_server` 至少需要补齐以下内容。

#### splitwise_cloud 侧

```text
1. 新增 DockerDecodeServerProcessManager：
   - start_slot(slot_id, host_http_port, host_grpc_port, gpu_device)
   - stop_slot(slot_id)
   - inspect_slot(slot_id)
   - health_check(slot_id)

2. 新增或预留 CloudNodeAgentClient：
   - splitwise_cloud 与 decode_server 不同机时使用；
   - 不直接 SSH，不直接暴露远端 Docker socket。

3. RuntimeSlotService 负责：
   - 分配 slot_id；
   - 分配端口；
   - 检查显存预算；
   - 启动 decode 容器；
   - 健康检查通过后写入 control_url / grpc_target；
   - 停止长时间空闲的额外 decode 容器。

4. SlotReaper 负责两级回收：
   - 先调用 /unload_model 释放显存；
   - 再在 process_idle_timeout 后停止按需启动的 decode 容器。
```

#### ModelSplit decode 镜像侧

```text
1. 增加 Dockerfile.decode；
2. 镜像只包含代码和 Python 依赖，不包含模型权重；
3. 模型目录通过只读 volume 挂载；
4. 日志目录单独挂载；
5. 容器内 HTTP / gRPC 端口固定，宿主机端口由后端分配；
6. 容器启动后必须提供 /health、/runtime_state、/unload_model。
```

#### 状态约束

```text
RuntimeSlot.process_state 表示容器进程状态；
RuntimeSlot.state 表示模型运行状态；
二者不能混淆。

例如：
  process_state = running, state = free
    表示容器还在，但没有模型驻留；

  process_state = running, state = ready
    表示容器运行中，模型已加载并可推理；

  process_state = stopped, state = free
    表示额外 slot 已停止容器。
```

---

## 13. ModelSplit 改造清单

### 13.1 runtime lifecycle

新增：

```text
unload_current_runtime()
active_request_count
last_used_at
draining 状态增强
request semaphore
```

---

### 13.2 HTTP 服务

prefill_server / decode_server 新增：

```text
POST /unload_model
GET  /runtime_state
```

---

### 13.3 协议

扩展：

```text
LoadStrategyRequest.runtime_route
```

---

### 13.4 prefill executor

修改 cloud gRPC target 获取方式：

```text
优先 runtime_route.cloud_decode_grpc_target；
fallback 到 DECODE_GRPC_TARGET。
```

---

### 13.5 integrity confirmation

第一阶段使用：

```text
cloud -> scheduler confirmation
```

不再默认使用：

```text
cloud -> edge confirmation
```

---

## 14. 恢复与观测

### 14.1 恢复策略

系统重启后：

```text
1. stale loading task -> failed；
2. expired session -> closed；
3. 无 binding 的 slot -> free 或 needs_reconcile；
4. 若 runtime 仍在运行，通过 /runtime_state 回填 slot 状态；
5. 与数据库状态不一致时，以 runtime_state + 后端策略进行 reconcile。
```

---

### 14.2 监控指标

建议增加：

```text
active_sessions_total
runtime_slots_total
runtime_slots_ready
runtime_slots_loading
runtime_slots_unloading
runtime_slot_start_total
runtime_slot_stop_total
runtime_slot_evict_total
runtime_idle_unload_total
runtime_slot_wait_queue_size
runtime_slot_active_requests
runtime_slot_active_sessions
```

---

## 15. 分阶段实施路线

### Phase 0：MVP，卸载能力与单 slot 状态接管

目标：先解决模型长期驻留，并让后端开始接管 runtime slot 状态。

范围：

```text
POST /unload_model
GET /runtime_state
RuntimeLifecycle.unload_current_runtime()
active_request_count
last_used_at
draining
EdgeSession.last_active_at
EdgeSession.lease_expires_at
RuntimeSlot 表
RuntimeBinding 表
SessionLeaseService
RuntimeSlotService
SlotReaper
单 cloud slot 状态接管
idle unload
scheduler 中转 cloud confirmation
```

明确不做：

```text
不做多 cloud slot
不改 gRPC proto
不做单进程多模型
不做 edge 多 slot
不做 active cloud 模型实例复用
不同时支持 cloud 直连 edge confirmation
```

---

### Phase 1：按需启动新的 cloud decode 服务

目标：当已有 cloud slot 正在服务其他会话时，为新的边端需求按需启动独立的 cloud `decode_server`。

范围：

```text
默认只保留 cloud-slot-0 常驻；
新的边端会话到来时，如果 cloud-slot-0 已被 active RuntimeBinding 占用，则尝试启动 cloud-slot-1；
每个新增 cloud slot 对应一个独立 decode_server Docker 容器；
新增 slot 启动后完成 /health 检查，再下发 /load_strategy；
会话结束后先 /unload_model，长时间空闲后再 stop / remove decode_server 容器。
```

实现建议：

```text
同机部署：splitwise_cloud 通过 DockerDecodeServerProcessManager 操作本机 Docker；
跨机部署：splitwise_cloud 调用 CloudNodeAgent，由 Agent 操作所在云端主机的 Docker；
不要通过人工 SSH、手动 python 启动脚本或 kill -9 管理 decode_server。
```

明确不做：

```text
不复用 active cloud 模型实例；
不因为 model_type / partition_digest 相同而把新会话绑定到已有 active slot；
不改 gRPC proto；
不做单进程多模型。
```

边端策略：

```text
每台边端设备仍默认 1 个 active edge slot。
```

### Phase 2：按需多进程 cloud slot 池增强

目标：一个云端设备可以按需启动并管理多个 decode_server 进程。

实现：

```text
cloud-slot-0 -> decode_server 进程 A
cloud-slot-1 -> decode_server 进程 B
cloud-slot-2 -> decode_server 进程 C
```

scheduler 根据 slot endpoint 下发 `/load_strategy`。

新增能力：

```text
端口分配；
Docker 容器启动 / 停止；
/health 检查；
RuntimeSlot 注册；
空闲容器回收；
显存预算控制；
CloudNodeAgent 跨机管理。
```

### Phase 3：暂不规划模型实例 active 复用

本方案不规划多个 active session 共享同一个 cloud 模型实例。

如果未来确实需要该能力，应重新设计为单独阶段，而不是在当前按需启动方案中顺手加入。

### Phase 4：单进程多 slot manager

目标：长期优化。

需要：

```text
RuntimeSlotManager
多个 RuntimeLifecycle
gRPC proto 增加 slot_id
请求按 slot_id 路由
```

---

## 16. 最终建议

最终建议采用：

```text
1. 后端统一管理 Session Lease、RuntimeSlot、RuntimeBinding；
2. ModelSplit runtime 只负责加载、推理、完整性状态、active_request_count 和卸载；
3. 第一阶段明确只做 MVP：/unload_model、/runtime_state、单 cloud slot 状态接管、idle unload；
4. active_request_count 由 runtime 通过 use_executor() 维护，并通过 /runtime_state 返回；
5. active cloud slot 不复用；新的边端需求必须绑定空闲 slot 或按需启动新的 cloud decode 服务；
6. 第一阶段默认只做 scheduler 中转 confirmation；
7. 边端第一阶段仍保持每台设备一个 active edge slot；
8. 多模型并发短期用按需启动的多 decode_server Docker 容器 slot 池实现；
9. LoadStrategyRequest 必须携带 runtime_route，不能再依赖全局 cloud gRPC target；
10. decode_server 动态启停应通过 DockerDecodeServerProcessManager / CloudNodeAgent 完成，不采用人工 SSH 或手动 kill；
11. 长期可评估单进程多 slot manager；active 模型实例复用不进入当前规划。
```

一句话总结：

```text
第一阶段先把“谁在用、何时释放、当前 runtime 是否空闲”管起来；
第二阶段再让新的边端需求按需获得独立 cloud decode 服务；
之后再推进动态进程管理、多模型并发和单进程多 slot manager。
```
