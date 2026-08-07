"""研报数据源：MCP 会话封装（HTTP 金十 / stdio 律动）+ 各源实现 + 聚合接口。

分层：
- base.py        数据模型（FlashItem/CalendarEvent）+ 错误类型 + 聚合器 ResearchDataProvider
- mcp_client.py  MCP 会话封装（复用连接、多次 call_tool）
- jin10.py      金十源（HTTP MCP：日历/快讯/搜索/详情）
- blockbeats.py 律动源（stdio MCP：快讯/搜索/指标组）
- fred.py       FRED 宏观序列（httpx 直调）
- polymarket.py Polymarket 预测概率（httpx 直调）

任何源失败抛 ResearchSourceError，由工具层转中文"数据不可用"哨兵，
不中断研报轮（vendor 降级模式，借鉴 TradingAgents）。
"""
