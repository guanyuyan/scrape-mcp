# scrape-mcp 容器镜像
#
# 默认只装 L1（curl_cffi）依赖，镜像小、启动快，能覆盖大多数静态/普通动态页。
# 需要 L2 真浏览器（Cloudflare/验证码/SPA）时用：
#   docker build --build-arg INSTALL_BROWSER=1 -t scrape-mcp:l2 .
#
# 运行（HTTP 传输，供远端 MCP 客户端接入）：
#   docker run -p 8002:8002 scrape-mcp
#   curl http://127.0.0.1:8002/healthz

FROM python:3.12-slim

ARG INSTALL_BROWSER=0

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install . && rm -rf /app/src /app/build

# L2 浏览器渲染层（可选）：安装 Chromium 及系统依赖，镜像会显著变大
RUN if [ "$INSTALL_BROWSER" = "1" ]; then \
        pip install playwright && playwright install --with-deps chromium; \
    fi

ENV SCRAPE_MCP_TRANSPORT=streamable-http \
    SCRAPE_MCP_HTTP_HOST=0.0.0.0 \
    SCRAPE_MCP_HTTP_PORT=8002

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/healthz', timeout=3)"

CMD ["scrape-mcp", "--transport", "streamable-http", "--port", "8002"]
