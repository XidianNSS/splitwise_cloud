# Meta-Llama-3.1-8B-Instruct 本机隔离调度实验

2026-10-26 在两个现有临时克隆完成普通 BF16 权重实验，未更新正式部署。
适配代码经独立审查后随本提交交付；正式部署须另行授权。
32 层、32 Q heads、8 KV heads，hidden/intermediate/vocab 为 4096/14336/128256；
canonical/runtime 名称统一为 `Meta-Llama-3.1-8B-Instruct`，使用 standard generation
和动态策略。启动准入为 Edge 16 GiB、Cloud 24 GiB 可用显存，不保证长上下文容量。

## 模型与策略

ModelSplit 原有 adapter 和 checkpoint loader 能加载真实权重。为完整支持其
generation_config.json 中的三个 EOS ID，decode 改为优先识别模型 GenerationConfig
的单个/多个 EOS，并覆盖首 token 与后续 token。CPU 受控回归覆盖全部三个 EOS，
真实样例以 128009 结束。依赖环境、模型文件和采样规则未改。

算法输入使用真实 32 层/32 heads 规格，没有为新模型写固定策略。FFN 2 继续统一
投影为整层 Cloud 1，保留算法的其他分配并公开 runtime_adjustments。该兼容会
改变算法原始性能方案，不代表实现了同一层 FFN 的边云内部切分。

## 实验拓扑与结果

- 模型 Prefill/Decode 都使用本机物理 NPU 5；逻辑编号 1 经可见设备限制后为
  npu:0。开始前及运行期间检查所有者，只允许本次测试进程占卡；未使用 NPU 2。
- backend、Coordinator、OpenAI、Prefill/Decode 的 HTTP/gRPC 数据面全部为本机
  localhost 私有端口 28510–28514、28520–28521，使用独立 SQLite、注册表和日志。
  策略计算复用已有 CPU HTTP 接口，不在其他机器加载或执行 Meta-Llama 权重。
- 任务 `95671051-021a-4a20-81f3-9425b6b687dc` completed、两端 ready，累计 1024 项
  head 分配中 Edge 258、Cloud 766，6 个 FFN 2 记录为 Cloud 1。
- 非流式返回 ` Paris.`，finish_reason=stop；SSE 6 条非空事件含 `[DONE]`。
- 生成后两端 active_request_count=0，会话关闭后 binding released；测试服务和
  端口均已退出，NPU 5 无剩余设备所有者，约 3399 MiB 为原有基础占用。

ModelSplit 另与完整 Hugging Face 模型做真实权重对照：两个问答样例分别输出
`2 + 2 = 4.` 和 `Paris.`，包含 EOS 的 9/3 tokens 均一致；BF16 prefill logits
相对 L2 差异约 1.65%/1.09%。未据短样例评价整体质量或保证任意输入逐 token 相同。

## 验证与复现

Runtime CPU 共 246 项，244 项通过、2 项跳过；backend CPU 105 项通过。

独占且允许测试的物理 NPU 上显式运行以下命令，需要原有 Ascend 环境及策略和
Prometheus 服务；工具自行核对设备映射与占用，不能用于普通 CI 或正式启动：

```bash
python scripts/validate_model_schedule.py \
  --modelsplit-root /local/ModelSplit_model_adaptation_tmp_20261024 \
  --model-path /local/Meta-Llama-3.1-8B-Instruct \
  --model-name Meta-Llama-3.1-8B-Instruct --physical-npu 5 \
  --base-port 28510 --output-root .private/meta-llama31-8b-validation
```

私有结果：`.private/meta-llama31-8b-validation/schedule-1792980696/result.json`，
同目录包含服务日志、DB、NPU 快照及策略 API 返回；运行期设备审计在 ModelSplit
临时克隆 `.private/meta-llama31-8b-validation/` 中，不提交这些私有数据。

## 后续固定策略跨机数据面验证

同日经用户授权，另在 nss-d CUDA GPU0 与云端物理 NPU 5 完成 ModelSplit 固定
策略跨机实验。全部权重文件 SHA-256 通过，代码同步到独立实验目录并逐文件校验；
两端保留各自 CUDA/Ascend 环境。每层 16/16 attention heads，FFN 按层交替，
32 层累计 heads 512/512、FFN 16/16。此次没有启动 backend 调度或调用算法接口，
不属于 splitwise_cloud 跨机动态调度验收。

原生 chat prompt 的两个 greedy 样例返回 `4.` 和 `Paris.`，均以 stop 结束；数学题
不同于此前本机基准 `2 + 2 = 4.`，首都样例 token 序列一致。差异来源未进一步
定位，不声称跨硬件逐 token 等价。OpenAI 非流式返回 Paris. / stop，SSE 6 条 data
事件含 `[DONE]`；两端活动计数归零。GPU/NPU 采样确认只存在本次计算进程。

私有服务、模型和测试端口全部回收，两端无剩余测试设备所有者，nss-d 回到
34 MiB/0% 且无 CUDA compute；正式 WebUI health=200，正式 adapter/executor 哈希
未变化。临时防火墙规则已移除。报告保存在 ModelSplit 临时克隆
`.private/meta-llama31-8b-validation/fixed-crosshost-1792991739/result.json`。

跨机动态调度、长上下文、并发或混淆工件尚未验收；正式部署保持原状。
