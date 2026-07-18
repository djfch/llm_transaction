"""监控后端（FastAPI）：只读监控 + 配置编辑 + WebSocket 推送。"""

from src.server.app import create_app
from src.server.deps import ServerDeps

__all__ = ["ServerDeps", "create_app"]
