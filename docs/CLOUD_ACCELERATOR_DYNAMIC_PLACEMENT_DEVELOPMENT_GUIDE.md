# 云端按可用显存动态选择加速卡开发指导

## 1. 文档目的

本文用于指导 `splitwise_cloud` 与 `ModelSplit` 后续实现以下能力：

> 当云端出现新的模型加载请求时，在当前可分配的加速卡中选择有效可用显存更多的一张，并保证切分策略使用的资源数据、数据库中的 slot 归属和 ModelSplit 子进程实际使用的设备完全一致。

本文以当前正式环境的两张昇腾 910B4-1 NPU 为主要目标，同时保留 NVIDIA CUDA 设备的通用语义。

这里的“动态选择”仅表示运行时的设备放置策略，不代表项目存在两套运行模式。正式运行与开发联调仍使用同一套业务流程。

本文是开发方案，不表示相关功能已经实现。

## 2. 结论

方案与当前代码总体适配，已有基础包括：

- Prometheus 已采集逐卡显存、利用率、设备编号和 PCI Bus 信息。
- cloud decode 已按独立进程和独立 runtime slot 管理。
- `CLOUD_SLOT_NPU_DEVICES=0,1` 已能让不同 slot 使用 `npu:0`、`npu:1`。
- cloud slot 预留已使用条件 UPDATE，能够防止同一 slot 被两个并发任务同时占用。
- ModelSplit 已支持 `cpu`、`cuda:<index>`、`npu:<index>`，并在进程初始化时设置实际设备。
- 模型注册表已有最低可用显存门槛。
- reconcile、slot 启停、失败退避和等待任务推进机制可以继续复用。

但当前尚未形成动态选卡闭环，不能只修改候选 slot 的排序。必须同时补齐：

1. Prometheus 物理设备标识与 ModelSplit 逻辑设备名之间的稳定映射。
2. 策略计算之前的轻量设备预留（placement lease）。
3. 对 `strategy_kind=algorithm` 的生成模型，策略输入必须使用已经预留的那张卡，而不是整机中临时最空闲的一张卡；BERT 固定策略不读取算法输入，但仍必须先完成同样的选卡和 lease。
4. 模型启动前的第二次资源检查和失败重选。
5. slot、task、binding、子进程和 runtime state 中一致的设备记录。
6. 并发、超时、取消、重启恢复和外部进程占用下的回滚机制。

推荐采用“固定 slot 与设备亲和关系，动态选择 slot”的设计，不建议让同一个运行中的 slot 临时切换设备。

## 3. 当前代码事实

### 3.1 当前显存指标语义

文件：`backend/app/services/prometheus_metrics.py`

当前代码同时查询两类指标：

- 聚合指标：对整机所有卡求和或求平均，主要用于整机视图。
- 逐卡指标：每张卡独立返回 `chip_id`、`util`、`used_mb`、`total_mb` 等字段。

构造原始结果时，`gpu_mem_used_mb` 和 `gpu_mem_total_mb` 最初是整机聚合值；随后 `_apply_best_available_chip_view()` 会在逐卡数据存在时，将顶层 GPU/HBM 字段覆盖为“可用显存最多的单张卡”视图。

因此，正常情况下当前发给策略模块的顶层显存值：

- 不是所有卡总和；
- 不是所有卡平均值；
- 是可用显存最多的单卡值。

完整逐卡数据仍保留在 `metrics.chips`。

需要注意：如果逐卡查询为空，当前代码会保留整机聚合值。这个回退适合监控展示，不适合动态选卡和单进程模型加载准入。动态放置必须在逐卡指标缺失时安全失败或等待，不能把两张卡的显存总量当成一张卡使用。

### 3.2 当前正式机器的编号差异

检查时，本机 Prometheus/npu-smi 暴露的设备为：

| 指标设备 | PCI Bus | 总 HBM | 已用 HBM | 可用 HBM |
|---|---|---:|---:|---:|
| NPU `2` | `0000:01:00.0` | 65536 MB | 3399 MB | 62137 MB |
| NPU `5` | `0000:81:00.0` | 65536 MB | 10003 MB | 55533 MB |

而 ModelSplit 使用的是逻辑设备：

- `npu:0`
- `npu:1`

