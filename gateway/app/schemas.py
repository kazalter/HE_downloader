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


class BatchFile(BaseModel):
    url: str
    rel_path: str = Field(..., description="相对 dest_dir 的路径，如 001.jpg 或 disc1/track01.mp3")
    headers: Optional[dict[str, str]] = None
    optional: bool = Field(False, description="可选文件失败不阻塞父任务完成，例如封面侧车")


class BatchCreate(BaseModel):
    """一个分组任务（如一本漫画/一个 ASMR）：父任务一张卡，内含多文件。"""
    name: str = Field(..., description="分组显示名，如作品标题")
    dest_dir: str = Field(..., description="目标目录（绝对路径，或相对 DOWNLOAD_DIR）")
    files: list[BatchFile]
    callback_url: Optional[str] = None


class HePushRequest(BaseModel):
    """从 HE 收藏导入：把选中的某来源收藏推给下载中心（网关转发给 HE 的 push）。"""
    source_type: Literal["asmr", "wnacg"]
    item_ids: list[int]


class HeSyncRequest(BaseModel):
    """重新同步某来源的收藏：网关转发给 HE 的 sync，复用 HE 已存的 cookie/token。
    只处理"已配置好的来源"；首次填 cookie/账号密码仍在 HE Manager 自己的面板。"""
    source_type: Literal["asmr", "wnacg"]


class HeXImportRequest(BaseModel):
    mode: Literal["all", "failed", "skipped"] = "all"


class JobFile(BaseModel):
    """分组任务里的单个子文件（前端可展开逐文件看进度）。"""
    name: str = ""              # 显示名（rel_path 的文件名段）
    rel_path: str = ""
    optional: bool = False
    status: str = "pending"     # 同 job 状态机
    total_bytes: int = 0
    completed_bytes: int = 0
    download_speed: int = 0
    progress: float = 0.0       # 0~100
    error: Optional[str] = None


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
