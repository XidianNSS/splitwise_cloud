# decode_server Docker 化与跨设备云端管理实施方案

## 1. 背景与目标

当前 `splitwise_backend` 对云端 `decode_server` 的管理能力，仍然建立在：

- backend 所在机器本地分配端口
- backend 所在机器本地 `subprocess.Popen(...)` 拉起 `decode_server`
- backend 所在机器本地保存 `pid`
- backend 所在机器本地做 stop / health check

因此，当前代码已经支持：

- backend 把任务路由到不同 `RuntimeSlot.control_url / grpc_target`
- 多个 cloud slot 在同一台机器上并发运行

但当前代码还**不支持**：

- backend 自动在**其他云端机器**上拉起 `decode_server`
- backend 统一管理多台云端 worker 上的 spawned cloud slot

本方案的目标是，把当前架构升级为：

1. `splitwise_backend` 只负责调度与 slot 生命周期
2. 每台云端推理机运行一个轻量 `decode node agent`
3. agent 负责在本机用 Docker 启动/停止 `decode_server`
4. backend 通过 agent 管理多台云端 worker
5. 一个 cloud slot 对应一个 Docker 化 `decode_server`
6. 后续可在不同云端机器上分配多个 decode slot，供多边端会话并发使用

---

## 2. 当前代码现状

### 2.1 当前已有能力

- `RuntimeSlot.control_url`
- `RuntimeSlot.grpc_target`
- `runtime_route`
- scheduler 中转 confirmation
- slot / binding / lifecycle / reconcile 基础能力

也就是说，**只要某个远端 `decode_server` 已经存在且可访问**，backend 已经能够把任务路由给它。

### 2.2 当前缺失能力

- `decode_server_process_manager.py` 仍然是**本机进程管理器**
- backend 无法远程拉起其他机器上的 `decode_server`
- spawned slot 的管理对象仍然是本机 `pid`，而不是跨机容器
- reconcile 仍然基于：
  - `process_pid`
  - `/health`
  - `/runtime_state`

因此，当前 Phase 2 的本质仍然是：

- **同机多 decode slot**
- 不是**跨机多云端 worker 管理**

---

## 3. 总体架构

### 3.1 角色划分

#### A. `splitwise_backend`

职责：

- 会话管理
- 调度策略管理
- `RuntimeSlot` / `RuntimeBinding` 生命周期管理
- 下发 `load_strategy`
- 注入 `runtime_route`
- 调用 node agent 启停容器
- 聚合 reconcile 状态

#### B. `decode node agent`

每台云端推理机部署一个。

职责：

- 本机 Docker 容器启动/停止
- 容器状态查询
- runtime 健康探测
- 返回 `control_url / grpc_target / container_id`

#### C. `decode_server` Docker 容器

职责：

- 运行 `ModelSplit_dev` 的云端 decode runtime
- 提供：
  - `/health`
  - `/load_strategy`
  - `/runtime_state`
  - `/unload_model`

---

## 4. 核心设计

### 4.1 一个 slot = 一个 decode 容器

第一版固定采用：

- 一个 `RuntimeSlot`
- 对应一个 Docker 容器
- 对应一个独立 `control_url + grpc_target`

优点：

- 生命周期简单
- 隔离性强
- 回收粒度清晰
- 日志与故障定位容易

第一版**不做**：

- 一个容器里跑多个 decode runtime
- 一个 active cloud instance 被多个会话复用

### 4.2 cloud 设备拆分

当前设备语义需要从单个 `cloud` 扩展为：

- `cloud_ctrl`：backend 所在机器
- `cloud_worker_1`
- `cloud_worker_2`
- `cloud_worker_3`

建议新增独立 worker 配置表，或扩展现有设备表，至少包含：

- `worker_id`
- `host_ip`
- `agent_base_url`
- `status`
- `gpu_inventory`
- `labels`
- `max_slots`

backend 是**控制中枢**，worker 是**真实承载 decode 容器的机器**。

### 4.3 `RuntimeSlot` 扩展字段

建议新增：

- `host_id`
- `host_ip`
- `agent_base_url`
- `container_id`
- `container_name`
- `gpu_device`
- `startup_source` (`base` / `spawned`)
- `runtime_env_name`

