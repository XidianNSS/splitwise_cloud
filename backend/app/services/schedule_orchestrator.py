import asyncio
import json
import logging
import uuid
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import SessionLocal
from app.models.models import Device, EdgeSession, RuntimeSlot, ScheduleTask
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
from app.services.decode_server_process_manager import current_runtime_env_metadata, start_decode_server_process, start_decode_server_process_for_slot, wait_for_slot_health
from app.services.runtime_startup_admission import check_runtime_startup_resources
from app.services.schedule_presenter import clamp_progress
from app.services.runtime_control_service import unload_runtime_slot
from app.services.runtime_binding_service import create_runtime_binding, update_runtime_binding
from app.services.runtime_slot_service import (
    ensure_runtime_slot,
    get_cloud_slot_by_id,
    get_running_free_cloud_slot,
    get_stopped_free_cloud_slot,
    list_cloud_slots,
    update_runtime_slot_state,
)
from app.services.session_lease_service import refresh_session_lease
from app.services.schedule_queue import (
    LOADING_RUNNING_STATUS,
    WAITING_CLOUD_SLOT_STATUS,
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
RUNTIME_APP_ENV, RUNTIME_ENV_FILE = current_runtime_env_metadata()


def _scheduler_confirmation_callback_url() -> str:
    path = settings.RUNTIME_CONFIRMATION_PATH.strip() or "/api/v1/schedule/runtime/confirmation/cloud"
    if path == "/api/v1/runtime/confirmation/cloud":
        path = "/api/v1/schedule/runtime/confirmation/cloud"
    if not path.startswith("/"):
        path = "/" + path
    return f"{settings.BACKEND_BASE_URL.rstrip('/')}" + path


def _next_cloud_slot_index(db: Session) -> int:
    cloud_slots = list_cloud_slots(db)
    if not cloud_slots:
        return 0
    return max(slot.slot_index for slot in cloud_slots) + 1


async def allocate_cloud_slot_for_task(db: Session, task: ScheduleTask, cloud_ip: str) -> tuple[RuntimeSlot, bool]:
    free_slot = get_running_free_cloud_slot(db)
    if free_slot is not None and free_slot.owner_binding_id in {None, task.runtime_binding_id}:
        return free_slot, False

    stopped_slot = get_stopped_free_cloud_slot(db)
    if stopped_slot is not None and stopped_slot.owner_binding_id in {None, task.runtime_binding_id}:
        process_info = start_decode_server_process_for_slot(stopped_slot.slot_id, stopped_slot.slot_index)
        health_ok = await wait_for_slot_health(process_info.control_url)
        if not health_ok:
            from app.services.decode_server_process_manager import stop_slot_process
            stop_slot_process(process_info.slot_id, process_pid=process_info.process_pid)
            raise RuntimeError(f"cloud slot {process_info.slot_id} 重启后健康检查失败")

        slot = ensure_runtime_slot(
            db,
            slot_id=process_info.slot_id,
            role="cloud",
            control_url=process_info.control_url,
            grpc_target=process_info.grpc_target,
            process_state="running",
            slot_index=process_info.slot_index,
            spawned_by_scheduler=True,
            base_env_name=RUNTIME_ENV_FILE,
            process_pid=process_info.process_pid,
        )
        update_runtime_slot_state(
            db,
            slot,
            slot_state="free",
            model_state="empty",
            owner_session_id=None,
            owner_binding_id=None,
            model_type=None,
            task_id=None,
            process_idle_deadline=None,
        )
        return slot, True

    slot_index = _next_cloud_slot_index(db)
    if slot_index >= settings.CLOUD_SLOT_MAX_COUNT:
        raise RuntimeError("没有可用的 cloud decode slot，且已达到开发环境最大 slot 数")

    process_info = start_decode_server_process(slot_index)
    health_ok = await wait_for_slot_health(process_info.control_url)
    if not health_ok:
        from app.services.decode_server_process_manager import stop_slot_process
        stop_slot_process(process_info.slot_id, process_pid=process_info.process_pid)
        raise RuntimeError(f"cloud slot {process_info.slot_id} 启动后健康检查失败")

    slot = ensure_runtime_slot(
        db,
        slot_id=process_info.slot_id,
        role="cloud",
        control_url=process_info.control_url,
        grpc_target=process_info.grpc_target,
        process_state="running",
        slot_index=slot_index,
        spawned_by_scheduler=True,
        base_env_name=RUNTIME_ENV_FILE,
        process_pid=process_info.process_pid,
    )
    return slot, True


async def accept_schedule_task(
    *,
    db: Session,
    openwebui_user_id: str,
    edge_session: EdgeSession,
    requested_model_type: str,
) -> dict:
    canonical_model_type = canonicalize_model_type(requested_model_type)

    existing_active_task = (
        db.query(ScheduleTask)
        .filter(
            ScheduleTask.edge_session_id == edge_session.session_id,
            ScheduleTask.status.in_(["accepted", "running"]),
            ScheduleTask.queue_status != "done",
        )
        .order_by(ScheduleTask.created_at.desc(), ScheduleTask.task_id.desc())
        .first()
    )
    if existing_active_task is not None:
        return {
            "status": "rejected",
            "task_id": existing_active_task.task_id,
            "phase": existing_active_task.phase,
            "phase_progress": existing_active_task.phase_progress,
            "overall_progress": existing_active_task.overall_progress,
            "message": "当前会话已有未完成的调度任务，请等待其完成或关闭会话后再发起新的模型加载",
        }

    edge_session.model_type = canonical_model_type
    edge_session = refresh_session_lease(db, edge_session)

    task_id = str(uuid.uuid4())
    edge_device_id = edge_session.edge_device_id
    cloud_device_id = edge_session.cloud_device_id

    async with STRATEGY_ADMISSION_LOCK:
        running_strategy_task = find_running_strategy_task(db)
        queued_strategy_count = count_queued_strategy_tasks(db)
        is_queued = running_strategy_task is not None or queued_strategy_count > 0

        edge_slot = ensure_runtime_slot(
            db,
            slot_id=f"edge-slot-{edge_device_id}",
            role="edge",
            control_url=build_runtime_control_url("edge", edge_session.edge_ip or settings.EDGE_RUNTIME_REAL_HOST or "127.0.0.1"),
        )
        cloud_slot = ensure_runtime_slot(
            db,
            slot_id="cloud-slot-0",
            role="cloud",
            process_state="stopped",
            slot_index=0,
            spawned_by_scheduler=True,
            base_env_name=RUNTIME_ENV_FILE,
        )
        binding = create_runtime_binding(
            db,
            session_id=edge_session.session_id,
            task_id=task_id,
            edge_slot_id=edge_slot.slot_id,
            cloud_slot_id=cloud_slot.slot_id,
        )

        task = ScheduleTask(
            task_id=task_id,
            openwebui_user_id=openwebui_user_id,
            edge_session_id=edge_session.session_id,
            runtime_binding_id=binding.binding_id,
            edge_slot_id=edge_slot.slot_id,
            cloud_slot_id=cloud_slot.slot_id,
            allocated_cloud_slot_id=cloud_slot.slot_id,
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


async def promote_waiting_loading_task() -> bool:
    db = SessionLocal()
    try:
        next_task = (
            db.query(ScheduleTask)
            .filter(
                ScheduleTask.queue_status == WAITING_CLOUD_SLOT_STATUS,
                ScheduleTask.status == "accepted",
                ScheduleTask.phase == "loading",
            )
            .order_by(ScheduleTask.created_at.asc(), ScheduleTask.task_id.asc())
            .first()
        )
        if not next_task:
            return False

        update_task(
            db,
            next_task,
            status="running",
            queue_status=LOADING_RUNNING_STATUS,
            queue_position=0,
            message="cloud slot 已空闲，正在继续下发模型启动请求",
            edge_status="dispatching",
            cloud_status="dispatching",
            edge_message="边端控制入口正在接收模型启动请求",
            cloud_message="云端控制入口正在接收模型启动请求",
            edge_strategy_progress=100,
            cloud_strategy_progress=100,
        )
        task_id = next_task.task_id
    finally:
        db.close()

    asyncio.create_task(dispatch_loading_task(task_id))
    logger.info("已自动推进 cloud slot 等待任务: task_id=%s", task_id)
    return True


async def cleanup_spawned_cloud_slot_after_dispatch_failure(db: Session, cloud_slot: RuntimeSlot) -> None:
    from app.services.decode_server_process_manager import stop_slot_process

    stop_ok = stop_slot_process(cloud_slot.slot_id, process_pid=cloud_slot.process_pid)
    if stop_ok:
        update_runtime_slot_state(
            db,
            cloud_slot,
            slot_state="free",
            model_state="empty",
            process_state="stopped",
            owner_session_id=None,
            owner_binding_id=None,
            model_type=None,
            task_id=None,
            process_pid=None,
            confirmation_status="none",
            process_idle_deadline=None,
            last_used_at=datetime.utcnow(),
        )
    else:
        update_runtime_slot_state(
            db,
            cloud_slot,
            slot_state="needs_reconcile",
            model_state="failed",
            process_state="failed",
            last_used_at=datetime.utcnow(),
        )


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

        edge_slot = ensure_runtime_slot(
            db,
            slot_id=task.edge_slot_id or f"edge-slot-{task.edge_device_id}",
            role="edge",
            control_url=build_runtime_control_url("edge", edge_ip),
        )
        try:
            cloud_slot, spawned_cloud_slot = await allocate_cloud_slot_for_task(db, task, cloud_ip)
        except Exception as exc:
            await fail_task_and_promote(db, task, "云端 decode slot 分配失败", str(exc))
            return

        binding = None
        if task.runtime_binding_id:
            from app.models.models import RuntimeBinding
            binding = db.query(RuntimeBinding).filter(RuntimeBinding.binding_id == task.runtime_binding_id).first()
            if binding is not None:
                update_runtime_binding(db, binding, cloud_slot_id=cloud_slot.slot_id)

        update_runtime_slot_state(
            db,
            edge_slot,
            owner_session_id=task.edge_session_id,
            owner_binding_id=task.runtime_binding_id,
            task_id=task.task_id,
            model_type=task.model_type,
            slot_state="bound",
            model_state="loading",
            process_state="running",
        )
        update_runtime_slot_state(
            db,
            cloud_slot,
            owner_session_id=task.edge_session_id,
            owner_binding_id=task.runtime_binding_id,
            task_id=task.task_id,
            model_type=task.model_type,
            slot_state="bound",
            model_state="loading",
            process_state="running",
            confirmation_status="pending",
        )
        task = update_task(
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
            edge_strategy_progress=100,
            cloud_strategy_progress=100,
            edge_integrity_progress=0,
            cloud_integrity_progress=0,
            edge_runtime_load_progress=0,
            cloud_runtime_load_progress=0,
            edge_message="边端控制入口正在接收模型启动请求",
            cloud_message="云端控制入口正在接收模型启动请求",
            cloud_slot_id=cloud_slot.slot_id,
            allocated_cloud_slot_id=cloud_slot.slot_id,
            spawned_cloud_slot=cloud_slot.slot_id if spawned_cloud_slot else None,
        )

        runtime_decision_payload = {
            "layer_partitions": decision_result["layer_partitions"],
        }
        runtime_model_name = runtime_model_type(model_type)
        runtime_route = {
            "cloud_slot_id": cloud_slot.slot_id,
            "cloud_control_url": cloud_slot.control_url,
            "cloud_decode_grpc_target": cloud_slot.grpc_target,
            "scheduler_integrity_callback_url": _scheduler_confirmation_callback_url(),
        }
        edge_dispatch_payload = {
            "task_id": task_id,
            "model_type": runtime_model_name,
            "decision": runtime_decision_payload,
            "runtime_route": runtime_route,
        }
        cloud_dispatch_payload = {
            "task_id": task_id,
            "model_type": runtime_model_name,
            "decision": runtime_decision_payload,
            "runtime_route": runtime_route,
        }

        logger.info(
            "准备下发模型启动请求体: task_id=%s, edge_payload=%s, cloud_slot=%s",
            task_id,
            json.dumps(edge_dispatch_payload, ensure_ascii=False),
            cloud_slot.slot_id,
        )

        results = await asyncio.gather(
            dispatch_strategy_to_runtime(
                node_role="edge",
                device_ip=edge_ip,
                payload=edge_dispatch_payload,
                control_url=edge_slot.control_url,
            ),
            dispatch_strategy_to_runtime(
                node_role="cloud",
                device_ip=cloud_ip,
                payload=cloud_dispatch_payload,
                control_url=cloud_slot.control_url,
            ),
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
            if spawned_cloud_slot:
                await cleanup_spawned_cloud_slot_after_dispatch_failure(db, cloud_slot)
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
            edge_strategy_progress=100,
            cloud_strategy_progress=100,
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
    if previous_queue_status == LOADING_RUNNING_STATUS:
        await promote_waiting_loading_task()


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
    if previous_queue_status == LOADING_RUNNING_STATUS:
        await promote_waiting_loading_task()


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
            edge_strategy_progress=5,
            cloud_strategy_progress=5,
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
            edge_strategy_progress=15,
            cloud_strategy_progress=15,
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
            edge_strategy_progress=30,
            cloud_strategy_progress=30,
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
            edge_strategy_progress=30,
            cloud_strategy_progress=30,
        )

        update_task(
            db,
            task,
            phase_progress=60,
            message="策略输入 JSON 已生成，正在请求切分策略模型",
            edge_strategy_progress=60,
            cloud_strategy_progress=60,
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
            edge_strategy_progress=100,
            cloud_strategy_progress=100,
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
        slot_id = task.edge_slot_id if node_role == "edge" else task.cloud_slot_id
        stage = (payload.stage or "runtime_load").strip().lower()
        if slot_id:
            slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == slot_id).first()
            if slot is not None:
                slot_fields = {
                    "active_request_count": 0,
                    "last_used_at": datetime.utcnow(),
                }
                if node_status in {"loading", "dispatching"}:
                    slot_fields["model_state"] = "loading"
                    slot_fields["slot_state"] = "bound"
                elif node_status in {"ready", "completed"}:
                    slot_fields["model_state"] = "ready"
                    slot_fields["slot_state"] = "bound"
                    slot_fields["integrity_status"] = "healthy"
                update_runtime_slot_state(db, slot, **slot_fields)
        if node_role == "edge":
            update_kwargs["edge_status"] = node_status
            update_kwargs["edge_message"] = payload.message
            if stage == "integrity":
                update_kwargs["edge_integrity_progress"] = progress
            else:
                update_kwargs["edge_runtime_load_progress"] = progress
                if node_status in {"ready", "completed"} and task.edge_integrity_progress == 0:
                    update_kwargs["edge_integrity_progress"] = 100
        else:
            update_kwargs["cloud_status"] = node_status
            update_kwargs["cloud_message"] = payload.message
            if stage == "integrity":
                update_kwargs["cloud_integrity_progress"] = progress
            else:
                update_kwargs["cloud_runtime_load_progress"] = progress
                if node_status in {"ready", "completed"} and task.cloud_integrity_progress == 0:
                    update_kwargs["cloud_integrity_progress"] = 100

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