当前进程检查能够确认 `npu:1` 实际落在物理 NPU `5`。开发时不得直接把 Prometheus 的 `chip_id=5` 转换成 `MODEL_DEVICE=npu:5`。

应建立显式映射，并优先使用 PCI Bus 或设备 UUID 作为稳定身份，同时保留 exporter 的 `chip_id` 用于查询：

```text
cloud-slot-0 -> npu:0 -> chip_id=2 -> pcie_bus=0000:01:00.0
cloud-slot-1 -> npu:1 -> chip_id=5 -> pcie_bus=0000:81:00.0
```

上述映射是当前机器实例，部署启动时仍必须重新校验，不能把物理编号写死在通用代码中。

### 3.3 当前策略计算顺序

文件：`backend/app/services/schedule_orchestrator.py`

当前主要顺序为：

```text
accept_schedule_task
  -> process_schedule_task
     -> 采集 edge/cloud/network 指标
     -> 资源预检查
     -> 调用策略服务 /infer
     -> 保存 strategy_payload
     -> promote_next_queued_strategy_task
     -> dispatch_loading_task
        -> 预留 edge slot
        -> allocate_cloud_slot_for_task
        -> 启动 cloud decode 进程
        -> 向 edge/cloud 下发 /load_strategy
```

这意味着策略计算时尚未分配 cloud slot。虽然顶层指标代表当时最空闲的单卡，但后续 `allocate_cloud_slot_for_task()` 可能分配另一张卡。

此外，当前任务算完策略后会先推进下一个策略任务，再进入本任务的 cloud slot 分配。两个任务可能先后基于同一张“当时最空闲卡”计算策略，然后在 loading 阶段才竞争 slot。

`BERT-Base-Uncased` 当前由 backend 生成 `fixed_bert_encoder` 决策并跳过 `/infer`。它不受“策略基于错误显存视图”影响，但仍会在固定决策生成后才分配 slot，因此动态放置实现不能绕过 BERT；BERT 同样需要 placement lease、逐卡准入、启动前二次检查和设备一致性对账。

这是实现动态选卡时必须消除的时序不一致。

### 3.4 当前 cloud slot 选择规则

文件：`backend/app/services/schedule_orchestrator.py`

`allocate_cloud_slot_for_task()` 当前将候选分成：

1. `running_free`
2. `retained`
3. `stopped`

然后按照 `list_cloud_slots()` 返回的 `slot_index` 顺序依次尝试预留。它没有读取逐卡显存，也没有计算设备分数。

因此只要 `cloud-slot-0` 符合当前候选条件，就通常先使用 `cloud-slot-0`，与实时显存多少无关。

同会话模型重加载走 `_reload_session_owned_slots_for_task()` 和 `_reserve_cloud_slot()`，会优先复用原有 cloud slot，也绕过普通新任务的候选选择流程。第一版动态选卡应明确保留这一行为，避免在模型切换时同时引入跨设备迁移。

### 3.5 当前 slot 创建和启动行为

文件：

- `backend/app/services/managed_cloud_slot_bootstrap_service.py`
- `backend/app/services/decode_server_process_manager.py`

当前 backend 启动时主要确保 `cloud-slot-0` 存在并尝试启动。其他 slot 通常在已有候选均不可用时才按需创建。

这会带来两个问题：

- 第一个请求到达时，数据库中可能只有 `cloud-slot-0`，调度器没有第二张卡对应的候选记录可比较。
- `cloud-slot-0` 被预启动，资源和生命周期状态天然偏向第一张卡。

动态选卡实现时，应在 backend 启动阶段注册所有已配置设备对应的 slot 元数据，但是否预启动空 decode 进程可以继续由运行参数控制。注册 slot 不等于必须加载模型。

### 3.6 当前进程设备绑定

文件：`backend/app/services/decode_server_process_manager.py`

当前 `_model_device_for_cloud_slot(slot_index)` 使用：

```text
CLOUD_SLOT_NPU_DEVICES[slot_index]
```

生成 `MODEL_DEVICE=npu:<index>`，再启动独立 ModelSplit decode 进程。

这部分可继续复用，但后续不应只依赖 `slot_index` 临时计算设备。slot 的 `model_device` 应在数据库中持久化，进程管理器应接收并校验显式设备值。

### 3.7 ModelSplit 的设备行为

文件：

- `ModelSplit/app/services/decode_server/app.py`
- `ModelSplit/app/runtime/device.py`

