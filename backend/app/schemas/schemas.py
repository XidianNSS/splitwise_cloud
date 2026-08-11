from typing import List, Optional

from pydantic import BaseModel

class LoginRequest(BaseModel):
    username: str
    password: str


class AuthTokenResponse(BaseModel):
    access_token: str
    token_type: str
    username: str
    role: str


class SessionInitResponse(BaseModel):
    session_id: Optional[str] = None
    openwebui_user_id: str
    openwebui_username: Optional[str] = None
    openwebui_role: Optional[str] = None
    edge_device: Optional[dict] = None
    cloud_device: Optional[dict] = None
    message: str


class SessionInitRequest(BaseModel):
    edge_device_ip: Optional[str] = ""

class UserCreate(BaseModel):
    username: str
    password: str

class DeviceCreate(BaseModel):
    id: str
    name: str
    value: str
    device_type: str


class EdgeTriggerRequest(BaseModel):
    """边缘端发送给云端中枢的触发请求"""
    model_type: str


class LayerPartition(BaseModel):
    layer_id: int
    head_assignments: List[int]  # 0为边端，1为云端
    ffn_assignment: int          # 当前 ModelSplit 只支持 0为边端、1为云端


class StrategyDisplayLayerPartition(BaseModel):
    layer_id: int
    head_assignments: List[int]
    ffn_assignment: int
    edge_head_count: int
    cloud_head_count: int

class StrategyDisplayDecisionPayload(BaseModel):
    layer_partitions: List[StrategyDisplayLayerPartition]
    edge_head_count_total: int
    cloud_head_count_total: int
    strategy_kind: Optional[str] = None
    capability: Optional[str] = None
    deployment_mode: Optional[str] = None


class ModelCatalogEntry(BaseModel):
    model_type: str
    runtime_model_type: str
    architecture: str
    capability: str
    deployment_mode: str
    strategy_kind: str


class RuntimeProgressCallbackRequest(BaseModel):
    task_id: str
    status: str
    progress: int
    message: str
    stage: Optional[str] = None
    node_role: Optional[str] = None


class ScheduleTaskAcceptedResponse(BaseModel):
    status: str
    task_id: str
    phase: str
    phase_progress: int
    overall_progress: int
    message: str


class ScheduleTaskStatusResponse(BaseModel):
    task_id: str
    status: str
    phase: str
    phase_progress: int
    overall_progress: int
    message: str
    edge_progress: int
    cloud_progress: int
    edge_strategy_progress: int
    edge_integrity_progress: int
    edge_runtime_load_progress: int
    cloud_strategy_progress: int
    cloud_integrity_progress: int
    cloud_runtime_load_progress: int
    edge_status: str
    cloud_status: str
    edge_message: str
    cloud_message: str
    queue_status: Optional[str] = None
    queue_position: Optional[int] = None
    runtime_binding_id: Optional[str] = None
    edge_slot_id: Optional[str] = None
    cloud_slot_id: Optional[str] = None
    allocated_cloud_slot_id: Optional[str] = None
    error_detail: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ScheduleTaskStrategyResponse(BaseModel):
    task_id: str
    model_type: str
    decision: StrategyDisplayDecisionPayload


class RuntimeSlotStatusResponse(BaseModel):
    slot_id: str
    role: str
    control_url: Optional[str] = None
    grpc_target: Optional[str] = None
    process_pid: Optional[int] = None
    spawned_by_scheduler: bool = False
    base_env_name: Optional[str] = None
    slot_index: int = 0
    process_state: str
    model_state: str
    slot_state: str
    owner_session_id: Optional[str] = None
    owner_binding_id: Optional[str] = None
    model_type: Optional[str] = None
    task_id: Optional[str] = None
    active_request_count: int
    integrity_status: str
    confirmation_status: str
    last_used_at: Optional[str] = None
    idle_deadline: Optional[str] = None
    process_idle_deadline: Optional[str] = None
    startup_deadline: Optional[str] = None
    startup_failure_count: int = 0
    retry_after: Optional[str] = None
    last_error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class RuntimeBindingStatusResponse(BaseModel):
    binding_id: str
    session_id: str
    task_id: Optional[str] = None
    edge_slot_id: Optional[str] = None
    cloud_slot_id: Optional[str] = None
    partition_digest: Optional[str] = None
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SessionHeartbeatRequest(BaseModel):
    session_id: str


class SessionHeartbeatResponse(BaseModel):
    session_id: str
    status: str
    lease_expires_at: str
    message: str


class SessionCloseRequest(BaseModel):
    session_id: str


class SessionCloseResponse(BaseModel):
    session_id: str
    status: str
    message: str


class CloudRuntimeConfirmationRequest(BaseModel):
    task_id: str
    cloud_slot_id: Optional[str] = None
    model_type: str
    server_param_digest: str
    partition_digest: str
    timestamp: int
    nonce: str


class CloudRuntimeConfirmationResponse(BaseModel):
    matched: bool
    reason: Optional[str] = None
