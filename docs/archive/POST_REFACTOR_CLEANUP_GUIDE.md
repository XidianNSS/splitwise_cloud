# 并发控制与算法对接改造后续清理指导文档

本文档用于收口两轮已经完成的改造：

1. 云端后端二阶段资源并发控制
2. 云端后端与算法切分策略计算模块的新同步 `/infer` 对接

目标不是再改协议，而是清理已经退出主流程的旧代码、旧文档和兼容残留，降低后续理解与修改成本。

---

## 1. 当前结论

当前主流程已经稳定切换到：

- 策略计算阶段：全局单资源串行放行
- 策略加载阶段：按边端设备 + 云端设备加载资源放行
- 算法对接方式：同步 `POST /infer`，直接发送环境 JSON，直接读取 HTTP JSON 响应

因此，后续清理应围绕两个方向展开：

- 删除已经退出主流程的旧排队/旧算法回调残留
- 同步所有仍描述旧流程的文档、注释、mock 默认值

---

## 2. 第一批：可直接清理的代码残留

这一批优先做，风险最低。

### 2.1 清理旧的“按设备对排队”辅助函数

文件：

- [schedule_queue.py](/home/nss-d/splitwise_cloud/backend/app/services/schedule_queue.py)

当前仍保留但已经退出主流程的函数：

- `find_active_task_for_device_pair`
- `count_queued_tasks_for_device_pair`
- `recalculate_queue_positions_for_device_pair`

清理理由：

- 当前排队控制已经切换为：
  - 全局策略队列
  - 加载资源队列
- 上述三个函数仍然体现旧的“设备对 running / queued / phase=queued”模型
- 当前主流程已不再依赖它们

清理建议：

1. 先全仓库确认无引用
2. 直接删除函数
3. 删除与其对应的旧注释

### 2.2 明确 `build_logical_queue_metrics` 的去留

文件：

- [schedule_queue.py](/home/nss-d/splitwise_cloud/backend/app/services/schedule_queue.py)

当前状态：

- `build_logical_queue_metrics()` 只返回：

```python
{
    "edge_queue_len": 0.0,
    "cloud_queue_len": 0.0,
}
```

判断：

- 它已经不再代表真实队列长度
- 目前只是为了维持算法请求体结构中的 `queue_len` 字段而保留的占位兼容层

清理建议：

- 不建议现在直接删除
- 建议做以下二选一：

方案 A：保留函数，但在函数上方加明确注释  
说明它是“算法接口兼容占位层，不代表真实资源队列”

方案 B：把它迁移到更明确的兼容命名，例如：

- `build_algorithm_queue_len_placeholders`

当前更推荐方案 A，避免引入不必要改动。

---

## 3. 第二批：算法对接改造后的旧逻辑收口

### 3.1 删除旧算法回调流程的剩余文档痕迹

当前代码已经不再使用：

- `task_id -> 算法模块`
- `state_vector`
- `/api/v1/schedule/strategy_callback`
- `POST /api/calculate`

但仓库文档中仍有旧描述。

当前已发现的旧文档位置：

- [EDGE_FRONTEND_INTEGRATION.md](/home/nss-d/splitwise_cloud/EDGE_FRONTEND_INTEGRATION.md)
- [EDGE_CLOUD_RUNTIME_INTEGRATION.md](/home/nss-d/splitwise_cloud/EDGE_CLOUD_RUNTIME_INTEGRATION.md)
- [后端云端对接效果.md](/home/nss-d/splitwise_cloud/后端云端对接效果.md)

其中残留内容包括：

- `Cloud->>Algo: POST /api/calculate`
- `Algo->>Cloud: POST /api/v1/schedule/strategy_callback`
- 旧的 `ALGORITHM_API_URL=http://127.0.0.1:5000/api/calculate`

清理建议：

1. 将上述文档中的算法时序统一改成：
   - `POST /infer`
   - 同步返回策略 JSON
2. 删除所有 callback 相关描述
3. 删除 `state_vector` 相关描述

### 3.2 收口算法对接简版文档中的“旧字段回顾”表述

文件：

- [ALGORITHM_STRATEGY_INTEGRATION_BRIEF.md](/home/nss-d/splitwise_cloud/ALGORITHM_STRATEGY_INTEGRATION_BRIEF.md)

当前状态：

- 主体内容已经是新协议
- 但仍保留了较多“旧流程对比式”表述

判断：

- 文档本身没有错误
- 但如果后续发给协作者长期使用，建议进一步收口成“只描述现状”的版本

清理建议：

- 保留一小段“旧流程已废弃”说明即可
- 删除重复提到旧 `state_vector` / `strategy_callback` 的位置

---

## 4. 第三批：mock 与默认配置的一致性清理

### 4.1 统一算法 mock 与 `.env` 的默认端口口径

文件：

- [backend/.env](/home/nss-d/splitwise_cloud/backend/.env)
- [mock_algorithm_server.py](/home/nss-d/splitwise_cloud/tests/mock_algorithm_server.py)

