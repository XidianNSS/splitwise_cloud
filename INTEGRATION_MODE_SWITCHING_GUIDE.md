# Integration Mode Switching Guide

本文档说明开发副本如何通过 `backend/.env` 灵活切换：

- 真实边端前端联调 / mock 边端前端联调
- 真实算法模块联调 / mock 算法模块联调
- 真实边端模型推理服务联调 / mock 边端模型推理服务联调
- 真实云端模型推理服务联调 / mock 云端模型推理服务联调

## 1. Core Rule

当前各模块的切换开关相互独立：

- `EDGE_FRONTEND_USE_MOCK`
- `ALGORITHM_USE_MOCK`
- `EDGE_RUNTIME_USE_MOCK`
- `CLOUD_RUNTIME_USE_MOCK`

因此支持混合联调，而不只是“全真”或“全 mock”两种模式。

## 2. Edge Frontend Switching

边端前端是入站调用者，云端后端不会主动连接它。

所以这里的“切换”含义是：

- `EDGE_FRONTEND_USE_MOCK=true`
  - 运行 `tests/mock_edge_client.py`
- `EDGE_FRONTEND_USE_MOCK=false`
  - 不运行 mock client
  - 改由真实边端前端直接调用云端后端 API

注意：

- 边端前端 API 本身没有变化
- 真实边端前端和 mock 边端前端都调用同一套：
  - `POST /api/v1/session/init`
  - `POST /api/v1/schedule/trigger`
  - `GET /api/v1/schedule/tasks/{task_id}`

## 3. Algorithm Switching

- `ALGORITHM_USE_MOCK=true`
  - 后端调用 `ALGORITHM_MOCK_API_URL`
  - 可运行 `tests/mock_algorithm_server.py`

- `ALGORITHM_USE_MOCK=false`
  - 后端调用 `ALGORITHM_REAL_API_URL`
  - 对接真实算法模块

## 4. Model Service Switching

边端模型推理服务与云端模型推理服务独立切换：

- `EDGE_RUNTIME_USE_MOCK=true|false`
- `CLOUD_RUNTIME_USE_MOCK=true|false`

### Edge Model Service

- mock 目标：`EDGE_RUNTIME_MOCK_HOST` + `EDGE_RUNTIME_MOCK_PORT`
- real 目标：`EDGE_RUNTIME_REAL_HOST` + `EDGE_RUNTIME_REAL_PORT`

说明：

- 若 `EDGE_RUNTIME_REAL_HOST` 留空，则后端默认使用前端在 `session/init` 传入的 `edge_device_ip`
- 当前真实边端模型推理服务固定控制端口：`9001`

### Cloud Model Service

- mock 目标：`CLOUD_RUNTIME_MOCK_HOST` + `CLOUD_RUNTIME_MOCK_PORT`
- real 目标：`CLOUD_RUNTIME_REAL_HOST` + `CLOUD_RUNTIME_REAL_PORT`

- 当前真实云端模型推理服务固定控制端口：`9002`

## 5. Recommended Profiles

### 全 mock 自联调

```env
EDGE_FRONTEND_USE_MOCK=true
ALGORITHM_USE_MOCK=true
EDGE_RUNTIME_USE_MOCK=true
CLOUD_RUNTIME_USE_MOCK=true
```

### 真实边端前端 + mock 算法 + mock 模型推理服务

```env
EDGE_FRONTEND_USE_MOCK=false
ALGORITHM_USE_MOCK=true
EDGE_RUNTIME_USE_MOCK=true
CLOUD_RUNTIME_USE_MOCK=true
```

### 真实边端前端 + 真实边端模型推理服务 + mock 云端模型推理服务

```env
EDGE_FRONTEND_USE_MOCK=false
ALGORITHM_USE_MOCK=false
EDGE_RUNTIME_USE_MOCK=false
CLOUD_RUNTIME_USE_MOCK=true
```

### 全真实联调

```env
EDGE_FRONTEND_USE_MOCK=false
ALGORITHM_USE_MOCK=false
EDGE_RUNTIME_USE_MOCK=false
CLOUD_RUNTIME_USE_MOCK=false
```

## 6. Startup Notes

当前 mock 脚本都会读取 `.env` 中对应的开关：

- 当某个 mock 开关为 `false` 时，对应 mock 脚本会直接退出，不再误占端口
- 因此后续可以保留统一的脚本入口，而通过 `.env` 控制是否真正启用 mock

涉及脚本：

- `tests/mock_edge_client.py`
- `tests/mock_algorithm_server.py`
- `tests/mock_edge_runtime_server.py`
- `tests/mock_cloud_runtime_server.py`
