# 模型推理服务对接说明

目标：让边端模型推理服务 / 云端模型推理服务开发者尽快明确当前如何与云端后端对接。

## 当前对接规则

- 不再使用 `register / unregister`
- 云端后端不会再动态发现模型推理服务节点
- 当前统一采用“固定控制端口 + 进度回调 + 完整性确认回调”方案

## 当前主流程

1. 边端前端向云端后端发起调度
2. 云端后端完成资源检查并拿到切分策略
3. 云端后端分别向边端模型推理服务、云端模型推理服务的固定控制端口发送启动请求
4. 模型推理服务内部启动对应模型并加载切分策略
5. 模型推理服务持续回调加载进度
6. 若启用 Aloepri 完整性链路，云端 runtime 还会向 scheduler 回调完整性确认
7. 云端后端聚合进度并更新任务状态

---

## 1. 控制面入口

当前控制路径：

```text
POST /load_strategy
```

此外，runtime 还应提供：

- `GET /health`
- `GET /runtime_state`
- `POST /unload_model`

### 固定控制端口（基础正式口径）

当前正式基础口径：

- edge real: `9001`
- cloud base decode real: `9002`

### 动态 cloud slot 说明

当前 backend 已支持自动托管多个 cloud decode slot。

因此：
- `cloud-slot-0` 可以是基础 cloud decode
- `cloud-slot-1 / cloud-slot-2 / ...` 可能使用新的动态端口
- **前端和 runtime 开发者不应假设所有云端 decode 都固定在 `9002`**

真正应以 backend 下发的 `runtime_route.cloud_control_url` 和 `runtime_route.cloud_decode_grpc_target` 为准。

---

## 2. 云端后端下发给模型推理服务的请求体

当前请求体是 `LoadStrategyRequest` 语义，核心字段如下：

```json
{
  "task_id": "624a1db9-eaa0-4257-9a38-7d6469357048",
  "model_type": "Llama-3.2-3b",
  "decision": {
    "layer_partitions": [
      {
        "layer_id": 0,
        "head_assignments": [0, 1, 0, 1],
        "ffn_assignment": 0,
        "edge_head_count": 2,
        "cloud_head_count": 2
      }
    ]
  },
  "runtime_route": {
    "cloud_slot_id": "cloud-slot-1",
    "cloud_control_url": "http://10.144.144.2:9011/load_strategy",
    "cloud_decode_grpc_target": "10.144.144.2:51101",
    "scheduler_integrity_callback_url": "http://10.144.144.2:8010/api/v1/schedule/runtime/confirmation/cloud"
  }
}
```

字段含义：

- `task_id`
  - 后续进度回调用它关联任务

- `model_type`
  - 本次要启动的模型

- `decision`
  - 切分策略

- `runtime_route`
  - 当前 runtime 所需的云端路由信息
  - 主要用于：
    - edge prefill 找到正确的 cloud decode gRPC 目标
    - cloud runtime 找到正确的 scheduler 完整性确认回调入口

### `runtime_route` 字段说明

- `cloud_slot_id`
  - 当前任务分配到的 cloud slot 标识

- `cloud_control_url`
  - 当前 cloud runtime 的控制面地址

- `cloud_decode_grpc_target`
  - 当前任务真正应连接的 cloud decode gRPC 目标
  - 当前 runtime 应优先使用它，而不是只依赖全局 `DECODE_GRPC_TARGET`

- `scheduler_integrity_callback_url`
  - 当前 Aloepri 完整性确认回调的 scheduler 地址
  - cloud runtime 应优先使用它，而不是直连 edge runtime

---

## 3. 模型推理服务控制端口应返回什么

建议快速返回：

```json
{
  "status": "accepted",
  "message": "model service startup accepted"
}
```

这里的 `accepted` 只表示“已接收启动请求”，不表示模型已经加载完成。

如果模型推理服务明确无法受理，也可以返回非 `accepted` 状态，云端后端会把任务标记为失败。

---

## 4. 模型推理服务进度回调

边端模型推理服务回调地址：

```http
POST /api/v1/schedule/runtime_callback/edge
```

云端模型推理服务回调地址：

```http
POST /api/v1/schedule/runtime_callback/cloud
```

### 回调体格式

```json
{
  "task_id": "624a1db9-eaa0-4257-9a38-7d6469357048",
  "status": "loading",
  "progress": 50,
  "message": "waiting cloud confirmation",
  "stage": "integrity"
}
```

当前回调字段：

- `task_id`
- `status`
- `progress`
- `message`
- `stage`（可选）
- `node_role`（可选；当前推荐使用带角色的回调地址，因此通常不必传）

### `stage` 当前语义

- `runtime_load`
  - 表示模型/执行器加载阶段
- `integrity`
  - 表示 Aloepri 完整性检验阶段
- 为空时
  - backend 会按兼容逻辑视为 `runtime_load`

### 失败时回调

```json
{
  "task_id": "624a1db9-eaa0-4257-9a38-7d6469357048",
  "status": "failed",
  "progress": 0,
  "message": "云端模型实例启动失败",
  "stage": "runtime_load"
}
```

### 完成时回调

```json
{
  "task_id": "624a1db9-eaa0-4257-9a38-7d6469357048",
  "status": "ready",
  "progress": 100,
  "message": "runtime is ready"
}
```

说明：
- 最终 `ready` 仍表示整个 runtime 已经就绪
- 包括模型加载完成、完整性确认完成（如果启用 Aloepri）

---

## 5. Aloepri 完整性确认回调

若启用 Aloepri 完整性链路，cloud runtime 不再直接把确认发送到 edge runtime，而是优先通过 scheduler 中转：

```http
POST /api/v1/schedule/runtime/confirmation/cloud
```

### 请求体格式

```json
{
  "task_id": "624a1db9-eaa0-4257-9a38-7d6469357048",
  "cloud_slot_id": "cloud-slot-1",
  "model_type": "Llama-3.2-3b",
  "server_param_digest": "...",
  "partition_digest": "...",
  "timestamp": 1710000000,
  "nonce": "abc123"
}
```

scheduler 会再把这条确认中转到 edge runtime。

---

## 6. 职责边界

模型推理服务负责：

- 接收 `/load_strategy`
- 根据 `model_type` 启动正确模型
- 加载切分策略
- 使用 `runtime_route` 中的云端路由信息
- 回调加载进度
- 若启用 Aloepri，则完成完整性检验并触发确认回调
- 对外维持类 OpenAI 推理入口

云端后端负责：

- 任务受理
- 资源检查
- 调算法模块
- 向模型推理服务下发策略
- 聚合进度和维护任务状态
- 中转 Aloepri 完整性确认
- 管理云端 slot / binding / lifecycle

---

## 7. 当前最关键的对接结论

如果只记一件事，请记这句：

**runtime 不应再只依赖固定 `DECODE_GRPC_TARGET`，而应优先使用 backend 下发的 `runtime_route.cloud_decode_grpc_target`；若启用 Aloepri，cloud runtime 的确认回调也应优先使用 `runtime_route.scheduler_integrity_callback_url`。**