保留现有：

- `control_url`
- `grpc_target`
- `process_state`
- `model_state`
- `slot_state`

### 4.4 manager 抽象

把当前 `decode_server_process_manager.py` 抽象成统一接口：

```python
class DecodeSlotManager(Protocol):
    async def start_slot(...) -> SlotStartResult: ...
    async def stop_slot(...) -> bool: ...
    async def inspect_slot(...) -> SlotInspectResult: ...
    async def health_check(...) -> bool: ...
```

后续提供两个实现：

1. `LocalProcessDecodeManager`
   - 复用当前本机 `subprocess` 逻辑

2. `RemoteDockerDecodeManager`
   - 调远端 `decode node agent`

这样可以保证：

- 当前单机开发版不受影响
- 后续远端 Docker 化实现可以平滑接入

---

## 5. decode node agent 设计

### 5.1 部署方式

每台云端推理机部署一个常驻 agent，例如：

- `decode_node_agent`

它由 backend 调用，本机负责管理 Docker 容器。

### 5.2 Agent API

#### `POST /slots/start`

请求字段建议包含：

- `slot_id`
- `model_env`
- `http_port`
- `grpc_port`
- `gpu_device`
- `image`
- `container_name`
- `extra_env`
- `mounts`

响应：

- `slot_id`
- `container_id`
- `control_url`
- `grpc_target`
- `host_ip`
- `status`

#### `POST /slots/{slot_id}/stop`

停止指定 slot 容器。

#### `GET /slots/{slot_id}/status`

返回：

- `container_exists`
- `container_running`
- `container_id`
- `exit_code`
- `control_url`
- `grpc_target`

#### `GET /health`

agent 自身健康检查。

### 5.3 agent 内部行为

agent 接到 `start` 请求后：

1. 检查端口占用
2. 选择 GPU
3. 执行 `docker run`
4. 挂载模型/配置/Aloepri 工件目录
5. 注入运行时环境变量
6. 等待 decode runtime `/health` ready
7. 返回 `control_url / grpc_target / container_id`

---

## 6. decode_server Docker 镜像设计

### 6.1 镜像内容

建议构建单独镜像，例如：

- `modelsplit/decode-server:<tag>`

镜像内容包含：

- `ModelSplit_dev` 代码
- Python 依赖
- `grpc`
- `torch`
- `transformers`
- 运行入口：
  - `python -m app.services.decode_server.app`

### 6.2 运行环境变量

至少需要传入：

- `APP_ENV`
- `ENV_FILE`
- `MODEL_REGISTRY_PATH`
- `MODEL_DEVICE`
- `RUNTIME_PORT`
- `CLOUD_RUNTIME_PORT`
- `DECODE_GRPC_BIND`
- `DECODE_GRPC_TARGET`
- `SCHEDULE_BACKEND_URL`
- `RUNTIME_INTEGRITY_TOKEN`

### 6.3 挂载目录

建议挂载：

- 模型目录
- Aloepri 工件目录
- `model_registry.json`
- 分区配置目录
- 日志目录

### 6.4 GPU 使用

建议显式指定 GPU：

- Docker:
  - `--gpus '"device=1"'`
- 或 `NVIDIA_VISIBLE_DEVICES`

并将结果写回：

- `RuntimeSlot.gpu_device`

---

## 7. backend 调度逻辑改造

### 7.1 slot 分配

当新会话需要 cloud decode slot 时：

1. backend 选择可用 worker
2. 查该 worker 是否已有 free slot
3. 若没有，则调用 agent 启动新容器
4. 启动成功后写入 `RuntimeSlot`
5. 通过 `runtime_route` 把：
   - `control_url`
   - `grpc_target`
   注入到对应 runtime

### 7.2 第一版分配策略

第一版建议保持保守：

- **不做 active cloud instance 复用**
- **资源不足直接失败**
- **不做复杂等待队列**

这样最容易从当前代码平滑演进。

### 7.3 worker 选择策略

第一版建议：

1. 只选 `status=healthy` 的 worker
2. 优先 `active_slot_count` 最少的 worker
3. 次优先 GPU 空闲内存更多的 worker

后续再扩展：

