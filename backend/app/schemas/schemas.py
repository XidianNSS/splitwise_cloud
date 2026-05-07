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
    ffn_assignment: int          # 0为边端，1为云端，2为拆分


class StrategyDisplayLayerPartition(BaseModel):
    layer_id: int
    head_assignments: List[int]
    ffn_assignment: int
    edge_head_count: int
    cloud_head_count: int

class RuntimeDecisionPayload(BaseModel):
    layer_partitions: List[LayerPartition]


class StrategyDisplayDecisionPayload(BaseModel):
    layer_partitions: List[StrategyDisplayLayerPartition]
    edge_head_count_total: int
    cloud_head_count_total: int


class RuntimeProgressCallbackRequest(BaseModel):
    task_id: str
    status: str
    progress: int
    message: str
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
    edge_status: str
    cloud_status: str
    edge_message: str
    cloud_message: str
    error_detail: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ScheduleTaskStrategyResponse(BaseModel):
    task_id: str
    model_type: str
    decision: StrategyDisplayDecisionPayload