当前状态：

- 这两者已经基本对齐：
  - mock 地址默认 `http://127.0.0.1:5000/infer`
- 这一块当前不用再做结构改动

建议：

- 后续只保留一份默认口径：
  - 真实算法：`8000`
  - mock 算法：`5000`
- 不要再在文档里混用 `5000/api/calculate` 这种旧地址

### 4.2 收口 runtime mock 的默认注册模型

文件：

- [mock_edge_runtime_server.py](/home/nss-d/splitwise_cloud/tests/mock_edge_runtime_server.py)
- [mock_cloud_runtime_server.py](/home/nss-d/splitwise_cloud/tests/mock_cloud_runtime_server.py)

当前状态：

- 默认注册模型键仍为：
  - `gpt2`

判断：

- 这不是当前主流程 bug
- 但它会造成理解偏差：
  - 代码常用 `llama-3.2-3b` 联调
  - mock 默认注册却仍写成 `gpt2`

清理建议：

方案 A：继续保留 `gpt2`，但在文件头明确注释  
“默认值仅用于本地最小联调，可通过环境变量覆盖”

方案 B：把默认值改成当前更常用的 `llama-3.2-3b`

当前更推荐方案 A，避免不必要地影响现有联调习惯。

---

## 5. 第四批：配置与注释的历史口径清理

### 5.1 清理 `.env` 中已经过时的认证注释

文件：

- [backend/.env](/home/nss-d/splitwise_cloud/backend/.env)

当前问题：

- `SECRET_KEY` 注释中仍提到：
  - `/api/v1/auth/exchange`
  - “云端后端自己签发的业务 token” 的旧描述链路

判断：

- 当前系统已经不再使用 `auth/exchange`
- 继续保留该说明会干扰对当前认证模型的理解

清理建议：

将注释收口为：

- `SECRET_KEY` 仅用于管理员登录等本系统自有 JWT 场景
- 普通边端主流程不再依赖 `exchange`

### 5.2 清理 schema 内的阶段性注释

文件：

- [schemas.py](/home/nss-d/splitwise_cloud/backend/app/schemas/schemas.py)

当前问题：

- 仍有少量“新增”“当前阶段”等开发过程注释

判断：

- 不影响运行
- 但会降低模型/接口定义文件的整洁度

清理建议：

- 将临时过程性注释改成稳定描述
- 避免继续保留“刚改过时留下的备注”

---

## 6. 第五批：结构层面的后续收口建议

这一批不是必须马上做，但值得进入后续计划。

### 6.1 继续拆分 `schedule.py`

文件：

- [schedule.py](/home/nss-d/splitwise_cloud/backend/app/api/v1/schedule.py)

当前状态：

- 算法请求构造
- 算法响应标准化
- 资源放行
- 队列推进
- runtime 下发
- 任务恢复

仍然集中在一个文件里。

判断：

- 当前逻辑已经可运行
- 但经过二阶段并发控制和新算法对接改造后，`schedule.py` 再次变长，理解成本上升

后续建议：

- 将算法请求与响应规范化抽到独立 service，例如：
  - `algorithm_dispatcher.py`
- 将启动恢复与自动推进逻辑抽到独立 service，例如：
  - `schedule_recovery.py`

这属于“结构优化”，不是当前最优先清理项。

### 6.2 明确 `queue_len` 的后续归属

当前状态：

- `queue_len` 已不参与真实资源控制
- 目前仅作为算法环境 JSON 的兼容占位字段存在

后续有两条路线：

1. 与算法模块确认后，彻底删除该字段
2. 重新定义为真正的设备级排队指标，再单独实现

在未与算法模块确认之前，不建议在这轮清理中继续改动它。

---

## 7. 推荐清理顺序

建议按以下顺序推进：

1. 删除旧设备对排队函数
2. 给 `build_logical_queue_metrics()` 增加明确兼容注释
3. 同步更新旧算法流程文档
4. 清理 `.env` 中旧 `auth/exchange` 注释
5. 收口 schema 中阶段性注释
6. 再考虑 `schedule.py` 的进一步解耦

---

## 8. 本轮清理的边界

本轮清理建议**不要**触碰以下内容：

- 二阶段资源并发控制本身的运行逻辑
- runtime `/load_strategy` 对接协议
- 边端前端当前正在使用的主接口格式
- `queue_len` 字段是否彻底删除
- runtime mock 的默认模型键行为

这些内容要么仍在使用，要么需要和协作者先对齐语义，不适合在“清理”阶段直接动。

---

## 9. 一句话总结

当前最值得优先收口的，不是主流程代码本身，而是：

- 旧设备对排队辅助函数
- 旧算法回调流程文档
- `.env` 和 schema 里的历史注释

先把这三类清干净，能明显降低后续继续改并发调度和多实例 runtime 方案时的理解成本。
