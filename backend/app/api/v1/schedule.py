import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import (
    get_current_edge_session,
    get_current_openwebui_user_id,
    get_db,
    verify_runtime_integrity_token,
)
from app.core.config import settings
from app.core.security import decode_openwebui_access_token
from app.db.database import SessionLocal
from app.models.models import EdgeSession, RuntimeBinding, RuntimeSlot, ScheduleTask
from app.schemas.schemas import (
    CloudRuntimeConfirmationRequest,
    CloudRuntimeConfirmationResponse,
    EdgeTriggerRequest,
    ModelCatalogEntry,
    RuntimeProgressCallbackRequest,
    RuntimeBindingStatusResponse,
    RuntimeSlotStatusResponse,
    ScheduleTaskAcceptedResponse,
    ScheduleTaskStrategyResponse,
    ScheduleTaskStatusResponse,
)
from app.services.schedule_orchestrator import (
    TASK_TERMINAL_STATUSES,
    accept_schedule_task,
    handle_runtime_progress,
    resolve_current_runtime_allocation,
)
from app.services.model_registry import canonicalize_model_type, list_model_catalog
from app.services.schedule_queue import LOADING_RUNNING_STATUS, WAITING_CLOUD_SLOT_STATUS
from app.services.runtime_control_service import forward_cloud_confirmation_to_edge
from app.services.runtime_state_transition_service import (
    RuntimeAllocationIdentity,
    RuntimeTransitionConflict,
    transition_runtime_slot,
)
from app.services.schedule_presenter import (
    build_strategy_display_layer_partitions,
    build_strategy_display_summary,
    serialize_runtime_binding,
    serialize_runtime_slot,
    serialize_task,
)

router = APIRouter()
logger = logging.getLogger("ScheduleRouter")


@router.get("/models", response_model=list[ModelCatalogEntry], summary="查询可调度模型及其能力")
async def list_schedule_models(
    current_openwebui_user_id: str = Depends(get_current_openwebui_user_id),
):
    _ = current_openwebui_user_id
    return list_model_catalog()


def decode_query_token_to_openwebui_user_id(token: str) -> str:
    payload = decode_openwebui_access_token(token)
    external_user_id = payload.get(settings.OPENWEBUI_USER_ID_CLAIM)
    if not isinstance(external_user_id, str) or not external_user_id.strip():
        raise HTTPException(status_code=401, detail="OpenWebUI token 缺少可识别的用户唯一 ID")
    return external_user_id.strip()


@router.post("/trigger", response_model=ScheduleTaskAcceptedResponse, status_code=202, summary="接收边端触发，异步启动调度任务")
async def collect_raw_json(
    request: EdgeTriggerRequest,
    current_openwebui_user_id: str = Depends(get_current_openwebui_user_id),
    edge_session: EdgeSession = Depends(get_current_edge_session),
    db: Session = Depends(get_db),
):
    if edge_session.cloud_device_id != "cloud":
        raise HTTPException(status_code=400, detail="当前阶段仅支持固定云端设备 cloud")

    return await accept_schedule_task(
        db=db,
        openwebui_user_id=current_openwebui_user_id,
        edge_session=edge_session,
        requested_model_type=request.model_type,
    )


@router.get("/runtime/slots", response_model=list[RuntimeSlotStatusResponse], summary="查询当前 runtime slot 状态")
async def list_runtime_slots(
    current_openwebui_user_id: str = Depends(get_current_openwebui_user_id),
    db: Session = Depends(get_db),
):
    _ = current_openwebui_user_id
    slots = db.query(RuntimeSlot).order_by(RuntimeSlot.role.asc(), RuntimeSlot.slot_id.asc()).all()
    return [serialize_runtime_slot(slot) for slot in slots]


@router.get("/runtime/bindings", response_model=list[RuntimeBindingStatusResponse], summary="查询当前 runtime binding 状态")
async def list_runtime_bindings(
    current_openwebui_user_id: str = Depends(get_current_openwebui_user_id),
    db: Session = Depends(get_db),
):
    _ = current_openwebui_user_id
    bindings = db.query(RuntimeBinding).order_by(RuntimeBinding.created_at.asc(), RuntimeBinding.binding_id.asc()).all()
    return [serialize_runtime_binding(binding) for binding in bindings]


