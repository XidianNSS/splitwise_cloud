# ModelSplit Runtime 对接说明

本文面向边端 prefill runtime 和云端 decode runtime 开发者。接口以当前
`splitwise_cloud/backend` 与 `ModelSplit` 代码为准。

## 1. 调用关系

一次调度的控制链路如下：

1. backend 计算切分策略并分配 edge/cloud runtime slot。
2. backend 同时向两端的 `POST /load_strategy` 下发任务。
3. runtime 异步加载模型，通过带角色的 callback 上报进度。
4. backend 通过 `/runtime_state` 对账，并在释放资源时调用 `/unload_model`。
5. 启用 Aloepri 时，cloud runtime 经 backend 把完整性确认转发给 edge runtime。

当前 runtime 同时支持 Llama/Qwen 生成链路和 `BERT-Base-Uncased` 编码链路。
BERT 的调度控制面、slot 和 callback 契约相同，但数据面使用 BERT RPC，且加载前
必须完成 artifact fingerprint 与 partition digest 的跨节点确认。

不使用 runtime 注册或注销接口。开发测试可启用 mock 配置，但正式服务与开发测试使用同一套协议。

## 2. 地址和端口

runtime 必须提供以下 HTTP 接口：

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/load_strategy` | 接收模型和切分策略 |
| `GET` | `/health` | 进程健康检查 |
| `GET` | `/integrity` | 查询 artifact、partition 和确认状态 |
| `GET` | `/runtime_state` | 返回真实生命周期状态 |
| `POST` | `/unload_model` | 卸载当前模型并清除任务身份 |

edge prefill 的正式控制端口是 `9001`，backend 根据 `/session/init` 识别出的边端 IP 访问它。

云端 decode 由 backend 托管。按当前 `.env.prod`：

- HTTP 端口从 `9020` 开始：`9020 + slot_index`
- gRPC 端口从 `51200` 开始：`51200 + slot_index`
- slot 上限为 `2`

调用方不得自行拼接 cloud decode 地址，必须使用 backend 下发的 `runtime_route`。

## 3. 加载策略

```http
POST /load_strategy
Content-Type: application/json
```

当前请求结构：

```json
{
  "task_id": "624a1db9-eaa0-4257-9a38-7d6469357048",
  "model_type": "Llama-3.2-3B-Instruct",
  "decision": {
    "layer_partitions": [
      {
        "layer_id": 0,
        "head_assignments": [0, 0, 1, 1],
        "ffn_assignment": 1
      }
    ]
  },
  "runtime_route": {
    "cloud_slot_id": "cloud-slot-0",
    "cloud_control_url": "http://10.144.144.4:9020/load_strategy",
    "cloud_decode_grpc_target": "10.144.144.4:51200",
    "scheduler_integrity_callback_url": "http://10.144.144.4:8010/api/v1/schedule/runtime/confirmation/cloud"
  }
}
```

字段约定：

- `task_id`：本次加载的唯一任务 ID；后续回调必须原样携带。
- `model_type`：传给 ModelSplit runtime 的规范模型名。
- `decision.layer_partitions`：逐层切分结果；head/FFN 中 `0` 表示 edge，`1` 表示 cloud。当前 ModelSplit `PartitionConfig` 不接受 FFN 值 `2`。
- BERT 固定发送 12 层、每层 12 个值为 `1` 的 `head_assignments`，
  `ffn_assignment=1`；这是 runtime 协议载体，不是算法计算结果。
- `runtime_route.cloud_slot_id`：本次分配的 cloud slot。
- `runtime_route.cloud_decode_grpc_target`：edge prefill/coordinator 应连接的 decode gRPC 地址。
- `runtime_route.scheduler_integrity_callback_url`：cloud runtime 的完整性确认回调地址。

当前 backend 总会下发 `runtime_route`。edge runtime 应优先使用其中的
`cloud_decode_grpc_target`，不能只依赖进程级 `DECODE_GRPC_TARGET`。

成功接收后应尽快返回，不要等待模型加载完成：

```json
{
  "status": "accepted",
  "message": "runtime loading started"
}
```

backend 接受 `status=accepted` 或 `status=ok`。HTTP 非 2xx、无法解析 JSON，或其他
`status` 都会被视为下发失败。backend 的单次下发超时为 5 秒。

runtime 正在加载其他任务时应返回 `409`；请求或模型非法可返回 `400`；运行环境不可用可返回 `503`。

## 4. 加载进度回调

| runtime | 回调地址 |
|---|---|
| edge | `POST /api/v1/schedule/runtime_callback/edge` |
| cloud | `POST /api/v1/schedule/runtime_callback/cloud` |

地址基于 `BACKEND_BASE_URL`。请求体：

```json
{
  "task_id": "624a1db9-eaa0-4257-9a38-7d6469357048",
  "status": "loading",
  "progress": 60,
  "message": "model weights loading",
  "stage": "runtime_load"
}
```

- `status`：`loading`、`ready`、`completed` 或 `failed`。
- `progress`：整数，backend 会限制在 `0..100`。
- `stage`：`runtime_load` 或 `integrity`；省略时按 `runtime_load` 处理。
- `node_role`：使用上述带角色路径时无需传递。

所有进度回调必须携带：

```http
Authorization: Bearer <RUNTIME_INTEGRITY_TOKEN>
```

加载成功必须以 `ready` 或 `completed`、`progress=100` 结束。加载失败使用
`status=failed`，并在 `message` 中给出可定位的原因。缺少或错误 token 返回 `401`；
backend 未配置 token 返回 `503`。backend 会同时校验 task、active session、binding、slot
和 model 所有权。旧 allocation 的迟到回调按幂等成功返回，但不会改变当前 slot 或任务。

如果 runtime 没有单独上报 `integrity` 阶段，最终 `ready/completed` 回调会让 backend
把该侧完整性进度补为 100。

## 5. 生命周期接口

### `GET /health`

```json
{"status":"ok","node_role":"cloud"}
```

`node_role` 为 `edge` 或 `cloud`。backend 托管 cloud runtime 时依靠该接口确认进程就绪。

### `GET /runtime_state`

必须反映进程内真实状态，尤其是卸载完成后不能残留旧 `task_id` 或 `model_type`。
edge 的 ready 保温状态还必须包含完整 `runtime_route`，且指向的 cloud slot、控制地址、
gRPC 地址、模型和 task 均与一个健康 ready cloud runtime 一致；缺失或无法验证时按不健康处理。

```json
{
  "node_role": "cloud",
  "ready": true,
  "draining": false,
  "task_id": "624a1db9-eaa0-4257-9a38-7d6469357048",
  "model_type": "Llama-3.2-3B-Instruct",
  "active_request_count": 0,
  "last_used_at": 1710000000.0,
  "server_param_digest": "...",
  "partition_digest": "...",
  "integrity_verified": true,
  "confirmation_passed": true,
  "runtime_route": {
    "cloud_slot_id": "cloud-slot-0",
    "cloud_decode_grpc_target": "10.144.144.4:51200"
  }
}
```

空闲且未加载模型时，`ready=false`、`draining=false`、`active_request_count=0`，并且
`task_id`、`model_type`、`runtime_route` 均为 `null`。

### `GET /integrity`

用于排查 Aloepri/BERT 的 artifact fingerprint、server parameter digest、partition digest 和 cloud confirmation。它不是普通存活探针；backend 的所有权对账仍以 `/runtime_state` 为主。

### `POST /unload_model`

```json
{"reason":"session closed"}
```

成功响应：

```json
{"unloaded":true,"reason":"session closed"}
```

卸载必须释放模型/执行器资源，并原子清除当前任务、模型、路由和完整性状态。加载尚未结束时返回 `409`。

## 6. Aloepri/BERT 完整性确认

cloud runtime 向 `runtime_route.scheduler_integrity_callback_url` 发送：

```http
Authorization: Bearer <RUNTIME_INTEGRITY_TOKEN>
Content-Type: application/json
```

```json
{
  "task_id": "624a1db9-eaa0-4257-9a38-7d6469357048",
  "cloud_slot_id": "cloud-slot-0",
  "model_type": "Llama-3.2-3B-Instruct",
  "server_param_digest": "...",
  "partition_digest": "...",
  "timestamp": 1710000000,
  "nonce": "unique-nonce"
}
```

backend 校验 token 和 slot 后，转发到 edge runtime：

```text
POST /integrity/cloud_confirmation
```

转发同样使用共享的 `RUNTIME_INTEGRITY_TOKEN`。最终响应为：

```json
{"matched":true,"reason":null}
```

BERT 使用同一确认拓扑，但 digest 来源不同：normal Edge 实测 tokenizer、normal
Cloud 实测 `config.json + model.safetensors`，两侧分别与相同的
`normal_model_identity` 比对后生成一致的组合 `server_param_digest`；混淆 BERT 仍使用
部署 artifact fingerprint。backend 把该摘要视为不透明值原样转发，不解析组成，因此
不需要数据库迁移。两种模式都必须同时匹配 partition digest。正式注册表按角色隔离：
prefill 可持有 `client_secret`，decode 只持有 `server_dir`/artifact fingerprint，
coordinator/OpenAI 不得包含 `encrypted_model`。

## 7. 联调检查

- `/health` 返回正确 `node_role`。
- `/load_strategy` 在 5 秒内返回 `accepted` 或 `ok`。
- edge 使用下发的 `cloud_decode_grpc_target`。
- 回调使用原始 `task_id`，携带共享 Bearer token，最终到达 100%。
- `/runtime_state` 与实际加载、推理、卸载状态一致。
- `/unload_model` 后不再返回旧任务 ID。
- Aloepri/BERT 两端与 backend 使用相同的 `RUNTIME_INTEGRITY_TOKEN`。
