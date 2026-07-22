# 边端前端对接说明

本文面向调用 `splitwise_cloud` 的边端前端。接口以当前 backend 路由和 Pydantic 模型为准。

正式 backend 基地址由 `SERVER_PUBLIC_BASE_URL`/`BACKEND_BASE_URL` 配置；当前
`.env.prod` 为：

```text
http://10.144.144.4:8010
```

## 1. 最小流程

1. 获取当前 OpenWebUI access token。
2. 调用 `POST /api/v1/session/init`，保存 `session_id`。
3. 调用 `POST /api/v1/schedule/trigger`，保存 `task_id`。
4. 轮询任务接口，或使用 SSE 订阅任务状态。
5. 会话持续使用时定期 heartbeat；退出时调用 close。

模型选择前可调用 `GET /api/v1/schedule/models`。生成模型在任务完成后调用
边端 `/v1/chat/completions`；`BERT-Base-Uncased` 调用边端 `/v1/embeddings`，
输入文本后返回 768 维向量，不会返回对话文本。

`/schedule/models` 是 scheduler catalog，边端 `/v1/models` 是该节点的 runtime/OpenAI 注册表。正式前端只应展示两者交集。

除 SSE 外，本文标注需要登录的接口都使用：

```http
Authorization: Bearer <openwebui_access_token>
```

不要调用旧的 `/api/v1/auth/exchange`。调度触发接口还必须携带 `Session-Id`。

## 2. 初始化会话

```http
POST /api/v1/session/init
Authorization: Bearer <openwebui_access_token>
Content-Type: application/json
```

```json
{
  "edge_device_ip": "10.144.144.5"
}
```

- `edge_device_ip` 是运行 ModelSplit edge runtime 的机器 IP，不是浏览器 IP。
- 建议总是显式传值。留空时 backend 依次尝试 `X-Forwarded-For`、`X-Real-IP` 和连接来源 IP。
- IP 必须匹配 backend 已登记的 edge 设备，否则返回 `403`。
- 同一用户、设备和有效租约会复用已有 session；租约为 2 小时。

响应示例：

```json
{
  "session_id": "530c57ad-df64-4eff-af80-f1f5339ce4ef",
  "openwebui_user_id": "user-id",
  "openwebui_username": "alice",
  "openwebui_role": "user",
  "edge_device": {
    "id": "edge_A",
    "name": "nss-m",
    "type": "edge",
    "ip": "10.144.144.5"
  },
  "cloud_device": {
    "id": "cloud",
    "name": "cloud",
    "type": "cloud",
    "ip": "10.144.144.4"
  },
  "message": "OpenWebUI token 校验通过，边端设备识别完成，会话初始化成功"
}
```

## 3. 发起调度

先查询 backend 实际支持的模型，避免在前端硬编码：

```http
GET /api/v1/schedule/models
Authorization: Bearer <openwebui_access_token>
```

每项返回 `model_type`、`runtime_model_type`、`architecture`、`capability`、
`deployment_mode` 和 `strategy_kind`。`capability=generation` 使用 chat completion；
`capability=embeddings` 使用 embeddings。

```http
POST /api/v1/schedule/trigger
Authorization: Bearer <openwebui_access_token>
Session-Id: <session_id>
Content-Type: application/json
```

```json
{
  "model_type": "Llama-3.2-3B-Instruct"
}
```

当前 scheduler catalog 接受以下名称，匹配时不区分大小写：

- `Llama-3.2-3b`
- `Llama-3.2-3B-Instruct`
- `BERT-Base-Uncased`

其中两种 Llama 提供生成接口，BERT 提供 embeddings 接口。ModelSplit 已有代码级
adapter/config 接入但 scheduler 尚未登记的 DeepSeek/Meta-Llama 模型不能直接从
正式前端选择；这也不代表相关模型已经完成真实权重端到端验收。

成功返回 HTTP `202`：

```json
{
  "status": "accepted",
  "task_id": "75ec72d7-aa1e-454f-a6d0-8b3de7b270d8",
  "phase": "strategy",
  "phase_progress": 0,
  "overall_progress": 0,
  "message": "任务已受理，开始计算切分策略"
}
```

## 4. 查询任务状态

```http
GET /api/v1/schedule/tasks/{task_id}
Authorization: Bearer <openwebui_access_token>
```

返回字段分为四组：

| 类别 | 字段 |
|---|---|
| 任务 | `task_id`, `status`, `phase`, `phase_progress`, `overall_progress`, `message`, `error_detail` |
| edge | `edge_progress`, `edge_strategy_progress`, `edge_integrity_progress`, `edge_runtime_load_progress`, `edge_status`, `edge_message` |
| cloud | `cloud_progress`, `cloud_strategy_progress`, `cloud_integrity_progress`, `cloud_runtime_load_progress`, `cloud_status`, `cloud_message` |
| 排队/资源 | `queue_status`, `queue_position`, `runtime_binding_id`, `edge_slot_id`, `cloud_slot_id`, `allocated_cloud_slot_id` |

主要状态语义：