@router.get("/queue/loading", response_model=list[ScheduleTaskStatusResponse], summary="查询当前 loading/waiting 队列状态")
async def list_loading_queue(
    current_openwebui_user_id: str = Depends(get_current_openwebui_user_id),
    db: Session = Depends(get_db),
):
    tasks = (
        db.query(ScheduleTask)
        .filter(
            ScheduleTask.openwebui_user_id == current_openwebui_user_id,
            ScheduleTask.phase == "loading",
            ScheduleTask.queue_status.in_([LOADING_RUNNING_STATUS, WAITING_CLOUD_SLOT_STATUS]),
            ScheduleTask.status.in_(["accepted", "running"]),
        )
        .order_by(ScheduleTask.created_at.asc(), ScheduleTask.task_id.asc())
        .all()
    )
    return [serialize_task(task) for task in tasks]


@router.get("/tasks/{task_id}", response_model=ScheduleTaskStatusResponse, summary="查询调度任务状态")
async def get_schedule_task_status(
    task_id: str,
    current_openwebui_user_id: str = Depends(get_current_openwebui_user_id),
    db: Session = Depends(get_db),
):
    task = (
        db.query(ScheduleTask)
        .filter(ScheduleTask.task_id == task_id, ScheduleTask.openwebui_user_id == current_openwebui_user_id)
        .first()
    )
    if not task:
        raise HTTPException(status_code=404, detail="未找到该调度任务")
    return serialize_task(task)


@router.get("/tasks/{task_id}/strategy", response_model=ScheduleTaskStrategyResponse, summary="获取调度任务的切分策略")
async def get_schedule_task_strategy(
    task_id: str,
    current_openwebui_user_id: str = Depends(get_current_openwebui_user_id),
    db: Session = Depends(get_db),
):
    task = (
        db.query(ScheduleTask)
        .filter(ScheduleTask.task_id == task_id, ScheduleTask.openwebui_user_id == current_openwebui_user_id)
        .first()
    )
    if not task:
        raise HTTPException(status_code=404, detail="未找到该调度任务")
    if not task.strategy_payload:
        raise HTTPException(status_code=409, detail="切分策略尚未生成，请在进入 loading 阶段后再拉取")

    try:
        decision = json.loads(task.strategy_payload)
    except json.JSONDecodeError as exc:
        logger.exception("任务策略反序列化失败: task_id=%s", task_id)
        raise HTTPException(status_code=500, detail="任务切分策略解析失败") from exc

    display_layers = build_strategy_display_layer_partitions(decision.get("layer_partitions", []))
    display_summary = build_strategy_display_summary(display_layers)

    return {
        "task_id": task.task_id,
        "model_type": task.model_type,
        "decision": {
            "layer_partitions": display_layers,
            "edge_head_count_total": display_summary["edge_head_count_total"],
            "cloud_head_count_total": display_summary["cloud_head_count_total"],
            "strategy_kind": decision.get("strategy_kind"),
            "capability": decision.get("capability"),
            "deployment_mode": decision.get("deployment_mode"),
        },
    }


