from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# M1 支持 url / magnet（都是 aria2.addUri）。torrent 文件上传、video(yt-dlp) 留到后续里程碑。
JobType = Literal["url", "magnet"]


class JobCreate(BaseModel):
    type: JobType = "url"
    source: str = Field(..., description="直链 URL 或 magnet: 链接")
    # 相对 DOWNLOAD_DIR 的子目录；绝对路径也接受。None = 直接落在下载根目录。
    dest_dir: Optional[str] = None
    filename: Optional[str] = Field(None, description="覆盖输出文件名（仅单文件直链有意义）")
    headers: Optional[dict[str, str]] = Field(None, description="透传给 aria2 的请求头，如 Cookie/Referer")
    # 下完后回调的 URL（M5 才真正触发；M2 仅存档）。
    callback_url: Optional[str] = None


class JobFile(BaseModel):
    path: str
    length: int = 0
    completed: int = 0


class JobView(BaseModel):
    id: str  # 稳定的 gateway job id（uuid），不随 aria2 重启/重试改变
    type: str
    status: str  # pending|active|waiting|paused|complete|error|canceled
    name: str = ""
    total_bytes: int = 0
    completed_bytes: int = 0
    download_speed: int = 0
    progress: float = 0.0  # 0~100
    dir: str = ""
    error: Optional[str] = None
    created_at: float = 0.0
    files: list[JobFile] = []


class GlobalStat(BaseModel):
    download_speed: int = 0
    upload_speed: int = 0
    num_active: int = 0
    num_waiting: int = 0
    num_stopped: int = 0
