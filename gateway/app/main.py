from __future__ import annotations

import asyncio
import json
import logging
import os
import posixpath
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import store
from .aria2 import Aria2Client, Aria2Error
from .config import settings
from .schemas import BatchCreate, GlobalStat, HePushRequest, JobCreate, JobView

log = logging.getLogger("gateway")

RECONCILE_INTERVAL = 1.0


# --- 生命周期：aria2 客户端 + DB + 对账协程 --------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    http = httpx.AsyncClient(timeout=30.0)
    app.state.http = http
    app.state.aria2 = Aria2Client(settings.aria2_rpc_url, settings.aria2_rpc_secret, http)
    store.init()
    task = asyncio.create_task(_reconcile_loop(app.state.aria2))
    app.state.reconcile_task = task
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await http.aclose()


app = FastAPI(title="HE_downloader gateway", version="0.3.0", lifespan=lifespan)

# Web 面板（静态文件由 gateway 顺带托管，无需 node 构建 / 额外容器）。
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


def get_aria2() -> Aria2Client:
    return app.state.aria2


# --- 鉴权（token 为空则放行） -----------------------------------------------
# 同时接受 Authorization: Bearer 头 与 ?token= 查询参数 —— 后者给浏览器
# EventSource 用（SSE 客户端无法自定义请求头）。面板页 `/` 与 /static 不鉴权，
# 否则连加载页面输入 token 都做不到。

async def require_auth(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = None,
) -> None:
    secret = settings.gateway_api_token
    if not secret:
        return
    if authorization == f"Bearer {secret}" or token == secret:
        return
    raise HTTPException(status_code=401, detail="缺少或无效的 token")


# --- aria2 选项 / 状态映射 ---------------------------------------------------

def _resolve_dir(dest_dir: Optional[str]) -> str:
    # 下载目录始终是容器内的 Linux 路径，用 posixpath 保证正斜杠（与宿主 OS 无关）。
    base = settings.download_dir
    if not dest_dir:
        return base
    if posixpath.isabs(dest_dir):
        return dest_dir
    return posixpath.join(base, dest_dir)


def _build_options(type: str, resolved_dir: str, filename: Optional[str], headers: Optional[dict]) -> dict:
    options: dict[str, object] = {"dir": resolved_dir}
    if filename:
        options["out"] = filename
    if headers:
        options["header"] = [f"{k}: {v}" for k, v in headers.items()]
    if type == "magnet":
        options["seed-time"] = "0"      # 下完即停、不做种
        options["bt-stop-timeout"] = "120"
    return options


def _build_file_options(dest_dir: str, rel_path: str, headers: Optional[dict]) -> dict:
    """分组任务里单个文件的 aria2 选项：dir = dest_dir + rel 的目录段，out = 文件名。"""
    sub = posixpath.dirname(rel_path or "")
    options: dict[str, object] = {"dir": posixpath.join(dest_dir, sub) if sub else dest_dir}
    out = posixpath.basename(rel_path or "")
    if out:
        options["out"] = out
    if headers:
        options["header"] = [f"{k}: {v}" for k, v in headers.items()]
    return options


_STATUS_MAP = {"complete": "complete", "error": "error", "removed": "canceled"}


def _aggregate_batch(files: list[dict]) -> dict:
    """把子文件状态聚合成父 job 的 status/进度/速度。"""
    total = sum(int(f.get("total_bytes") or 0) for f in files)
    completed = sum(int(f.get("completed_bytes") or 0) for f in files)
    speed = sum(int(f.get("download_speed") or 0) for f in files if f.get("status") == "active")
    sts = [f.get("status") for f in files]
    if files and all(s == "complete" for s in sts):
        status = "complete"
    elif any(s in ("active", "waiting", "pending") for s in sts):
        status = "active"
    elif any(s == "paused" for s in sts):
        status = "paused"
    elif any(s == "error" for s in sts):
        status = "error"
    else:
        status = "canceled"
    n_err = sum(1 for s in sts if s == "error")
    return {
        "status": status,
        "total_bytes": total,
        "completed_bytes": completed,
        "download_speed": speed,
        "error": f"{n_err} 个文件失败" if n_err else None,
    }


