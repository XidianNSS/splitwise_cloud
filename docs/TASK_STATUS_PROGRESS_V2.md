# 任务状态查询变化说明（前端三段式加载进度版）

本文档用于补充说明当前 `/api/v1/schedule/tasks/{task_id}` 与旧版对接文档
`docs/对接/EDGE_FRONTEND_INTEGRATION.md` 相比，**任务状态查询结果有哪些变化**，以及边端前端应该如何读取新的加载进度字段。

适用目标：
- 边端前端继续展示“边端”和“云端”两张卡片
- 每张卡片保留 **1 条总进度条**
- 每张卡片下展示 3 行子进度：
  1. 切分策略加载
  2. 模型完整性检验
  3. 模型加载

---

## 1. 本次变化概览

相较旧版，`GET /api/v1/schedule/tasks/{task_id}` 的**接口地址没有变化**，但返回结构新增了 6 个字段，用于支持三段式加载进度展示：

- `edge_strategy_progress`
- `edge_integrity_progress`
- `edge_runtime_load_progress`
- `cloud_strategy_progress`
- `cloud_integrity_progress`
- `cloud_runtime_load_progress`

同时：

- `edge_progress`
- `cloud_progress`

这两个字段的含义也发生了变化。

### 旧版含义
旧版中：
- `edge_progress` / `cloud_progress` 更接近 runtime 回调的单一加载进度
- 前端只能粗粒度显示“边端加载中 / 云端加载中”

### 新版含义
新版中：
- `edge_progress` / `cloud_progress` 变成**卡片总进度**
- 总进度由三段式子进度按固定权重计算：
  - 切分策略准备和下发：40%
  - 模型完整性检验：30%
  - 模型加载：30%

也就是：

```text
edge_progress = round(edge_strategy_progress * 0.4 + edge_integrity_progress * 0.3 + edge_runtime_load_progress * 0.3)
cloud_progress = round(cloud_strategy_progress * 0.4 + cloud_integrity_progress * 0.3 + cloud_runtime_load_progress * 0.3)
```

因此：
- `edge_progress` / `cloud_progress` 继续适合作为大进度条展示
- 新增的 6 个字段适合作为三行子项展示

---

## 2. 不变的部分

以下内容和旧版保持一致：

### 2.1 查询接口不变

```http
GET /api/v1/schedule/tasks/{task_id}
Authorization: Bearer <openwebui_token>
```

### 2.2 SSE 接口不变

```http
GET /api/v1/schedule/tasks/{task_id}/stream?token=<openwebui_token>
```

SSE 推送的 `data` 内容与 `/tasks/{task_id}` 返回结构保持一致，因此前端如果已经接入 SSE，只需要补读新增字段即可。

### 2.3 任务主状态机不变
backend 内部仍然使用：
- `phase = strategy`
- `phase = loading`
- `phase = completed`

因此前端原有基于以下字段的逻辑仍可保留：
- `status`
- `phase`
- `phase_progress`
- `overall_progress`
- `message`
- `error_detail`

---

## 3. 新增字段说明

### 3.1 边端侧新增字段

#### `edge_strategy_progress`
边端卡片的“切分策略加载”子进度，范围 `0-100`。

说明：
- 由 backend 统一驱动
- 与云端侧的 `cloud_strategy_progress` 始终同步
- 表示当前任务的：
  - 用户校验
  - 环境指标采集
  - 算法策略请求
  - 策略准备和下发前置阶段

#### `edge_integrity_progress`
边端卡片的“模型完整性检验”子进度，范围 `0-100`。

典型阶段：
- 结构校验开始
- 结构校验通过
- digest 准备完成
- 等待云端确认
- 云端确认通过

#### `edge_runtime_load_progress`
边端卡片的“模型加载”子进度，范围 `0-100`。

典型阶段：
- adapter created
- tokenizer loaded
- model loaded
- executor created

---

### 3.2 云端侧新增字段

#### `cloud_strategy_progress`
云端卡片的“切分策略加载”子进度，范围 `0-100`。

说明：
- 与 `edge_strategy_progress` 同步
- 因为切分策略准备和下发属于同一份调度任务的公共阶段

#### `cloud_integrity_progress`
云端卡片的“模型完整性检验”子进度，范围 `0-100`。

典型阶段：
- 结构校验开始
- 结构校验通过
- digest 准备完成
- 向 scheduler 报告确认
- 确认完成

#### `cloud_runtime_load_progress`
云端卡片的“模型加载”子进度，范围 `0-100`。

典型阶段：
- adapter created
- tokenizer loaded
- model loaded
- executor created

---

## 4. 当前建议前端重点关注的字段

相较旧版，前端现在建议重点读取这些字段：

### 4.1 通用状态字段
- `status`
- `phase`
- `phase_progress`
- `overall_progress`
- `message`
- `error_detail`

