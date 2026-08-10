"""研报数据源连通性实测脚本（第一期）：金十 MCP / BlockBeats MCP / FRED / Polymarket。

用法：
  uv run python scripts/verify_research.py --sources   # 只测四类源连通性（实施第一步）
  uv run python scripts/verify_research.py             # 全流程（拉数据→组装→跑研报→落库）

密钥只从环境变量 / .env 读取（JIN10_MCP_TOKEN / BLOCKBEATS_API_KEY / FRED_API_KEY），
永不打印、永不落日志。不进测试套件（需要真实网络与密钥）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

# Windows GBK 控制台打印中文/emoji 兜底（L9，同 scripts/check_file_size.py 惯例）
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

JIN10_MCP_URL = os.environ.get("JIN10_MCP_URL", "https://mcp.jin10.com/mcp")
BLOCKBEATS_MCP_CMD = os.environ.get("BLOCKBEATS_MCP_CMD", "npx -y blockbeats-mcp")
FRED_BASE = os.environ.get("FRED_BASE_URL", "https://api.stlouisfed.org/fred")
POLYMARKET_BASE = os.environ.get("POLYMARKET_BASE_URL", "https://gamma-api.polymarket.com")


def _section(title: str) -> None:
    """在终端打印带分隔线的章节标题，便于区分实测输出的各阶段。

    参数：
        title: str，章节标题文本

    返回：
        None，向标准输出打印分隔线与标题
    """
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


async def test_jin10() -> None:
    """金十 HTTP MCP：握手 + tools/list + 拉一次日历/快讯。

    参数：
        无

    返回：
        None：金十 HTTP MCP：握手 + tools/list + 拉一次日历/快讯
    """
    token = os.environ.get("JIN10_MCP_TOKEN", "")
    if not token:
        print("[FAIL]  金十 MCP：JIN10_MCP_TOKEN 未配置（请填 .env）")
        return
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        import httpx2

        async with streamable_http_client(
            JIN10_MCP_URL,
            http_client=httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}),
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                print(
                    f"[OK]  金十 MCP：握手成功，工具 {len(names)} 个：{names[:8]}{'…' if len(names) > 8 else ''}"
                )
                if "list_calendar" in names:
                    result = await session.call_tool("list_calendar", {})
                    text = "".join(c.text or "" for c in result.content)
                    print(f"    list_calendar 返回：{len(text)} 字符（前 120：{text[:120]}）")
    except Exception as e:
        print(f"[FAIL]  金十 MCP 失败：{type(e).__name__}: {e}")


async def test_blockbeats() -> None:
    """BlockBeats stdio MCP（Windows 走 cmd /c、POSIX 直接 exec）：握手 + tools/list + 拉一次快讯。

    参数：
        无

    返回：
        None：BlockBeats stdio MCP（Windows 走 cmd /c、POSIX 直接 exec）：握手 + tools/list + 拉一次快讯
    """
    key = os.environ.get("BLOCKBEATS_API_KEY", "")
    if not key:
        print("[FAIL]  BlockBeats MCP：BLOCKBEATS_API_KEY 未配置（请填 .env）")
        return
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        from src.research.providers.mcp_client import _stdio_command

        env = dict(os.environ)
        env["BLOCKBEATS_API_KEY"] = key
        command, args = _stdio_command(BLOCKBEATS_MCP_CMD)
        params = StdioServerParameters(command=command, args=args, env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                print(
                    f"[OK]  BlockBeats MCP：握手成功，工具 {len(names)} 个：{names[:8]}{'…' if len(names) > 8 else ''}"
                )
                if "get_newsflash_24h" in names:
                    result = await session.call_tool("get_newsflash_24h", {})
                    text = "".join(c.text or "" for c in result.content)
                    print(f"    get_newsflash_24h 返回：{len(text)} 字符（前 120：{text[:120]}）")
    except Exception as e:
        print(f"[FAIL]  BlockBeats MCP 失败：{type(e).__name__}: {e}")


async def test_fred() -> None:
    """FRED 宏观序列：有 key 则实测一条序列。

    参数：
        无

    返回：
        None：FRED 宏观序列：有 key 则实测一条序列
    """
    key = os.environ.get("FRED_API_KEY", "")
    if not key:
        print(
            "[FAIL]  FRED：FRED_API_KEY 未配置（免费注册：https://fred.stlouisfed.org/docs/api/api_key.html）"
        )
        return
    try:
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{FRED_BASE}/series/observations",
                params={
                    "series_id": "DGS10",
                    "api_key": key,
                    "file_type": "json",
                    "observation_start": "2026-01-01",
                    "sort_order": "desc",
                },
            )
            resp.raise_for_status()
            rows = resp.json().get("observations", [])
            ok = [r for r in rows if r.get("value") not in (".", "", None)]
            print(
                f"[OK]  FRED：DGS10(10Y 美债) 返回 {len(ok)} 个观测点，最新 {ok[0]['date']}={ok[0]['value']}"
            )
    except Exception as e:
        print(f"[FAIL]  FRED 失败：{type(e).__name__}: {e}")


async def test_polymarket() -> None:
    """Polymarket 预测市场（公开 API，无 key；注意需跟随重定向）。

    参数：
        无

    返回：
        None：Polymarket 预测市场（公开 API，无 key；注意需跟随重定向）
    """
    try:
        import httpx

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(
                f"{POLYMARKET_BASE}/public-search",
                params={"q": "Fed rate cut", "limit_per_type": 3},
            )
            resp.raise_for_status()
            events = resp.json().get("events", [])
            print(f"[OK]  Polymarket：'Fed rate cut' 返回 {len(events)} 个事件")
            for e in events[:2]:
                title = e.get("title", "?")
                print(f"    - {title}")
    except Exception as e:
        print(f"[FAIL]  Polymarket 失败：{type(e).__name__}: {e}")


async def main() -> None:
    """脚本入口：按命令行参数分发为"只测数据源连通性"或"研报全流程"两种模式。

    参数：无（读取 sys.argv，含 --sources 时只测四类数据源连通性，否则跑完整研报流程）

    返回：
        None，实测结果打印到标准输出
    """
    if "--sources" in sys.argv:
        _section("研报数据源连通性实测")
        await test_jin10()
        await test_blockbeats()
        await test_fred()
        await test_polymarket()
        return
    await run_full_report()


async def run_full_report() -> None:
    """全流程：装配 → 预注入组装（含 token 估算）→ 跑研报 → 落库确认。

    使用项目现有配置与 researcher 凭证的 LLM provider；LLM 未配置时提示。

    参数：
        无

    返回：
        None：全流程：装配 → 预注入组装（含 token 估算）→ 跑研报 → 落库确认
    """
    from src.audit.trail import AuditTrail
    from src.agent.providers.factory import build_provider
    from src.config import load_settings
    from src.memory.db import Database
    from src.memory.repo import Repo
    from src.research.setup import build_research

    _section("研报全流程（装配 → 注入 → 研报 → 落库）")
    settings = load_settings()
    db = Database()
    await db.open("data/agent.db")
    try:
        repo = Repo(db)
        audit = AuditTrail(repo, settings.audit)
        if os.environ.get("LLM_MOCK") == "1":
            from src.research.mock_provider import ResearchMockProvider

            provider = ResearchMockProvider()
        else:
            provider = build_provider(settings, False, settings.agents.researcher.credential)
        components = build_research(settings, repo, audit, provider)
        print(
            f"已装配源：{'、'.join(components.data_provider.sources_ready) or '（无，请检查 .env 密钥）'}"
        )
        if not components.data_provider.sources_ready:
            print(
                "提示：JIN10_MCP_TOKEN / BLOCKBEATS_API_KEY / FRED_API_KEY 需填入 .env 才能拉取数据"
            )
        # 预注入组装 + token 估算（中文字符 ≈ 1.5 token/字 粗估）
        from src.research.preinject import build_preinjection
        from src.research.tool_handlers import ResearchToolDeps

        deps = ResearchToolDeps(provider=components.data_provider, repo=repo, mode=settings.mode)
        briefing = await build_preinjection(deps, hours=24)
        estimate = int(len(briefing) * 1.5)
        print(f"预注入组装完成：{len(briefing)} 字符，token 粗估约 {estimate:,}")
        print(f"首段预览：{briefing[:200]}…")
        # 跑研报（mock 模式产出确定性结果，真实模式调用 LLM）
        result = await components.agent.run(report_type="manual", hours=24)
        print(
            f"研报结果：ok={result.get('ok')} direction={result.get('direction')} "
            f"confidence={result.get('confidence')} report_id={result.get('report_id')}"
        )
        if not result.get("ok"):
            print(f"失败原因：{result.get('error')}")
        report = await repo.research.latest_report(include_error=True)
        if report is not None:
            print(
                f"落库确认：id={report.id} type={report.report_type} "
                f"direction={report.direction} error={'有' if report.error else '无'}"
            )
        # 收集完整结果：研报 + 工具调用链 + 元数据，三路输出（终端/JSON/MD）
        round_id = ""
        if result.get("round_id"):
            round_id = result["round_id"]
        elif report is not None:
            round_id = report.round_id
        tool_calls = await repo.list_audit_tool_calls(round_id) if round_id else []
        bundle = build_report_bundle(
            report=report,
            tool_calls=tool_calls,
            sources=components.data_provider.sources_ready,
            briefing_chars=len(briefing),
            token_estimate=estimate,
        )
        print_terminal(bundle, result)
        paths = write_report_files(bundle)
        for path in paths:
            print(f"已输出：{path}")
    finally:
        await db.close()


def build_report_bundle(
    *, report, tool_calls, sources: list[str], briefing_chars: int, token_estimate: int
) -> dict:
    """组装完整研报数据包（JSON/MD/终端三路共用）。

    参数：
        report: Any，已保存的研报记录
        tool_calls: Any，本轮工具调用记录列表
        sources: list[str]，本轮实际使用的数据源名称
        briefing_chars: int，预注入简报字符数
        token_estimate: int，预注入简报的估算 token 数

    返回：
        dict：组装完整研报数据包（JSON/MD/终端三路共用）
    """
    import json
    import time

    calls_out = []
    for c in tool_calls:
        try:
            result = json.loads(c.result_json or "{}")
            text = result.get("text", "") if isinstance(result, dict) else str(result)
        except json.JSONDecodeError:
            text = c.result_json
        calls_out.append(
            {
                "seq": c.seq,
                "tool": c.tool,
                "args": json.loads(c.args_json or "{}"),
                "result": text,
                "duration_ms": c.duration_ms,
            }
        )
    payload = {}
    if report is not None and not report.error:
        try:
            payload = json.loads(report.raw_json or "{}")
        except json.JSONDecodeError:
            payload = {"raw": report.raw_json}
    return {
        "report_id": report.id if report else None,
        "report_type": report.report_type if report else "",
        "direction": report.direction if report else "",
        "confidence": report.confidence if report else "",
        "horizon": report.horizon if report else "",
        "evidence": json.loads(report.evidence_json or "[]") if report else [],
        "risks": json.loads(report.risks_json or "[]") if report else [],
        "narrative": report.narrative if report else "",
        "payload": payload,
        "error": report.error if report else "",
        "sources_ready": sources,
        "preinjection": {"chars": briefing_chars, "token_estimate": token_estimate},
        "tool_calls": calls_out,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def print_terminal(bundle: dict, result: dict) -> None:
    """终端输出：研报摘要 + 全文 + 工具调用链（结果截断展示，全文看文件）。

    参数：
        bundle: dict，汇总研报正文、结论与工具链的数据包
        result: dict，待序列化或返回的执行结果

    返回：
        None：终端输出：研报摘要 + 全文 + 工具调用链（结果截断展示，全文看文件）
    """
    print(
        f"\n研报结果：ok={result.get('ok')} direction={bundle['direction']} "
        f"confidence={bundle['confidence']} report_id={bundle['report_id']}"
    )
    if not result.get("ok"):
        print(f"失败原因：{result.get('error')}")
    if bundle["error"]:
        print(f"落库为失败报告：error={bundle['error'][:200]}")
        return
    _section(f"研报全文（id={bundle['report_id']}）")
    print(
        f"方向：{bundle['direction']} | 置信度：{bundle['confidence']} | 周期：{bundle['horizon'] or '未指定'}"
    )
    print("\n依据（evidence）：")
    for i, item in enumerate(bundle["evidence"], 1):
        if isinstance(item, dict):
            print(f"  {i}. {item.get('point', '')}（来源：{item.get('source', '?')}）")
        else:
            print(f"  {i}. {item}")
    print("\n风险（risks）：")
    for i, item in enumerate(bundle["risks"], 1):
        print(f"  {i}. {item}")
    print(f"\n研判正文（narrative）：\n{bundle['narrative'] or '（空）'}")
    _section(f"工具调用链（共 {len(bundle['tool_calls'])} 次）")
    if not bundle["tool_calls"]:
        print("（本轮无工具调用——LLM 直接基于预注入输出）")
    for c in bundle["tool_calls"]:
        print(f"\n--- 第 {c['seq']} 次调用：{c['tool']} ---")
        print(f"入参：{c['args']}")
        text = c["result"]
        print(
            f"返回（前 800 字符）：\n{text[:800]}{'…（完整见输出文件）' if len(text) > 800 else ''}"
        )


def write_report_files(bundle: dict) -> list[str]:
    """落盘 JSON（机器可读完整版）与 MD（人读文档），返回文件路径列表。

    参数：
        bundle: dict，汇总研报正文、结论与工具链的数据包

    返回：
        list[str]：落盘 JSON（机器可读完整版）与 MD（人读文档），返回文件路径列表
    """
    import json
    from pathlib import Path

    out_dir = Path("output")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    rid = bundle["report_id"] or "failed"
    json_path = out_dir / f"research_report_{rid}_{stamp}.json"
    json_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out_dir / f"research_report_{rid}_{stamp}.md"
    md_path.write_text(_render_markdown(bundle), encoding="utf-8")
    return [str(json_path), str(md_path)]


def _render_markdown(bundle: dict) -> str:
    """渲染人读 MD 文档：研报结论 → 依据/风险 → 正文 → 工具调用链（含完整返回）。

    参数：
        bundle: dict，汇总研报正文、结论与工具链的数据包

    返回：
        str：渲染人读 MD 文档：研报结论 → 依据/风险 → 正文 → 工具调用链（含完整返回）
    """
    lines = [
        f"# 研报 #{bundle['report_id']}（{bundle['generated_at']}）",
        "",
        f"- 类型：{bundle['report_type']}",
        f"- 方向：**{bundle['direction']}** ｜ 置信度：**{bundle['confidence']}** ｜ 周期：{bundle['horizon'] or '未指定'}",
        f"- 数据源：{'、'.join(bundle['sources_ready']) or '无'}",
        f"- 预注入：{bundle['preinjection']['chars']} 字符（token 粗估 {bundle['preinjection']['token_estimate']:,}）",
        "",
        "## 研判正文",
        "",
        bundle["narrative"] or "（空）",
        "",
        "## 依据",
        "",
    ]
    for i, item in enumerate(bundle["evidence"], 1):
        if isinstance(item, dict):
            lines.append(f"{i}. {item.get('point', '')}（来源：{item.get('source', '?')}）")
        else:
            lines.append(f"{i}. {item}")
    lines += ["", "## 风险", ""]
    lines += [f"{i}. {item}" for i, item in enumerate(bundle["risks"], 1)]
    lines += ["", "## 工具调用链", ""]
    if not bundle["tool_calls"]:
        lines.append("（本轮无工具调用——LLM 直接基于预注入输出）")
    for c in bundle["tool_calls"]:
        lines += [
            f"### 第 {c['seq']} 次：`{c['tool']}`（耗时 {c['duration_ms']}ms）",
            "",
            f"入参：```json\n{json.dumps(c['args'], ensure_ascii=False, indent=2)}\n```",
            "",
            f"返回：\n```\n{c['result']}\n```",
            "",
        ]
    if bundle["error"]:
        lines += ["## 失败信息", "", f"```\n{bundle['error']}\n```", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    asyncio.run(main())
