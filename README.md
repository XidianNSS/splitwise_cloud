# splitwise_cloud

`splitwise_cloud` 是 ModelSplit 边云推理系统的 scheduler/backend。它不执行推理，而是管理用户会话、策略计算、runtime slot、模型加载、完整性确认、任务进度和云端 decode 进程。

## 核心能力

- 使用 OpenWebUI access token 初始化边端 session。
- 串行调度策略计算，维护 loading/cloud slot 等待队列。
- 采集 Prometheus 设备指标和网络指标。
- 同步调用算法服务 `/infer` 获取 layer-wise 策略。
- 向 edge prefill 和 cloud decode 下发 `/load_strategy`。
- 聚合 strategy/integrity/runtime-load 三段进度。
- 管理 edge/cloud slot、binding、session lease 和资源回收。
- 启动、停止并对账 backend 托管的 cloud decode 子进程。
- 启动时恢复任务队列和 runtime ownership，运行期间周期 reconcile。
- 提供管理员运行态 dashboard 和只读告警信息。

正式运行和开发联调使用同一套业务协议；mock runtime、mock algorithm 和 mock frontend 仅是开发工具。

## 系统流程

```text
OpenWebUI/边端前端
  -> POST /api/v1/session/init
  -> POST /api/v1/schedule/trigger
  -> 采集 edge/cloud/network 指标
  -> algorithm POST /infer
  -> 分配 edge/cloud runtime slot
  -> ModelSplit POST /load_strategy
  -> runtime progress/integrity callback
  -> task completed
  -> 前端从边端 OpenAI API 发起推理
```

任务状态通过轮询或 SSE 返回。边端和云端各自进度为：

```text
strategy 40% + integrity 30% + runtime load 30%
```

## 目录结构

```text
backend/
├── app/
│   ├── api/v1/        # auth、session、schedule、admin runtime
│   ├── core/          # 配置、env、lifespan、安全
│   ├── db/            # SQLite engine/session
│   ├── models/        # ORM 数据模型
│   ├── schemas/       # API schema
│   ├── services/      # 调度、slot、恢复、指标和进程管理
│   └── web/           # dashboard 路由
├── .env.example
├── .env.prod
└── requirements.txt
frontend/              # dashboard 静态资源
docs/对接/             # 当前三方对接协议
docs/archive/          # 历史设计/迁移文档
monitor/               # Prometheus/Grafana 配置
scripts/               # backend 启动脚本
tests/                 # unittest 与开发 mock
```

## 关键模块

| 模块 | 职责 |
|---|---|
| `schedule_orchestrator.py` | 调度主流程、策略与 runtime 下发 |
| `schedule_queue.py` | strategy/loading 排队 |
| `algorithm_dispatcher.py` | 算法请求和响应规范化 |
| `runtime_dispatcher.py` | runtime 控制面调用 |
| `decode_server_process_manager.py` | cloud decode 子进程和端口 |
| `runtime_slot_reconcile_service.py` | 数据库与真实 runtime 对账 |
| `runtime_control_service.py` | state、unload、完整性转发 |
| `schedule_recovery.py` | backend 启动任务恢复 |
| `slot_reaper.py` | session/slot 回收 |
| `prometheus_metrics.py` | NVIDIA/Ascend 指标采集 |
| `network_probe.py` | RTT、丢包和可选 iperf3 |

## API 概览

### OpenWebUI 用户接口

```text
POST /api/v1/session/init
POST /api/v1/session/heartbeat
POST /api/v1/session/close
POST /api/v1/schedule/trigger
GET  /api/v1/schedule/tasks/{task_id}
GET  /api/v1/schedule/tasks/{task_id}/stream
GET  /api/v1/schedule/tasks/{task_id}/strategy
GET  /api/v1/schedule/runtime/slots
GET  /api/v1/schedule/runtime/bindings
GET  /api/v1/schedule/queue/loading
```

除 SSE 使用 query token 外，其余接口使用 `Authorization: Bearer <OpenWebUI token>`；调度触发还需要 `Session-Id`。

### Runtime 接口

```text
POST /api/v1/schedule/runtime_callback/edge
POST /api/v1/schedule/runtime_callback/cloud
POST /api/v1/schedule/runtime/confirmation/cloud
```

完整性确认接口要求共享的 `RUNTIME_INTEGRITY_TOKEN`。进度 callback 当前不校验 token，只应暴露在可信内网。

### 管理接口

```text
POST /api/v1/login
GET  /api/v1/admin/runtime/overview
GET  /
```

管理员 overview 使用 backend 自己签发的 admin JWT。根路径是运行态 dashboard。

