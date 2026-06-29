# 云端运行态页面开发指导文档

## 1. 当前前端页面确认

当前 `splitwise_cloud` 前端是一个轻量单页应用，入口文件为：

- `frontend/dashboard.html`
- `frontend/dashboard.css`
- `frontend/dashboard.js`
- `backend/app/web/dashboard.py`

后端在访问 `/` 时返回 `frontend/dashboard.html`，静态资源挂载在 `/static`。

当前页面内已经存在两个视图：

1. `view-hardware`
   - 导航名称：`硬件监控大盘`
   - 当前已开发完成。
   - 主要功能是通过设备选择器切换 Grafana iframe。
   - 相关前端函数主要是 `loadSystemDevices()`、`switchGrafanaDevice()`。

2. `view-sandbox`
   - 导航名称：`协同推理沙盘`
   - 当前是待开发占位页。
   - 页面内容目前只有“沙盘核心交互区域 (待开发)”。
   - 后续云端运行态展示页面应在这个视图内开发。

后续开发必须避免把两个页面混在一起：

- 不要修改 `view-hardware` 内的 Grafana iframe 和设备选择逻辑，除非任务明确要求调整硬件监控页面。
- 不要复用 `custom-device-selector`、`grafana-frame` 等硬件监控页元素承载运行态信息。
- 建议给待开发页新增独立 DOM 前缀，例如 `runtime-*`、`cloud-runtime-*`、`slot-*`。
- 建议给待开发页新增独立函数前缀，例如 `loadRuntimeOverview()`、`renderCloudSlots()`、`renderTaskTimeline()`。

## 2. 待开发页面定位

建议将 `view-sandbox` 从“协同推理沙盘”明确升级为：

**云端运行态总览页面**

该页面关注的是 `splitwise_cloud` 正在托管和调度的运行时资源，而不是硬件指标本身。硬件显存、NPU 利用率仍由第一个 Grafana 监控页负责；本页面只展示与调度、模型加载、decode 服务托管相关的信息。

页面核心问题应回答：

- 云端当前托管了哪些 cloud decode_server？
- 每个 decode_server 是否运行、监听哪个 HTTP/gRPC 端口、PID 是多少？
- 每个 decode_server 是否加载了模型？
- 每个 decode_server 正在被哪个边端 session 或绑定使用？
- 当前任务处于策略计算、完整性校验、模型加载、ready 等哪个阶段？
- 当前是否有等待 cloud slot 的任务？
- 当前是否存在异常，例如 slot 进程停止、模型加载失败、完整性确认失败、gRPC 不可用。

## 3. 建议展示模块

### 3.1 顶部概览卡片

建议展示 4 到 6 个摘要指标：

- Cloud slot 总数
- Running cloud slot 数
- Ready model slot 数
- Bound slot 数
- Waiting queue 数
- Active request 数

这些指标主要来自：

- `RuntimeSlot.process_state`
- `RuntimeSlot.model_state`
- `RuntimeSlot.slot_state`
- `RuntimeSlot.active_request_count`
- loading queue 数量

### 3.2 Cloud decode_server 托管列表

这是页面最核心的区域，建议用表格或卡片展示所有 `role=cloud` 的 runtime slot。

建议字段：

- `slot_id`
- `slot_index`
- `process_state`
- `model_state`
- `slot_state`
- `model_type`
- `task_id`
- `owner_session_id`
- `owner_binding_id`
- `process_pid`
- `control_url`
- `grpc_target`
- `active_request_count`
- `integrity_status`
- `confirmation_status`
- `last_used_at`
- `updated_at`

建议状态解释：

- `process_state=running`：decode_server 进程正在运行。
- `process_state=stopped`：slot 已登记但进程未启动。
- `model_state=empty`：进程没有加载模型。
- `model_state=loading`：模型正在加载或等待完整性阶段完成。
- `model_state=ready`：模型已经可用。
- `slot_state=free`：可被新任务分配。
- `slot_state=bound`：已绑定到某个 session/binding。
- `slot_state=needs_reconcile`：运行态与数据库状态可能不一致，需要重点提示。

### 3.3 Edge 与 Cloud 绑定关系

建议展示当前 `RuntimeBinding` 列表，重点看一条 session 如何绑定 edge slot 与 cloud slot。

建议字段：

- `binding_id`
- `session_id`
- `task_id`
- `edge_slot_id`
- `cloud_slot_id`
- `partition_digest`
- `status`
- `created_at`
- `updated_at`

