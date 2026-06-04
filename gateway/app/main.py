from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException

from .aria2 import Aria2Client, Aria2Error
from .config import settings
from .schemas import GlobalStat, JobCreate, JobFile, JobView


# --- aria2 客户端生命周期 ---------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    http = httpx.AsyncClient(timeout=30.0)
    app.state.http = http
    app.state.aria2 = Aria2Client(settings.aria2_rpc_url, settings.aria2_rpc_secret, http)
    try:
        yield
    finally:
        await http.aclose()


app = FastAPI(title="HE_downloader gateway", version="0.1.0", lifespan=lifespan)


def get_aria2() -> Aria2Client:
    return app.state.aria2


# --- 鉴权（token 为空则放行） -----------------------------------------------

async def require_auth(authorization: Optional[str] = Header(None)) -> None:
    token = settings.gateway_api_token
    if not token:
        return
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="缺少或无效的 Bearer token")


# --- 辅助 -------------------------------------------------------------------

def _resolve_dir(dest_dir: Optional[str]) -> str:
    base = settings.download_dir
    if not dest_dir:
        return base
    if os.path.isabs(dest_dir):
        return dest_dir
    return os.path.join(base, dest_dir)


def _normalize(raw: dict) -> JobView:
    total = int(raw.get("totalLength", 0) or 0)
    completed = int(raw.get("completedLength", 0) or 0)
    status = raw.get("status", "")
    is_bt = "bittorrent" in raw

    name = ""
    bt = raw.get("bittorrent") or {}
    if isinstance(bt, dict):
        name = (bt.get("info") or {}).get("name", "") or ""
    files_raw = raw.get("files", []) or []
    if not name and files_raw:
        first_path = files_raw[0].get("path", "")
        name = os.path.basename(first_path) if first_path else ""

    files = [
        JobFile(
            path=f.get("path", ""),
            length=int(f.get("length", 0) or 0),
            completed=int(f.get("completedLength", 0) or 0),
        )
        for f in files_raw
    ]

    return JobView(
        id=raw.get("gid", ""),
        type="magnet" if is_bt else "url",
        status=status,
        name=name,
        total_bytes=total,
        completed_bytes=completed,
        download_speed=int(raw.get("downloadSpeed", 0) or 0),
        progress=round(completed / total * 100, 1) if total else 0.0,
        dir=raw.get("dir", ""),
        error=raw.get("errorMessage") if status == "error" else None,
        files=files,
    )


# --- 路由 -------------------------------------------------------------------

@app.get("/health")
async def health(aria2: Aria2Client = Depends(get_aria2)):
    try:
        version = await aria2.get_version()
        return {"status": "ok", "aria2": version.get("version")}
    except (Aria2Error, httpx.HTTPError) as exc:
        raise HTTPException(status_code=503, detail=f"aria2 不可达: {exc}")


@app.post("/jobs", response_model=JobView, dependencies=[Depends(require_auth)])
async def create_job(payload: JobCreate, aria2: Aria2Client = Depends(get_aria2)):
    options: dict[str, object] = {"dir": _resolve_dir(payload.dest_dir)}
    if payload.filename:
        options["out"] = payload.filename
    if payload.headers:
        options["header"] = [f"{k}: {v}" for k, v in payload.headers.items()]
    if payload.type == "magnet":
        # 下完即停、不做种 —— 不依赖镜像 conf，逐任务下发更稳。
        options["seed-time"] = "0"
        options["bt-stop-timeout"] = "120"

    try:
        gid = await aria2.add_uri([payload.source], options)
        raw = await aria2.tell_status(gid)
    except Aria2Error as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"aria2 不可达: {exc}")
    return _normalize(raw)


@app.get("/jobs", response_model=list[JobView], dependencies=[Depends(require_auth)])
async def list_jobs(aria2: Aria2Client = Depends(get_aria2)):
    try:
        active = await aria2.tell_active()
        waiting = await aria2.tell_waiting()
        stopped = await aria2.tell_stopped()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"aria2 不可达: {exc}")
    return [_normalize(r) for r in [*active, *waiting, *stopped]]


@app.get("/jobs/{gid}", response_model=JobView, dependencies=[Depends(require_auth)])
async def get_job(gid: str, aria2: Aria2Client = Depends(get_aria2)):
    try:
        raw = await aria2.tell_status(gid)
    except Aria2Error:
        raise HTTPException(status_code=404, detail="任务不存在")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"aria2 不可达: {exc}")
    return _normalize(raw)


@app.post("/jobs/{gid}/pause", response_model=JobView, dependencies=[Depends(require_auth)])
async def pause_job(gid: str, aria2: Aria2Client = Depends(get_aria2)):
    return await _control(aria2, gid, "pause")


@app.post("/jobs/{gid}/resume", response_model=JobView, dependencies=[Depends(require_auth)])
async def resume_job(gid: str, aria2: Aria2Client = Depends(get_aria2)):
    return await _control(aria2, gid, "unpause")


@app.post("/jobs/{gid}/cancel", response_model=JobView, dependencies=[Depends(require_auth)])
async def cancel_job(gid: str, aria2: Aria2Client = Depends(get_aria2)):
    return await _control(aria2, gid, "remove")


async def _control(aria2: Aria2Client, gid: str, action: str) -> JobView:
    try:
        await getattr(aria2, action)(gid)
        raw = await aria2.tell_status(gid)
    except Aria2Error as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"aria2 不可达: {exc}")
    return _normalize(raw)


@app.get("/stat", response_model=GlobalStat, dependencies=[Depends(require_auth)])
async def global_stat(aria2: Aria2Client = Depends(get_aria2)):
    try:
        s = await aria2.get_global_stat()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"aria2 不可达: {exc}")
    return GlobalStat(
        download_speed=int(s.get("downloadSpeed", 0) or 0),
        upload_speed=int(s.get("uploadSpeed", 0) or 0),
        num_active=int(s.get("numActive", 0) or 0),
        num_waiting=int(s.get("numWaiting", 0) or 0),
        num_stopped=int(s.get("numStopped", 0) or 0),
    )
