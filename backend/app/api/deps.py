import os
from datetime import datetime

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jwt import PyJWTError

from app.core.config import settings
from app.core.security import decode_internal_access_token, decode_openwebui_access_token
from app.db.database import SessionLocal
from app.models.models import Device, EdgeSession, User

security = HTTPBearer()


def decode_token_to_username(token: str) -> str:
    try:
        payload = decode_internal_access_token(token)
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Token 数据异常")
        return username
    except PyJWTError:
        raise HTTPException(status_code=401, detail="Token 已过期或被篡改，请重新登录")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """基础门禁：校验 Token"""
    token = credentials.credentials
    return decode_token_to_username(token)


async def get_current_openwebui_payload(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    token = credentials.credentials
    try:
        return decode_openwebui_access_token(token)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PyJWTError as exc:
        raise HTTPException(status_code=401, detail="OpenWebUI token 无效或已过期") from exc


async def get_current_openwebui_user_id(
    payload: dict = Depends(get_current_openwebui_payload),
):
    external_user_id = payload.get(settings.OPENWEBUI_USER_ID_CLAIM)
    if not isinstance(external_user_id, str) or not external_user_id.strip():
        raise HTTPException(status_code=401, detail="OpenWebUI token 缺少可识别的用户唯一 ID")
    return external_user_id.strip()


def extract_ips(device_value: str | None) -> list[str]:
    if not device_value:
        return []
    parts = device_value.replace("|", ",").split(",")
    results: list[str] = []
    for part in parts:
        candidate = part.strip()
        if not candidate:
            continue
        host = candidate.split(":")[0].strip()
        if host:
            results.append(host)
    return results


def resolve_edge_device_by_ip(edge_device_ip: str, db) -> Device:
    candidate_ip = edge_device_ip.strip()
    if not candidate_ip:
        raise HTTPException(status_code=400, detail="edge_device_ip 不能为空")

    devices = db.query(Device).filter(Device.device_type == "edge").all()
    for device in devices:
        if candidate_ip in extract_ips(device.value):
            return device

    raise HTTPException(status_code=403, detail=f"edge_device_ip={candidate_ip} 未匹配到已登记的边端设备")


def resolve_request_source_ip(request: Request) -> str | None:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        candidate = forwarded_for.split(",")[0].strip()
        if candidate:
            return candidate

    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip

    if request.client and request.client.host:
        return request.client.host.strip()

    return None


async def get_current_edge_session(
    db = Depends(get_db),
    openwebui_user_id: str = Depends(get_current_openwebui_user_id),
    session_id: str = Header(..., alias="Session-Id"),
):
    edge_session = (
        db.query(EdgeSession)
        .filter(
            EdgeSession.session_id == session_id,
            EdgeSession.status == "active",
        )
        .first()
    )
    if not edge_session:
        raise HTTPException(status_code=401, detail="会话不存在或已失效，请重新初始化")

    if edge_session.openwebui_user_id != openwebui_user_id:
        raise HTTPException(status_code=403, detail="会话所属用户与当前 OpenWebUI token 不一致")

    now = datetime.utcnow()
    lease_expires_at = edge_session.lease_expires_at if getattr(edge_session, "lease_expires_at", None) is not None else edge_session.expires_at
    if lease_expires_at <= now:
        edge_session.status = "expired"
        edge_session.lease_expires_at = edge_session.expires_at if getattr(edge_session, "lease_expires_at", None) is None else edge_session.lease_expires_at
        db.add(edge_session)
        db.commit()
        raise HTTPException(status_code=401, detail="会话已过期，请重新初始化")

    edge_session.updated_at = now
    edge_session.last_active_at = now
    db.add(edge_session)
    db.commit()
    db.refresh(edge_session)
    return edge_session

async def get_current_admin(
    username: str = Depends(get_current_user),
    db = Depends(get_db)  # 直接使用上面的 get_db 依赖
):
    """高级门禁：必须是 admin"""
    user = db.query(User).filter(User.username == username).first()
    if not user or user.role != "admin":
        raise HTTPException(status_code=403, detail="权限不足！只有管理员可执行此操作")
    return user


async def verify_runtime_integrity_token(authorization: str = Header(..., alias="Authorization")) -> None:
    expected = os.getenv("RUNTIME_INTEGRITY_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="RUNTIME_INTEGRITY_TOKEN 未配置")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="缺少 Bearer token")
    token = authorization[len(prefix):].strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="runtime integrity token 无效")
    return None