def _map_raw(raw: dict) -> dict:
    """aria2 tellStatus 结果 → store.update_progress 的字段。"""
    total = int(raw.get("totalLength", 0) or 0)
    completed = int(raw.get("completedLength", 0) or 0)
    aria_status = raw.get("status", "")
    status = _STATUS_MAP.get(aria_status, aria_status)

    name = ""
    bt = raw.get("bittorrent") or {}
    if isinstance(bt, dict):
        name = (bt.get("info") or {}).get("name", "") or ""
    files_raw = raw.get("files", []) or []
    if not name and files_raw:
        first_path = files_raw[0].get("path", "")
        name = os.path.basename(first_path) if first_path else ""

    return {
        "status": status,
        "name": name,
        "total_bytes": total,
        "completed_bytes": completed,
        "download_speed": int(raw.get("downloadSpeed", 0) or 0),
        "error": raw.get("errorMessage") if status == "error" else None,
    }


def _row_to_view(row: dict) -> JobView:
    total = int(row.get("total_bytes", 0) or 0)
    completed = int(row.get("completed_bytes", 0) or 0)
    return JobView(
        id=row["id"],
        type=row["type"],
        status=row["status"],
        name=row.get("name") or "",
        total_bytes=total,
        completed_bytes=completed,
        download_speed=int(row.get("download_speed", 0) or 0),
        progress=round(completed / total * 100, 1) if total else 0.0,
        dir=row.get("dest_dir") or settings.download_dir,
        error=row.get("error"),
        created_at=float(row.get("created_at", 0) or 0),
        files=[],
    )


# --- 对账协程：DB 为真相源，aria2 重启则据 DB 重新入队 ----------------------

async def _reconcile_once(aria2: Aria2Client) -> None:
    active = await aria2.tell_active()
    waiting = await aria2.tell_waiting()
    stopped = await aria2.tell_stopped()
    by_gid = {r.get("gid"): r for r in [*active, *waiting, *stopped] if r.get("gid")}

    for job in store.non_terminal():
        if job.get("type") == "batch":
            await _reconcile_batch(job, by_gid, aria2)
            continue
        gid = job.get("gid")
        raw = by_gid.get(gid) if gid else None
        if raw is not None:
            store.update_progress(job["id"], **_map_raw(raw))
            continue
        # aria2 不认识这个 gid（刚崩溃重启丢了队列，或 job 还没拿到 gid）→ 据 DB 重新入队。
        # 配合下载目录里的 .aria2 控制文件 + aria2 --continue，已下的部分会续传。
        opts = _build_options(job["type"], job.get("dest_dir") or settings.download_dir,
                              job.get("filename"), store.headers_of(job))
        try:
            new_gid = await aria2.add_uri([job["source"]], opts)
            store.set_gid(job["id"], new_gid)
            store.set_status(job["id"], "active")
            log.info("requeued job %s -> gid %s", job["id"], new_gid)
        except Aria2Error as exc:
            store.set_status(job["id"], "error", str(exc))


async def _reconcile_batch(job: dict, by_gid: dict, aria2: Aria2Client) -> None:
    """分组任务：逐子文件同步/补入队，再把聚合结果写回父 job 行。"""
    dest = job.get("dest_dir") or settings.download_dir
    for f in store.batch_files(job["id"]):
        if f.get("status") in store.TERMINAL:
            continue
        gid = f.get("gid")
        raw = by_gid.get(gid) if gid else None
        if raw is not None:
            m = _map_raw(raw)
            store.file_update(f["id"], status=m["status"], total_bytes=m["total_bytes"],
                              completed_bytes=m["completed_bytes"],
                              download_speed=int(raw.get("downloadSpeed", 0) or 0),
                              error=m["error"])
        elif gid is None:
            opts = _build_file_options(dest, f.get("rel_path") or "", store.headers_of(f))
            try:
                ngid = await aria2.add_uri([f["url"]], opts)
                store.file_set_gid(f["id"], ngid)
                store.file_update(f["id"], status="active", total_bytes=int(f.get("total_bytes") or 0),
                                  completed_bytes=int(f.get("completed_bytes") or 0))
            except Aria2Error as exc:
                store.file_update(f["id"], status="error", total_bytes=0, completed_bytes=0, error=str(exc))
        else:
            # gid 在 aria2 里消失了（重启丢队列）→ 清掉 gid，下一拍重新入队续传。
            store.file_set_gid(f["id"], None)

    files = store.batch_files(job["id"])
    agg = _aggregate_batch(files)
    store.update_progress(job["id"], name=job.get("name") or "", **agg)


