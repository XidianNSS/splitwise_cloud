# 切分策略算法服务对接说明

本文描述当前 `splitwise_cloud/backend` 与切分策略算法服务之间的 HTTP 协议。

该协议只用于 `strategy_kind=algorithm` 的生成模型。`BERT-Base-Uncased`
是 encoder-only 模型，backend 为它生成固定 12 层协议，不会调用算法服务；
算法服务无需适配 BERT。

## 1. 调用方式

backend 对算法服务发起一次同步 HTTP 请求：

```http
POST <ALGORITHM_API_URL>
Content-Type: application/json
```

算法服务必须在同一个 HTTP response 中返回最终切分策略，不使用异步 callback。

地址选择规则：

- 正式运行：`ALGORITHM_USE_MOCK=false`，使用 `ALGORITHM_REAL_API_URL`。
- 开发测试：`ALGORITHM_USE_MOCK=true`，使用 `ALGORITHM_MOCK_API_URL`。
- 超时：`ALGORITHM_API_TIMEOUT_SECONDS`。

当前 `.env.prod` 使用：

```text
http://10.144.144.6:8050/infer
```

开发测试开关只用于方便联调，协议与正式服务完全相同。

## 2. 请求体

示例中的数值仅用于说明结构；backend 会发送实时指标或配置的回退值。

```json
{
  "model_type": "Llama-3.2-3B-Instruct",
  "prompt_len": 96,
  "env": {
    "edge": {
      "device": "cuda:0",
      "model_spec": {
        "num_hidden_layers": 28,
        "num_attention_heads": 24
      },
      "metrics": {
        "cpu_percent": 20.1,
        "memory_percent": 31.4,
        "gpu_util_percent": 42.0,
        "gpu_mem_used_mb": 8192.0,
        "gpu_mem_total_mb": 24576.0,
        "accelerator_type": "nvidia",
        "chips": [
          {
            "chip_id": "0",
            "util": 42.0,
            "used_mb": 8192.0,
            "total_mb": 24576.0
          }
        ]
      },
      "storage_limit_gb": 16.0
    },
    "cloud": {
      "device": "npu:0",
      "model_spec": {
        "num_hidden_layers": 28,
        "num_attention_heads": 24
      },
      "metrics": {
        "cpu_percent": 8.2,
        "memory_percent": 22.5,
        "gpu_util_percent": 35.0,
        "gpu_mem_used_mb": 12000.0,
        "gpu_mem_total_mb": 65536.0,
        "accelerator_type": "ascend",
        "chips": []
      }
    },
    "network": {
      "edge_rtt_ms": 0.27,
      "cloud_rtt_ms": 0.02,
      "edge_to_cloud_rtt_ms": 0.27,
      "estimated_bandwidth_mbps": 1000.0,
      "packet_loss": 0.0
    }
  }
}
```

### 顶层字段

- `model_type`：用户请求的规范模型名。
- `prompt_len`：当前 backend 固定为 `96`。
- `env`：本次计算使用的边端、云端和网络环境。

当前支持的模型键为：

- `Llama-3.2-3b`
- `Llama-3.2-3B-Instruct`

这两种 Llama 3B 会调用算法服务。BERT 使用固定 encoder 策略，不会调用本接口；DeepSeek/Meta-Llama 尚未加入 scheduler，因此当前也不会发到算法服务。

### `env.edge` 和 `env.cloud`

- `device`：根据指标生成的 `cpu`、`cuda:0` 或 `npu:0`。
- `model_spec`：只包含 `num_hidden_layers` 和 `num_attention_heads`。
- `metrics`：包含整机 CPU/内存、当前选中单卡的利用率和显存视图、加速器类型，以及可用时的 `chips` 明细。
- `edge.storage_limit_gb`：边端可用单卡显存预算，单位 GB；指标不可用时回退为 `16.0`。

算法服务应忽略不认识的 `metrics` 扩展字段，以便 backend 后续增加监控数据而不破坏协议。

### `env.network`

