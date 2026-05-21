from app.models.models import ScheduleTask


def clamp_progress(value: int) -> int:
    return max(0, min(100, int(value)))


def calc_overall_progress(phase: str, phase_progress: int) -> int:
    phase_progress = clamp_progress(phase_progress)
    if phase == "strategy":
        return phase_progress // 2
    if phase == "loading":
        return 50 + phase_progress // 2
    if phase == "completed":
        return 100
    return phase_progress


def calc_weighted_stage_progress(
    strategy_progress: int,
    integrity_progress: int,
    runtime_load_progress: int,
) -> int:
    return round(
        clamp_progress(strategy_progress) * 0.4
        + clamp_progress(integrity_progress) * 0.3
        + clamp_progress(runtime_load_progress) * 0.3
    )


def serialize_task(task: ScheduleTask) -> dict:
    return {
        "task_id": task.task_id,
        "status": task.status,
        "phase": task.phase,
        "phase_progress": task.phase_progress,
        "overall_progress": task.overall_progress,
        "message": task.message,
        "edge_progress": task.edge_progress,
        "cloud_progress": task.cloud_progress,
        "edge_strategy_progress": task.edge_strategy_progress,
        "edge_integrity_progress": task.edge_integrity_progress,
        "edge_runtime_load_progress": task.edge_runtime_load_progress,
        "cloud_strategy_progress": task.cloud_strategy_progress,
        "cloud_integrity_progress": task.cloud_integrity_progress,
        "cloud_runtime_load_progress": task.cloud_runtime_load_progress,
        "edge_status": task.edge_status,
        "cloud_status": task.cloud_status,
        "edge_message": task.edge_message,
        "cloud_message": task.cloud_message,
        "queue_status": task.queue_status,
        "queue_position": task.queue_position,
        "runtime_binding_id": task.runtime_binding_id,
        "edge_slot_id": task.edge_slot_id,
        "cloud_slot_id": task.cloud_slot_id,
        "allocated_cloud_slot_id": task.allocated_cloud_slot_id,
        "error_detail": task.error_detail,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


def build_strategy_display_layer_partitions(layer_partitions: list[dict]) -> list[dict]:
    display_layers = []
    for layer in layer_partitions:
        head_assignments = list(layer.get("head_assignments", []))
        edge_head_count = sum(1 for assignment in head_assignments if assignment == 0)
        cloud_head_count = sum(1 for assignment in head_assignments if assignment == 1)
        display_layers.append(
            {
                "layer_id": layer.get("layer_id"),
                "head_assignments": head_assignments,
                "ffn_assignment": layer.get("ffn_assignment"),
                "edge_head_count": edge_head_count,
                "cloud_head_count": cloud_head_count,
            }
        )
    return display_layers


def build_strategy_display_summary(display_layers: list[dict]) -> dict:
    return {
        "edge_head_count_total": sum(layer.get("edge_head_count", 0) for layer in display_layers),
        "cloud_head_count_total": sum(layer.get("cloud_head_count", 0) for layer in display_layers),
    }


def _iso(value):
    return value.isoformat() if value else None


def serialize_runtime_slot(slot) -> dict:
    return {
        "slot_id": slot.slot_id,
        "role": slot.role,
        "control_url": slot.control_url,
        "grpc_target": getattr(slot, "grpc_target", None),
        "process_pid": getattr(slot, "process_pid", None),
        "spawned_by_scheduler": bool(getattr(slot, "spawned_by_scheduler", 0)),
        "base_env_name": getattr(slot, "base_env_name", None),
        "slot_index": getattr(slot, "slot_index", 0),
        "process_state": slot.process_state,
        "model_state": slot.model_state,
        "slot_state": slot.slot_state,
        "owner_session_id": slot.owner_session_id,
        "owner_binding_id": slot.owner_binding_id,
        "model_type": slot.model_type,
        "task_id": slot.task_id,
        "active_request_count": slot.active_request_count,
        "integrity_status": slot.integrity_status,
        "confirmation_status": slot.confirmation_status,
        "last_used_at": _iso(slot.last_used_at),
        "idle_deadline": _iso(slot.idle_deadline),
        "process_idle_deadline": _iso(slot.process_idle_deadline),
        "created_at": _iso(slot.created_at),
        "updated_at": _iso(slot.updated_at),
    }


def serialize_runtime_binding(binding) -> dict:
    return {
        "binding_id": binding.binding_id,
        "session_id": binding.session_id,
        "task_id": binding.task_id,
        "edge_slot_id": binding.edge_slot_id,
        "cloud_slot_id": binding.cloud_slot_id,
        "partition_digest": binding.partition_digest,
        "status": binding.status,
        "created_at": _iso(binding.created_at),
        "updated_at": _iso(binding.updated_at),
    }