async def _reconcile_loop(aria2: Aria2Client) -> None:
    while True:
        try:
            await _reconcile_once(aria2)
        except asyncio.CancelledError:
            raise
        except (Aria2Error, httpx.HTTPError) as exc:
            log.debug("reconcile skipped: %s", exc)  # aria2 还没起来等下一拍
        except Exception:  # noqa: BLE001
            log.exception("reconcile error")
        await asyncio.sleep(RECONCILE_INTERVAL)


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
    resolved = _resolve_dir(payload.dest_dir)
    job_id = store.create(
        type=payload.type, source=payload.source, dest_dir=resolved,
        filename=payload.filename, headers=payload.headers, callback_url=payload.callback_url,
    )
    opts = _build_options(payload.type, resolved, payload.filename, payload.headers)
    try:
        gid = await aria2.add_uri([payload.source], opts)
    except Aria2Error as exc:
        store.set_status(job_id, "error", str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except httpx.HTTPError as exc:
        store.set_status(job_id, "error", f"aria2 不可达: {exc}")
        raise HTTPException(status_code=503, detail=f"aria2 不可达: {exc}")
    store.set_gid(job_id, gid)
    store.set_status(job_id, "active")
    return _row_to_view(store.get(job_id))


@app.post("/jobs/batch", response_model=JobView, dependencies=[Depends(require_auth)])
async def create_batch_job(payload: BatchCreate, aria2: Aria2Client = Depends(get_aria2)):
    """一次提交多文件作为一个分组任务（如一本漫画/一个 ASMR）。HE Manager 走这里推收藏。"""
    if not payload.files:
        raise HTTPException(status_code=400, detail="files 不能为空")
    resolved = _resolve_dir(payload.dest_dir)
    job_id = store.create_batch(
        name=payload.name, dest_dir=resolved, callback_url=payload.callback_url,
        files=[{"url": f.url, "rel_path": f.rel_path, "headers": f.headers} for f in payload.files],
    )
    # 立即入队（不等对账那一拍），失败的文件标 error，由对账兜底/重试。
    for f in store.batch_files(job_id):
        opts = _build_file_options(resolved, f.get("rel_path") or "", store.headers_of(f))
        try:
            gid = await aria2.add_uri([f["url"]], opts)
            store.file_set_gid(f["id"], gid)
            store.file_update(f["id"], status="active", total_bytes=0, completed_bytes=0)
        except (Aria2Error, httpx.HTTPError) as exc:
            store.file_update(f["id"], status="error", total_bytes=0, completed_bytes=0, error=str(exc))
    store.update_progress(job_id, name=payload.name, **_aggregate_batch(store.batch_files(job_id)))
    return _row_to_view(store.get(job_id))


@app.get("/jobs", response_model=list[JobView], dependencies=[Depends(require_auth)])
async def list_jobs():
    return [_row_to_view(r) for r in store.list_all()]


@app.get("/jobs/{job_id}", response_model=JobView, dependencies=[Depends(require_auth)])
async def get_job(job_id: str):
    row = store.get(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _row_to_view(row)


def _require_job(job_id: str) -> dict:
    row = store.get(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    return row


async def _control(job: dict, aria2: Aria2Client, *, method: str, job_status: str, file_status: str) -> None:
    """对单任务的 gid 或分组任务所有子文件 gid 执行 aria2 操作 + 落库状态。"""
    if job.get("type") == "batch":
        for f in store.batch_files(job["id"]):
            if f.get("status") in store.TERMINAL:
                continue
            if f.get("gid"):
                try:
                    await getattr(aria2, method)(f["gid"])
                except Aria2Error:
                    pass
            store.file_set_status(f["id"], file_status)
    elif job.get("gid"):
        try:
            await getattr(aria2, method)(job["gid"])
        except Aria2Error:
            pass
    store.set_status(job["id"], job_status)


@app.post("/jobs/{job_id}/pause", response_model=JobView, dependencies=[Depends(require_auth)])
async def pause_job(job_id: str, aria2: Aria2Client = Depends(get_aria2)):
    await _control(_require_job(job_id), aria2, method="pause", job_status="paused", file_status="paused")
    return _row_to_view(store.get(job_id))


@app.post("/jobs/{job_id}/resume", response_model=JobView, dependencies=[Depends(require_auth)])
async def resume_job(job_id: str, aria2: Aria2Client = Depends(get_aria2)):
    await _control(_require_job(job_id), aria2, method="unpause", job_status="active", file_status="active")
    return _row_to_view(store.get(job_id))


@app.post("/jobs/{job_id}/cancel", response_model=JobView, dependencies=[Depends(require_auth)])
async def cancel_job(job_id: str, aria2: Aria2Client = Depends(get_aria2)):
    await _control(_require_job(job_id), aria2, method="remove", job_status="canceled", file_status="canceled")
    return _row_to_view(store.get(job_id))


@app.post("/jobs/{job_id}/retry", response_model=JobView, dependencies=[Depends(require_auth)])
async def retry_job(job_id: str, aria2: Aria2Client = Depends(get_aria2)):
    row = _require_job(job_id)
    if row["status"] not in store.TERMINAL:
        raise HTTPException(status_code=409, detail="任务未结束，无需重试")
    if row.get("type") == "batch":
        # 只重试失败/取消的子文件：清 gid + 置 pending，对账协程会重新入队。
        for f in store.batch_files(row["id"]):
            if f.get("status") in ("error", "canceled"):
                store.file_set_gid(f["id"], None)
                store.file_set_status(f["id"], "pending")
        store.set_status(job_id, "active")
        return _row_to_view(store.get(job_id))
    opts = _build_options(row["type"], row.get("dest_dir") or settings.download_dir,
                          row.get("filename"), store.headers_of(row))
    try:
        gid = await aria2.add_uri([row["source"]], opts)
    except Aria2Error as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    store.set_gid(job_id, gid)
    store.set_status(job_id, "active")
    return _row_to_view(store.get(job_id))


@app.delete("/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def delete_job(job_id: str, aria2: Aria2Client = Depends(get_aria2)):
    row = _require_job(job_id)
    if row["status"] not in store.TERMINAL:
        await _control(row, aria2, method="remove", job_status="canceled", file_status="canceled")
    store.delete(job_id)
    return {"deleted": job_id}


@app.get("/events", dependencies=[Depends(require_auth)])
async def events():
    """SSE：任务列表有变化时推送一次完整快照（个人规模够用）。"""
    async def gen():
        last = None
        while True:
            payload = json.dumps(
                [_row_to_view(r).model_dump() for r in store.list_all()],
                ensure_ascii=False,
            )
            if payload != last:
                yield f"data: {payload}\n\n"
                last = payload
            await asyncio.sleep(RECONCILE_INTERVAL)

    return StreamingResponse(gen(), media_type="text/event-stream")


# --- 从 HE 收藏导入（网关代理 HE Manager） ----------------------------------
# 一站式：下载中心列出 HE 的 asmr/wnacg 收藏、勾选 → 触发 HE 解析并推回网关。
# HE 的地址 + token 留在网关服务端，前端只跟网关同源打交道（无 CORS、token 不进浏览器）。

def _he_configured() -> bool:
    return bool(settings.he_manager_url)


def _he_url(path: str) -> str:
    return settings.he_manager_url.rstrip("/") + path


def _he_headers() -> dict:
    return {"Authorization": f"Bearer {settings.he_manager_token}"} if settings.he_manager_token else {}


async def _he_get(path: str, params: Optional[dict] = None):
    resp = await app.state.http.get(_he_url(path), params=params, headers=_he_headers(), timeout=30.0)
    resp.raise_for_status()
    return resp.json()


@app.get("/he/enabled")
async def he_enabled():
    return {"enabled": _he_configured()}


@app.get("/he/sources", dependencies=[Depends(require_auth)])
async def he_sources():
    if not _he_configured():
        raise HTTPException(status_code=503, detail="未配置 HE Manager（HE_MANAGER_URL）")
    try:
        return await _he_get("/external/sources")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"HE 不可达: {exc}")


@app.get("/he/favorites", dependencies=[Depends(require_auth)])
async def he_favorites(source_type: Optional[str] = None, search: Optional[str] = None):
    if not _he_configured():
        raise HTTPException(status_code=503, detail="未配置 HE Manager（HE_MANAGER_URL）")
    params = {}
    if source_type:
        params["source_type"] = source_type
    if search:
        params["search"] = search
    try:
        return await _he_get("/external/favorites", params or None)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"HE 不可达: {exc}")


@app.post("/he/push", dependencies=[Depends(require_auth)])
async def he_push(payload: HePushRequest):
    if not _he_configured():
        raise HTTPException(status_code=503, detail="未配置 HE Manager（HE_MANAGER_URL）")
    # 该来源的下载位置（HE 的 push 端点要求传 download_root_path）从 source 取。
    try:
        sources = await _he_get("/external/sources")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"HE 不可达: {exc}")
    root = ""
    for s in sources:
        if (s.get("source_type") or "wnacg") == payload.source_type:
            root = (s.get("download_root_path") or "").strip()
            break
    if not root:
        raise HTTPException(status_code=400, detail=f"HE 里 {payload.source_type} 来源没设下载位置")
    try:
        resp = await app.state.http.post(
            _he_url(f"/external/{payload.source_type}/push"),
            json={"item_ids": payload.item_ids, "download_root_path": root},
            headers=_he_headers(), timeout=120.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"HE 不可达: {exc}")
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(status_code=resp.status_code, detail=str(detail) or "HE 推送失败")
    return resp.json()


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
