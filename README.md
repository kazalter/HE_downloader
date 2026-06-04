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
- [ ] **M2** SQLite 任务持久化 + 生命周期(pause/resume/cancel/retry) + SSE 进度 + 重启不丢
- [ ] **M3** 种子文件上传 + yt-dlp 视频站解析
- [ ] **M4** Vue 面板
- [ ] **M5** Bearer 鉴权强化 + 完成 webhook 回调
- [ ] **M6**（HE 仓内）HE 推任务客户端 + 下完触发 scanner 入库

## 部署（Dockge）

1. 把本仓克隆/放到 Dockge 的 stacks 目录，例如 `/opt/stacks/he_downloader/`。
2. 复制 `.env.example` 为 `.env`，至少改 `ARIA2_RPC_SECRET` 和 `DOWNLOAD_DIR`。
3. 在 Dockge 里 import 这个 stack 并 Start（或 `docker compose up -d --build`）。

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
