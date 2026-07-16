import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.models import RuntimeBinding


def create_runtime_binding(
    db: Session,
    *,
    session_id: str,
    task_id: str,
    edge_slot_id: str | None,
    cloud_slot_id: str | None,
    partition_digest: str | None = None,
    status: str = "binding",
) -> RuntimeBinding:
    binding = RuntimeBinding(
        binding_id=str(uuid.uuid4()),
        session_id=session_id,
        task_id=task_id,
        edge_slot_id=edge_slot_id,
        cloud_slot_id=cloud_slot_id,
        partition_digest=partition_digest,
        status=status,
    )
    db.add(binding)
    db.commit()
    db.refresh(binding)
    return binding


def update_runtime_binding(db: Session, binding: RuntimeBinding, **fields) -> RuntimeBinding:
    for key, value in fields.items():
        setattr(binding, key, value)
    binding.updated_at = datetime.utcnow()
    db.add(binding)
    db.commit()
    db.refresh(binding)
    return binding
