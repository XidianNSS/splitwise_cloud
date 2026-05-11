# Splitwise 云端后端

## 项目定位

这是 Splitwise Cloud Edge 的云端后端代码，负责完成以下工作：
- 接收边端前端发起的调度请求
- 识别边端设备与云端设备
- 采集设备与网络环境信息
- 请求切分策略计算模块生成切分策略
- 将模型名称与切分策略下发给边端 / 云端模型推理服务
- 接收模型推理服务回调的加载进度，并维护任务状态

如果你是第一次接手这个项目，建议先看完本 README，再按角色查看对应对接文档。

## 目录结构

核心目录：
- `backend/app/`：后端主程序
- `backend/.env`：正式运行配置
- `backend/.env.dev`：开发隔离配置
- `scripts/run_server.sh`：正式版启动脚本
- `scripts/run_server_dev.sh`：开发版启动脚本
- `tests/`：mock 前端、mock 算法服务、mock 模型推理服务
- `data/`：SQLite 数据库文件

当前数据库：
- 正式版：`data/cloud_edge.db`
- 开发版：`data/cloud_edge_dev.db`

## 主要代码模块介绍

为了方便快速理解代码，可以按下面的方式阅读：

### 1. `backend/app/api/`
这一层是接口入口层，主要负责：
- 定义 HTTP 路由
- 接收请求参数
- 调用服务层
- 返回响应

当前最重要的接口文件是：
- `backend/app/api/v1/session.py`
  - 会话初始化、边端设备识别
- `backend/app/api/v1/schedule.py`
  - 调度相关接口、任务查询、策略查询、模型推理服务进度回调
- `backend/app/api/v1/devices.py`
  - 设备查询与 Prometheus 目标相关接口
- `backend/app/api/v1/auth.py`
  - 后端管理员登录接口
- `backend/app/api/v1/users.py`
  - 管理员账号管理接口

### 2. `backend/app/services/`
这一层是核心业务层，也是最值得重点阅读的部分。

建议优先阅读顺序：
- `schedule_orchestrator.py`
  - 调度主流程入口
  - 串起环境采集、资源检查、算法请求、模型推理服务下发、状态推进
- `algorithm_dispatcher.py`
  - 负责构造发给切分策略计算模块的 JSON
  - 负责接收并规范化算法返回结果
- `runtime_dispatcher.py`
  - 负责向边端 / 云端模型推理服务发送 `POST /load_strategy`
- `runtime_startup_admission.py`
  - 负责模型启动前的资源预检查
- `schedule_queue.py`
  - 负责调度队列推进
- `schedule_recovery.py`
  - 负责后端重启后的任务恢复与队列恢复
- `schedule_task_service.py`
  - 负责数据库中的任务状态读写
- `schedule_presenter.py`
  - 负责把内部任务对象整理成接口返回格式
- `network_probe.py`
  - 负责 RTT、丢包、带宽探测
- `prometheus_metrics.py`
  - 负责从 Prometheus 采集 CPU / 内存 / GPU 指标
- `model_registry.py`
  - 负责维护支持模型的结构参数、名称映射和资源门槛

### 3. `backend/app/core/`
这一层是基础设施配置层，主要负责：
- 全局配置加载
- 安全相关工具
- 生命周期管理
- 正式版 / 开发版环境切换

重点文件：
- `config.py`
  - 所有运行配置的统一入口
- `env_loader.py`
  - 决定读取 `.env` 还是 `.env.dev`
- `lifespan.py`
  - 应用启动时的恢复逻辑
- `security.py`
  - JWT 与认证辅助逻辑

### 4. `backend/app/db/`、`backend/app/models/`、`backend/app/schemas/`
这三层分别负责：
- `db/`：数据库连接与初始化
- `models/`：SQLAlchemy 数据表定义
- `schemas/`：Pydantic 请求 / 响应模型定义

如果你想看数据库结构，优先看：
- `backend/app/models/models.py`

### 5. `backend/app/web/`
这一层负责云端前端页面相关逻辑，目前主要是：
- `dashboard.py`

### 6. `tests/`
这里主要不是单元测试，而是联调用 mock 工具。

最常用的是：
- `mock_edge_client.py`
  - 模拟边端前端
- `mock_algorithm_server.py`
  - 模拟切分策略计算模块
- `mock_edge_runtime_server.py`
  - 模拟边端模型推理服务
- `mock_cloud_runtime_server.py`
  - 模拟云端模型推理服务

如果你是第一次读代码，建议阅读顺序是：
1. 先看本 README
2. 再看 `backend/app/api/v1/schedule.py` 了解接口入口
3. 再看 `backend/app/services/schedule_orchestrator.py` 了解主流程
4. 最后按需要深入 `algorithm_dispatcher.py`、`runtime_dispatcher.py`、`model_registry.py` 等具体模块

