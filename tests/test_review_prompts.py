"""src/review/prompts.py 测试：缺文件告警一次（不重复骚扰）、缺省空正文兜底。"""

from src.review import prompts
from src.review.prompts import ReviewPromptLoader


def test_missing_file_warns_once(tmp_path, monkeypatch):
    """验证缺少复盘提示词文件时只告警一次并返回可用兜底正文。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: MonkeyPatch，用于拦截日志告警调用

    返回：
        None，通过断言验证重复加载不会重复告警
    """
    warnings: list[str] = []
    monkeypatch.setattr(prompts.logger, "warning", lambda *args: warnings.append(str(args)))
    loader = ReviewPromptLoader(tmp_path / "review_prompt.md")  # 文件不存在
    full, _ = loader.system_prompt("工具说明")
    assert "REVIEW_ATTRIBUTION_POLICY_V1" in full  # 空正文仍获得强制归因纪律
    assert full.endswith("\n\n工具说明")  # 工具说明仍位于完整提示词末尾
    loader.system_prompt("工具说明")
    loader.system_prompt("工具说明")
    assert len(warnings) == 1


def test_existing_file_no_warning(tmp_path, monkeypatch):
    """验证复盘提示词文件存在时正常加载且不产生告警。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: MonkeyPatch，用于拦截日志告警调用

    返回：
        None，通过断言验证正文拼接结果和空告警列表
    """
    warnings: list[str] = []
    monkeypatch.setattr(prompts.logger, "warning", lambda *args: warnings.append(str(args)))
    path = tmp_path / "review_prompt.md"
    path.write_text("# 复盘纪律", encoding="utf-8")
    full, _ = ReviewPromptLoader(path).system_prompt("工具说明")
    assert full.startswith("# 复盘纪律\n\n## 强制复盘附录")
    assert full.endswith("\n\n工具说明")
    assert warnings == []
