"""整机冒烟（点火试车）：真实 build_app + uvicorn + 真实 web/dist 静态托管 + HTTP 断言。

与 tests/ 的 ASGITransport 契约测试互补：走真实端口、真实静态文件、真实 JSON 序列化。
用法：uv run python scripts/e2e_web_smoke.py
前置：web/dist 已构建（cd web && npm run build）；缺失则提示并非零退出。
退出码 0 = 通过（打印 E2E WEB SMOKE PASS）；任一步断言失败抛异常非零退出。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from src.bootstrap import build_app  # noqa: E402
from src.config import load_settings, load_watchlist  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "web" / "dist"
DB_PATH = "data/e2e_web_smoke.db"
PORT = 8187
BASE = f"http://127.0.0.1:{PORT}"
MOCK_EQUITY_MARKER = "10842.36"  # 前端 mock 假权益常量：生产构建应已 tree-shake，绝不应出现


def check_dist() -> None:
    """前置检查：web/dist 必须已构建（否则静态托管无内容可测）。"""
    if not (DIST / "index.html").is_file():
        print("未找到 web/dist 构建产物，请先执行：cd web && npm run build", file=sys.stderr)
        sys.exit(1)


def check_no_mock_bundle() -> None:
    """反 mock 检查：dist 的 js 资产不得含 mock 假权益常量（混入了说明构建/引用出错）。"""
    assets = list((DIST / "assets").glob("*.js"))
    assert assets, "web/dist/assets 下应有 js 资产"
    for path in assets:
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert MOCK_EQUITY_MARKER not in text, (
            f"生产构建疑似混入 mock 数据（{MOCK_EQUITY_MARKER}）：{path.name}"
        )
    print(f"3. 反 mock 检查通过（{len(assets)} 个 js 资产均不含 {MOCK_EQUITY_MARKER}）")


async def wait_ready(client: httpx.AsyncClient, timeout: float = 15.0) -> None:
    """带超时的重试循环：/api/status 应答 200 即视为 uvicorn 就绪。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            if (await client.get("/api/status")).status_code == 200:
                return
        except httpx.TransportError:
            pass  # 端口未就绪：连接被拒，继续重试
        if loop.time() > deadline:
            raise TimeoutError(f"服务 {timeout}s 内未就绪：{BASE}")
        await asyncio.sleep(0.2)


async def check_paper_reset_chain(client: httpx.AsyncClient) -> None:
    """操作链：reset 12345 → 账户读出 12345 → 复位 10000（无论成败都复位，防脏写 config.yaml）。"""
    r = await client.post("/api/paper/reset", json={"equity": 12345})
    assert r.status_code == 200, f"paper reset 12345 → {r.status_code}"
    try:
        available = float((await client.get("/api/account")).json()["available"])
        assert available == 12345.0, f"reset 后 available 应为 12345，实际 {available}"
    finally:
        r = await client.post("/api/paper/reset", json={"equity": 10000})
        assert r.status_code == 200, f"复位 initial_equity=10000 → {r.status_code}"
    print("5. 操作链通过（reset 12345 → account 读出 12345 → 已复位 10000）")


async def check_http(client: httpx.AsyncClient) -> None:
    """HTTP 断言链：首页静态托管 / 状态端点 / paper reset 操作链 / agent live。"""
    r = await client.get("/")
    assert r.status_code == 200, f"GET / → {r.status_code}"
    assert "/assets/" in r.text and ".js" in r.text, "首页 HTML 应引用 dist 构建的 js 资产"
    print("2. 首页静态托管通过（200 且引用 dist js 资产）")
    check_no_mock_bundle()
    body = (await client.get("/api/status")).json()
    for key in ("mode", "agent_running", "llm_configured"):
        assert key in body, f"/api/status 缺键 {key}"
    print(f"4. /api/status 通过（mode={body['mode']}，agent_running={body['agent_running']}）")
    await check_paper_reset_chain(client)
    body = (await client.get("/api/agent/live")).json()
    for key in ("in_round", "round", "tool_calls"):
        assert key in body, f"/api/agent/live 缺键 {key}"
    print("6. /api/agent/live 通过（in_round/round/tool_calls 键齐）")


async def main() -> None:
    check_dist()
    print("1. 前置检查通过（web/dist 已构建）")
    # paper reset 会经 write_settings 重写 config.yaml（丢手写注释）：先备份，跑完原样恢复
    config_path = ROOT / "config.yaml"
    config_backup = config_path.read_bytes()
    Path(DB_PATH).unlink(missing_ok=True)  # 干净库起跑，避免历史数据干扰断言
    settings = load_settings()
    settings.server.port = PORT
    ctx = await build_app(
        settings, load_watchlist(), mock_llm=True, mock_market=True, db_path=DB_PATH
    )
    assert ctx.server is not None
    server_task = asyncio.create_task(ctx.server.serve())
    try:
        async with httpx.AsyncClient(base_url=BASE, timeout=5.0) as client:
            await wait_ready(client)
            await check_http(client)
        print("E2E WEB SMOKE PASS")
    finally:
        # 优雅关闭（要点同 bootstrap.shutdown：should_exit 后收尾 server 任务与 db）
        ctx.server.should_exit = True
        await asyncio.gather(server_task, return_exceptions=True)
        await ctx.db.close()
        config_path.write_bytes(config_backup)  # 恢复 config.yaml 原样，避免弄脏工作区


if __name__ == "__main__":
    asyncio.run(main())
