from __future__ import annotations

import logging
import os

from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import RuntimeSlot
from app.schemas.schemas import CloudRuntimeConfirmationRequest
from app.services.runtime_slot_service import update_runtime_slot_state

logger = logging.getLogger("RuntimeControlService")


async def fetch_runtime_state(slot: RuntimeSlot, *, timeout: float = 5.0) -> dict:
    if not slot.control_url:
        raise RuntimeError(f"runtime slot {slot.slot_id} 缺少 control_url")
    base_url = slot.control_url.removesuffix("/load_strategy")
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.get(f"{base_url}/runtime_state", timeout=timeout)
        response.raise_for_status()
        return response.json()


async def unload_runtime_slot(
    db: Session,
    slot: RuntimeSlot,
    *,
    reason: str,
    timeout: float = 10.0,
) -> dict:
    if not slot.control_url:
        raise RuntimeError(f"runtime slot {slot.slot_id} 缺少 control_url")
    base_url = slot.control_url.removesuffix("/load_strategy")
    update_runtime_slot_state(
        db,
        slot,
        slot_state="unloading",
        model_state="draining",
        last_used_at=datetime.utcnow(),
    )
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.post(
            f"{base_url}/unload_model",
            json={"reason": reason},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    idle_deadline = None
    process_idle_deadline = None
    if bool(getattr(slot, "spawned_by_scheduler", 0)):
        process_idle_deadline = datetime.utcnow() + timedelta(seconds=settings.CLOUD_SLOT_PROCESS_IDLE_TIMEOUT_SECONDS)
    update_runtime_slot_state(
        db,
        slot,
        slot_state="free",
        model_state="empty",
        owner_session_id=None,
        owner_binding_id=None,
        model_type=None,
        task_id=None,
        active_request_count=0,
        confirmation_status="none",
        idle_deadline=idle_deadline,
        process_idle_deadline=process_idle_deadline,
        last_used_at=datetime.utcnow(),
    )
    return payload


async def forward_cloud_confirmation_to_edge(
    *,
    edge_slot: RuntimeSlot,
    payload: CloudRuntimeConfirmationRequest,
    timeout: float | None = None,
) -> tuple[bool, str | None]:
    if not edge_slot.control_url:
        return False, "edge slot 缺少 control_url"
    base_url = edge_slot.control_url.rsplit("/load_strategy", 1)[0]
    timeout = timeout if timeout is not None else settings.RUNTIME_CONFIRMATION_FORWARD_TIMEOUT_SECONDS
    token = os.getenv("RUNTIME_INTEGRITY_TOKEN", "").strip()
    if not token:
        return False, "RUNTIME_INTEGRITY_TOKEN 未配置"
    request_payload = {
        "task_id": payload.task_id,
        "model_type": payload.model_type,
        "server_param_digest": payload.server_param_digest,
        "partition_digest": payload.partition_digest,
        "timestamp": payload.timestamp,
        "nonce": payload.nonce,
    }
    target_url = f"{base_url}/integrity/cloud_confirmation"
    logger.info(
        "Forwarding cloud confirmation to edge: task_id=%s edge_slot=%s target=%s timeout=%s",
        payload.task_id,
        edge_slot.slot_id,
        target_url,
        timeout,
    )
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(
                target_url,
                json=request_payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload_json = response.json()
    except httpx.HTTPError as exc:
        logger.warning(
            "Failed to forward cloud confirmation: task_id=%s edge_slot=%s target=%s error=%s",
            payload.task_id,
            edge_slot.slot_id,
            target_url,
            exc,
        )
        return False, str(exc)
    matched = bool(payload_json.get("matched"))
    reason = payload_json.get("reason")
    logger.info(
        "Edge cloud confirmation response: task_id=%s edge_slot=%s matched=%s reason=%s",
        payload.task_id,
        edge_slot.slot_id,
        matched,
        reason,
    )
    return matched, reason
