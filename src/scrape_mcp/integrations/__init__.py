"""第三方框架适配：可选依赖，不在核心包导入时加载。

- ``langchain``：``build_tools()`` 返回 LangChain ``StructuredTool`` 列表
- ``llamaindex``：``build_reader()`` 返回可直接塞进 ingestion pipeline 的 Reader

两组适配都只是薄封装，参数与返回载荷和 MCP 工具完全一致。
"""
