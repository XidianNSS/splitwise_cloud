import asyncio
import json
import logging
import uuid
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import SessionLocal
from app.models.models import Device, EdgeSession, ScheduleTask
from app.services.algorithm_dispatcher import (
    build_algorithm_request_payload,
    derive_edge_storage_limit_gb_from_metrics,
    request_algorithm_decision,
)
from app.services.model_registry import (
    canonicalize_model_type,
    resolve_model_type_key,
    runtime_model_type,
)
from app.services.network_probe import get_network_metrics
from app.services.prometheus_metrics import get_prometheus_metrics
from app.services.runtime_dispatcher import (
    build_runtime_control_url,
    dispatch_strategy_to_runtime,
    extract_ip,
)
from app.services.runtime_startup_admission import check_runtime_startup_resources
from app.services.schedule_presenter import clamp_progress
from app.services.schedule_queue import (
    LOADING_RUNNING_STATUS,
    STRATEGY_QUEUED_STATUS,
    STRATEGY_RUNNING_STATUS,
    count_queued_strategy_tasks,
    find_running_strategy_task,
    promote_next_strategy_task,
    recalculate_strategy_queue_positions,
)
from app.services.schedule_task_service import fail_task, update_task

TASK_TERMINAL_STATUSES = {"completed", "failed"}
STRATEGY_ADMISSION_LOCK = asyncio.Lock()

logger = logging.getLogger("ScheduleOrchestrator")