展示方式可以是：

- 左侧 edge slot，右侧 cloud slot，中间用连线或箭头表示绑定。
- 或简单表格先实现，后续再升级拓扑图。

该区域用于回答：

“这个 cloud decode_server 当前是给哪个边端设备或 session 使用的？”

当前 runtime slot 里只有 `owner_session_id`，没有直接存边端设备 IP。若页面需要展示 `nss-m`、`nss-d` 这类可读名称，需要通过 session 或 device 表补充映射接口。

### 3.4 当前调度任务与阶段进度

建议展示 loading/waiting 队列和最近任务。

已有任务状态字段比较适合做阶段进度条：

- `phase`
- `phase_progress`
- `overall_progress`
- `message`
- `edge_status`
- `cloud_status`
- `edge_message`
- `cloud_message`
- `edge_strategy_progress`
- `cloud_strategy_progress`
- `edge_integrity_progress`
- `cloud_integrity_progress`
- `edge_runtime_load_progress`
- `cloud_runtime_load_progress`
- `queue_status`
- `queue_position`
- `error_detail`

建议将任务阶段映射为以下中文状态：

- `strategy`：切分策略计算阶段
- `loading`：模型加载阶段
- `completed`：模型加载完成，等待推理或已经可推理
- `failed`：任务失败

更细的阶段建议根据 progress 字段组合展示：

- 策略计算：`edge_strategy_progress`、`cloud_strategy_progress`
- 模型完整性检验：`edge_integrity_progress`、`cloud_integrity_progress`
- 模型加载：`edge_runtime_load_progress`、`cloud_runtime_load_progress`
- 推理就绪：slot 的 `model_state=ready` 且 runtime_state 中 `ready=true`

### 3.5 异常告警区

建议在页面右侧或顶部展示异常列表。

告警规则建议：

- `process_state != running` 且 `slot_state=bound`：绑定中的 slot 进程异常。
- `slot_state=needs_reconcile`：状态需要 reconcile。
- `model_state=loading` 超过预期时间：模型加载可能卡住。
- `confirmation_status=failed`：完整性确认失败。
- `integrity_status=failed`：完整性校验失败。
- `task.status=failed`：调度任务失败，展示 `error_detail`。
- `active_request_count > 0`：slot 正在推理，不应被卸载。

## 4. 现有后端接口与缺口

当前已有接口：

- `GET /api/v1/schedule/runtime/slots`
- `GET /api/v1/schedule/runtime/bindings`
- `GET /api/v1/schedule/queue/loading`
- `GET /api/v1/schedule/tasks/{task_id}`
- `GET /api/v1/schedule/tasks/{task_id}/strategy`
- `GET /api/v1/schedule/tasks/{task_id}/stream`

但要注意一个关键点：

当前 `schedule` 相关接口依赖的是 OpenWebUI token，即 `get_current_openwebui_user_id`。而当前云端前端登录使用的是内部管理员 token，即 `/api/v1/login` 返回的 token。这意味着 `view-sandbox` 如果直接复用现有管理员登录态调用 `schedule` 接口，可能会鉴权失败。

因此建议新增一个管理员只读总览接口，不要让前端直接绕过鉴权或混用 OpenWebUI token。

建议新增接口：

`GET /api/v1/admin/runtime/overview`

鉴权：

- 使用 `get_current_admin`
- 只读
- 不改变调度状态
- 不启动或停止任何 runtime

建议返回结构：

```json
{
  "cloud_slots": [],
  "edge_slots": [],
  "bindings": [],
  "loading_queue": [],
  "recent_tasks": [],
  "summary": {
    "cloud_slot_total": 0,
    "cloud_slot_running": 0,
    "cloud_slot_ready": 0,
    "cloud_slot_bound": 0,
    "active_request_total": 0,
    "waiting_task_total": 0,
    "failed_task_total": 0
  }
}
```

如果需要展示 decode_server 的实时 runtime_state，可以后端在该接口里对每个 running cloud slot 的 `control_url` 派生 `/runtime_state` 做短超时请求，并增加：

```json
{
  "runtime_state": {
    "ready": true,
    "draining": false,
    "task_id": "...",
    "model_type": "Llama-3.2-3B-Instruct",
    "active_request_count": 0,
    "integrity_verified": true,
    "confirmation_passed": true
  },
  "runtime_state_error": null
}
```

