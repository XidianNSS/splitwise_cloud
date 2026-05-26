import os
from pathlib import Path

from app.core.env_loader import PROJECT_ROOT, load_backend_env

load_backend_env()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    SECRET_KEY: str = os.getenv("SECRET_KEY", "default-fallback-secret")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_HOURS: int = 24
    OPENWEBUI_JWT_SECRET: str = os.getenv("OPENWEBUI_JWT_SECRET", "")
    OPENWEBUI_JWT_ALGORITHM: str = os.getenv("OPENWEBUI_JWT_ALGORITHM", "HS256")
    OPENWEBUI_SKIP_SIGNATURE_VERIFY: bool = env_bool("OPENWEBUI_SKIP_SIGNATURE_VERIFY", False)
    OPENWEBUI_USER_ID_CLAIM: str = "id"
    OPENWEBUI_USERNAME_CLAIMS: tuple[str, ...] = tuple(
        claim.strip()
        for claim in os.getenv("OPENWEBUI_USERNAME_CLAIMS", "sub,username,email,name,preferred_username").split(",")
        if claim.strip()
    )
    OPENWEBUI_ROLE_CLAIMS: tuple[str, ...] = tuple(
        claim.strip()
        for claim in os.getenv("OPENWEBUI_ROLE_CLAIMS", "role,groups").split(",")
        if claim.strip()
    )
    OPENWEBUI_EXPECTED_ISSUER: str = os.getenv("OPENWEBUI_EXPECTED_ISSUER", "")
    OPENWEBUI_EXPECTED_AUDIENCE: str = os.getenv("OPENWEBUI_EXPECTED_AUDIENCE", "")

    HOST: str = os.getenv("SERVER_HOST", "0.0.0.0")
    PORT: int = int(os.getenv("SERVER_PORT", 8010))
    PUBLIC_BASE_URL: str = os.getenv("SERVER_PUBLIC_BASE_URL", f"http://127.0.0.1:{PORT}")
    BACKEND_BASE_URL: str = os.getenv("BACKEND_BASE_URL", PUBLIC_BASE_URL)
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")
    RUNTIME_CONTROL_PATH: str = os.getenv("RUNTIME_CONTROL_PATH", "/load_strategy")
    EDGE_RUNTIME_USE_MOCK: bool = env_bool("EDGE_RUNTIME_USE_MOCK", False)
    CLOUD_RUNTIME_USE_MOCK: bool = env_bool("CLOUD_RUNTIME_USE_MOCK", False)
    EDGE_RUNTIME_REAL_HOST: str = os.getenv("EDGE_RUNTIME_REAL_HOST", "").strip()
    CLOUD_RUNTIME_REAL_HOST: str = os.getenv("CLOUD_RUNTIME_REAL_HOST", "").strip()
    EDGE_RUNTIME_MOCK_HOST: str = os.getenv("EDGE_RUNTIME_MOCK_HOST", "127.0.0.1").strip()
    CLOUD_RUNTIME_MOCK_HOST: str = os.getenv("CLOUD_RUNTIME_MOCK_HOST", "127.0.0.1").strip()
    EDGE_RUNTIME_REAL_PORT: int = int(os.getenv("EDGE_RUNTIME_REAL_PORT", os.getenv("EDGE_RUNTIME_PORT", "9001")))
    CLOUD_RUNTIME_REAL_PORT: int = int(os.getenv("CLOUD_RUNTIME_REAL_PORT", os.getenv("CLOUD_RUNTIME_PORT", "9002")))
    CLOUD_RUNTIME_REAL_GRPC_TARGET: str = os.getenv("CLOUD_RUNTIME_REAL_GRPC_TARGET", "127.0.0.1:52163").strip()
    EDGE_RUNTIME_MOCK_PORT: int = int(os.getenv("EDGE_RUNTIME_MOCK_PORT", os.getenv("EDGE_RUNTIME_PORT", "18001")))
    CLOUD_RUNTIME_MOCK_PORT: int = int(os.getenv("CLOUD_RUNTIME_MOCK_PORT", os.getenv("CLOUD_RUNTIME_PORT", "18002")))
    MODEL_STARTUP_RESOURCE_CHECK_ENABLED: bool = env_bool("MODEL_STARTUP_RESOURCE_CHECK_ENABLED", True)
    MODEL_STARTUP_MAX_MEMORY_PERCENT: float = float(os.getenv("MODEL_STARTUP_MAX_MEMORY_PERCENT", 95.0))
    MODELSPLIT_DEV_ROOT: str = os.getenv("MODELSPLIT_DEV_ROOT", "/home/nss-d/wyy/ModelSplit_dev")
    CLOUD_SLOT_HTTP_BASE_PORT: int = int(os.getenv("CLOUD_SLOT_HTTP_BASE_PORT", "19113"))
    CLOUD_SLOT_GRPC_BASE_PORT: int = int(os.getenv("CLOUD_SLOT_GRPC_BASE_PORT", "52163"))
    CLOUD_SLOT_MAX_COUNT: int = int(os.getenv("CLOUD_SLOT_MAX_COUNT", "4"))
    CLOUD_SLOT_PROCESS_IDLE_TIMEOUT_SECONDS: int = int(os.getenv("CLOUD_SLOT_PROCESS_IDLE_TIMEOUT_SECONDS", "30"))
    RUNTIME_SLOT_RECONCILE_INTERVAL_SECONDS: float = float(os.getenv("RUNTIME_SLOT_RECONCILE_INTERVAL_SECONDS", "5"))
    RUNTIME_CONFIRMATION_PATH: str = os.getenv("RUNTIME_CONFIRMATION_PATH", "/api/v1/runtime/confirmation/cloud")
    RUNTIME_CONFIRMATION_FORWARD_TIMEOUT_SECONDS: float = float(os.getenv("RUNTIME_CONFIRMATION_FORWARD_TIMEOUT_SECONDS", "30"))

    PROMETHEUS_URL: str = os.getenv("PROMETHEUS_URL", "http://10.144.144.2:9090")
    ALGORITHM_USE_MOCK: bool = env_bool("ALGORITHM_USE_MOCK", False)
    ALGORITHM_REAL_API_URL: str = os.getenv("ALGORITHM_REAL_API_URL", "http://127.0.0.1:8050/infer")
    ALGORITHM_MOCK_API_URL: str = os.getenv("ALGORITHM_MOCK_API_URL", "http://127.0.0.1:5000/infer")
    ALGORITHM_API_URL: str = ALGORITHM_MOCK_API_URL if ALGORITHM_USE_MOCK else ALGORITHM_REAL_API_URL
    ALGORITHM_API_TIMEOUT_SECONDS: float = float(os.getenv("ALGORITHM_API_TIMEOUT_SECONDS", 30.0))
    NETWORK_PING_COUNT: int = int(os.getenv("NETWORK_PING_COUNT", 4))
    NETWORK_PING_TIMEOUT_SECONDS: float = float(os.getenv("NETWORK_PING_TIMEOUT_SECONDS", 1.0))
    NETWORK_ENABLE_IPERF3: bool = env_bool("NETWORK_ENABLE_IPERF3", False)
    NETWORK_IPERF3_PORT: int = int(os.getenv("NETWORK_IPERF3_PORT", 5201))
    NETWORK_IPERF3_DURATION_SECONDS: int = int(os.getenv("NETWORK_IPERF3_DURATION_SECONDS", 1))
    NETWORK_IPERF3_TIMEOUT_SECONDS: float = float(os.getenv("NETWORK_IPERF3_TIMEOUT_SECONDS", 6.0))
    NETWORK_DEFAULT_EDGE_RTT_MS: float = float(os.getenv("NETWORK_DEFAULT_EDGE_RTT_MS", 4.84))
    NETWORK_DEFAULT_CLOUD_RTT_MS: float = float(os.getenv("NETWORK_DEFAULT_CLOUD_RTT_MS", 2.72))
    NETWORK_DEFAULT_BANDWIDTH_MBPS: float = float(os.getenv("NETWORK_DEFAULT_BANDWIDTH_MBPS", 1000.0))
    NETWORK_DEFAULT_PACKET_LOSS: float = float(os.getenv("NETWORK_DEFAULT_PACKET_LOSS", 0.0))
    PROMETHEUS_QUERY_TIMEOUT: float = float(os.getenv("PROMETHEUS_QUERY_TIMEOUT", 3.0))
    PROMETHEUS_CACHE_SECONDS: float = float(os.getenv("PROMETHEUS_CACHE_SECONDS", 15.0))
    ASCEND_IPS: frozenset[str] = frozenset(
        ip.strip() for ip in os.getenv("ASCEND_IPS", "").split(",") if ip.strip()
    )
    ASCEND_NPU_EXPORTER_PORT: int = int(os.getenv("ASCEND_NPU_EXPORTER_PORT", 9500))
    NETWORK_PROBE_CACHE_SECONDS: float = float(os.getenv("NETWORK_PROBE_CACHE_SECONDS", 30.0))
    NETWORK_MAX_CONCURRENT_PROBES: int = int(os.getenv("NETWORK_MAX_CONCURRENT_PROBES", 5))
    FRONTEND_DIR: Path = PROJECT_ROOT / "frontend"


settings = Settings()
