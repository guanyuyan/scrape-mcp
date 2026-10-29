"""scrape-mcp：反爬感知抓取 + 极省 Token 内容精简的 MCP Server。

MCP 之外也提供 Python 入口（与 MCP 工具共用同一套实现）：

```python
from scrape_mcp.sdk import fetch

payload = asyncio.run(fetch("https://example.com"))
```

单独放 ``scrape_mcp.sdk`` 是为了不和内部 ``scrape_mcp.extract`` 子包重名。
"""

from __future__ import annotations

__version__ = "0.17.0"
