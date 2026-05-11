# 切分策略计算模块对接简版说明

本文档说明当前云端后端与“切分策略计算模块”的真实对接协议。

当前对接方式已经改成：

1. 云端后端主动向算法服务发送一份完整环境 JSON
2. 算法服务同步计算
3. 算法服务直接在 HTTP Response 中返回切分策略 JSON

当前采用的是单次同步请求模型：

- 云端后端直接发送一份完整环境 JSON
- 算法服务同步返回最终切分策略 JSON
- 不再依赖旧的异步回调链路

---

## 1. 云端后端 -> 算法服务

### 请求地址

当前由后端配置项控制：

```text
ALGORITHM_API_URL
```

当前代码默认值：

```text
http://127.0.0.1:8050/infer
```

说明：

- `ALGORITHM_API_URL` 需要根据算法模块真实服务地址修改
- 当前本地 mock 联调时，也可以临时改到其他端口，例如 `http://127.0.0.1:5000/infer`

### 请求方法

```http
POST
```

### 请求头

```http
Content-Type: application/json
```

### 请求体格式

当前后端直接发送完整 JSON，不再做向量编码。

示例：

```json
{
  "model_type": "gpt2",
  "prompt_len": 96,
  "env": {
    "edge": {
      "device": "cpu",
      "model_spec": {
        "num_hidden_layers": 12,
        "num_attention_heads": 12
      },
      "metrics": {
        "cpu_percent": 50.0,
        "memory_percent": 50.0,
        "gpu_util_percent": 0.0,
        "gpu_mem_used_mb": 0,
        "gpu_mem_total_mb": 1,
        "queue_len": 4
      },
      "storage_limit_gb": 16
    },
    "cloud": {
      "device": "cuda:0",
      "model_spec": {
        "num_hidden_layers": 12,
        "num_attention_heads": 12
      },
      "metrics": {
        "cpu_percent": 50.0,
        "memory_percent": 50.0,
        "gpu_util_percent": 50.0,
        "gpu_mem_used_mb": 8000,
        "gpu_mem_total_mb": 16000,
        "queue_len": 4
      }
    },
    "network": {
      "edge_rtt_ms": 25.0,
      "cloud_rtt_ms": 25.0,
      "edge_to_cloud_rtt_ms": 25.0,
      "estimated_bandwidth_mbps": 500.0,
      "packet_loss": 0.2
    }
  }
}
```

### 字段说明

- `model_type`
  - 当前请求的模型标识
  - 例如：
    - `gpt2`
    - `tinyllama`
    - `Llama-3.2-3b`

- `prompt_len`
  - 当前请求的提示词长度
  - 当前后端固定发送 `96`

- `env.edge.device`
  - 边端设备运行标签
  - 当前后端会根据 GPU 显存指标动态写成：
    - `cpu`
    - 或 `cuda:0`

- `env.cloud.device`
  - 云端设备运行标签
  - 当前后端同样根据 GPU 显存指标动态写成：
    - `cpu`
    - 或 `cuda:0`

- `env.edge.model_spec`
- `env.cloud.model_spec`
  - 当前只发送算法模块需要的最小模型规格：
    - `num_hidden_layers`
    - `num_attention_heads`

- `env.edge.metrics`
- `env.cloud.metrics`
  - 当前后端发送真实或回退后的监控指标：
    - `cpu_percent`
    - `memory_percent`
    - `gpu_util_percent`
    - `gpu_mem_used_mb`
    - `gpu_mem_total_mb`
    - `queue_len`

- `env.edge.storage_limit_gb`
  - 仅边端包含该字段
  - 当前由后端根据边端可用显存预算推导

- `env.network`
  - 当前网络相关指标：
    - `edge_rtt_ms`
    - `cloud_rtt_ms`
    - `edge_to_cloud_rtt_ms`
    - `estimated_bandwidth_mbps`
    - `packet_loss`

### 云端后端当前行为

- 请求超时由配置项控制：

```text
ALGORITHM_API_TIMEOUT_SECONDS
```

- 当前默认值：

```text
30
```

- 后端会直接读取同步返回的 JSON
- 不再等待异步回调

---

## 2. 算法服务 -> 云端后端同步响应

### 响应格式

算法服务应直接返回最终切分策略 JSON。

示例：

```json
{
  "status": "ok",
  "model_type": "gpt2",
  "layer_partitions": [
    {
      "layer_id": 0,
      "head_assignments": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
      "ffn_assignment": 1,
      "edge_heads": 0,
      "cloud_heads": 12
    },
    {
      "layer_id": 1,
      "head_assignments": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
      "ffn_assignment": 1,
      "edge_heads": 0,
      "cloud_heads": 12
    }
  ]
}
```

### 字段说明

- `status`
  - 建议返回 `ok`
  - 当前后端会校验该字段；如果存在且不是 `ok`，则视为算法异常

- `model_type`
  - 建议返回本次请求对应的模型标识

- `layer_partitions`
  - 必填
  - 每层切分结果数组

#### `layer_partitions[*]` 格式

```json
{
  "layer_id": 0,
  "head_assignments": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
  "ffn_assignment": 1,
  "edge_heads": 0,
  "cloud_heads": 12
}
```

字段含义：

- `layer_id`
  - 当前层编号

- `head_assignments`
  - 当前层 attention heads 的切分结果
  - `0` 表示边端
  - `1` 表示云端

- `ffn_assignment`
  - 当前层 FFN 的切分结果
  - `0` 表示边端
  - `1` 表示云端
  - `2` 表示拆分

- `edge_heads`
  - 当前层分给边端的 head 数量

- `cloud_heads`
  - 当前层分给云端的 head 数量

说明：

- 当前后端会优先信任 `head_assignments`
- 如果响应中带了 `edge_heads` / `cloud_heads`，后端会一并保留并用于内部标准化结果

---

## 3. 云端后端收到同步响应后的行为

当算法服务同步返回成功后，云端后端会：

1. 校验 `status`
2. 校验并解析 `layer_partitions`
3. 将结果保存到任务的 `strategy_payload`
4. 进入加载资源判定
5. 向边端模型推理服务和云端模型推理服务下发 `/load_strategy`
6. 等待两边模型推理服务回调加载进度

因此，对算法服务来说，最关键的是：

- 能正确接收新的环境 JSON
- 能同步返回合法的 `layer_partitions`

---

## 4. 当前对算法模块的最小要求

当前算法模块只需要满足两点：

- 能正确接收 `model_type + prompt_len + env JSON`
- 能在同一个 HTTP Response 中同步返回合法的切分策略 JSON

旧的 `task_id`、`state_vector` 与 `strategy_callback` 回调链路，当前都已经退出主流程，不需要再兼容。

---

## 5. 对接结论

一句话总结：

**云端后端现在直接发 `model_type + prompt_len + env JSON` 到 `/infer`，算法服务直接同步返回 `status + model_type + layer_partitions`。**
