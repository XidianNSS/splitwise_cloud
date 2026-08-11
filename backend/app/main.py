import logging
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.core.config import settings
from app.core.lifespan import lifespan
from app.web import dashboard

from app.api.v1 import admin_runtime, auth, devices, schedule, session, users

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


def create_app() -> FastAPI:
    app = FastAPI(title="云边协同调度枢纽", version="3.0.0", lifespan=lifespan)

    # 挂载跨域中间件
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router, prefix="/api/v1", tags=["认证"])
    app.include_router(session.router, prefix="/api/v1", tags=["普通用户会话"])
    app.include_router(users.router, prefix="/api/v1/users", tags=["账号管理"])
    app.include_router(devices.router, prefix="/api/v1/system/devices", tags=["设备管理"])
    app.include_router(schedule.router, prefix="/api/v1/schedule", tags=["协同调度"])
    app.include_router(admin_runtime.router, prefix="/api/v1/admin/runtime", tags=["运行态总览"])
    app.include_router(dashboard.router)
    app.mount("/static", StaticFiles(directory=settings.FRONTEND_DIR), name="static")

    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=False)
