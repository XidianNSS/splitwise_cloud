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
)
from app.core.config import settings
from app.core.security import decode_openwebui_access_token
from app.db.database import SessionLocal
from app.models.models import EdgeSession, ScheduleTask
from app.schemas.schemas import (
    EdgeTriggerRequest,
    RuntimeProgressCallbackRequest,
    ScheduleTaskAcceptedResponse,
    ScheduleTaskStrategyResponse,
    ScheduleTaskStatusResponse,
)
from app.services.schedule_orchestrator import (
    TASK_TERMINAL_STATUSES,
    accept_schedule_task,
    handle_runtime_progress,
)
from app.services.schedule_presenter import (
    build_strategy_display_layer_partitions,
    build_strategy_display_summary,
    serialize_task,
)

router = APIRouter()
logger = logging.getLogger("ScheduleRouter")


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
        },
    }


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
async def receive_edge_runtime_progress(payload: RuntimeProgressCallbackRequest):
    result = await handle_runtime_progress(payload, callback_role="edge")
    if result.get("status") == "error":
        raise HTTPException(status_code=result.get("http_status", 400), detail=result["message"])
    return result


@router.post("/runtime_callback/cloud", summary="【推理节点专用】接收云端模型加载进度回调")
async def receive_cloud_runtime_progress(payload: RuntimeProgressCallbackRequest):
    result = await handle_runtime_progress(payload, callback_role="cloud")
    if result.get("status") == "error":
        raise HTTPException(status_code=result.get("http_status", 400), detail=result["message"])
    return result