decode 服务在模块导入时读取 `MODEL_DEVICE`，随后调用：

```python
torch.cuda.set_device(index)
```

或：

```python
torch.npu.set_device(f"npu:{index}")
```

因此：

- 一个已经启动的 decode 进程不能通过后续 `/load_strategy` 请求切换设备。
- 若设备变化，必须停止旧进程并使用新的 `MODEL_DEVICE` 重启。
- 固定 slot 与设备亲和关系符合当前 ModelSplit 的初始化方式。

当前 `/health` 和 `/runtime_state` 没有返回实际 device，调度器无法从 runtime 侧验证数据库设备记录是否真实生效，后续需要补充。

### 3.8 当前真实策略服务行为

当前正式策略服务位于 nss-d 的 `policy_api` 容器，对外端口为 `8050`。

检查其当前代码可知：

- 请求的 `env` 是通用字典，增加额外逐卡字段不会立即破坏 Pydantic 请求解析。
- `encode_state()` 不读取 `metrics.chips`。
- cloud 显存特征只使用 `gpu_mem_used_mb / gpu_mem_total_mb` 比例。
- 当前 27 维输入不包含 cloud 绝对可用显存。
- 当前 `is_cuda` 特征只识别以 `cuda` 开头的设备，`npu:0` 会被编码为非 CUDA。

因此，第一阶段可以维持现有模型输入维度，将顶层显存字段改为“已预留卡”的单卡指标；但如果要让策略模型真正理解不同容量的异构卡，需要增加特征并重新训练或校准策略 checkpoint。

## 4. 推荐架构

### 4.1 核心原则

1. **固定亲和、动态选择**：每个 cloud slot 固定对应一个逻辑设备；新任务动态选择 slot。
2. **先预留、后计算**：策略计算必须基于已经轻量预留的设备。
3. **设备选择由调度器负责**：策略服务负责层切分，不负责设备所有权和并发分配。
4. **逐卡准入**：单进程加载只能使用目标卡的显存，不使用整机求和或平均值。
5. **数据库是所有权真相源**：Prometheus 反映资源使用，数据库负责防止并发重复分配。
6. **启动前二次检查**：外部进程可能在策略计算期间占用显存，必须重新确认。
7. **失败必须释放 lease**：策略失败、取消、超时、进程失败和 backend 重启均不能留下永久预留。
8. **默认不超分**：一张卡默认最多绑定一个可加载模型的 cloud slot。

### 4.2 目标流程

```text
任务进入策略队列
  -> 采集 edge/network 和 cloud 逐卡指标（选卡路径强制新鲜数据）
  -> 将物理指标设备映射到固定 cloud slot / MODEL_DEVICE
  -> 过滤忙碌、故障、退避中、指标过期和显存不足的候选
  -> 按有效可用显存排序
  -> 原子创建 placement lease
  -> 生成模型: 使用已选卡指标请求 /infer
     BERT: 生成固定编码协议，不调用算法服务
  -> 保存策略和已选卡指标快照
  -> 模型启动前重新采集已选卡指标
  -> 资源仍满足要求：将 lease 转为正式 loading ownership
  -> 启动或复用该 slot 的 decode 进程
  -> 以 slot.model_device 设置 MODEL_DEVICE
  -> 向 edge/cloud 下发 /load_strategy
  -> runtime state 回报实际 device，reconcile 持续核对
```

如果二次检查失败：

```text
释放旧 lease
  -> 选择其他设备
  -> 生成模型使用新设备指标重新计算策略
     BERT 复用/重建与设备无关的固定协议
  -> 限制重试次数
  -> 无可用设备时进入 FIFO 等待
```

不得把基于设备 A 指标生成的策略直接下发给设备 B。

## 5. 数据模型设计

### 5.1 RuntimeSlot 固定设备字段

建议为 `RuntimeSlot` 增加：

| 字段 | 含义 |
|---|---|
| `model_device` | ModelSplit 使用的逻辑设备，例如 `npu:0` |
| `metric_chip_id` | exporter 暴露的卡号，例如 `2` |
| `metric_pcie_bus` | 稳定物理标识，例如 `0000:01:00.0` |

这些字段属于 slot 固定配置，slot 释放、模型卸载和会话结束时不能清空。

### 5.2 轻量 placement lease

