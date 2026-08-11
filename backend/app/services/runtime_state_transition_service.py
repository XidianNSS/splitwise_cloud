"""Guarded, atomic state transitions for scheduler-managed runtimes.

The tuple ``(owner_session_id, owner_binding_id, task_id)`` is the allocation
identity.  Every update also compares the slot/binding snapshot read by the
caller, so a delayed callback or a coroutine resumed after a network wait
cannot overwrite a newer allocation.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.models import RuntimeBinding, RuntimeSlot


class RuntimeTransitionConflict(RuntimeError):
    """The runtime was changed or reallocated after the caller read it."""


@dataclass(frozen=True)
class RuntimeAllocationIdentity:
    session_id: str | None
    binding_id: str | None
    task_id: str | None

    @classmethod
    def from_slot(cls, slot: RuntimeSlot) -> "RuntimeAllocationIdentity":
        return cls(
            session_id=slot.owner_session_id,
            binding_id=slot.owner_binding_id,
            task_id=slot.task_id,
        )


@dataclass(frozen=True)
class RuntimeSlotSnapshot:
    process_state: str
    model_state: str
    slot_state: str
    allocation: RuntimeAllocationIdentity
    model_type: str | None
    process_pid: int | None

    @classmethod
    def from_slot(cls, slot: RuntimeSlot) -> "RuntimeSlotSnapshot":
        return cls(
            process_state=slot.process_state,
            model_state=slot.model_state,
            slot_state=slot.slot_state,
            allocation=RuntimeAllocationIdentity.from_slot(slot),
            model_type=slot.model_type,
            process_pid=slot.process_pid,
        )


@dataclass(frozen=True)
class RuntimeBindingSnapshot:
    status: str
    task_id: str | None
    edge_slot_id: str | None
    cloud_slot_id: str | None

    @classmethod
    def from_binding(cls, binding: RuntimeBinding) -> "RuntimeBindingSnapshot":
        return cls(
            status=binding.status,
            task_id=binding.task_id,
            edge_slot_id=binding.edge_slot_id,
            cloud_slot_id=binding.cloud_slot_id,
        )


_SLOT_MUTABLE_FIELDS = {
    "control_url",
    "grpc_target",
    "process_pid",
    "spawned_by_scheduler",
    "base_env_name",
    "slot_index",
    "process_state",
    "model_state",
    "slot_state",
    "owner_session_id",
    "owner_binding_id",
    "model_type",
    "task_id",
    "active_request_count",
    "integrity_status",
    "confirmation_status",
    "last_used_at",
    "idle_deadline",
    "process_idle_deadline",
    "startup_deadline",
    "startup_failure_count",
    "retry_after",
    "last_error",
}

_BINDING_MUTABLE_FIELDS = {
    "task_id",
    "edge_slot_id",
    "cloud_slot_id",
    "partition_digest",
    "status",
}


def _validate_fields(fields: dict[str, Any], allowed: set[str], entity: str) -> None:
    unsupported = sorted(set(fields) - allowed)
    if unsupported:
        raise ValueError(f"unsupported {entity} transition fields: {unsupported}")


def transition_runtime_slot(
    db: Session,
    slot: RuntimeSlot,
    *,
    expected: RuntimeSlotSnapshot | None = None,
    expected_allocation: RuntimeAllocationIdentity | None = None,
    commit: bool = True,
    **fields: Any,
) -> RuntimeSlot:
    """Apply a compare-and-set transition to one runtime slot.

    ``expected`` defaults to the complete state/owner snapshot carried by the
    supplied ORM object.  Supplying ``expected_allocation`` adds an explicit
    allocation-generation precondition for callbacks and compound workflows.
    """
    _validate_fields(fields, _SLOT_MUTABLE_FIELDS, "runtime slot")
    if not fields:
        return slot

    snapshot = expected or RuntimeSlotSnapshot.from_slot(slot)
    allocation = expected_allocation or snapshot.allocation
    query = db.query(RuntimeSlot).filter(
        RuntimeSlot.slot_id == slot.slot_id,
        RuntimeSlot.process_state == snapshot.process_state,
        RuntimeSlot.model_state == snapshot.model_state,
        RuntimeSlot.slot_state == snapshot.slot_state,
        RuntimeSlot.owner_session_id == allocation.session_id,
        RuntimeSlot.owner_binding_id == allocation.binding_id,
        RuntimeSlot.task_id == allocation.task_id,
        RuntimeSlot.model_type == snapshot.model_type,
        RuntimeSlot.process_pid == snapshot.process_pid,
    )
    values = dict(fields)
    values["updated_at"] = datetime.utcnow()
    changed = query.update(values, synchronize_session=False)
    if changed != 1:
        db.rollback()
        raise RuntimeTransitionConflict(
            f"runtime slot {slot.slot_id} changed before transition"
        )

    if commit:
        db.commit()
        db.refresh(slot)
    else:
        db.expire(slot)
        db.refresh(slot)
    return slot


def transition_runtime_binding(
    db: Session,
    binding: RuntimeBinding,
    *,
    expected: RuntimeBindingSnapshot | None = None,
    commit: bool = True,
    **fields: Any,
) -> RuntimeBinding:
    """Apply a compare-and-set transition to one runtime binding."""
    _validate_fields(fields, _BINDING_MUTABLE_FIELDS, "runtime binding")
    if not fields:
        return binding

    snapshot = expected or RuntimeBindingSnapshot.from_binding(binding)
    query = db.query(RuntimeBinding).filter(
        RuntimeBinding.binding_id == binding.binding_id,
        RuntimeBinding.session_id == binding.session_id,
        RuntimeBinding.status == snapshot.status,
        RuntimeBinding.task_id == snapshot.task_id,
        RuntimeBinding.edge_slot_id == snapshot.edge_slot_id,
        RuntimeBinding.cloud_slot_id == snapshot.cloud_slot_id,
    )
    values = dict(fields)
    values["updated_at"] = datetime.utcnow()
    changed = query.update(values, synchronize_session=False)
    if changed != 1:
        db.rollback()
        raise RuntimeTransitionConflict(
            f"runtime binding {binding.binding_id} changed before transition"
        )

    if commit:
        db.commit()
        db.refresh(binding)
    else:
        db.expire(binding)
        db.refresh(binding)
    return binding
