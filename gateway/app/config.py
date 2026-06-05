from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Gateway 配置，全部来自环境变量（compose 注入）。"""

    aria2_rpc_url: str = "http://aria2:6800/jsonrpc"
    aria2_rpc_secret: str = ""

    # 留空表示不鉴权（仅限可信局域网）。非空则要求 Authorization: Bearer <token>。
    gateway_api_token: str = ""

    # 容器内的下载根目录，与 aria2 容器共享同一挂载点。
    download_dir: str = "/downloads"

    # 可选默认代理；运行时以 SQLite 里的设置为准。空 = 不用代理。
    aria2_all_proxy: str = ""

    # 裸 magnet 链（只有 btih，没有 tr=）很容易卡在 0B/0B 元数据阶段。
    # 给 aria2 补一组公共 tracker；需要禁用时可在环境里设 BT_TRACKERS 为空。
    bt_trackers: str = (
        "udp://zer0day.ch:1337/announce,"
        "udp://tracker.publictracker.xyz:6969/announce,"
        "udp://tracker.opentrackr.org:1337/announce,"
        "udp://open.demonii.com:1337/announce,"
        "udp://open.stealth.si:80/announce,"
        "udp://vito-tracker.space:6969/announce,"
        "udp://tracker.torrent.eu.org:451/announce,"
        "udp://tracker.theoks.net:6969/announce,"
        "udp://tracker.qu.ax:6969/announce,"
        "udp://tracker.iperson.xyz:6969/announce"
    )

    # HE Manager 后端地址 + token（用于"从 HE 收藏导入"：网关代理 HE 列收藏/触发推送）。
    # 留空则下载中心不显示 HE 收藏面板。
    he_manager_url: str = ""
    he_manager_token: str = ""

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


settings = Settings()