不能直接把现有 `_reserve_cloud_slot()` 提前到策略计算之前。该函数会立即把 slot 改为：

```text
slot_state=bound
model_state=loading
```

而策略尚未生成，retained slot 中的旧模型也可能仍然存在。这样会让数据库状态提前进入 loading，并使失败恢复难以区分“只选了卡”和“已经开始加载”。

建议增加独立 lease 字段：

| 字段 | 含义 |
|---|---|
| `placement_task_id` | 当前轻量预留该设备的任务 |
| `placement_binding_id` | 对应 runtime binding |
| `placement_reserved_at` | 预留时间 |
| `placement_expires_at` | lease 到期时间 |
| `placement_metrics_json` | 选卡时的单卡指标快照 |

原子预留条件至少包括：

- 当前 slot 没有其他有效 placement lease；
- slot 不处于 bound/loading/needs_reconcile/failed；
- 当前 owner/binding 状态允许新任务使用；
- `retry_after` 已到期；
- task 和 binding 仍是有效、非终态记录。

策略成功并进入 loading 时，再将 lease 原子转换为现有正式 ownership：

```text
owner_session_id
owner_binding_id
task_id
slot_state=bound
model_state=loading
```

转换必须校验 `placement_task_id` 和 `placement_binding_id`，防止过期任务覆盖新任务。

### 5.3 ScheduleTask 历史记录

建议在任务记录中持久化：

- `selected_cloud_slot_id`
- `selected_cloud_model_device`
- `selected_cloud_metric_chip_id`
- `placement_metrics_json`

当前已有 `cloud_slot_id` 和 `allocated_cloud_slot_id`，可以在 lease 成功后写入其中一个，但应明确字段语义，避免把“已选卡”与“已经启动模型”混为一谈。

即使 slot 后续被其他任务复用，历史任务仍应能够说明当时实际使用了哪张卡。

### 5.4 配置格式

不建议仅依赖多个平行数组，因为数组顺序错误会产生静默错绑。推荐使用一组显式映射对象，至少包含：

```json
[
  {
    "slot_id": "cloud-slot-0",
    "model_device": "npu:0",
    "metric_chip_id": "2",
    "metric_pcie_bus": "0000:01:00.0"
  },
  {
    "slot_id": "cloud-slot-1",
    "model_device": "npu:1",
    "metric_chip_id": "5",
    "metric_pcie_bus": "0000:81:00.0"
  }
]
```

可以通过独立 JSON 配置文件加载，并由 `.env.prod` 指定路径。启动校验失败时应拒绝启用对应 slot，而不是自动猜测映射。

现有 `CLOUD_SLOT_NPU_DEVICES` 可暂时保留用于兼容，但新逻辑的真相源应只有一个，不能同时维护两套可能冲突的映射。

## 6. 指标与策略请求规范

### 6.1 建议的 cloud metrics 结构

保留旧字段以兼容现有策略模型，同时增加明确字段：

```json
{
  "gpu_util_percent": 0.0,
  "gpu_mem_total_mb": 65536.0,
  "gpu_mem_used_mb": 3399.0,
  "gpu_mem_free_mb": 62137.0,
  "accelerator_type": "ascend",
  "selected_chip_id": "2",
  "selected_runtime_device": "npu:0",
  "selected_pcie_bus": "0000:01:00.0",
  "sampled_at": "2026-07-17T00:00:00Z",
  "chips": [],
  "aggregate": {
    "total_mb": 131072.0,
    "used_mb": 13402.0,
    "free_mb": 117670.0
  }
}
```

语义要求：

- 顶层 `gpu_mem_*` 始终表示已预留的单张卡。
- `aggregate` 只用于展示和容量统计。
- `chips` 保留整机逐卡数据，策略服务可以忽略不认识的扩展字段。
- `sampled_at` 和指标年龄必须参与准入。
- 不再使用“顶层字段有时是单卡、有时是整机”的模糊回退。

### 6.2 有效可用显存

建议选卡时使用：

```text
effective_free_mb =
    measured_free_mb
    - scheduler_reserved_mb
    - safety_margin_mb
```

第一版默认一张卡一个 slot、不允许超分时，`scheduler_reserved_mb` 可以由 placement/ownership 是否存在转换成不可选状态；仍应保留安全余量，防止模型加载峰值和指标采集延迟导致 OOM。

