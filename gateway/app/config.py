from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Gateway 配置，全部来自环境变量（compose 注入）。"""

    aria2_rpc_url: str = "http://aria2:6800/jsonrpc"
    aria2_rpc_secret: str = ""

    # 留空表示不鉴权（仅限可信局域网）。非空则要求 Authorization: Bearer <token>。
    gateway_api_token: str = ""

    # 容器内的下载根目录，与 aria2 容器共享同一挂载点。
    download_dir: str = "/downloads"

    # HE Manager 后端地址 + token（用于"从 HE 收藏导入"：网关代理 HE 列收藏/触发推送）。
    # 留空则下载中心不显示 HE 收藏面板。
    he_manager_url: str = ""
    he_manager_token: str = ""

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


settings = Settings()