该请求必须设置短超时，例如 1 到 2 秒，避免页面总览被单个异常 runtime 卡死。

## 5. 推荐页面布局

建议 `view-sandbox` 使用以下结构：

1. 顶部摘要区
   - 云端 slot 总数
   - 运行中 decode_server 数
   - ready 模型数
   - active request 总数
   - loading/waiting 任务数

2. 主区域左侧：Cloud decode_server 卡片列表
   - 每个 cloud slot 一张卡
   - running/free/ready/bound 用不同颜色标签
   - 显示 HTTP/gRPC/PID/model/task/session

3. 主区域右侧：当前任务阶段时间线
   - 展示最近 running/loading/completed/failed 任务
   - 对每个任务展示 strategy、integrity、runtime_load 三段进度
   - edge 与 cloud 两列对比

4. 底部：绑定关系表
   - session_id
   - edge_slot_id
   - cloud_slot_id
   - task_id
   - binding status

5. 告警条
   - 显示 needs_reconcile、failed、stopped-bound、confirmation failed 等异常。

## 6. 状态文案建议

建议前端统一封装状态翻译函数，避免后续页面散落大量 if/else。

推荐映射：

```js
const PHASE_LABELS = {
  strategy: "切分策略计算",
  loading: "模型加载与完整性确认",
  completed: "加载完成，等待推理",
  failed: "失败"
};

const MODEL_STATE_LABELS = {
  empty: "未加载模型",
  loading: "模型加载中",
  ready: "模型已就绪"
};

const SLOT_STATE_LABELS = {
  free: "空闲",
  bound: "已绑定",
  unloading: "卸载中",
  needs_reconcile: "状态待校正"
};

const PROCESS_STATE_LABELS = {
  running: "进程运行中",
  stopped: "进程未启动",
  failed: "进程异常"
};
```

## 7. 前端开发边界

建议新增或修改：

- 在 `frontend/dashboard.html` 中替换 `view-sandbox` 内部占位内容。
- 在 `frontend/dashboard.js` 中新增 runtime overview 数据加载和渲染函数。
- 在 `frontend/dashboard.css` 中新增 runtime 页面样式，类名前缀建议为 `.runtime-*`。

不建议修改：

- `view-hardware` 的 DOM 结构。
- `switchGrafanaDevice()`。
- `loadSystemDevices()` 的 Grafana 设备切换职责。
- 登录、管理员账号、设备 CRUD 逻辑，除非是为了抽取公共请求函数。

## 8. 推荐实现步骤

第一步：新增后端管理员只读接口。

- 新增 `backend/app/api/v1/admin_runtime.py` 或类似文件。
- 使用 `get_current_admin` 鉴权。
- 汇总 runtime slots、bindings、loading queue、recent tasks。
- 初版先只读数据库，不主动访问 runtime 进程。

第二步：实现前端静态布局。

- 替换 `view-sandbox` 的占位内容。
- 先使用 mock 数据渲染页面，确认视觉结构不影响硬件监控页。

第三步：接入真实接口。

- 使用现有 `fetchWithAuth()` 调用管理员只读总览接口。
- 页面进入 `view-sandbox` 时立即刷新一次。
- 增加手动刷新按钮。
- 可选增加 3 到 5 秒自动刷新，但进入硬件监控页时应停止自动刷新。

第四步：增加 runtime_state 实时状态。

- 后端对 running slot 调用 `/runtime_state`。
- 前端展示 `ready`、`draining`、`active_request_count`、`integrity_verified`、`confirmation_passed`。

第五步：增强任务阶段可视化。

- 根据 task progress 字段展示 strategy、integrity、runtime load 三段进度。
- 对 failed task 展示 `error_detail`。

## 9. 验收标准

页面开发完成后应满足：

- 硬件监控大盘仍能正常切换 Grafana 设备。
- 协同推理沙盘页面不再是占位内容。
- 页面能展示当前 cloud decode_server 列表。
- 页面能展示每个 decode_server 的进程状态、模型状态、绑定 session、task、HTTP/gRPC 地址。
- 页面能展示任务所处阶段：切分策略计算、完整性校验、模型加载、ready/等待推理、失败。
- 页面能识别并突出异常 slot 或失败任务。
- 页面使用管理员登录 token 即可访问，不依赖 OpenWebUI token。
- 所有接口只读，不触发调度、不启动/停止 decode_server、不修改数据库状态。