候选必须先满足模型注册表中的 `cloud_min_free_gpu_mem_mb`，再比较剩余显存。

### 6.3 候选排序

先进行硬过滤，再排序：

硬过滤：

- 设备映射有效；
- 指标完整且未过期；
- slot 未被其他任务正式占用；
- placement lease 为空或已确认过期并回收；
- slot 不处于失败、对账或退避状态；
- 有效可用显存达到模型门槛。

推荐排序键：

1. `effective_free_mb` 降序；
2. 同会话合法复用优先；
3. `running + empty` 优先于需要启动进程的 stopped slot；
4. 最后用 `slot_index` 稳定排序。

对于 retained slot，第一版使用当前实测可用显存进行保守判断，不要未经测量就把旧模型可能释放的显存全部加回。后续若能可靠获得该 runtime 的实际模型占用，再引入“卸载后预计可用显存”。

### 6.4 策略模块职责

调度器请求策略服务时：

```text
env.cloud.device
```

必须是实际预留的 `npu:0` 或 `npu:1`，不能继续固定返回 `npu:0`。

`env.cloud.metrics.gpu_mem_*` 必须是该设备的单卡指标。

策略响应仍只需要返回 `layer_partitions`。不建议让策略服务返回设备号，因为策略服务没有数据库 slot 所有权、并发任务和进程生命周期信息。

该小节只适用于 `strategy_kind=algorithm`。`BERT-Base-Uncased` 不调用策略服务，但 backend 保存的任务指标快照仍应指向实际 lease 的设备，便于准入、审计和 dashboard 展示。

## 7. 调度流程改造重点

### 7.1 拆分“预留设备”和“启动进程”

当前 `allocate_cloud_slot_for_task()` 同时承担：

- 找候选；
- 正式预留 slot；
- 必要时停止残留进程；
- 启动 decode 进程；
- 等待健康检查。

动态选卡后建议拆分为：

```text
select_and_lease_cloud_accelerator()
convert_placement_lease_to_loading_ownership()
ensure_cloud_slot_process_started()
```

选择和 lease 必须短小，只在数据库原子操作期间持锁。不得在 `CLOUD_SLOT_ALLOCATION_LOCK` 内执行 Prometheus 网络请求、策略 HTTP 请求、进程启动或长时间健康检查。

推荐流程：先在锁外获取指标快照，再进入锁内重新检查数据库候选并完成条件 UPDATE。若锁内发现候选已变化，则使用同一快照尝试下一个候选或重新采集。

### 7.2 策略队列与设备等待队列

当前策略队列一次只允许一个任务处于 `running_strategy`。这个约束可以保留。

新的任务推进规则建议为：

- 没有可用设备时，不应先生成一个无法确定目标卡的策略。
- 引入语义明确的 `waiting_accelerator_placement` 状态，按 FIFO 重试选卡。
- placement lease 成功后才进入策略计算。
- 策略结束后保留 lease，直到正式转入 loading 或失败释放。
- 下一策略任务可以在前一任务完成策略后启动；此时前一任务的卡已被 lease，下一任务会自然选择其他卡。

不要让没有 `strategy_payload` 的任务进入现有 `dispatch_loading_task()`；也不要让基于旧设备指标的 waiting loading 任务直接换卡后继续加载。

### 7.3 同会话重加载

第一版保持以下规则：

- 同会话切换模型时继续复用原 edge/cloud slot。
- 使用原 cloud slot 对应设备的最新指标重新计算策略。
- 原 slot 忙、正在加载或正在推理时继续等待。
- 不在第一版实现“同会话从 npu:0 在线迁移到 npu:1”。

这样可以避免同时改变现有 `_reload_session_owned_slots_for_task()` 的卸载、旧 binding 释放和新 binding 接管语义。

跨设备迁移应作为后续独立功能设计。

### 7.4 二次检查与重选

在以下时间点再次采集已选卡指标：

1. 策略计算结束后、启动 decode 进程前；
2. ModelSplit 报告开始加载后，可记录一次用于诊断；
3. 模型加载失败且错误可能与 OOM 相关时。

如果第一次和第二次指标差异超过阈值，或第二次已不满足最低显存：

- 不启动进程；
- 条件释放当前 lease；
- 最多重新选择和重新计算策略有限次数；
- 仍无设备时进入等待队列。

