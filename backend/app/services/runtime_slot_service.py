from sqlalchemy.orm import Session

from app.models.models import RuntimeSlot
from app.services.runtime_state_transition_service import transition_runtime_slot


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
    fields = {}
    if control_url and slot.control_url != control_url:
        fields["control_url"] = control_url
    if grpc_target and slot.grpc_target != grpc_target:
        fields["grpc_target"] = grpc_target
    if slot_index is not None:
        fields["slot_index"] = slot_index
    if base_env_name is not None:
        fields["base_env_name"] = base_env_name
    if process_pid is not None:
        fields["process_pid"] = process_pid
    fields["spawned_by_scheduler"] = (
        1
        if spawned_by_scheduler
        else int(getattr(slot, "spawned_by_scheduler", 0) or 0)
    )
    if process_state:
        fields["process_state"] = process_state
    return transition_runtime_slot(db, slot, **fields)


def list_cloud_slots(db: Session) -> list[RuntimeSlot]:
    return (
        db.query(RuntimeSlot)
        .filter(RuntimeSlot.role == "cloud")
        .order_by(RuntimeSlot.slot_index.asc(), RuntimeSlot.slot_id.asc())
        .all()
    )


def get_cloud_slot_by_id(db: Session, slot_id: str) -> RuntimeSlot | None:
    return db.query(RuntimeSlot).filter(RuntimeSlot.role == "cloud", RuntimeSlot.slot_id == slot_id).first()


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


def collect_allocated_cloud_ports(
    db: Session,
    *,
    exclude_slot_id: str | None = None,
) -> tuple[set[int], set[int]]:
    http_ports: set[int] = set()
    grpc_ports: set[int] = set()

    query = db.query(RuntimeSlot).filter(RuntimeSlot.role == "cloud")
    if exclude_slot_id:
        query = query.filter(RuntimeSlot.slot_id != exclude_slot_id)

    slots = query.all()
    for slot in slots:
        http_port = _extract_port_from_control_url(slot.control_url)
        grpc_port = _extract_port_from_grpc_target(slot.grpc_target)
        if http_port is not None:
            http_ports.add(http_port)
        if grpc_port is not None:
            grpc_ports.add(grpc_port)

    return http_ports, grpc_ports