### 4.2 边端卡片字段
- `edge_progress`
- `edge_strategy_progress`
- `edge_integrity_progress`
- `edge_runtime_load_progress`
- `edge_status`
- `edge_message`

### 4.3 云端卡片字段
- `cloud_progress`
- `cloud_strategy_progress`
- `cloud_integrity_progress`
- `cloud_runtime_load_progress`
- `cloud_status`
- `cloud_message`

---

## 5. 建议的前端展示方式

### 5.1 边端卡片

总进度条：
- 使用 `edge_progress`

三行子项：
- 切分策略加载：`edge_strategy_progress`
- 模型完整性检验：`edge_integrity_progress`
- 模型加载：`edge_runtime_load_progress`

辅助文案可优先使用：
- `edge_message`

---

### 5.2 云端卡片

总进度条：
- 使用 `cloud_progress`

三行子项：
- 切分策略加载：`cloud_strategy_progress`
- 模型完整性检验：`cloud_integrity_progress`
- 模型加载：`cloud_runtime_load_progress`

辅助文案可优先使用：
- `cloud_message`

---

## 6. 与旧版文档相比，前端需要改什么

旧版 `EDGE_FRONTEND_INTEGRATION.md` 中，前端主要关注：

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

现在建议前端在原有基础上，**新增读取下面 6 个字段**：

- `edge_strategy_progress`
- `edge_integrity_progress`
- `edge_runtime_load_progress`
- `cloud_strategy_progress`
- `cloud_integrity_progress`
- `cloud_runtime_load_progress`

如果前端暂时还没来得及接这 6 个字段：
- 旧版逻辑仍能工作
- 只是只能继续显示粗粒度总进度，无法展示三段式加载明细

---

## 7. 示例返回

下面是一个典型的 `GET /api/v1/schedule/tasks/{task_id}` 返回示例：

```json
{
  "task_id": "task-123",
  "status": "running",
  "phase": "loading",
  "phase_progress": 55,
  "overall_progress": 77,
  "message": "waiting cloud confirmation",
  "edge_progress": 82,
  "cloud_progress": 79,
  "edge_strategy_progress": 100,
  "edge_integrity_progress": 80,
  "edge_runtime_load_progress": 60,
  "cloud_strategy_progress": 100,
  "cloud_integrity_progress": 70,
  "cloud_runtime_load_progress": 60,
  "edge_status": "loading",
  "cloud_status": "loading",
  "edge_message": "waiting cloud confirmation",
  "cloud_message": "reporting confirmation to scheduler",
  "queue_status": "loading_running",
  "queue_position": 0,
  "runtime_binding_id": "binding-123",
  "edge_slot_id": "edge-slot-edge_A",
  "cloud_slot_id": "cloud-slot-0",
  "allocated_cloud_slot_id": "cloud-slot-0",
  "error_detail": null,
  "created_at": "2026-05-21T10:00:00",
  "updated_at": "2026-05-21T10:00:05"
}
```

---

## 8. 进度语义说明

### 8.1 为什么总进度不是直接等于某一个子进度
因为前端当前每张卡片只有 1 条总进度条，但真实加载过程希望拆成三段展示：

1. 切分策略准备和下发
2. 模型完整性检验
3. 模型加载

所以 backend 现在采用固定权重聚合。

### 8.2 三段权重固定为
- 策略：40%
- 完整性：30%
- 模型加载：30%

前端**不需要自己重新计算**，直接使用 backend 返回的：
- `edge_progress`
- `cloud_progress`

即可。

如果前端希望自己做一致性校验，可以按本文档中的加权公式计算。

---

## 9. 兼容性说明

### 9.1 对旧前端兼容
旧前端如果只读取：
- `edge_progress`
- `cloud_progress`
- `message`

依然可以工作。

### 9.2 对新前端推荐
新前端推荐读取新增的 6 个字段，以实现你当前界面设计中的：
- 1 条总进度条
- 3 行子进度

---

## 10. 建议前端接入顺序

建议边端前端同学按以下顺序开发：

1. 保留旧版任务轮询 / SSE 逻辑不动
2. 在卡片中继续使用：
   - `edge_progress`
   - `cloud_progress`
3. 新增三行子项，分别渲染：
   - `edge_strategy_progress`
   - `edge_integrity_progress`
   - `edge_runtime_load_progress`
   - `cloud_strategy_progress`
   - `cloud_integrity_progress`
   - `cloud_runtime_load_progress`
4. 文案继续优先使用：
   - `edge_message`
   - `cloud_message`
5. 失败态继续使用：
   - `status == "failed"`
   - `message`
   - `error_detail`

---

## 11. 与旧文档最关键的差异总结

如果只记一件事，请记这句：

**现在 `/api/v1/schedule/tasks/{task_id}` 不再只返回粗粒度的边端/云端总进度，而是额外返回 6 个三段式子进度字段，前端应优先用它们来展示“切分策略加载 / 模型完整性检验 / 模型加载”。**