### 7.5 进程启动

进程管理器应接收：

```text
slot_id
slot_index
model_device
metric_chip_id
```

并进行以下校验：

- 数据库 slot 的 `model_device` 与配置映射相同；
- 当前 task 的 placement lease/ownership 仍指向该 slot；
- `MODEL_DEVICE` 注入值与 slot 记录一致；
- 不允许调用方用任意字符串覆盖设备。

ModelSplit 进程启动后，应通过 `/runtime_state` 返回实际 device，backend 健康检查成功后再把 `process_state` 设为 `running`。

## 8. 并发与一致性要求

### 8.1 Prometheus 不能代替资源锁

两个并发任务可能读取到完全相同的逐卡快照，并同时认为同一张卡最空闲。因此：

- Prometheus 只用于排序和准入；
- placement lease/数据库条件 UPDATE 才负责唯一所有权；
- 第一个任务成功预留后，第二个任务必须在数据库层失败并尝试下一张卡。

### 8.2 不能只依赖 asyncio.Lock

当前 `CLOUD_SLOT_ALLOCATION_LOCK` 是进程内锁，在正式 backend 单进程运行时有效，但不能覆盖未来多 worker 或多个 backend 实例。

实现仍应保留数据库条件 UPDATE，锁只用于降低同一进程内的冲突概率。所有权正确性不能依赖锁本身。

### 8.3 lease 超时与重启恢复

placement lease 必须有过期时间。reconcile 每轮应处理：

- task 已终态但 lease 仍存在；
- binding 已释放但 lease 仍存在；
- `placement_expires_at` 已过期；
- backend 重启时遗留但没有进入 loading 的 lease；
- slot 实际进程设备与数据库设备不一致。

清理必须带 task/binding 条件，避免旧任务的延迟清理释放新任务的 lease。

### 8.4 失败回滚

以下路径都必须释放或恢复 placement：

- 策略 API 超时、4xx、5xx；
- 策略响应校验失败；
- 任务被同会话新模型取代；
- 用户会话关闭或过期；
- 指标二次检查失败；
- decode 进程启动失败或健康检查失败；
- edge slot 预留失败；
- edge/cloud `/load_strategy` 任一侧下发失败；
- backend shutdown 和 startup recovery。

对于 retained slot，失败回滚不能错误地把仍存在的旧模型标成 empty。placement 状态与模型生命周期状态分离是避免该问题的关键。

## 9. ModelSplit 配合修改

ModelSplit 不负责选卡，但需要提供可验证性。

建议修改：

1. `RuntimeLifecycle.get_runtime_state()` 增加 `device`。
2. `RuntimeStateResponse` 增加 `device`。
3. `/health` 可选增加 `device` 和 `slot_id`。
4. decode 启动日志固定记录：
   - `CLOUD_SLOT_ID`
   - `CLOUD_SLOT_INDEX`
   - `MODEL_DEVICE`
5. 模型加载回调中可附带 device；若不修改回调协议，至少保证 runtime state 可查询。
6. 单元测试覆盖 `MODEL_DEVICE=npu:1` 时实际调用 `torch.npu.set_device("npu:1")`。

设备在进程导入阶段确定，因此不得尝试通过 `/load_strategy` 修改运行中进程的设备。

## 10. 策略服务配合修改

### 10.1 兼容阶段

第一阶段保持现有 27 维模型输入和 checkpoint：

- backend 顶层 `gpu_mem_*` 改为已选卡指标；
- `env.cloud.device` 改为实际设备；
- 策略服务将 `cuda:*` 和 `npu:*` 都识别为 accelerator；
- 新增字段只用于日志和审计，暂不改变神经网络输入维度。

必须对“将 npu 视为 accelerator”在现有 checkpoint 上做回归验证，不能静默改变特征含义后直接上线。

### 10.2 模型升级阶段

若需要支持不同显存容量的异构卡，建议增加：

- cloud absolute free memory；
- cloud total memory；
- accelerator 类型 one-hot；
- 可选的加载队列或设备占用状态。

这会改变 `STATE_DIM`，必须：

- 版本化策略输入；
- 重新训练或迁移 checkpoint；
- 新旧模型并行回放同一批样本；
- 验证切分结果、显存可行性和推理时延。

