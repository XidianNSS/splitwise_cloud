from fastapi import APIRouter, HTTPException, Depends
from app.models.models import User, Device
from app.schemas.schemas import DeviceCreate
from app.api.deps import get_current_admin, get_db
from sqlalchemy.orm import Session

router = APIRouter()


@router.get("", summary="【Admin】获取全量设备列表")
async def list_devices(admin_user: User = Depends(get_current_admin), db=Depends(get_db)):
    devices = db.query(Device).all()
    return [{"id": d.id, "name": d.name, "value": d.value, "type": d.device_type} for d in devices]


@router.post("", summary="【Admin】录入新设备")
async def create_device(dev_in: DeviceCreate, admin_user: User = Depends(get_current_admin), db=Depends(get_db)):
    if db.query(Device).filter(Device.id == dev_in.id).first():
        raise HTTPException(status_code=400, detail="设备编号已存在")

    db.add(Device(id=dev_in.id, name=dev_in.name, value=dev_in.value, device_type=dev_in.device_type))

    db.commit()
    return {"status": "success", "message": "设备录入成功"}


@router.delete("/{device_id}", summary="【Admin】删除设备")
async def delete_device(device_id: str, admin_user: User = Depends(get_current_admin), db=Depends(get_db)):
    if device_id == "cloud":
        raise HTTPException(status_code=400, detail="主节点不可删")
    dev = db.query(Device).filter(Device.id == device_id).first()
    if not dev:
        raise HTTPException(status_code=404, detail="未找到设备")

    db.delete(dev)

    db.commit()
    return {"status": "success"}


@router.get("/prometheus/targets/{job_type}", summary="提供给 Prometheus 的动态服务发现接口")
async def get_prometheus_targets(job_type: str, db: Session = Depends(get_db)):
    """
    Prometheus 会定时拉取这个接口。
    job_type 支持：
    - node: node_exporter，默认端口 9100
    - gpu: dcgm_exporter，默认端口 9400
    - npu: ascend-npu-exporter，默认端口 9500
    """
    target_ports = {
        "node": ":9100",
        "gpu": ":9400",
        "npu": ":9500",
    }
    target_port = target_ports.get(job_type)
    if target_port is None:
        raise HTTPException(status_code=400, detail="job_type 仅支持 node/gpu/npu")

    devices = db.query(Device).all()
    targets_list = []

    for dev in devices:
        endpoints = [endpoint.strip() for endpoint in dev.value.split('|') if endpoint.strip()]
        target_ip_port = next((endpoint for endpoint in endpoints if target_port in endpoint), None)

        if target_ip_port:
            labels = {
                "device_id": dev.id,
                "device_name": dev.name,
                "device_type": dev.device_type,
            }
            if job_type == "node":
                labels["exporter_type"] = "node_exporter"
            elif job_type == "gpu":
                labels["exporter_type"] = "dcgm_exporter"
            elif job_type == "npu":
                labels["exporter_type"] = "ascend_npu_exporter"
                labels["accelerator_type"] = "ascend"

            targets_list.append({
                "targets": [target_ip_port],
                "labels": labels,
            })

    return targets_list