- `status=accepted|running`：任务尚未结束。
- `status=completed`：边云两侧均已就绪。
- `status=failed`：终态；展示 `message` 和 `error_detail`。
- `phase=strategy`：采集指标并计算切分策略。
- `phase=loading`：分配资源并加载边云 runtime。
- `phase=completed`：任务完成。

`edge_progress` 和 `cloud_progress` 已由 backend 计算，前端不要重复计算。其权重是：

```text
side_progress = strategy * 40% + integrity * 30% + runtime_load * 30%
```

`overall_progress` 是任务阶段总进度：strategy 占前 50%，loading 占后 50%。

排队时重点展示：

- `queue_status=queued_strategy`：等待策略计算。
- `queue_status=running_strategy`：正在计算策略。
- `queue_status=waiting_cloud_slot`：策略已完成，等待 cloud slot。
- `queue_status=running_loading`：正在下发或加载 runtime。
- `queue_position`：队列位置；非排队状态通常为 `0` 或 `null`。

前端终止轮询的唯一条件应是 `status` 为 `completed` 或 `failed`。

当 BERT 任务为 `completed` 后，调用所选边端的 ModelSplit OpenAI API：

```http
POST http://<edge-ip>:9003/v1/embeddings
Content-Type: application/json

{"model":"BERT-Base-Uncased","input":"text to encode"}
```

`input` 可以是一个字符串或最多 16 个字符串；数组响应按输入顺序返回。`data[*].embedding` 固定为 768 个 float，`usage` 是整批输入 token 数。该数据面请求不经过 cloud backend；
backend 只负责提前完成模型、slot 和边云路由准备。

## 5. SSE 订阅

```http
GET /api/v1/schedule/tasks/{task_id}/stream?token=<url_encoded_openwebui_access_token>
Accept: text/event-stream
```

每条事件格式：

```text
data: {与任务查询接口相同的 JSON}
```

服务每秒推送一次，任务进入终态后主动结束。浏览器原生 `EventSource` 不能设置
Authorization header，因此该接口使用 query token；必须先 `encodeURIComponent(token)`，并避免在日志中记录完整 URL。

## 6. 获取切分策略

```http
GET /api/v1/schedule/tasks/{task_id}/strategy
Authorization: Bearer <openwebui_access_token>
```

策略尚未生成时返回 `409`。成功响应已经是展示格式：

```json
{
  "task_id": "75ec72d7-aa1e-454f-a6d0-8b3de7b270d8",
  "model_type": "Llama-3.2-3B-Instruct",
  "decision": {
    "layer_partitions": [
      {
        "layer_id": 0,
        "head_assignments": [0, 0, 1, 1],
        "ffn_assignment": 1,
        "edge_head_count": 2,
        "cloud_head_count": 2
      }
    ],
    "edge_head_count_total": 2,
    "cloud_head_count_total": 2,
    "strategy_kind": null,
    "capability": null,
    "deployment_mode": null
  }
}
```

生成模型的后三个 metadata 字段可能为 `null`；BERT 返回 `fixed_bert_encoder`、`embeddings` 和 `encrypted`。当前 runtime 的 `ffn_assignment` 只支持 `0=edge` 或 `1=cloud`。

## 7. 会话续期和关闭

### 续期

```http
POST /api/v1/session/heartbeat
Authorization: Bearer <openwebui_access_token>
Content-Type: application/json

{"session_id":"530c57ad-df64-4eff-af80-f1f5339ce4ef"}
```

响应包含新的 `lease_expires_at`。长时间打开的页面应在租约过期前调用。

### 关闭

```http
POST /api/v1/session/close
Authorization: Bearer <openwebui_access_token>
Content-Type: application/json

{"session_id":"530c57ad-df64-4eff-af80-f1f5339ce4ef"}
```

关闭后 backend 会释放 binding，并按 runtime 生命周期规则保留或卸载模型。关闭请求应由明确的退出/切换设备操作触发，不建议依赖浏览器 `beforeunload`。

## 8. 最小 JavaScript 示例

```javascript
const baseUrl = "http://10.144.144.4:8010";

async function request(path, token, options = {}) {
  const response = await fetch(`${baseUrl}${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail ?? `HTTP ${response.status}`);
  return payload;
}

const session = await request("/api/v1/session/init", token, {
  method: "POST",
  body: JSON.stringify({ edge_device_ip: "10.144.144.5" }),
});

const task = await request("/api/v1/schedule/trigger", token, {
  method: "POST",
  headers: { "Session-Id": session.session_id },
  body: JSON.stringify({ model_type: "Llama-3.2-3B-Instruct" }),
});
```

## 9. 常见错误

| HTTP | 常见原因 |
|---:|---|
| `400` | 参数或模型名不支持；当前 session 不是固定 cloud 设备 |
| `401` | token 无效、session 无效或租约已过期 |
| `403` | edge IP 未登记，或 session 不属于当前 token 用户 |
| `404` | session/task 不存在 |
| `409` | session 已关闭/过期，或策略尚未生成 |
| `500` | backend 内部错误 |
| `503` | OpenWebUI JWT 校验配置缺失 |

前端不负责选择 cloud slot、调用算法服务、向 runtime 下发策略或聚合加载进度；这些都由 backend 完成。
