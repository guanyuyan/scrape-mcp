# syntax=docker/dockerfile:1

# scrape-mcp 容器镜像（多阶段构建、非 root 运行）
#
# 默认只装 L1（curl_cffi）依赖：镜像小、启动快，覆盖大多数静态/普通动态页。
# 需要 L2 真浏览器（Cloudflare/验证码/SPA）时：
#   docker build --build-arg INSTALL_BROWSER=1 -t scrape-mcp:l2 .
#
# 运行（HTTP 传输，供远端 MCP 客户端接入）：
#   docker run -p 8002:8002 -e SCRAPE_MCP_HTTP_TOKEN=<令牌> scrape-mcp
#   curl http://127.0.0.1:8002/healthz
# 容器绑定 0.0.0.0，必须通过环境变量提供访问令牌，否则服务拒绝启动。

# ---- 构建阶段：编译工具只存在于此，产物是 wheel；运行镜像不带工具链 ----
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# 受限网络下可换源，不传则用 pip 默认源：
#   docker build --build-arg PIP_INDEX_URL=https://<镜像源>/simple .
ARG PIP_INDEX_URL
# 依赖都有预编译 wheel，这里只负责打好本项目与依赖的 wheel，运行镜像不带任何工具链
RUN python -m pip wheel ${PIP_INDEX_URL:+--index-url $PIP_INDEX_URL} --wheel-dir /wheels .

# ---- 运行阶段：最小依赖面 + 非 root（uid/gid 10001）----
FROM python:3.12-slim AS runtime

ARG INSTALL_BROWSER=0

LABEL org.opencontainers.image.title="scrape-mcp" \
      org.opencontainers.image.source="https://github.com/guanyuyan/scrape-mcp"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/home/app \
    SCRAPE_MCP_DATA_DIR=/data \
    SCRAPE_MCP_TRANSPORT=streamable-http \
    SCRAPE_MCP_HTTP_HOST=0.0.0.0 \
    SCRAPE_MCP_HTTP_PORT=8002

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels mcp-scrape \
    && rm -rf /wheels

# L2 浏览器层（可选）：Chromium 与系统库只装进运行镜像，镜像会显著变大；
# 浏览器目录收紧为"所有人可读"，否则非 root 用户无法启动
RUN if [ "$INSTALL_BROWSER" = "1" ]; then \
        playwright install --with-deps chromium \
        && chmod -R a+rX /ms-playwright; \
    fi

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /home/app \
        --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data

USER app
WORKDIR /home/app

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/healthz', timeout=3)"

CMD ["scrape-mcp", "--transport", "streamable-http", "--port", "8002"]
