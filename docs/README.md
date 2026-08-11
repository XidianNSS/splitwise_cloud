# splitwise_cloud 文档索引

文档以当前 `backend/app`、ModelSplit runtime 契约和正式 `.env.prod` 为准。根目录 [README](../README.md) 说明整体架构、启动方式和当前模型范围。

## 当前对接文档

- [边端前端对接](对接/EDGE_FRONTEND_INTEGRATION.md)：OpenWebUI token、session、调度、任务进度、SSE 和推理数据面入口。
- [ModelSplit Runtime 对接](对接/RUNTIME_CONTROL_PORT_INTEGRATION.md)：`/load_strategy`、runtime route、callback、完整性确认、状态和卸载。
- [切分策略算法服务对接](对接/ALGORITHM_STRATEGY_INTEGRATION_BRIEF.md)：生成模型的同步 `/infer` 请求/响应；BERT 不调用该服务。

## 后续开发指导

- [云端按可用显存动态选卡](CLOUD_ACCELERATOR_DYNAMIC_PLACEMENT_DEVELOPMENT_GUIDE.md)：后续实现 placement lease、逐卡准入和固定 slot/设备亲和关系的指导文档。该功能当前尚未实现，不能当作现行运行行为。

## 归档

`archive/` 保存已经实施、被替代或仅用于历史决策追踪的方案。归档文档不作为当前接口、配置或部署依据；遇到冲突时以当前代码、根 README 和上述三份对接文档为准。