## 核心流程

当前主流程如下：
1. 边端前端调用 `POST /api/v1/session/init` 初始化会话
2. 边端前端调用 `POST /api/v1/schedule/trigger` 发起调度
3. 云端后端采集边端 / 云端设备指标与网络指标
4. 云端后端调用切分策略计算模块 `POST /infer`
5. 云端后端分别向边端模型推理服务、云端模型推理服务发送 `POST /load_strategy`
6. 边端 / 云端模型推理服务通过 `runtime_callback` 回调加载进度
7. 前端通过 `GET /api/v1/schedule/tasks/{task_id}` 轮询任务状态

## 如何运行

### 1. 正式版运行

正式版使用：
- 配置文件：`backend/.env`
- 后端端口：`8010`
- 数据库：`data/cloud_edge.db`

启动命令：
```bash
bash scripts/run_server.sh
```

适用场景：
- 当前真实系统运行
- 与真实边端前端、真实模型推理服务、真实切分策略计算模块联调

### 2. 开发版运行

开发版使用：
- 配置文件：`backend/.env.dev`
- 后端端口：`8110`
- 数据库：`data/cloud_edge_dev.db`

启动命令：
```bash
bash scripts/run_server_dev.sh
```

适用场景：
- 本地开发
- 自联调
- 不希望影响当前正在运行的正式系统

## 正式版与开发版是否可以同时运行

可以。

当前已经完成以下隔离：
- 正式版和开发版使用不同端口
- 正式版和开发版使用不同数据库
- mock 脚本支持跟随 `BACKEND_ENV_FILE` 切换环境配置

## 开发版配套 mock 如何使用

如果要让开发版后端配套 mock 一起运行，请先在当前 shell 中设置：
```bash
export BACKEND_ENV_FILE=/home/nss-d/wyy/splitwise_cloud_next/backend/.env.dev
```

然后启动 mock：
```bash
venv/bin/python tests/mock_algorithm_server.py
venv/bin/python tests/mock_edge_runtime_server.py
venv/bin/python tests/mock_cloud_runtime_server.py
venv/bin/python tests/mock_edge_client.py
```

这样这些 mock 会自动读取 `.env.dev`，不会碰正式版配置。

## 常用对接文档

按角色查看：

边端前端开发者：
- `EDGE_FRONTEND_INTEGRATION.md`

模型推理服务开发者：
- `RUNTIME_CONTROL_PORT_INTEGRATION.md`

切分策略计算模块开发者：
- `ALGORITHM_STRATEGY_INTEGRATION_BRIEF.md`

## 当前关键配置项

正式版最常用配置在 `backend/.env`：
- `SERVER_PORT`
- `SQLITE_DB_PATH`
- `EDGE_RUNTIME_USE_MOCK`
- `CLOUD_RUNTIME_USE_MOCK`
- `ALGORITHM_USE_MOCK`
- `ALGORITHM_REAL_API_URL`
- `BACKEND_BASE_URL`

开发版最常用配置在 `backend/.env.dev`：
- `SERVER_PORT=8110`
- `SQLITE_DB_PATH=data/cloud_edge_dev.db`
- `EDGE_RUNTIME_MOCK_PORT=18101`
- `CLOUD_RUNTIME_MOCK_PORT=18102`
- `ALGORITHM_MOCK_API_URL=http://127.0.0.1:15100/infer`

## 当前支持的模型

目前代码中已接入的模型包括：
- `gpt2`
- `tinyllama`
- `Llama-3.2-3b`
- `Llama-3.2-3B-Instruct`

这些模型的结构参数与名称映射定义在：
- `backend/app/services/model_registry.py`

## 你最常改的代码位置

如果你要继续开发，最常会改到这些文件：
- `backend/app/services/model_registry.py`
  - 添加新模型
- `backend/app/services/schedule_orchestrator.py`
  - 调度主流程
- `backend/app/services/algorithm_dispatcher.py`
  - 与切分策略计算模块对接
- `backend/app/services/runtime_dispatcher.py`
  - 与模型推理服务对接
- `backend/app/services/runtime_startup_admission.py`
  - 资源预检查
- `tests/mock_algorithm_server.py`
  - 算法 mock
- `tests/mock_edge_runtime_server.py`
  - 边端模型推理服务 mock
- `tests/mock_cloud_runtime_server.py`
  - 云端模型推理服务 mock

## 当前建议

- 正式系统运行时，优先使用 `scripts/run_server.sh`
- 开发验证优先使用 `scripts/run_server_dev.sh`
- 要做开发版整套自联调时，先设置 `BACKEND_ENV_FILE` 再启动 mock
- 新增模型时，优先检查 `model_registry.py`、算法模块命名、模型推理服务命名是否一致
