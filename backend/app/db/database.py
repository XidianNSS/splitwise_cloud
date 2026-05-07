import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# __file__ 当前在 backend/app/db/database.py
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
BACKEND_ENV_FILE = BASE_DIR / "backend" / ".env"

load_dotenv(BACKEND_ENV_FILE)

# 默认拼接绝对路径：.../splitwise_cloud/data/cloud_edge.db
db_path_value = os.getenv("SQLITE_DB_PATH", "data/cloud_edge.db")
DB_PATH = Path(db_path_value)
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH
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
