# 边端前端对接说明

目标：让边端前端尽快接入当前云端后端。

## 当前只需要记住的流程

1. 从 OpenWebUI 读取当前 token
2. 确定当前使用的边端设备 IP
3. 调 `POST /api/v1/session/init`
4. 保存返回的 `session_id`
5. 调 `POST /api/v1/schedule/trigger`
6. 用 `GET /api/v1/schedule/tasks/{task_id}` 轮询任务状态
7. 如需展示切分策略，在任务进入 `loading` 后调 `GET /api/v1/schedule/tasks/{task_id}/strategy`

一句话版本：

**先用 OpenWebUI token 调 `/api/v1/session/init` 初始化会话。推荐显式传 `edge_device_ip`；若留空，后端会回退使用请求来源 IP。后续再用 `openwebui_token + Session-Id` 发起调度。**

## 当前前端需要传什么

说明：如果边端前端页面不在边端设备本机，仍然建议显式传真实边端模型推理服务所在设备的 IP。


### 1. 初始化会话

```http
POST /api/v1/session/init
Authorization: Bearer <openwebui_token>
Content-Type: application/json
```

请求体：

```json
{
  "edge_device_ip": "10.144.144.3"
}
```

说明：

- `edge_device_ip` 是边端模型推理服务所在设备的 IP
- 不是当前浏览器所在机器的 IP
- 若该字段为空，后端会回退尝试使用请求来源 IP 识别设备

成功响应示例：

```json
{
  "session_id": "530c57ad-df64-4eff-af80-f1f5339ce4ef",
  "edge_device": {
    "id": "edge_A",
    "ip": "10.144.144.3"
  },
  "cloud_device": {
    "id": "cloud",
    "ip": "10.144.144.2"
  },
  "message": "OpenWebUI token 校验通过，边端设备识别完成，会话初始化成功"
}
```

### 2. 发起调度

```http
POST /api/v1/schedule/trigger
Authorization: Bearer <openwebui_token>
Session-Id: <session_id>
Content-Type: application/json
```

请求体：

```json
{
  "model_type": "Llama-3.2-3b"
}
```

当前支持的模型：

- `gpt2`
- `tinyllama`
- `Llama-3.2-3b`

成功响应示例：

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

### 3. 查询任务状态

```http
GET /api/v1/schedule/tasks/{task_id}
Authorization: Bearer <openwebui_token>
```

前端重点关注这些字段：

- `status`
- `phase`
- `phase_progress`
- `overall_progress`
- `message`
- `edge_progress`
- `cloud_progress`
- `edge_message`
- `cloud_message`
- `error_detail`

状态展示建议：

- `phase = "strategy"`：正在计算切分策略
- `phase = "loading"`：边云模型正在加载
- `status = "completed"`：任务完成
- `status = "failed"`：展示 `message` 和 `error_detail`

### 4. 获取切分策略

```http
GET /api/v1/schedule/tasks/{task_id}/strategy
Authorization: Bearer <openwebui_token>
```

建议在任务进入 `loading` 后再拉取。

## 前端当前不需要做什么

- 不需要调用 `/api/v1/auth/exchange`
- 不需要自己选择云端设备
- 不需要把切分策略发给模型推理服务
- 不需要直接和算法模块通信
- 不需要直接和边端 / 云端模型推理服务通信

这些都由云端后端负责。

## 最小示例

```javascript
async function initSession(openwebuiToken, edgeDeviceIp) {
  const res = await fetch("http://10.144.144.2:8010/api/v1/session/init", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${openwebuiToken}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ edge_device_ip: edgeDeviceIp })
  });
  return await res.json();
}

async function triggerTask(openwebuiToken, sessionId, modelType) {
  const res = await fetch("http://10.144.144.2:8010/api/v1/schedule/trigger", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${openwebuiToken}`,
      "Session-Id": sessionId,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ model_type: modelType })
  });
  return await res.json();
}
```

## 常见错误

- `401`：OpenWebUI token 无效，或 `Session-Id` 与 token 不匹配
- `400`：参数错误，或模型名不支持
- `403`：`edge_device_ip` 未匹配到已登记边端设备
- `404`：任务不存在
- `409`：策略还没生成，过早拉取 `/strategy`
- `500`：算法服务、模型推理服务或后端内部异常