- labels
- 区域
- 机型偏好
- 权重

---

## 8. 自动 reconcile 如何升级

### 8.1 当前 reconcile 的信息源

当前 backend reconcile 主要依赖：

- DB slot 状态
- `process_pid`
- `/health`
- `/runtime_state`

### 8.2 Docker 化后的 reconcile 信息源

Docker 化后建议改成四层：

1. backend DB slot 状态
2. node agent `/slots/{slot_id}/status`
3. runtime `/health`
4. runtime `/runtime_state`

### 8.3 收敛规则

#### spawned cloud slot

- agent 说容器不存在：
  - slot -> `free / empty / stopped`
- agent 说容器在，但 `/health` 不通：
  - 尝试 stop
  - 失败 -> `needs_reconcile`
- runtime_state 健康：
  - slot -> `bound / ready / running`

#### base cloud slot

- runtime 不可达：
  - 标 `process_state=failed`
  - 标 `slot_state=needs_reconcile`
  - 不自动 stop

### 8.4 设计目标

让 `needs_reconcile` 仍然只作为短暂过渡状态，而不是长期人工清理状态。

---

## 9. 分阶段实施

## Phase A：抽象 manager

目标：

- 保持当前行为不变
- 把本机 process manager 抽象成统一接口

交付：

- `DecodeSlotManager`
- `LocalProcessDecodeManager`

## Phase B：实现 node agent

目标：

- 每台云端 worker 能本机启动 Docker 化 decode 容器

交付：

- `decode_node_agent`
- `POST /slots/start`
- `POST /slots/stop`
- `GET /slots/status`
- `GET /health`

## Phase C：backend 接 remote docker manager

目标：

- spawned cloud slot 可分配到远端 worker

交付：

- `RemoteDockerDecodeManager`
- worker 选择逻辑
- `RuntimeSlot.host_id/container_id/gpu_device` 写库

## Phase D：reconcile 升级

目标：

- backend 可自动收敛远端容器状态

交付：

- agent + runtime 双层状态探测
- 启动时 reconcile
- 周期 reconcile

## Phase E：生产化增强

目标：

- 面向长期运行

交付：

- GPU 资源预算
- worker admission control
- 容器日志采集
- backend 重启恢复
- metrics / alert

---

## 10. 测试计划

### 10.1 单元测试

需要覆盖：

1. worker 选择逻辑
2. remote manager start/stop/status
3. slot -> runtime_route 注入
4. reconcile with agent status
5. container gone -> slot free
6. container alive + runtime ready -> slot healthy

### 10.2 集成测试

需要覆盖：

1. backend + mock agent
2. 两个会话分配到两个 worker
3. 关闭一个会话只回收其对应容器
4. backend 重启后 reconcile 恢复 slot

### 10.3 真实联调

真实多机联调建议最小拓扑：

1. 一台 backend
2. 两台 cloud worker
3. 两台 edge 设备
4. 两个会话同时推理

验证目标：

- 不串 slot
- 不串 container
- 不串 runtime_route
- 不互相影响

---

## 11. 第一版推荐落地选择

为了最快、最稳落地，建议第一版先采用：

- backend 仍使用当前 `splitwise_cloud_dev`
- 新增单独的 `decode_node_agent`
- 每个 slot 一个 Docker 容器
- 不做 active cloud 实例复用
- 不做复杂排队
- 不做 K8s
- 只支持 cloud decode 容器化

这样做的好处是：

1. 与当前 Phase 2 代码最兼容
2. 风险集中在“远端拉起与收敛”这一层
3. 便于逐步把当前本机多 slot 扩展为跨机多 worker

---

## 12. 当前结论

当前代码中，云端后端控制端对 spawned `decode_server` 的自动启动/停止能力，**仍然只能作用于 backend 所在机器本地**。

如果后续要实现：

- backend 独立部署
- 多台云端推理机承载 spawned cloud slot
- backend 统一管理跨设备 decode slot

则最推荐的路径是：

- **backend 调度中枢 + 每台 worker 一个 node agent + 一个 slot 一个 decode 容器**

这条方案与当前 slot/binding/runtime_route/reconcile 体系兼容性最好，也最适合从现有开发版平滑演进。
