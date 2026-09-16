"""全局配置：可用环境变量 SCRAPE_MCP_XXX 或项目根目录 .env 覆盖。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_hosts(raw: str) -> set[str]:
    """把逗号分隔的主机名单解析成小写集合，忽略空项与首尾点。"""
    return {h.strip().lower().lstrip(".") for h in raw.split(",") if h.strip()}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCRAPE_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # curl_cffi 的浏览器指纹模板，决定 TLS/JA3 与默认 UA
    impersonate: str = "chrome"
    request_timeout: float = 20.0
    max_response_bytes: int = 5 * 1024 * 1024
    # 单次返回的 token 预算；hard_max_tokens 是 Agent 可申请的上限
    default_max_tokens: int = 4000
    hard_max_tokens: int = 8000
    proxy: str | None = None
    accept_language: str = "zh-CN,zh;q=0.9,en;q=0.8"

    # ---- M4 缓存与批量 ----
    # L1 明文响应磁盘缓存 TTL（秒）；0 表示关闭。只缓存判定的正常 HTML 页
    cache_ttl: int = 300
    # web_batch 单批并发上限
    batch_max_concurrency: int = 4
    batch_max_urls: int = 20

    # ---- 阶段 0 合规（责任边界）----
    # 默认遵循目标站 robots.txt；拉取不到规则时视为允许（不因拿不到而阻断可用性）
    respect_robots: bool = True
    # robots.txt 拉取结果的内存缓存 TTL（秒）
    robots_cache_ttl: float = 86400
    # 主机白名单 / 黑名单（逗号分隔，写域名即可，不含协议与端口）。denied 优先于 allowed：
    #   - allowed_hosts 非空时，仅允许命中名单内的主机（=白名单模式）
    #   - denied_hosts 非空时，命中即拒绝（=黑名单模式），可两者并存
    allowed_hosts: str = ""
    denied_hosts: str = ""
    # 全网全局抓取限流（每秒请求数，web_fetch 与 web_batch 共用）；0 表示不限速
    max_qps: float = 10.0

    @property
    def allowed_host_set(self) -> set[str]:
        return _split_hosts(self.allowed_hosts)

    @property
    def denied_host_set(self) -> set[str]:
        return _split_hosts(self.denied_hosts)

    # ---- L2 浏览器渲染（Playwright）----
    # 只在 L1 被判为拦截时才启用：启动浏览器比 curl_cffi 贵一到两个数量级
    l2_enabled: bool = True
    l2_headless: bool = True
    # 浏览器渠道：留空用 Playwright 自带 Chromium（需先 playwright install chromium），
    # 也可填 chrome / msedge 复用系统已装浏览器
    l2_channel: str | None = None
    l2_navigate_timeout: float = 30.0
    # 挑战页（Cloudflare/验证码）靠 JS 自动放行的观察窗口：期间反复取内容，
    # 一旦不再命中挑战特征就提前返回，不必等满
    l2_settle_timeout: float = 8.0
    l2_locale: str = "zh-CN"
    # 隐身加固：context 统一时区，避免 headless 默认 UTC 与 UA/语言穿帮
    l2_timezone: str = "Asia/Shanghai"

    # ---- M3 登录态持久化 ----
    # 登录态文件（Playwright storage_state）落盘目录，按站点域名各存一份
    data_dir: str = "~/.scrape_mcp"
    # login 工具默认打开带界面的窗口供手动登录；确认后可按需切回无头
    login_timeout: float = 180.0

    # ---- 传输模式 ----
    # stdio（默认，MCP 进程管道）/ streamable-http（HTTP，Postman/curl 直连）
    transport: str = "stdio"
    # streamable-http 时监听 host/port
    http_host: str = "127.0.0.1"
    http_port: int = 8000
    # streamable-http 纯 JSON 响应（True）vs 事件流（False）
    http_json: bool = True
    # streamable-http 免握手/免会话（True）；有状态需先 initialize
    http_stateless: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
