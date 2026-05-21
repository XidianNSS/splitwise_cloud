# Splitwise 云端后端

这个仓库是 Splitwise 边云推理体系中的 scheduler / cloud backend。

它负责：
- 接收前端调度请求
- 识别边端 / 云端设备归属
- 采集设备与网络状态
- 请求算法服务生成切分策略
- 向边端 / 云端 runtime 下发加载策略
- 跟踪 runtime 加载进度与任务状态
- 管理云端 slot / binding 生命周期

---

## 当前能力范围

当前代码已经支持：
- 面向 OpenWebUI 前端请求的 session 初始化
- task-based 调度
- 同步算法决策请求
- 动态 runtime 策略下发
- Aloepri 完整性确认中转
- 云端 decode slot 自动托管
- backend 启动恢复与周期 reconcile
- 面向前端展示的三段式加载进度

---

## 主要目录

- `backend/app/`：后端主程序
- `backend/.env.example`：backend 环境模板
- `backend/.env.dev`：通用开发 profile
- `backend/.env.wyy`：WYY 开发 profile
- `backend/.env.prod`：正式试运行 / 生产风格 profile
- `scripts/run_server.sh`：主要启动入口
- `scripts/run_server_dev.sh`：`.env.dev` 的便捷启动包装脚本
- `tests/`：mock 与回归测试
- `data/`：SQLite 数据文件

---

## 核心模块

### API 层
- `backend/app/api/v1/session.py`
  - session init / heartbeat / close
- `backend/app/api/v1/schedule.py`
  - schedule trigger
  - 任务状态查询
  - 策略查询
  - runtime callback 接口
  - runtime slot / binding 可观测接口
- `backend/app/api/v1/devices.py`
- `backend/app/api/v1/auth.py`
- `backend/app/api/v1/users.py`

### Service 层
当前比较关键的服务包括：

- `schedule_orchestrator.py`
- `algorithm_dispatcher.py`
- `runtime_dispatcher.py`
- `runtime_startup_admission.py`
- `schedule_queue.py`
- `schedule_recovery.py`
- `schedule_task_service.py`
- `schedule_presenter.py`
- `network_probe.py`
- `prometheus_metrics.py`
- `model_registry.py`
- `decode_server_process_manager.py`
- `managed_cloud_slot_bootstrap_service.py`
- `runtime_slot_service.py`
- `runtime_binding_service.py`
- `runtime_control_service.py`
- `runtime_slot_reconcile_service.py`
- `startup_recovery_service.py`
- `slot_reaper.py`
- `session_lease_service.py`

### 基础设施层
- `backend/app/core/config.py`
- `backend/app/core/env_loader.py`
- `backend/app/core/lifespan.py`
- `backend/app/core/security.py`

### 持久化与 schema
- `backend/app/db/`
- `backend/app/models/`
- `backend/app/schemas/`

---

## 当前主流程

1. 前端调用 `POST /api/v1/session/init`
2. 前端调用 `POST /api/v1/schedule/trigger`
3. backend 采集边端 / 云端指标与网络指标
4. backend 通过 `POST /infer` 请求算法决策
5. backend 向边端和云端 runtime 下发 `POST /load_strategy`
6. runtime 通过 callback API 回传加载进度
7. 前端通过 `GET /api/v1/schedule/tasks/{task_id}` 或 SSE 查看任务状态

---

## 任务状态与进度

backend 内部仍保留主 phase：
- `strategy`
- `loading`
- `completed`

但为了前端展示，当前任务状态已经支持边端和云端各自的三段式加载进度：
- strategy progress
- integrity progress
- runtime load progress

总进度的权重为：
- strategy：40%
- integrity：30%
- runtime load：30%

最终聚合到：
- `edge_progress`
- `cloud_progress`

同时任务状态响应中也会返回各阶段子进度字段。

---

## Runtime 对接方式

当前 backend 与 runtime 的主要接口为：
- 固定控制入口：`POST /load_strategy`
- 进度回调：
  - `POST /api/v1/schedule/runtime_callback/edge`
  - `POST /api/v1/schedule/runtime_callback/cloud`
- Aloepri 完整性确认中转：
  - `POST /api/v1/schedule/runtime/confirmation/cloud`

在云端 decode 路由上，backend 现在使用 task-level `runtime_route`，而不是假设整个系统只有一个静态 cloud decode 目标。

---

## 云端 slot 管理

当前 backend 已支持：
- `cloud-slot-0` 自动 bootstrap
- 动态分配额外 cloud slot
- 空闲 managed cloud slot 自动 stop
- runtime slot reconcile
- startup ownership recovery
- stale task / session 的 binding / slot 清理

这意味着在单云端主机模式下，backend 已经可以直接管理 cloud decode slot 池。

---

## 环境 profile

当前 backend env 文件包括：
- `.env.example`：参考模板
- `.env.dev`：通用开发 profile
- `.env.wyy`：WYY 开发 profile
- `.env.prod`：正式试运行 / 生产风格 profile

当前 env 选择规则：
- `scripts/run_server.sh` 默认读取 `backend/.env`
- 若设置了 `BACKEND_ENV_FILE`，则优先读取该文件
- `scripts/run_server_dev.sh` 只是把 `BACKEND_ENV_FILE=backend/.env.dev` 包了一层

---

## 启动 backend

### 默认 / 正式 env

```bash
cd /path/to/splitwise_cloud
source venv/bin/activate
bash scripts/run_server.sh
```

### 显式指定 env 文件

```bash
cd /path/to/splitwise_cloud
source venv/bin/activate
BACKEND_ENV_FILE=backend/.env.wyy bash scripts/run_server.sh
```

### 开发便捷启动

```bash
cd /path/to/splitwise_cloud
source venv/bin/activate
bash scripts/run_server_dev.sh
```

当前 backend 启动脚本也已把 env 文件视为固定配置：
- 若配置端口被占用，启动会直接失败
- 不再自动改写 env 文件去顺延端口

---

## 测试 / Mock 工具

常用 mock 工具包括：
- `mock_edge_client.py`
- `mock_algorithm_server.py`
- `mock_edge_runtime_server.py`
- `mock_cloud_runtime_server.py`

backend 编译检查：

```bash
cd backend
../venv/bin/python -m compileall app
```

运行 backend 回归测试：

```bash
cd /path/to/splitwise_cloud
./venv/bin/python -m unittest tests.test_session_and_slot_lifecycle -v
```

---

## 对接文档

按角色查看：

前端：
- `docs/对接/EDGE_FRONTEND_INTEGRATION.md`
- `docs/对接/TASK_STATUS_PROGRESS_V2.md`

Runtime 开发者：
- `docs/对接/RUNTIME_CONTROL_PORT_INTEGRATION.md`

算法服务开发者：
- `docs/对接/ALGORITHM_STRATEGY_INTEGRATION_BRIEF.md`
