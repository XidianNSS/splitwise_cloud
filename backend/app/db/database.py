import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.env_loader import PROJECT_ROOT, load_backend_env

load_backend_env()

# 默认拼接绝对路径：<project>/data/cloud_edge.db
base_dir = PROJECT_ROOT

db_path_value = os.getenv("SQLITE_DB_PATH", "data/cloud_edge.db")
DB_PATH = Path(db_path_value)
if not DB_PATH.is_absolute():
    DB_PATH = base_dir / DB_PATH
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    bind=engine,
)
Base = declarative_base()
