# HE_downloader

通用下载中心 —— 独立于 HE Manager 的自托管下载服务。一个统一的 API + 面板，
后面接 [aria2](https://aria2.github.io/)（HTTP / 磁力 / 种子）和 yt-dlp（视频站）等引擎。
HE Manager、其它 app、以及你手动丢的磁力/链接都往这里推任务。

跑在 Linux，用 [Dockge](https://github.com/louislam/dockge) 当 docker-compose stack 管理。

## 架构

```
HE Manager / 其它 app / 你 ──推任务──▶ gateway (FastAPI 薄网关 + 面板)
                                          │  统一 API · 任务持久化 · SSE · 完成回调
                                          ├──▶ aria2   (HTTP / 磁力 / 种子, 下完即停不做种)
                                          └──▶ yt-dlp  (视频站, 内嵌 gateway, 后续里程碑)
```

- **gateway** 是唯一对外入口，引擎（aria2 / yt-dlp）不重复造轮子。
- 磁力/种子 **下完即停、不做种**：gateway 给每个 torrent 任务下发 `seed-time=0`。

## 里程碑

- [x] **M1** 骨架 + compose：gateway `POST /jobs` 让 aria2 下一个测试直链 / 磁力
- [x] **M2** SQLite 任务持久化 + 生命周期(pause/resume/cancel/retry) + SSE 进度 + 重启不丢
- [ ] **M3** 种子文件上传 + yt-dlp 视频站解析
- [x] **M4** Vue 面板（buildless，gateway 顺带托管）+ 分组任务可展开看逐文件进度
- [x] **M5** Bearer 鉴权 + 完成 webhook 回调（见下）
- [x] **M6** 从 HE 收藏一键导入（网关代理 HE，token 不进浏览器）+ 下完回调触发 scanner 入库
  - 模态里「同步」按钮：`POST /he/sync` 让 HE 据已存 cookie/token 去源站重拉收藏（凭据不进浏览器）。
    **首次配置**（填 cookie / asmr 账号密码 / 过滤器）仍在 HE Manager 自己的面板，下载中心只做"重同已配置源"。

## 完成回调（webhook）

给任务带上 `callback_url`，任务跑到终态（`complete` / `error`，**用户主动 cancel/delete 不回调**）时
网关会 `POST` 一次到该地址。HE 据此触发 scanner 入库。同一任务只回调一次（DB 里 `callback_fired`
原子认领去重，重启也不会重复发），网络错误 / 5xx 会退避重试几次，4xx 视为已送达不再重试。

载荷：

```jsonc
{
  "event": "complete",          // 即 job.status：complete | error
  "job": { /* 同 GET /jobs/<id> 的 JobView */
    "id": "…", "type": "batch", "status": "complete", "name": "…",
    "dir": "/mnt/hdd/manga/book1", "total_bytes": 0, "completed_bytes": 0,
    "files": [ { "name": "001.jpg", "rel_path": "001.jpg", "status": "complete",
                 "total_bytes": 0, "completed_bytes": 0, "progress": 100.0 } ]
  }
}
```

单文件任务 `files` 为空数组；分组任务（batch）带逐文件明细。

## 部署（Dockge）

1. 把本仓克隆/放到 Dockge 的 stacks 目录，例如 `/opt/stacks/he_downloader/`。
2. 复制 `.env.example` 为 `.env`，至少改 `ARIA2_RPC_SECRET` 和 `DOWNLOAD_DIR`。
3. 确保 `DOWNLOAD_DIR` 归 `PUID:PGID`（默认 1000）所属：`sudo chown -R 1000:1000 <下载目录>`。
   aria2 以该用户身份写文件，目录属 root 会 `Permission denied`。
4. 在 Dockge 里 import 这个 stack 并 Start（或 `docker compose up -d --build`）。

## 冒烟测试（M1）

下一个测试直链：

```bash
curl -s -X POST http://<host>:8011/jobs \
  -H 'Content-Type: application/json' \
  -d '{"type":"url","source":"https://speed.hetzner.de/100MB.bin"}'
# -> {"id":"<gid>", ...}

curl -s http://<host>:8011/jobs            # 看所有任务
curl -s http://<host>:8011/jobs/<gid>      # 看单个任务进度
```

下完的文件落在 `DOWNLOAD_DIR`（容器内 `/downloads`）。

## 端口

| 服务    | 容器端口 | 默认宿主端口 |
|---------|---------|------------|
| gateway | 8000    | 8011       |
| aria2   | 6800    | 6800 (RPC) |
