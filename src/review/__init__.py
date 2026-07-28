"""复盘子系统：每日复盘历史交易与决策，LLM 归因分析后可改写策略书（自进化闭环）。

组成：stats（代码侧统计口径）/ strategy（策略版本管理）/ prompts（复盘提示词加载）/
tool_schemas + tool_handlers + tools（8 个复盘工具）/ agent（多轮工具调用循环）/
scheduler（每日定时 + 手动触发）。

解耦约束：本包不 import src/agent/* 任何模块（LLMProvider 协议、工具说明渲染等
以鸭子类型/本地实现消化，允许少量重复）；只依赖 src/memory、src/audit、src/config、
src/utils。安全不变量见 docs/superpowers/specs/2026-07-27-review-agent-design.md §7。
"""
