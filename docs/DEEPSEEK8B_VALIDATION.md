# DeepSeek Llama 8B 调度接入与隔离验证

## 接入内容

模型名统一为 `DeepSeek-R1-Distill-Llama-8B`，architecture 为 llama，32 层、32 个
attention heads、8 个 KV heads、hidden size 4096、intermediate size 14336、vocab
128256。catalog 提供 standard generation，继续调用真实策略算法。

BF16 权重约 14.96 GiB；启动准入保守设置边端 16 GiB、云端 24 GiB 可用显存，
保留额外运行开销。此下限不是任意上下文长度的显存保证。

真实 policy_api 返回过 `ffn_assignment=2`，表示 FFN 半边/半云执行。现有 runtime
仅支持整层 FFN。因此该模型通过显式 `split_ffn_fallback=cloud` 选择整层 Cloud，
保留 head 分配，将每项调整写入 `runtime_adjustments` 和告警日志。策略查询接口的
`decision.runtime_adjustments` 返回相同的结构化记录，未调整及历史任务返回空数组。不会修改原始
响应对象或正式策略服务，也不对其他模型自动启用投影。此选择改变预测性能方案，
不能宣称运行的是算法原始 split FFN。所有模型的返回策略在下发前检查模型身份、
层数/层号、head 维度/取值、可选计数和 FFN 值。所有分配、层号及提供的计数必须
为 JSON 整数，拒绝浮点数、字符串、布尔值和 `null`，避免截断或跳过检查。

## 实测范围

2026-10-24 在独立开发克隆中，使用 localhost 私有端口、私有 SQLite、独立角色
注册表与日志，真实策略服务、真实 8B 权重和 ModelSplit HTTP/gRPC 完成：

1. catalog 查询、带 JWT 的 session init、schedule trigger。
2. 真实策略生成与兼容投影、backend 托管 Decode、两端加载和 ready 回调。
3. task completed，两端 task ID 一致，edge 路由指向分配的 cloud slot。
4. 非流式 chat completion 和 SSE `[DONE]`。
5. session close 后 binding released；backend 退出时托管 Decode 停止。

该测试的 Edge/Cloud 角色均使用物理 NPU 5（逻辑设备 1，经可见设备限制后为
npu:0），不是 CUDA 边端与 Ascend 云端的三机验收。开始前与运行期间检查设备
文件/NPU 进程，只允许本次测试进程占用；检测到其他进程即退出自己的测试。未
使用保留的物理 NPU 2 或 nss-m GPU，未改动正式数据库、配置或服务。

ModelSplit 另修复了保存的 ByteLevel tokenizer 被新版 AutoTokenizer 改写以及
decode attention mask 时序问题。两个补全样例的连续 12 个 greedy token 与完整
模型一致；开放式思考提示仍观察到 BF16 数值差异及措辞分歧，不能宣称任意
提示逐 token 相同。普通模式验收也不等于该 8B 混淆工件通过验收。

## nss-d CUDA / 云端 Ascend 跨机验证

2026-10-24，nss-d RTX 5090（CUDA 0）与云端物理 NPU 5 完成真实 BF16 权重的
隔离调度和推理。两端保留各自 Python/PyTorch/CUDA/Ascend 环境；边端权重与云端
逐分片 SHA-256 一致，没有追加 CUDA 专用模型修复，也没有更新正式部署。

- 任务 `ce671d79-bd06-4e63-9797-ac529239096c` completed，两端 ready。
- 32 层累计 1024 项 head 分配为 Edge 322、Cloud 702；FFN 为 Edge 2、Cloud 30。
  第 30 层 FFN 2 → 1 的调整实际通过策略查询 API 返回。
- 真实 GPU Prefill PID 3008340，物理 NPU 5 Decode PID 723480；证据包含设备
  进程和显存快照。nss-m 与物理 NPU 2 均未用于测试。
- 非流式请求成功，SSE 含 `[DONE]`；聊天 24-token 输出截断于思考片段，未评价
  最终答案质量。两个原始补全样例各 12-token 输出文本与此前完整模型参考一致。
- 推理后两端 active_request_count=0，session close 后 binding released。测试服务
  和端口已退出，GPU 回到原有 34 MiB 基础占用，NPU 5 无测试进程。WebUI health
  200、正式生成源码哈希不变，临时按对端 IP 放行的新端口规则均已撤销并核实。

私有结果：`.private/deepseek8b-validation/crosshost-1792834423/result.json`；同目录
`nssd-evidence.tar.gz` 包含边端日志、依赖版本、权重校验及正式部署前后检查。
首次尝试因新端口未放行而在加载模型前退出，日志另行保留；没有用旧正式端口绕过。

## 复现

CPU 测试：

```bash
python -m unittest tests.test_deepseek8b_scheduler tests.test_strategy_adjustments_api -v
```

当前三机开发环境可显式执行 `scripts/validate_model_schedule.py`，指定隔离
ModelSplit 克隆、本地权重及允许且无其他服务占用的物理 NPU。脚本使用临时开发
端口，结果、SQLite 和日志放在 `.private/deepseek8b-validation/`；该目录不进 Git。
需要已有 Python 环境、Ascend toolkit、真实 Prometheus 与 CPU 策略 API，不能把
它当作普通单元测试或正式部署启动命令。托管日志目录通过 `CLOUD_SLOT_LOG_DIR`
隔离，默认值兼容原有部署。

正式部署前仍需用户审核、各节点环境/角色注册表核对及 nss-m 独立验收、
长上下文、取消/并发/切换和生成质量验收。本次开发不会自动推送或部署。