当前两张 NPU 总显存相同，仅按空闲显存选择设备时，调度器完成绝对容量判断即可；不需要为了第一版选卡功能立即重训策略模型。

## 11. 建议的代码改动范围

### 11.1 splitwise_cloud

| 文件/模块 | 主要改动 |
|---|---|
| `backend/app/core/config.py` | 解析并校验 slot/逻辑设备/指标设备映射 |
| `backend/.env.example`、`.env.prod` | 增加映射配置路径或配置项 |
| `backend/app/models/models.py` | 增加设备亲和字段和 placement lease 字段 |
| `backend/app/core/lifespan.py` | SQLite 兼容迁移、启动校验和遗留 lease 恢复 |
| `backend/app/services/prometheus_metrics.py` | 明确 aggregate、逐卡、selected-card 语义和新鲜采集接口 |
| 新建 `accelerator_placement_service.py` | 映射、过滤、评分、原子 lease、转换和释放 |
| `backend/app/services/schedule_orchestrator.py` | 调整策略前选卡、等待队列、二次检查和失败重选流程 |
| `backend/app/services/runtime_startup_admission.py` | 按已选单卡执行资源准入 |
| `backend/app/services/decode_server_process_manager.py` | 使用 slot 持久化设备启动进程并校验 |
| `backend/app/services/managed_cloud_slot_bootstrap_service.py` | 注册所有配置 slot，不只注册 slot-0 |
| `backend/app/services/runtime_slot_reconcile_service.py` | 对账 lease、映射和 runtime 实际 device |
| `backend/app/services/managed_cloud_slot_cleanup_service.py` | 清理 ownership 时保留固定设备亲和字段 |
| `backend/app/services/schedule_presenter.py` | 管理接口展示设备和 placement 状态 |
| `backend/app/api/v1/admin_runtime.py` | dashboard/overview 展示选卡和异常 |

### 11.2 ModelSplit

| 文件/模块 | 主要改动 |
|---|---|
| `app/runtime/runtime_lifecycle.py` | runtime state 返回实际 device |
| `app/protocols/integrity_schemas.py` | 扩展 `RuntimeStateResponse` |
| `app/protocols/schedule_schemas.py` | 如扩展 health，增加 device/slot 字段 |
| `app/services/decode_server/app.py` | health/state 输出和启动日志 |
| `tests/` | 多设备环境变量和状态输出测试 |

### 11.3 外部策略服务

当前真实策略服务不在上述两个主仓库内，开发时需要同步修改 nss-d `policy_api` 对应源码和镜像：

- 正确识别 `npu`；
- 记录 selected device；
- 保持额外 metrics 字段兼容；
- 后续如增加状态特征，则版本化并重新生成镜像/checkpoint。

## 12. 测试计划

### 12.1 指标单元测试

- 两张等容量卡时选择 free MB 更大的卡。
- 异构容量卡时按绝对 free MB，而不是按使用率。
- used/total/free 缺字段时拒绝动态准入。
- 逐卡查询失败时不使用整机求和做单卡准入。
- chip_id、PCI Bus 与逻辑设备映射正确。
- 指标过期时拒绝使用。

### 12.2 placement 单元测试

- 卡 A 40GB、卡 B 50GB 时选择 B。
- B 被 lease 后第二个任务选择 A。
- 两张卡均不足时进入等待。
- lease 条件 UPDATE 失败后尝试下一候选。
- lease 过期可被 reconcile 回收。
- 旧 task 释放操作不能清除新 task 的 lease。
- retained slot 选卡失败时不破坏原模型状态。
- 同会话重加载保持原 slot。

### 12.3 调度集成测试

- 策略请求中的 selected device 与最终 cloud slot 一致。
- 策略请求中的顶层显存来自最终选中卡。
- BERT 跳过 `/infer`，但仍先获得 placement lease，并使用所选卡完成二次准入和 decode 启动。
- 策略失败后 lease 被释放。
- 二次检查失败后重新选卡并重新请求策略。
- 新卡重算失败后任务进入等待或失败终态，不遗留进程。
- edge 预留失败时 cloud placement 被释放。
- cloud 进程启动失败时 task/binding/slot 全部正确回滚。
- backend 重启可以恢复或清理遗留 lease。

### 12.4 ModelSplit 测试