详细协议见 [docs/对接](docs/对接)。

## 环境安装

```bash
python -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt
```

正式机器也可以使用 conda；`scripts/run_server.sh` 优先激活
`BACKEND_CONDA_ENV`，默认环境名为 `splitwise_backend`，找不到 conda 时回退到项目 `venv`。

## 环境配置

当前仓库保留：

- `backend/.env.example`：脱敏模板。
- `backend/.env.prod`：当前正式部署参考。

复制模板创建自己的配置：

```bash
cp backend/.env.example backend/.env.local
```

使用 `BACKEND_ENV_FILE` 显式选择；未设置时读取 `backend/.env`：

```bash
BACKEND_ENV_FILE=backend/.env.local bash scripts/run_server.sh
```

关键配置：

| 配置 | 用途 |
|---|---|
| `SERVER_PUBLIC_BASE_URL`, `BACKEND_BASE_URL` | 前端地址和 runtime callback 基地址 |
| `SQLITE_DB_PATH` | SQLite 文件 |
| `OPENWEBUI_*` | OpenWebUI JWT 校验 |
| `ALGORITHM_*` | 正式算法地址、开发测试地址和超时 |
| `PROMETHEUS_URL`, `ASCEND_IPS` | 指标源和设备类型 |
| `MODELSPLIT_DEV_ROOT`, `MODELSPLIT_PYTHON_BIN` | cloud decode 代码和 Python |
| `CLOUD_SLOT_*` | slot 数量、HTTP/gRPC 端口、设备和回收时间 |
| `RUNTIME_INTEGRITY_TOKEN` | backend/runtime 共享完整性 token |

`OPENWEBUI_SKIP_SIGNATURE_VERIFY=true` 只适合受控联调环境。能够取得 OpenWebUI JWT secret 时应设为 `false`。

## 当前正式部署

```text
backend:             10.144.144.4:8010
algorithm API:       10.144.144.6:8050
cloud-slot-0 HTTP:   10.144.144.4:9020
cloud-slot-0 gRPC:   10.144.144.4:51200
cloud-slot-1 HTTP:   10.144.144.4:9021
cloud-slot-1 gRPC:   10.144.144.4:51201
```

当前 slot 上限是 2，并分配到 `CLOUD_SLOT_NPU_DEVICES=0,1`。backend 启动时 bootstrap 第一个 cloud slot；额外 slot 按需启动。停止 backend 时会清理其托管的 cloud decode 进程。

## 启动

正式配置：

```bash
BACKEND_ENV_FILE=backend/.env.prod bash scripts/run_server.sh
```

开发便捷脚本 `scripts/run_server_dev.sh` 固定读取 `backend/.env.dev`。仓库不再保存机器专用 `.env.dev`，使用前自行从 `.env.example` 复制并填写：

```bash
cp backend/.env.example backend/.env.dev
bash scripts/run_server_dev.sh
```

启动检查：

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8010/
curl -sS http://127.0.0.1:9020/health
curl -sS http://127.0.0.1:9020/runtime_state
```

backend 当前没有 `/health` 路由；根 dashboard 返回 `200` 可用于 HTTP 存活检查。

## 数据与恢复

backend 使用 SQLite。启动时会：

1. 创建表并补充当前必要列。
2. 初始化默认设备和管理员。
3. 恢复非终态任务。
4. 标记过期 session。
5. 对账已有 runtime slot/ownership。
6. bootstrap cloud slot 并启动周期 reaper。

不要手工只修改某一张 slot/binding/task 表；数据库状态必须与 runtime
`/runtime_state` 一起维护。

## 测试

```bash
python -m compileall backend/app tests
python -m unittest discover tests
```

重点回归：

```bash
python -m unittest tests.test_session_and_slot_lifecycle -v
python -m unittest tests.test_prometheus_metrics -v
python -m unittest tests.test_algorithm_api -v
```

开发工具：

- `tests/mock_edge_client.py`
- `tests/mock_algorithm_server.py`
- `tests/mock_edge_runtime_server.py`
- `tests/mock_cloud_runtime_server.py`
- `backend/print_algorithm_request_preview.py`

## 对接文档

- [边端前端](docs/对接/EDGE_FRONTEND_INTEGRATION.md)
- [ModelSplit Runtime](docs/对接/RUNTIME_CONTROL_PORT_INTEGRATION.md)
- [算法服务](docs/对接/ALGORITHM_STRATEGY_INTEGRATION_BRIEF.md)

`docs/archive/` 仅保存历史方案和迁移记录，不作为当前代码依据。
