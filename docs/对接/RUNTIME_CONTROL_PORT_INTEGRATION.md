# 模型推理服务对接说明

目标：让边端模型推理服务 / 云端模型推理服务 开发者尽快明确当前如何与云端后端对接。

## 当前对接规则

- 不再使用 `register / unregister`
- 云端后端不会再动态发现模型推理服务节点
- 当前统一采用“固定控制端口 + 进度回调”方案

## 当前主流程

1. 边端前端向云端后端发起调度
2. 云端后端完成资源检查并拿到切分策略
3. 云端后端分别向边端模型推理服务、云端模型推理服务的固定控制端口发送启动请求
4. 模型推理服务内部启动对应模型并加载切分策略
5. 模型推理服务持续回调加载进度
6. 云端后端聚合进度并更新任务状态

## 固定控制端口

当前控制路径：

```text
POST /load_strategy
```

当前端口约定：

- edge mock: `18001`
- cloud mock: `18002`
- edge real: `9001`
- cloud real: `9002`

## 云端后端下发给模型推理服务的请求体

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
  }
}
```

字段含义：

- `task_id`：后续进度回调用它关联任务
- `model_type`：本次要启动的模型
- `decision`：切分策略

## 模型推理服务控制端口应返回什么

建议快速返回：

```json
{
  "status": "accepted",
  "message": "model service startup accepted"
}
```

这里的 `accepted` 只表示“已接收启动请求”，不表示模型已经加载完成。

如果模型推理服务明确无法受理，也可以返回非 `accepted` 状态，云端后端会把任务标记为失败。

## 模型推理服务进度回调

边端模型推理服务回调地址：

```http
POST /api/v1/schedule/runtime_callback/edge
```

云端模型推理服务回调地址：

```http
POST /api/v1/schedule/runtime_callback/cloud
```

回调体格式：

```json
{
  "task_id": "624a1db9-eaa0-4257-9a38-7d6469357048",
  "status": "loading",
  "progress": 50,
  "message": "边端正在加载 Llama-3.2-3b 权重"
}
```

失败时继续沿用同一结构：

```json
{
  "task_id": "624a1db9-eaa0-4257-9a38-7d6469357048",
  "status": "failed",
  "progress": 0,
  "message": "云端模型实例启动失败"
}
```

完成时建议回调：

```json
{
  "task_id": "624a1db9-eaa0-4257-9a38-7d6469357048",
  "status": "ready",
  "progress": 100,
  "message": "模型加载完成"
}
```

## 职责边界

模型推理服务负责：

- 接收 `/load_strategy`
- 根据 `model_type` 启动正确模型
- 加载切分策略
- 回调进度
- 对外维持类 OpenAI 推理入口

云端后端负责：

- 任务受理
- 资源检查
- 调算法模块
- 向模型推理服务下发策略
- 聚合进度和维护任务状态
