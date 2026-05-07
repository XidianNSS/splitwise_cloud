from datetime import datetime, timedelta
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import (
    extract_ips,
    get_current_openwebui_payload,
    get_current_openwebui_user_id,
    get_db,
    resolve_request_source_ip,
    resolve_edge_device_by_ip,
)
from app.models.models import Device, EdgeSession
from app.core.security import extract_claim
from app.core.config import settings
from app.schemas.schemas import SessionInitRequest, SessionInitResponse

router = APIRouter()


@router.post("/session/init", response_model=SessionInitResponse, summary="使用 OpenWebUI token 初始化普通用户会话")
async def init_openwebui_session(
    request: Request,
    payload: SessionInitRequest,
    openwebui_payload: dict = Depends(get_current_openwebui_payload),
    openwebui_user_id: str = Depends(get_current_openwebui_user_id),
    db: Session = Depends(get_db),
):
    openwebui_username = extract_claim(openwebui_payload, settings.OPENWEBUI_USERNAME_CLAIMS)
    openwebui_role = extract_claim(openwebui_payload, settings.OPENWEBUI_ROLE_CLAIMS)
    edge_ip = (payload.edge_device_ip or "").strip()
    if not edge_ip:
        edge_ip = (resolve_request_source_ip(request) or "").strip()
    if not edge_ip:
        raise HTTPException(status_code=400, detail="edge_device_ip 为空，且无法从请求来源解析边端设备 IP")
    edge_device = resolve_edge_device_by_ip(edge_ip, db)
    cloud_device = db.query(Device).filter(Device.id == "cloud").first()
    if not cloud_device:
        raise HTTPException(status_code=500, detail="未找到固定云端设备 cloud")
    cloud_ips = extract_ips(cloud_device.value)
    if not cloud_ips:
        raise HTTPException(status_code=500, detail="固定云端设备 cloud 缺少可识别的 IP")

    now = datetime.utcnow()
    active_session = (
        db.query(EdgeSession)
        .filter(
            EdgeSession.openwebui_user_id == openwebui_user_id,
            EdgeSession.edge_device_id == edge_device.id,
            EdgeSession.edge_ip == edge_ip,
            EdgeSession.status == "active",
            EdgeSession.expires_at > now,
        )
        .order_by(EdgeSession.updated_at.desc())
        .first()
    )

    if active_session:
        active_session.updated_at = now
        active_session.cloud_device_id = "cloud"
        db.add(active_session)
        db.commit()
        db.refresh(active_session)
        session_id = active_session.session_id
    else:
        expires_at = now + timedelta(hours=2)
        new_session = EdgeSession(
            session_id=str(uuid.uuid4()),
            openwebui_user_id=openwebui_user_id,
            edge_device_id=edge_device.id,
            edge_ip=edge_ip,
            cloud_device_id="cloud",
            model_type=None,
            status="active",
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )
        db.add(new_session)
        db.commit()
        db.refresh(new_session)
        session_id = new_session.session_id

    return {
        "session_id": session_id,
        "openwebui_user_id": openwebui_user_id,
        "openwebui_username": openwebui_username if isinstance(openwebui_username, str) else None,
        "openwebui_role": openwebui_role if isinstance(openwebui_role, str) else None,
        "edge_device": {
            "id": edge_device.id,
            "name": edge_device.name,
            "type": edge_device.device_type,
            "ip": edge_ip,
        },
        "cloud_device": {
            "id": cloud_device.id,
            "name": cloud_device.name,
            "type": cloud_device.device_type,
            "ip": cloud_ips[0],
        },
        "message": "OpenWebUI token 校验通过，边端设备识别完成，会话初始化成功",
    }