- `edge_rtt_ms`：backend 到 edge 的 RTT。
- `cloud_rtt_ms`：backend 到 cloud 的 RTT。
- `edge_to_cloud_rtt_ms`：当前代码取 edge RTT 作为估计值。
- `estimated_bandwidth_mbps`：实测有效带宽；未启用或探测失败时使用配置回退值。
- `packet_loss`：探测到的最大丢包率；失败时使用配置回退值。

## 3. 成功响应

HTTP 状态必须为 2xx，body 必须是 JSON：

```json
{
  "status": "ok",
  "model_type": "Llama-3.2-3B-Instruct",
  "layer_partitions": [
    {
      "layer_id": 0,
      "head_assignments": [0, 0, 1, 1],
      "ffn_assignment": 1,
      "edge_heads": 2,
      "cloud_heads": 2
    }
  ]
}
```

字段约定：

- `status`：建议始终返回 `ok`；字段存在但不为 `ok` 时 backend 判定失败。
- `model_type`：建议原样返回；省略时 backend 使用请求值。
- `layer_partitions`：非空数组，是唯一必需的策略结果。
- `layer_id`：层编号。
- `head_assignments`：每个 attention head 的归属；`0=edge`，`1=cloud`。
- `ffn_assignment`：`0=edge`，`1=cloud`。虽然 backend 当前只做整数规范化，但下游 ModelSplit `PartitionConfig` 不接受值 `2`，算法服务不得返回 `2`。
- `edge_heads`、`cloud_heads`：可选。省略时 backend 根据 `head_assignments` 计算；提供时必须与其一致。

推荐每个模型返回完整层数，并保证每层的 `head_assignments` 长度等于
`num_attention_heads`。backend 会把上述数字转换为整数，但算法服务仍应主动校验范围、层数和 head 数，避免生成可解析但无法执行的策略。

当前 backend 不会在保存前完整验证 layer 连续性、head 取值范围和 FFN 取值范围；这些错误会在 ModelSplit `/load_strategy` 阶段被拒绝并使任务失败。因此算法服务必须保证 `layer_id` 从 0 连续递增、head 仅为 `0/1`、FFN 仅为 `0/1`。

## 4. 失败响应

算法无法计算时应返回明确的非 2xx 状态和简短错误信息，例如：

```http
HTTP/1.1 422 Unprocessable Entity
Content-Type: application/json

{"detail":"unsupported model_type"}
```

以下情况会使调度任务失败：

- 请求超过 `ALGORITHM_API_TIMEOUT_SECONDS`。
- HTTP 状态不是 2xx。
- body 不是 JSON。
- `status` 存在且不是 `ok`。
- `layer_partitions` 缺失、不是数组或为空。
- layer 字段不能转换为预期的整数结构。

backend 会把异常记录到任务的 `message`/`error_detail`，不会重试同一次算法请求。

## 5. backend 后续处理

成功响应后 backend 会：

1. 规范化 `layer_partitions` 并保存到任务。
2. 将任务从 `strategy` 推进到 `loading`。
3. 分配或复用 edge/cloud runtime slot。
4. 向两端的 `/load_strategy` 下发同一份切分策略。
5. 等待两端 runtime 回调加载结果。

算法服务不需要接收 `task_id`，也不需要调用 backend callback。旧的
`state_vector`、`strategy_callback` 和异步结果回传均不在当前主流程中。

## 6. 最小实现示例

```python
from fastapi import FastAPI

app = FastAPI()


@app.post("/infer")
async def infer(payload: dict) -> dict:
    layers = payload["env"]["edge"]["model_spec"]["num_hidden_layers"]
    heads = payload["env"]["edge"]["model_spec"]["num_attention_heads"]
    return {
        "status": "ok",
        "model_type": payload["model_type"],
        "layer_partitions": [
            {
                "layer_id": layer_id,
                "head_assignments": [1] * heads,
                "ffn_assignment": 1,
                "edge_heads": 0,
                "cloud_heads": heads,
            }
            for layer_id in range(layers)
        ],
    }
```

联调时重点检查：HTTP 响应耗时、完整层数、每层 head 数，以及 `edge_heads/cloud_heads` 与分配数组是否一致。
