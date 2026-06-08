import logging
from datetime import datetime
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base, SessionLocal
from app.core.config import settings
from app.core.security import get_password_hash

logger = logging.getLogger("InitDB")


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(String)
    device_type: Mapped[str] = mapped_column(String, default="edge")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String)


class ScheduleTask(Base):
    __tablename__ = "schedule_tasks"

    task_id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    openwebui_user_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    edge_session_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    runtime_binding_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    model_type: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="accepted")
    phase: Mapped[str] = mapped_column(String, default="strategy")
    phase_progress: Mapped[int] = mapped_column(Integer, default=0)
    overall_progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(String, default="任务已受理")
    edge_device_id: Mapped[str | None] = mapped_column(String, nullable=True)
    cloud_device_id: Mapped[str | None] = mapped_column(String, nullable=True)
    edge_progress: Mapped[int] = mapped_column(Integer, default=0)
    cloud_progress: Mapped[int] = mapped_column(Integer, default=0)
    edge_strategy_progress: Mapped[int] = mapped_column(Integer, default=0)
    edge_integrity_progress: Mapped[int] = mapped_column(Integer, default=0)
    edge_runtime_load_progress: Mapped[int] = mapped_column(Integer, default=0)
    cloud_strategy_progress: Mapped[int] = mapped_column(Integer, default=0)
    cloud_integrity_progress: Mapped[int] = mapped_column(Integer, default=0)
    cloud_runtime_load_progress: Mapped[int] = mapped_column(Integer, default=0)
    edge_status: Mapped[str] = mapped_column(String, default="pending")
    cloud_status: Mapped[str] = mapped_column(String, default="pending")
    queue_status: Mapped[str] = mapped_column(String, default="pending")
    queue_position: Mapped[int] = mapped_column(Integer, default=0)
    edge_slot_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    cloud_slot_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    allocated_cloud_slot_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    spawned_cloud_slot: Mapped[str | None] = mapped_column(String, nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    edge_message: Mapped[str] = mapped_column(String, default="等待边端模型加载")
    cloud_message: Mapped[str] = mapped_column(String, default="等待云端模型加载")
    strategy_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class EdgeSession(Base):
    __tablename__ = "edge_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    session_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    openwebui_user_id: Mapped[str] = mapped_column(String, index=True)
    edge_device_id: Mapped[str] = mapped_column(String, index=True)
    edge_ip: Mapped[str] = mapped_column(String, index=True)
    cloud_device_id: Mapped[str] = mapped_column(String, index=True)
    model_type: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


def init_db_data():
    """
    系统首次启动时，检查并注入默认的物理设备和管理员账号
    """
    db = SessionLocal()
    admin_username = (settings.ADMIN_USERNAME or "").strip() or "admin"
    admin_password = settings.ADMIN_PASSWORD or "admin123"

    if not db.query(Device).first():
        logger.info("🛠️ 首次启动：正在向数据库注入默认物理设备资产...")
        db.add_all([
            Device(
            id="cloud",
            name="☁️ 云端总枢纽 (Ascend NPU)",
            value="10.144.144.4:9100|10.144.144.4:9500",
            device_type="cloud",
        ),
        Device(
            id="edge_A",
            name="📱 边缘节点 A",
            value="10.144.144.5:9100|10.144.144.5:9400",
            device_type="edge",
        ),
            # Device(id="cloud", name="☁️ 云端总枢纽 (RTX 5090)", value="10.144.144.2:9400|10.144.144.2:9100",
            #        device_type="cloud"),
            # Device(id="edge_A", name="📱 边缘节点 A (RTX 4090)", value="10.144.144.3:9100|10.144.144.3:9400",
            #        device_type="edge"),
            # Device(id="edge_B", name="📱 边缘节点 B (RTX 3080)", value="10.144.144.4:9100|10.144.144.4:9400",
            #        device_type="edge")
        ])
        db.commit()

    existing_admin = db.query(User).filter(User.role == "admin").first()
    if not existing_admin:
        logger.info("🛠️ 首次启动：正在向数据库注入初始管理员账号...")
        admin = User(
            username=admin_username,
            hashed_password=get_password_hash(admin_password),
            role="admin",
        )
        db.add(admin)
        db.commit()

    db.close()



class RuntimeSlot(Base):
    __tablename__ = "runtime_slots"

    slot_id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    role: Mapped[str] = mapped_column(String, index=True)
    control_url: Mapped[str | None] = mapped_column(String, nullable=True)
    grpc_target: Mapped[str | None] = mapped_column(String, nullable=True)
    process_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spawned_by_scheduler: Mapped[int] = mapped_column(Integer, default=0)
    base_env_name: Mapped[str | None] = mapped_column(String, nullable=True)
    slot_index: Mapped[int] = mapped_column(Integer, default=0)
    process_state: Mapped[str] = mapped_column(String, default="running")
    model_state: Mapped[str] = mapped_column(String, default="empty")
    slot_state: Mapped[str] = mapped_column(String, default="free")
    owner_session_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    owner_binding_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    model_type: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    active_request_count: Mapped[int] = mapped_column(Integer, default=0)
    integrity_status: Mapped[str] = mapped_column(String, default="unknown")
    confirmation_status: Mapped[str] = mapped_column(String, default="none")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    idle_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    process_idle_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RuntimeBinding(Base):
    __tablename__ = "runtime_bindings"

    binding_id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    session_id: Mapped[str] = mapped_column(String, index=True)
    task_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    edge_slot_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    cloud_slot_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    partition_digest: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="binding")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