async def accept_schedule_task(
    *,
    db: Session,
    openwebui_user_id: str,
    edge_session: EdgeSession,
    requested_model_type: str,
) -> dict:
    canonical_model_type = canonicalize_model_type(requested_model_type)

    edge_session.model_type = canonical_model_type
    edge_session.updated_at = datetime.utcnow()
    db.add(edge_session)
    db.commit()
    db.refresh(edge_session)

    task_id = str(uuid.uuid4())
    edge_device_id = edge_session.edge_device_id
    cloud_device_id = edge_session.cloud_device_id

    async with STRATEGY_ADMISSION_LOCK:
        running_strategy_task = find_running_strategy_task(db)
        queued_strategy_count = count_queued_strategy_tasks(db)
        is_queued = running_strategy_task is not None or queued_strategy_count > 0

        task = ScheduleTask(
            task_id=task_id,
            openwebui_user_id=openwebui_user_id,
            edge_session_id=edge_session.session_id,
            model_type=canonical_model_type,
            status="accepted",
            phase="strategy",
            phase_progress=0,
            overall_progress=0,
            message="等待进入切分策略计算队列" if is_queued else "任务已受理，开始计算切分策略",
            edge_device_id=edge_device_id,
            cloud_device_id=cloud_device_id,
            edge_status="pending",
            cloud_status="pending",
            queue_status=STRATEGY_QUEUED_STATUS if is_queued else STRATEGY_RUNNING_STATUS,
            queue_position=queued_strategy_count + 1 if is_queued else 0,
            edge_message="等待切分策略计算完成",
            cloud_message="等待切分策略计算完成",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(task)
        db.commit()

    if not is_queued:
        asyncio.create_task(
            process_schedule_task(
                task_id,
                openwebui_user_id,
                edge_session.session_id,
                {"model_type": canonical_model_type},
            )
        )

    return {
        "status": "accepted",
        "task_id": task_id,
        "phase": "strategy",
        "phase_progress": 0,
        "overall_progress": 0,
        "message": "等待进入切分策略计算队列" if is_queued else "任务已受理，开始计算切分策略",
    }


async def promote_next_queued_strategy_task() -> bool:
    async with STRATEGY_ADMISSION_LOCK:
        db = SessionLocal()
        try:
            next_task = promote_next_strategy_task(db)
            if not next_task:
                return False
        finally:
            db.close()

    asyncio.create_task(
        process_schedule_task(
            next_task.task_id,
            next_task.openwebui_user_id,
            next_task.edge_session_id,
            {"model_type": next_task.model_type},
        )
    )
    logger.info("已自动推进策略队列任务: task_id=%s", next_task.task_id)
    return True


async def dispatch_loading_task(task_id: str) -> None:
    db = SessionLocal()
    try:
        task = db.query(ScheduleTask).filter(ScheduleTask.task_id == task_id).first()
        if not task or task.status in TASK_TERMINAL_STATUSES:
            return
        if not task.strategy_payload:
            await fail_task_and_promote(db, task, "切分策略尚未生成", "策略加载前缺少 strategy_payload")
            return
        if not task.edge_device_id or not task.cloud_device_id:
            await fail_task_and_promote(db, task, "任务设备信息缺失", "策略加载前缺少边端或云端设备标识")
            return

        try:
            decision_result = json.loads(task.strategy_payload)
        except json.JSONDecodeError as exc:
            await fail_task_and_promote(db, task, "切分策略解析失败", str(exc))
            return

        model_type = task.model_type
        edge_device = db.query(Device).filter(Device.id == task.edge_device_id).first()
        cloud_device = db.query(Device).filter(Device.id == task.cloud_device_id).first()
        if not edge_device or not cloud_device:
            await fail_task_and_promote(db, task, "任务设备信息缺失", "策略下发前未找到边端或云端设备资产")
            return
        edge_ip = extract_ip(edge_device.value)
        cloud_ip = extract_ip(cloud_device.value)
        if not edge_ip or not cloud_ip:
            await fail_task_and_promote(db, task, "设备 IP 信息缺失", "策略下发前无法解析边端或云端控制入口 IP")
            return

        update_task(
            db,
            task,
            status="running",
            phase="loading",
            phase_progress=5,
            message="切分策略计算完成，正在向边云控制入口下发模型启动请求",
            queue_status=LOADING_RUNNING_STATUS,
            queue_position=0,
            edge_status="dispatching",
            cloud_status="dispatching",
            edge_progress=0,
            cloud_progress=0,
            edge_message="边端控制入口正在接收模型启动请求",
            cloud_message="云端控制入口正在接收模型启动请求",
        )

        runtime_decision_payload = {
            "layer_partitions": decision_result["layer_partitions"],
        }
        runtime_model_name = runtime_model_type(model_type)
        edge_dispatch_payload = {
            "task_id": task_id,
            "model_type": runtime_model_name,
            "decision": runtime_decision_payload,
        }
        cloud_dispatch_payload = {
            "task_id": task_id,
            "model_type": runtime_model_name,
            "decision": runtime_decision_payload,
        }

        logger.info(
            "准备下发模型启动请求体: task_id=%s, runtime_payload=%s",
            task_id,
            json.dumps(edge_dispatch_payload, ensure_ascii=False),
        )
        logger.info(
            "开始向固定控制端口下发模型启动请求: task_id=%s, edge_target=%s, cloud_target=%s",
            task_id,
            build_runtime_control_url("edge", edge_ip),
            build_runtime_control_url("cloud", cloud_ip),
        )

        results = await asyncio.gather(
            dispatch_strategy_to_runtime(node_role="edge", device_ip=edge_ip, payload=edge_dispatch_payload),
            dispatch_strategy_to_runtime(node_role="cloud", device_ip=cloud_ip, payload=cloud_dispatch_payload),
            return_exceptions=True,
        )

        dispatch_errors: list[str] = []
        for result in results:
            if isinstance(result, Exception):
                dispatch_errors.append(str(result))
                continue
            if str(result.get("status", "accepted")).strip().lower() not in {"accepted", "ok"}:
                dispatch_errors.append(json.dumps(result, ensure_ascii=False))
        if dispatch_errors:
            await fail_task_and_promote(db, task, "切分策略下发失败", " | ".join(dispatch_errors))
            return

        update_task(
            db,
            task,
            phase="loading",
            phase_progress=15,
            message="模型启动请求已受理，等待边云推理节点完成模型加载",
            edge_status="loading",
            cloud_status="loading",
            edge_message="等待边端开始加载模型",
            cloud_message="等待云端开始加载模型",
        )
    finally:
        db.close()


async def fail_task_and_promote(
    db: Session,
    task: ScheduleTask,
    message: str,
    error_detail: str | None = None,
) -> None:
    previous_queue_status = task.queue_status
    fail_task(db, task, message, error_detail)
    if previous_queue_status == STRATEGY_QUEUED_STATUS:
        recalculate_strategy_queue_positions(db)
        await promote_next_queued_strategy_task()
    elif previous_queue_status == STRATEGY_RUNNING_STATUS:
        await promote_next_queued_strategy_task()


async def complete_task_and_promote(db: Session, task: ScheduleTask) -> None:
    previous_queue_status = task.queue_status
    update_task(
        db,
        task,
        status="completed",
        message="边云模型均已加载完成，任务结束",
        edge_status="ready",
        cloud_status="ready",
        edge_progress=100,
        cloud_progress=100,
        edge_message="边端模型已加载完成",
        cloud_message="云端模型已加载完成",
    )
    if previous_queue_status == STRATEGY_QUEUED_STATUS:
        recalculate_strategy_queue_positions(db)
        await promote_next_queued_strategy_task()
    elif previous_queue_status == STRATEGY_RUNNING_STATUS:
        await promote_next_queued_strategy_task()


async def process_schedule_task(task_id: str, openwebui_user_id: str, edge_session_id: str, trigger_payload: dict) -> None:
    db = SessionLocal()
    try:
        task = db.query(ScheduleTask).filter(ScheduleTask.task_id == task_id).first()
        if not task:
            logger.error("后台任务启动失败，未找到调度任务: %s", task_id)
            return

        raw_model_type = trigger_payload["model_type"]
        model_type_key = resolve_model_type_key(raw_model_type)
        if model_type_key is None:
            await fail_task_and_promote(db, task, "不支持的模型类型", f"不支持的模型类型: {raw_model_type}")
            return
        model_type = canonicalize_model_type(raw_model_type)

        update_task(
            db,
            task,
            status="running",
            phase="strategy",
            phase_progress=5,
            message="正在校验用户权限并准备采集环境指标",
            queue_status=STRATEGY_RUNNING_STATUS,
            queue_position=0,
            dispatched_at=datetime.utcnow(),
            edge_message="等待切分策略计算完成",
            cloud_message="等待切分策略计算完成",
        )

        edge_session = (
            db.query(EdgeSession)
            .filter(
                EdgeSession.session_id == edge_session_id,
                EdgeSession.openwebui_user_id == openwebui_user_id,
                EdgeSession.status == "active",
            )
            .first()
        )
        if not edge_session:
            await fail_task_and_promote(
                db,
                task,
                "初始化会话不存在",
                f"未找到 session_id={edge_session_id} 对应的有效会话",
            )
            return

        edge_device = db.query(Device).filter(Device.id == edge_session.edge_device_id).first()
        cloud_device = db.query(Device).filter(Device.id == edge_session.cloud_device_id).first()
        if not edge_device or not cloud_device:
            await fail_task_and_promote(db, task, "会话设备信息缺失", "边端或云端设备在资产表中不存在")
            return

        edge_device_id = edge_device.id
        cloud_device_id = cloud_device.id
        edge_ip = extract_ip(edge_device.value)
        cloud_ip = extract_ip(cloud_device.value)

        update_task(
            db,
            task,
            phase_progress=15,
            message="用户授权校验完成，正在采集边云环境指标",
            edge_device_id=edge_device_id,
            cloud_device_id=cloud_device_id,
        )

        if not cloud_ip or not edge_ip or not cloud_device_id or not edge_device_id:
            await fail_task_and_promote(
                db,
                task,
                "用户设备分配不完整",
                "触发失败：该用户分配的设备不完整，无法凑齐端云流水线 (需1云1边)",
            )
            return

        edge_metrics, cloud_metrics, network_metrics = await asyncio.gather(
            get_prometheus_metrics(edge_ip),
            get_prometheus_metrics(cloud_ip),
            get_network_metrics(edge_ip, cloud_ip),
        )
        edge_storage_limit_gb = derive_edge_storage_limit_gb_from_metrics(edge_metrics)

        logger.info(
            "边端可用显存预算采集完成: task_id=%s, edge_ip=%s, storage_limit_gb=%s",
            task_id,
            edge_ip,
            edge_storage_limit_gb,
        )

        raw_input_json = build_algorithm_request_payload(
            model_type=model_type,
            model_type_key=model_type_key,
            edge_metrics=edge_metrics,
            cloud_metrics=cloud_metrics,
            network_metrics=network_metrics,
            edge_storage_limit_gb=edge_storage_limit_gb,
        )

        logger.info(
            "任务触发: task_id=%s, openwebui_user_id=%s, model=%s, raw_input_json=%s",
            task_id,
            openwebui_user_id,
            model_type,
            json.dumps(raw_input_json, ensure_ascii=False, indent=2),
        )

        update_task(
            db,
            task,
            phase_progress=25,
            message="环境指标采集完成，正在进行模型启动资源预检查",
        )

        resource_failures = check_runtime_startup_resources(
            model_type_key=model_type_key,
            edge_metrics=edge_metrics,
            cloud_metrics=cloud_metrics,
        )
        if resource_failures:
            await fail_task_and_promote(
                db,
                task,
                "边云设备资源不足，暂不发起策略计算",
                " | ".join(resource_failures),
            )
            return

        update_task(
            db,
            task,
            phase_progress=40,
            message="资源预检查通过，正在组装策略输入 JSON",
        )

        update_task(
            db,
            task,
            phase_progress=60,
            message="策略输入 JSON 已生成，正在请求切分策略模型",
        )
        decision_result = await request_algorithm_decision(task_id, model_type, raw_input_json)

        update_task(
            db,
            task,
            strategy_payload=json.dumps(decision_result, ensure_ascii=False),
            phase="loading",
            phase_progress=0,
            message="切分策略计算完成，准备下发模型启动请求",
            queue_status=LOADING_RUNNING_STATUS,
            queue_position=0,
        )
        await promote_next_queued_strategy_task()
        await dispatch_loading_task(task_id)

    except httpx.TimeoutException:
        task = db.query(ScheduleTask).filter(ScheduleTask.task_id == task_id).first()
        if task:
            await fail_task_and_promote(
                db,
                task,
                "请求算法切分策略服务超时",
                f"请求算法切分策略服务超时 ({settings.ALGORITHM_API_TIMEOUT_SECONDS}s)",
            )
    except httpx.HTTPError as exc:
        task = db.query(ScheduleTask).filter(ScheduleTask.task_id == task_id).first()
        if task:
            await fail_task_and_promote(db, task, "请求算法切分策略服务失败", str(exc))
    except Exception as exc:
        logger.exception("调度任务执行异常: task_id=%s", task_id)
        task = db.query(ScheduleTask).filter(ScheduleTask.task_id == task_id).first()
        if task:
            await fail_task_and_promote(db, task, "调度任务执行失败", str(exc))
    finally:
        db.close()


async def handle_runtime_progress(payload, callback_role: str | None = None) -> dict:
    db = SessionLocal()
    try:
        task = db.query(ScheduleTask).filter(ScheduleTask.task_id == payload.task_id).first()
        if not task:
            return {"status": "error", "message": "未找到对应的调度任务", "http_status": 404}
        if task.status in TASK_TERMINAL_STATUSES:
            return {"status": "success", "message": "任务已处于终态，忽略重复回调"}

        resolved_node_role = callback_role or payload.node_role
        if not resolved_node_role:
            return {"status": "error", "message": "缺少 node_role，且未使用带角色的回调地址", "http_status": 400}

        node_role = resolved_node_role.lower()
        node_status = payload.status.lower()
        progress = clamp_progress(payload.progress)

        if node_role not in {"edge", "cloud"}:
            return {"status": "error", "message": "node_role 仅支持 edge 或 cloud", "http_status": 400}

        if node_status == "failed":
            await fail_task_and_promote(db, task, payload.message, f"{node_role} runtime failed")
            return {"status": "success", "message": "失败状态已记录"}

        update_kwargs = {
            "status": "running",
            "phase": "loading",
            "message": payload.message,
        }
        if node_role == "edge":
            update_kwargs["edge_progress"] = progress
            update_kwargs["edge_status"] = node_status
            update_kwargs["edge_message"] = payload.message
        else:
            update_kwargs["cloud_progress"] = progress
            update_kwargs["cloud_status"] = node_status
            update_kwargs["cloud_message"] = payload.message

        task = update_task(db, task, **update_kwargs)

        if (
            task.edge_progress >= 100
            and task.cloud_progress >= 100
            and task.edge_status in {"ready", "completed"}
            and task.cloud_status in {"ready", "completed"}
        ):
            await complete_task_and_promote(db, task)

        return {"status": "success", "message": "加载进度已记录"}
    finally:
        db.close()
