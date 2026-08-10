"""LLM 凭证管理端点：单条凭证的增、改、删。

凭证定义存 config.yaml（llm.credentials 列表），key 明文只存 .env（按 api_key_env
变量名落盘）；一次请求先保存定义、再按需写 key，跨文件不保证原子，失败时会明确
报告已保存的部分。两处分离是密钥铁规，任何 API 响应/日志永不包含 key 明文。
编辑时 name 锁定（name 是 agents 引用外键 + api_key_env 推导来源；改名走"删除重建"）。
llm.credentials 为空时经 resolve_credentials() 物化 default 再增改（零迁移双轨制）。
凭证写权收归本模块端点：PUT /api/config 会剥离 body 里 llm 段的 credentials 键
（见 routes_config.put_config）。运行期生效列表以 runtime 共享实例为准——手改
config.yaml 的 llm.credentials 段会被下次 API 写操作覆盖，须重启进程才以文件为准。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from src.config import CredentialConfig, Settings
from src.config_io import ConfigError, read_settings_raw, set_env_keys, write_settings
from src.server.deps import ServerDeps
from src.server.routes_config import (
    _config_422,
    _run_llm_reconfigure,
    _secrets_settings,
    _write_back,
)

# 凭证名规则（与前端一致）：小写字母/数字/连字符；只在请求体校验，
# 不动 CredentialConfig 模型（存量配置不受影响）
_NAME_PATTERN = r"^[a-z0-9-]+$"

# provider 取值与 CredentialConfig 的 Literal 保持一致
_Provider = Literal["anthropic", "openai_compat", "openai_responses"]


class _CredentialFields(BaseModel):
    """凭证公共字段：api_key 空串/纯空白 = 不写 .env；拒绝控制字符（同 SecretsBody 防护）。

    model 去空白后非空；max_tokens ≥ 1；openai_compat 必须填 openai_base_url（去空白后非空）。
    校验只加在请求体，不动 CredentialConfig（存量配置不受影响，同上）。
    """

    provider: _Provider = "anthropic"
    model: str = Field(min_length=1)
    max_tokens: int = Field(default=4096, ge=1)
    openai_base_url: str = ""
    thinking_effort: str = ""  # 空=跟随模型默认 / off / on / low / medium / high / xhigh / max
    api_key: str = ""

    @field_validator("thinking_effort")
    @classmethod
    def _thinking_effort_valid(cls, value: str) -> str:
        """只接受统一档位枚举；空白按空串处理（不传参数）。

        参数：
            value: str，待转换或校验的配置值
        返回：
            str，只接受统一档位枚举；空白按空串处理（不传参数）
        异常：
            ValueError，推理强度不是允许档位或空值时抛出
        """
        value = value.strip()
        if value not in ("", "off", "on", "low", "medium", "high", "xhigh", "max"):
            raise ValueError(
                "thinking_effort 必须是 off / on / low / medium / high / xhigh / max 之一（或留空）"
            )
        return value

    @field_validator("model")
    @classmethod
    def _model_not_blank(cls, value: str) -> str:
        """model 去空白后必须非空（纯空白 422），写入 strip 后的值。

        参数：
            value: str，待转换或校验的配置值
        返回：
            str，model 去空白后必须非空（纯空白 422），写入 strip 后的值
        异常：
            ValueError，模型名称去除空白后为空时抛出
        """
        value = value.strip()
        if not value:
            raise ValueError("model 不能为空白")
        return value

    @field_validator("api_key")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        """拒绝换行/回车/NUL（防 .env 换行注入，set_env_keys 同名防护的双层之一）。

        参数：
            value: str，待转换或校验的配置值
        返回：
            str，拒绝换行/回车/NUL（防 .env 换行注入，set_env_keys 同名防护的双层之一）
        异常：
            ValueError，密钥包含换行、回车或 NUL 字符时抛出
        """
        if any(c in value for c in ("\r", "\n", "\0")):
            raise ValueError("密钥值不允许包含换行/回车/NUL 字符")
        return value

    @model_validator(mode="after")
    def _openai_compat_requires_base_url(self) -> _CredentialFields:
        """openai_compat 的 base_url 去空白后必须非空（缺了 provider 无法连通）。

        参数：无
        返回：
            _CredentialFields，openai_compat 的 base_url 去空白后必须非空（缺了 provider 无法连通）
        异常：
            ValueError，openai_compat 凭证缺少非空 base_url 时抛出
        """
        if self.provider == "openai_compat" and not self.openai_base_url.strip():
            raise ValueError("provider 为 openai_compat 时 openai_base_url 不能为空")
        return self


class CredentialCreateBody(_CredentialFields):
    """POST /api/credentials 请求体：name 必填且符合命名规则。"""

    name: str = Field(pattern=_NAME_PATTERN)


class CredentialUpdateBody(_CredentialFields):
    """PUT /api/credentials/{name} 请求体：无 name（路径参数即身份，name 锁定不可改）。"""


def _effective_credentials(deps: ServerDeps) -> tuple[Settings, list[CredentialConfig]]:
    """生效配置与凭证列表：llm.credentials 为空时 resolve_credentials() 合成 default。

    生效列表以 runtime 共享实例为准：运行期手改 config.yaml 的 llm.credentials 段
    会被下次 API 写操作覆盖（须重启进程才以文件为准）。

    参数：
        deps: ServerDeps，当前模块所需的依赖集合
    返回：
        tuple[Settings, list[CredentialConfig]]，生效配置与凭证列表：llm.credentials 为空时 resolve_credentials() 合成 default
    """
    settings = _secrets_settings(deps)
    return settings, settings.llm.resolve_credentials()


def _save_credentials(deps: ServerDeps, credentials: list[CredentialConfig]) -> None:
    """凭证全量列表合并进 config.yaml 原文并落盘（write_settings 自带 agents 引用校验），

    然后把 llm.credentials 原地写回运行时共享实例（保持同一实例，下轮决策即生效）。

    参数：
        deps: ServerDeps，当前模块所需的依赖集合
        credentials: list[CredentialConfig]，完整凭证配置列表
    返回：
        None，凭证全量列表合并进 config.yaml 原文并落盘（write_settings 自带 agents 引用校验），
    异常：
        HTTPException，配置校验失败时转换为 422 响应
    """
    merged = read_settings_raw(deps.config_path)
    merged.setdefault("llm", {})["credentials"] = [c.model_dump() for c in credentials]
    try:
        new = write_settings(merged, deps.config_path)
    except ValueError as exc:  # ConfigError 及 validate_mode 的 ValueError
        raise _config_422(exc) from exc
    if deps.runtime_settings is not None:
        _write_back(deps.runtime_settings, new, ["llm.credentials"])


async def _save_key_and_reconfigure(
    deps: ServerDeps, key_env: str, api_key: str, cred_name: str
) -> dict[str, Any]:
    """写 key（strip 后非空才按 api_key_env 落 .env）并热重建 provider，组装统一响应。

    契约逐字：{"saved": true, "key_saved": bool, "llm_configured": bool, "llm_error": str}
    （命名对齐 put_config；热重建失败不 422，配置合法已落盘）。响应永不回显明文。
    api_key 纯空白按未填处理（key_saved=false）；非空时写 strip 后的值（粘贴常带首尾空白）。
    .env 写入抛 OSError（磁盘满/权限）映 500：凭证定义已落盘的半完成态诚实回报，
    指引经 PUT /api/credentials/{name} 补 key。

    参数：
        deps: ServerDeps，当前模块所需的依赖集合
        key_env: str，保存密钥的环境变量名
        api_key: str，待保存的 API 密钥
        cred_name: str，凭证名称
    返回：
        dict[str, Any]，写 key（strip 后非空才按 api_key_env 落 .env）并热重建 provider，组装统一响应
    异常：
        HTTPException，密钥含控制字符时返回 422，或 .env 写入失败时返回 500
    """
    api_key = api_key.strip()
    key_saved = False
    if api_key:
        try:
            set_env_keys({key_env: api_key}, deps.env_path)
        except ConfigError as exc:  # 控制字符注入防护（双层之一，请求体校验已先拦）
            raise _config_422(exc) from exc
        except OSError as exc:  # 半完成态：定义已落盘，key 未写
            raise HTTPException(
                status_code=500,
                detail=(
                    f"凭证定义已保存，但 key 写入 .env 失败：{exc}。"
                    f"请用编辑功能（PUT /api/credentials/{cred_name}）补 key"
                ),
            ) from exc
        key_saved = True
    reconfigure = await _run_llm_reconfigure(deps)
    return {
        "saved": True,
        "key_saved": key_saved,
        "llm_configured": reconfigure["llm_configured"],
        "llm_error": reconfigure["error"],
    }


def create_credentials_router(deps: ServerDeps) -> APIRouter:
    """创建凭证管理路由：注册单条凭证的新增、编辑、删除三个端点。

    参数：
        deps: ServerDeps，服务器依赖（配置路径、.env 路径、运行时共享配置等），
            供端点读写 config.yaml/.env 并触发 LLM 热重建

    返回：
        APIRouter：挂载 POST /api/credentials 与 PUT/DELETE /api/credentials/{name} 的路由器
    """
    router = APIRouter(prefix="/api")

    @router.post("/credentials")
    async def post_credential(body: CredentialCreateBody) -> dict[str, Any]:
        """新增凭证：重名 422；空 credentials 首次创建时物化 default 再追加；只热重建一次。

        参数：
            body: CredentialCreateBody，请求体或配置更新模型
        返回：
            dict[str, Any]，新增凭证：重名 422；空 credentials 首次创建时物化 default 再追加；只热重建一次
        异常：
            HTTPException，凭证名称已存在时返回 422
        """
        _, credentials = _effective_credentials(deps)
        if any(c.name == body.name for c in credentials):
            raise HTTPException(status_code=422, detail=f"凭证已存在: {body.name}")
        cred = CredentialConfig(
            name=body.name,
            provider=body.provider,
            model=body.model,
            max_tokens=body.max_tokens,
            openai_base_url=body.openai_base_url,
            thinking_effort=body.thinking_effort,
            # api_key_env 留空：按 LLM_KEY_<NAME> 推导（见 CredentialConfig 校验器）
        )
        _save_credentials(deps, [*credentials, cred])
        return await _save_key_and_reconfigure(deps, cred.api_key_env, body.api_key, body.name)

    @router.put("/credentials/{name}")
    async def put_credential(name: str, body: CredentialUpdateBody) -> dict[str, Any]:
        """按名更新凭证：未知名 404；api_key_env 保持不变（.env 里的 key 位置不动）；

        default 合成凭证被编辑时随全量写回自然物化列表。

        参数：
            name: str，工具、凭证或对象名称
            body: CredentialUpdateBody，请求体或配置更新模型
        返回：
            dict[str, Any]，按名更新凭证：未知名 404；api_key_env 保持不变（.env 里的 key 位置不动）；
        异常：
            HTTPException，目标凭证不存在时返回 404
        """
        _, credentials = _effective_credentials(deps)
        old = next((c for c in credentials if c.name == name), None)
        if old is None:
            raise HTTPException(status_code=404, detail=f"凭证不存在: {name}")
        updated = CredentialConfig(
            name=old.name,
            provider=body.provider,
            model=body.model,
            max_tokens=body.max_tokens,
            openai_base_url=body.openai_base_url,
            thinking_effort=body.thinking_effort,
            api_key_env=old.api_key_env,  # 保持不变：name 锁定，推导来源不动
        )
        _save_credentials(deps, [updated if c.name == name else c for c in credentials])
        return await _save_key_and_reconfigure(deps, updated.api_key_env, body.api_key, name)

    @router.delete("/credentials/{name}")
    async def delete_credential(name: str) -> dict[str, Any]:
        """删除凭证：未知名 404；被 agents.trader/reviewer 引用 422（先解除引用）；

        .env 里的 key 保留不删（与现状一致，无害）。

        参数：
            name: str，工具、凭证或对象名称
        返回：
            dict[str, Any]，删除凭证：未知名 404；被 agents.trader/reviewer 引用 422（先解除引用）；
        异常：
            HTTPException，目标凭证不存在时返回 404，或仍被 Agent 引用时返回 422
        """
        settings, credentials = _effective_credentials(deps)
        if not any(c.name == name for c in credentials):
            raise HTTPException(status_code=404, detail=f"凭证不存在: {name}")
        used_by = [
            agent
            for agent, binding in (
                ("trader", settings.agents.trader),
                ("reviewer", settings.agents.reviewer),
            )
            if binding.credential == name
        ]
        if used_by:
            raise HTTPException(
                status_code=422,
                detail=f"凭证被 agents.{', agents.'.join(used_by)} 引用，请先解除引用: {name}",
            )
        _save_credentials(deps, [c for c in credentials if c.name != name])
        return await _save_key_and_reconfigure(deps, "", "", name)  # 删除不写 key，仅热重建

    return router
