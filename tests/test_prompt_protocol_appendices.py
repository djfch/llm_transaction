"""版本化协议附录确保未入库的运行时提示词也立即获得新契约。"""

from src.agent.prompts import PromptLoader
from src.research.prompts import ResearchPromptLoader


def test_research_prompt_always_appends_asset_view_protocol(tmp_path):
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
    path = tmp_path / "system_prompt.md"
    path.write_text("自定义执行策略", encoding="utf-8")

    prompt, _ = PromptLoader(path).system_prompt([])

    assert "自定义执行策略" in prompt
    assert "EXECUTION_RESEARCH_POLICY_V2" in prompt
    assert "当前下单合约" in prompt
    assert "结构延续" in prompt
