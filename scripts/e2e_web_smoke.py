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
    """确认前端生产构建入口存在，否则提示构建命令并终止冒烟流程。

    参数：无

    返回：
        None，仅执行 web/dist/index.html 前置检查
    """
    if not (DIST / "index.html").is_file():
        print("未找到 web/dist 构建产物，请先执行：cd web && npm run build", file=sys.stderr)
        sys.exit(1)


def check_no_mock_bundle() -> None:
    """确认生产 JavaScript 资产存在且不包含前端模拟权益常量。

    参数：无

    返回：
        None，扫描构建资产并把通过数量输出到控制台
    """
    assets = list((DIST / "assets").glob("*.js"))
    assert assets, "web/dist/assets 下应有 js 资产"
    for path in assets:
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert MOCK_EQUITY_MARKER not in text, (
            f"生产构建疑似混入 mock 数据（{MOCK_EQUITY_MARKER}）：{path.name}"
        )
    print(f"3. 反 mock 检查通过（{len(assets)} 个 js 资产均不含 {MOCK_EQUITY_MARKER}）")


async def wait_ready(client: httpx.AsyncClient, timeout: float = 15.0) -> None:
    """轮询状态接口直至真实服务就绪或超过等待时限。

    参数：
        client: httpx.AsyncClient，已配置冒烟服务基础地址的客户端
        timeout: float，允许服务启动的最长秒数

    返回：
        None，服务首次返回状态码 200 时结束轮询

    异常：
        TimeoutError: 服务在限定时间内始终未就绪时抛出
    """
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
    """验证模拟权益重置与账户读取链路，并在结束前恢复默认权益。

    参数：
        client: httpx.AsyncClient，已连接真实冒烟服务的客户端

    返回：
        None，执行两次权益写入、一次账户读取并输出通过信息
    """
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
    """验证首页静态托管、监控接口、模拟重置和实时决策接口的 HTTP 契约。

    参数：
        client: httpx.AsyncClient，已连接真实冒烟服务的客户端

    返回：
        None，依次执行 HTTP 断言与模拟账户写读操作
    """
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
    """整机冒烟主流程：备份配置、干净库起真实服务、跑 HTTP 断言链，最后优雅关闭并恢复现场。

    参数：无

    返回：None，副作用为启动并停止 uvicorn 服务、删除并重建冒烟数据库、备份并原样恢复 config.yaml
    """
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
