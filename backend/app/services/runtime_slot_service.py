from datetime import datetime

from sqlalchemy.orm import Session

from app.models.models import RuntimeSlot


def ensure_runtime_slot(
    db: Session,
    *,
    slot_id: str,
    role: str,
    control_url: str | None = None,
    grpc_target: str | None = None,
    process_state: str = "running",
    slot_index: int | None = None,
    spawned_by_scheduler: bool = False,
    base_env_name: str | None = None,
    process_pid: int | None = None,
) -> RuntimeSlot:
    slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == slot_id).first()
    if slot is None:
        slot = RuntimeSlot(
            slot_id=slot_id,
            role=role,
            control_url=control_url,
            grpc_target=grpc_target,
            process_state=process_state,
            model_state="empty",
            slot_state="free",
            slot_index=slot_index or 0,
            spawned_by_scheduler=1 if spawned_by_scheduler else 0,
            base_env_name=base_env_name,
            process_pid=process_pid,
        )
        db.add(slot)
        db.commit()
        db.refresh(slot)
        return slot
    if control_url and slot.control_url != control_url:
        slot.control_url = control_url
    if grpc_target and slot.grpc_target != grpc_target:
        slot.grpc_target = grpc_target
    if slot_index is not None:
        slot.slot_index = slot_index
    if base_env_name is not None:
        slot.base_env_name = base_env_name
    if process_pid is not None:
        slot.process_pid = process_pid
    slot.spawned_by_scheduler = 1 if spawned_by_scheduler else int(getattr(slot, "spawned_by_scheduler", 0) or 0)
    if process_state:
        slot.process_state = process_state
    db.add(slot)
    db.commit()
    db.refresh(slot)
    return slot


def update_runtime_slot_state(db: Session, slot: RuntimeSlot, **fields) -> RuntimeSlot:
    for key, value in fields.items():
        setattr(slot, key, value)
    slot.updated_at = datetime.utcnow()
    db.add(slot)
    db.commit()
    db.refresh(slot)
    return slot


def list_cloud_slots(db: Session) -> list[RuntimeSlot]:
    return (
        db.query(RuntimeSlot)
        .filter(RuntimeSlot.role == "cloud")
        .order_by(RuntimeSlot.slot_index.asc(), RuntimeSlot.slot_id.asc())
        .all()
    )


def get_cloud_slot_by_id(db: Session, slot_id: str) -> RuntimeSlot | None:
    return db.query(RuntimeSlot).filter(RuntimeSlot.role == "cloud", RuntimeSlot.slot_id == slot_id).first()


def get_running_free_cloud_slot(db: Session) -> RuntimeSlot | None:
    return (
        db.query(RuntimeSlot)
        .filter(
            RuntimeSlot.role == "cloud",
            RuntimeSlot.process_state == "running",
            RuntimeSlot.slot_state == "free",
        )
        .order_by(RuntimeSlot.slot_index.asc(), RuntimeSlot.slot_id.asc())
        .first()
    )


def get_stopped_free_cloud_slot(db: Session) -> RuntimeSlot | None:
    return (
        db.query(RuntimeSlot)
        .filter(
            RuntimeSlot.role == "cloud",
            RuntimeSlot.process_state == "stopped",
            RuntimeSlot.slot_state == "free",
            RuntimeSlot.spawned_by_scheduler == 1,
        )
        .order_by(RuntimeSlot.slot_index.asc(), RuntimeSlot.slot_id.asc())
        .first()
    )


def get_cloud_slot_zero(db: Session) -> RuntimeSlot | None:
    return db.query(RuntimeSlot).filter(RuntimeSlot.role == "cloud", RuntimeSlot.slot_id == "cloud-slot-0").first()


def get_active_cloud_slots(db: Session) -> list[RuntimeSlot]:
    return (
        db.query(RuntimeSlot)
        .filter(
            RuntimeSlot.role == "cloud",
            RuntimeSlot.slot_state == "bound",
            RuntimeSlot.owner_binding_id.isnot(None),
        )
        .order_by(RuntimeSlot.slot_index.asc(), RuntimeSlot.slot_id.asc())
        .all()
    )


def get_active_cloud_slot(db: Session) -> RuntimeSlot | None:
    active_slots = get_active_cloud_slots(db)
    return active_slots[0] if active_slots else None


def is_cloud_slot_available(db: Session) -> bool:
    return get_running_free_cloud_slot(db) is not None


def _extract_port_from_control_url(control_url: str | None) -> int | None:
    if not control_url:
        return None
    try:
        host_part = control_url.split('//', 1)[1].split('/', 1)[0]
        return int(host_part.rsplit(':', 1)[1])
    except Exception:
        return None


def _extract_port_from_grpc_target(grpc_target: str | None) -> int | None:
    if not grpc_target:
        return None
    try:
        return int(grpc_target.rsplit(':', 1)[1])
    except Exception:
        return None


def collect_allocated_cloud_ports(db: Session) -> tuple[set[int], set[int]]:
    http_ports: set[int] = set()
    grpc_ports: set[int] = set()
    slots = db.query(RuntimeSlot).filter(RuntimeSlot.role == "cloud").all()
    for slot in slots:
        http_port = _extract_port_from_control_url(slot.control_url)
        grpc_port = _extract_port_from_grpc_target(slot.grpc_target)
        if http_port is not None:
            http_ports.add(http_port)
        if grpc_port is not None:
            grpc_ports.add(grpc_port)
    return http_ports, grpc_ports