- `MODEL_DEVICE=npu:0` 和 `npu:1` 分别设置正确设备。
- runtime state 返回实际 device。
- 数据库期望设备与 runtime 返回设备不一致时，backend 标记异常而不是继续加载。

### 12.5 正式环境验收

1. 控制两张卡显存占用差异，发起一个新任务，确认选择空闲显存更多的卡。
2. 反转两张卡占用，再次发起任务，确认选择随资源变化。
3. nss-m、nss-d 同时发起完整请求，确认两个任务分别获得不同 cloud slot/NPU。
4. 同时核对：
   - Prometheus 逐卡指标；
   - `schedule_tasks`；
   - `runtime_bindings`；
   - `runtime_slots`；
   - decode 进程环境变量；
   - ModelSplit `/runtime_state`；
   - `npu-smi info`。
5. 注入策略超时、进程启动失败、指标缺失和会话关闭，确认没有 orphan lease、orphan process 或错误 slot ownership。

验收必须以完整模型选择、完整性检查、模型加载和边端数据面请求成功为终点：生成模型验证 chat completion，BERT 验证 768 维 embedding；不能只验证进程启动。

## 13. 推荐开发顺序

### 阶段 A：指标和映射

1. 定义明确的设备映射配置。
2. 启动时校验逻辑设备、chip_id 和 PCI Bus。
3. 重构 metrics 数据结构，区分 selected、chips 和 aggregate。
4. 完成指标测试。

### 阶段 B：数据模型和 placement service

1. 增加 slot 固定设备字段。
2. 增加 placement lease 和超时字段。
3. 实现原子 lease、转换、释放和过期回收。
4. 完成并发与回滚测试。

### 阶段 C：调度主流程

1. 将新任务选卡移动到策略请求之前。
2. 增加设备等待状态和 FIFO 推进。
3. 策略请求改用已选卡视图。
4. 增加启动前二次检查和有限重选。
5. 保持同会话重加载固定 slot。

### 阶段 D：进程和 runtime 可验证性

1. 进程管理器使用持久化 `model_device`。
2. ModelSplit state/health 返回实际 device。
3. reconcile 校验期望设备与实际设备。
4. dashboard 展示 placement 和设备信息。

### 阶段 E：策略服务兼容

1. 修复 NPU accelerator 特征语义并回归现有 checkpoint。
2. 验证 selected-card 顶层指标。
3. 再决定是否升级输入维度和重训模型。

### 阶段 F：部署和正式链路测试

1. 备份正式数据库。
2. 部署 schema 和配置映射。
3. 重启 backend，并验证 slot 映射和 reconcile。
4. 单请求验证动态选卡。
5. 双边端并发验证。
6. 故障注入和恢复验证。

## 14. 开发时必须特别注意

- 不要把 Prometheus `chip_id` 直接当成 `npu:<index>`。
- 不要继续让顶层显存字段在“单卡”和“整机求和”之间静默切换。
- 不要在网络请求或进程健康等待期间持有全局 slot 分配锁。
- 不要只增加排序而忽略数据库原子预留。
- 不要在策略计算前直接调用现有 `_reserve_cloud_slot()`。
- 不要在 slot 清理时清空固定设备映射。
- 不要在换卡后复用基于上一张卡生成的切分策略。
- 不要让旧任务的失败清理释放新任务的 lease 或进程。
- 不要把运行中 decode 进程的设备视为可热切换参数。
- 不要修改策略输入维度后继续无验证地使用旧 checkpoint。
- 不要只做 mock 端口测试；最终必须验证真实模型加载、完整性检查和推理。

## 15. 第一版功能边界

第一版应实现：

- 两张云端 NPU 中按有效可用显存选择一张；
- 一个 cloud slot 固定绑定一张 NPU；
- 两个并发任务可以安全使用不同 NPU；
- 单卡显存不足时选择另一张卡；
- 两张卡都不可用时进入等待；
- 策略输入、数据库、进程环境和 runtime state 的设备一致。

第一版不包含：

- 同一个模型跨两张卡进行 Tensor Parallel 或 Pipeline Parallel；
- 一个运行中 decode 进程热切换设备；
- 同会话加载过程中跨卡迁移；
- 默认允许多个模型 slot 超分同一张卡；
- 未经重训直接改变策略神经网络输入维度。

完成上述边界后，再评估异构卡、同卡多模型和跨卡模型并行。
