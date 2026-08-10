"""版本化协议附录确保未入库的运行时提示词也立即获得新契约。"""

from src.agent.prompts import PromptLoader
from src.research.prompts import ResearchPromptLoader


def test_research_prompt_always_appends_asset_view_protocol(tmp_path):
    """校验未入库的自定义研报提示词也会被追加资产视角协议附录。

    参数：
        tmp_path: Path，pytest 临时目录夹具，用于写入自定义 research_prompt.md

    返回：
        None，断言生成的系统提示词同时包含自定义策略全文与
        RESEARCH_ASSET_VIEWS_PROTOCOL_V2 附录（出现 get_research_market_data、
        asset_views），且旧字段 return_24h / return_72h 不再出现
    """
    path = tmp_path / "research_prompt.md"
    path.write_text("自定义研报策略", encoding="utf-8")

    prompt, _ = ResearchPromptLoader(path).system_prompt("## 可用工具")

    assert "自定义研报策略" in prompt
    assert "RESEARCH_ASSET_VIEWS_PROTOCOL_V2" in prompt
    assert "get_research_market_data" in prompt
    assert "asset_views" in prompt
    assert "return_24h" not in prompt
    assert "return_72h" not in prompt


def test_execution_prompt_always_appends_contract_research_policy(tmp_path):
    """校验未入库的自定义执行提示词也会被追加合约级研报策略附录。

    参数：
        tmp_path: Path，pytest 临时目录夹具，用于写入自定义 system_prompt.md

    返回：
        None，断言生成的系统提示词同时包含自定义策略全文与
        EXECUTION_RESEARCH_POLICY_V2 附录（出现"当前下单合约""结构延续"约束）
    """
    path = tmp_path / "system_prompt.md"
    path.write_text("自定义执行策略", encoding="utf-8")

    prompt, _ = PromptLoader(path).system_prompt([])

    assert "自定义执行策略" in prompt
    assert "EXECUTION_RESEARCH_POLICY_V2" in prompt
    assert "当前下单合约" in prompt
    assert "结构延续" in prompt