@router.post(
    "/runtime/confirmation/cloud",
    response_model=CloudRuntimeConfirmationResponse,
    summary="接收 cloud runtime 完整性确认并中转到 edge runtime",
)
async def confirm_cloud_runtime_integrity(
    payload: CloudRuntimeConfirmationRequest,
    _verified: None = Depends(verify_runtime_integrity_token),
    db: Session = Depends(get_db),
):
    cloud_context, stale_reason = resolve_current_runtime_allocation(
        db,
        task_id=payload.task_id,
        node_role="cloud",
    )
    if cloud_context is None and stale_reason == "task not found":
        raise HTTPException(status_code=404, detail="未找到对应的调度任务")
    if cloud_context is None:
        logger.warning(
            "忽略非当前 allocation 的 cloud confirmation: task_id=%s reason=%s",
            payload.task_id,
            stale_reason,
        )
        return CloudRuntimeConfirmationResponse(
            matched=False,
            reason=f"stale allocation ignored: {stale_reason}",
        )

    edge_context, edge_reason = resolve_current_runtime_allocation(
        db,
        task_id=payload.task_id,
        node_role="edge",
    )
    if edge_context is None:
        logger.warning(
            "忽略缺少当前 edge allocation 的 cloud confirmation: task_id=%s reason=%s",
            payload.task_id,
            edge_reason,
        )
        return CloudRuntimeConfirmationResponse(
            matched=False,
            reason=f"stale edge allocation ignored: {edge_reason}",
        )

    task = cloud_context.task
    cloud_slot = cloud_context.slot
    edge_slot = edge_context.slot
    cloud_slot_id = cloud_slot.slot_id
    edge_slot_id = edge_slot.slot_id
    if payload.cloud_slot_id != cloud_slot_id:
        return CloudRuntimeConfirmationResponse(
            matched=False,
            reason="stale allocation ignored: cloud_slot_id mismatch",
        )
    if (
        payload.model_type
        and canonicalize_model_type(payload.model_type)
        != canonicalize_model_type(task.model_type)
    ):
        return CloudRuntimeConfirmationResponse(
            matched=False,
            reason="stale allocation ignored: model_type mismatch",
        )

    logger.info(
        "Received cloud confirmation: task_id=%s cloud_slot_id=%s allocated_cloud_slot_id=%s edge_slot_id=%s",
        payload.task_id,
        payload.cloud_slot_id,
        cloud_slot_id,
        edge_slot_id,
    )
    matched, reason = await forward_cloud_confirmation_to_edge(
        edge_slot=edge_slot,
        payload=payload,
    )

    db.rollback()
    refreshed_cloud_context, refreshed_reason = resolve_current_runtime_allocation(
        db,
        task_id=payload.task_id,
        node_role="cloud",
    )
    refreshed_edge_context, refreshed_edge_reason = resolve_current_runtime_allocation(
        db,
        task_id=payload.task_id,
        node_role="edge",
    )
    if refreshed_cloud_context is None or refreshed_edge_context is None:
        stale_after_forward = refreshed_reason if refreshed_cloud_context is None else refreshed_edge_reason
        logger.warning(
            "cloud confirmation 转发期间 allocation 已变化，忽略结果: task_id=%s reason=%s",
            payload.task_id,
            stale_after_forward,
        )
        return CloudRuntimeConfirmationResponse(
            matched=False,
            reason=f"stale allocation ignored after forwarding: {stale_after_forward}",
        )
    if (
        refreshed_cloud_context.slot.slot_id != cloud_slot_id
        or refreshed_edge_context.slot.slot_id != edge_slot_id
    ):
        return CloudRuntimeConfirmationResponse(
            matched=False,
            reason="stale allocation ignored after forwarding: slot mapping changed",
        )

    logger.info(
        "Cloud confirmation result: task_id=%s cloud_slot_id=%s edge_slot_id=%s matched=%s reason=%s",
        payload.task_id,
        cloud_slot_id,
        edge_slot_id,
        matched,
        reason,
    )

    try:
        transition_runtime_slot(
            db,
            refreshed_cloud_context.slot,
            expected_allocation=RuntimeAllocationIdentity(
                session_id=refreshed_cloud_context.session.session_id,
                binding_id=refreshed_cloud_context.binding.binding_id,
                task_id=payload.task_id,
            ),
            confirmation_status="passed" if matched else "failed",
        )
    except RuntimeTransitionConflict:
        return CloudRuntimeConfirmationResponse(
            matched=False,
            reason="stale allocation ignored while committing confirmation",
        )

    return CloudRuntimeConfirmationResponse(matched=matched, reason=reason)


@router.get("/tasks/{task_id}/stream", summary="SSE 推送调度任务进度")
async def stream_schedule_task_status(task_id: str, token: str = Query(...)):
    current_openwebui_user_id = decode_query_token_to_openwebui_user_id(token)

    async def event_generator():
        while True:
            db = SessionLocal()
            try:
                task = (
                    db.query(ScheduleTask)
                    .filter(
                        ScheduleTask.task_id == task_id,
                        ScheduleTask.openwebui_user_id == current_openwebui_user_id,
                    )
                    .first()
                )
                if not task:
                    payload = json.dumps({"status": "error", "message": "未找到该调度任务"}, ensure_ascii=False)
                    yield f"data: {payload}\n\n"
                    break

                payload = json.dumps(serialize_task(task), ensure_ascii=False)
                yield f"data: {payload}\n\n"

                if task.status in TASK_TERMINAL_STATUSES:
                    break
            finally:
                db.close()

            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@router.post("/runtime_callback/edge", summary="【推理节点专用】接收边端模型加载进度回调")
async def receive_edge_runtime_progress(
    payload: RuntimeProgressCallbackRequest,
    _verified: None = Depends(verify_runtime_integrity_token),
):
    result = await handle_runtime_progress(payload, callback_role="edge")
    if result.get("status") == "error":
        raise HTTPException(status_code=result.get("http_status", 400), detail=result["message"])
    return result


@router.post("/runtime_callback/cloud", summary="【推理节点专用】接收云端模型加载进度回调")
async def receive_cloud_runtime_progress(
    payload: RuntimeProgressCallbackRequest,
    _verified: None = Depends(verify_runtime_integrity_token),
):
    result = await handle_runtime_progress(payload, callback_role="cloud")
    if result.get("status") == "error":
        raise HTTPException(status_code=result.get("http_status", 400), detail=result["message"])
    return result
