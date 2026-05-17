# 删除 `ScheduleTask.username` 的安全迁移方案

当前状态：`已完成，可归档`

说明：

- SQLAlchemy 模型中已经移除 `ScheduleTask.username`
- SQLite 数据库中的 `schedule_tasks.username` 列已经完成物理删除
- 迁移后已完成主链路联调验证
- 本文档后续更适合作为迁移留档记录，而不是继续执行的活文档

本文档用于指导如何安全移除 `schedule_tasks` 表中的历史废弃字段：

- `ScheduleTask.username`

当前结论已经比较明确：

- 该字段已不再参与当前主流程
- 代码中已停止继续写入该字段
- 当前任务归属已经由 `openwebui_user_id` 承担

因此，这个字段已经具备“可删除”的前提。

但由于当前项目使用的是 SQLite，删除列不适合直接草率处理，因此建议采用分阶段、安全迁移的方式。

## 1. 迁移目标

本次迁移完成后，项目应达到下面状态：

- SQLAlchemy 模型中不再定义 `ScheduleTask.username`
- SQLite 数据库中的 `schedule_tasks.username` 列被物理删除
- 调度主链路继续正常：
  - `session/init`
  - `schedule/trigger`
  - `tasks`
  - `strategy`
  - `stream`

## 2. 为什么不能直接“顺手删掉”

原因主要有 3 个：

1. SQLite 删除列通常不是轻量修改  
   - 往往需要重建表
   - 再复制数据

2. `schedule_tasks` 是核心业务表  
   - 里面保存任务状态、策略结果、进度信息
   - 一旦迁移脚本有问题，影响范围比一般配置表更大

3. 需要确保旧数据不丢失  
   - `task_id`
   - `openwebui_user_id`
   - `edge_session_id`
   - `model_type`
   - 进度与消息字段
   - `strategy_payload`
   - 时间戳

因此推荐采用“先确认无引用，再执行一次性结构迁移”的方式。

## 3. 当前已满足的前置条件

目前已确认：

1. 代码中已停止写入 `ScheduleTask.username`
2. 当前查询任务归属使用的是 `openwebui_user_id`
3. 当前返回给前端的任务状态中不包含 `username`
4. 全项目搜索未发现业务逻辑再读取 `ScheduleTask.username`

所以迁移前的主要风险已经降低。

## 4. 推荐迁移策略

建议分为 3 个阶段。

## 4.1 阶段 A：代码层明确废弃

这一阶段不动数据库表结构，只做代码层收口。

### 目标

- 先让代码层彻底不再依赖该字段
- 给后续物理迁移降低风险

### 要做的事情

1. 从 [models.py](/home/nss-d/splitwise_cloud/backend/app/models/models.py) 中删除：
   - `ScheduleTask.username = Column(...)`

2. 确认项目内没有任何：
   - `task.username`
   - `ScheduleTask.username`
   - 按该字段做过滤或返回

3. 不需要补新的替代字段  
   - 因为 `openwebui_user_id` 已经是现行归属字段

### 阶段 A 风险

- 如果数据库中仍有 `username` 列，而 ORM 模型已不声明它，通常不会影响读取
- 因为 ORM 只映射自己声明的列

所以这一步风险较低。

## 4.2 阶段 B：SQLite 表结构物理迁移

这一阶段才真正删除数据库列。

### 推荐做法

对 `schedule_tasks` 执行一次“重建表迁移”：

1. 新建临时表，例如：
   - `schedule_tasks_new`

2. 新表结构中保留所有当前仍在使用的列，但**不再包含**：
   - `username`

3. 将旧表数据复制到新表：
   - 只复制保留列

4. 删除旧表：
   - `schedule_tasks`

5. 将新表重命名为：
   - `schedule_tasks`

6. 重建必要索引

### 迁移时必须保留的列

至少包括：

- `task_id`
- `openwebui_user_id`
- `edge_session_id`
- `model_type`
- `status`
- `phase`
- `phase_progress`
- `overall_progress`
- `message`
- `edge_device_id`
- `cloud_device_id`
- `edge_progress`
- `cloud_progress`
- `edge_status`
- `cloud_status`
- `edge_message`
- `cloud_message`
- `strategy_payload`
- `error_detail`
- `created_at`
- `updated_at`

### 迁移时的实现位置建议

建议放在 [models.py](/home/nss-d/splitwise_cloud/backend/app/models/models.py) 的轻量迁移函数中，新增一个专门的迁移分支，例如：

- 检查 `schedule_tasks` 是否存在 `username` 列
- 如果存在，则执行一次表重建迁移

### 为什么建议放在轻量迁移中

因为你当前项目已经有类似的轻量补列机制。  
这次只是第一次需要做“删列式迁移”，风格上仍然可以延续当前体系。

## 4.3 阶段 C：迁移后验证与清理

迁移完成后，需要做一次回归验证。

### 要验证的内容

1. 后端启动正常
2. 不会因为 ORM 映射缺列报错
3. 旧任务数据仍可查询
4. 新任务可正常创建
5. `tasks / strategy / stream` 正常

### 建议额外检查

直接检查 SQLite 表结构，确认：

- `schedule_tasks` 中已经不存在 `username`

## 5. 推荐的实施顺序

为了最大限度降低风险，建议顺序如下：

1. 先备份数据库文件
2. 做阶段 A：模型层删字段
3. 启动项目并跑一轮完整联调
4. 再做阶段 B：SQLite 物理迁移
5. 再跑一轮完整联调
6. 最后确认表结构已完成收口

## 6. 联调验证清单

迁移前后都建议跑下面这组：

### 6.1 普通用户主链路

1. `POST /api/v1/session/init`
2. `POST /api/v1/schedule/trigger`
3. `GET /api/v1/schedule/tasks/{task_id}`
4. `GET /api/v1/schedule/tasks/{task_id}/strategy`
5. `GET /api/v1/schedule/tasks/{task_id}/stream`

### 6.2 管理员主链路

1. `POST /api/v1/login`
2. `GET /api/v1/users`
3. `GET /api/v1/system/devices`

### 6.3 任务旧数据检查

如果数据库里已有历史任务，建议手工确认：

1. 历史任务还能查出来
2. 历史任务策略还能读出来
3. 没有因为删列导致整表损坏

## 7. 风险等级判断

### 阶段 A：低风险

- 只是删 ORM 模型字段
- 不改数据库结构

### 阶段 B：中风险

- 需要 SQLite 重建表
- 需要复制旧数据

### 阶段 C：低风险

- 主要是验证和收口

所以最合理的推进方式是：

- 先单独做阶段 A
- 验证稳定后再做阶段 B

## 8. 最终推荐结论

关于 `ScheduleTask.username` 的删除，最稳妥的做法不是“一步到位硬删”，而是：

> 先做代码层移除，再做 SQLite 表重建式物理迁移，最后通过完整联调确认主链路和历史数据都不受影响。

这也是当前项目里风险最低、最符合你“保证代码健壮性优先”要求的方案。
