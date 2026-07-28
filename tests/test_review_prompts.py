"""src/review/prompts.py 测试：缺文件告警一次（不重复骚扰）、缺省空正文兜底。"""

from src.review import prompts
from src.review.prompts import ReviewPromptLoader


def test_missing_file_warns_once(tmp_path, monkeypatch):
    """首次加载即缺文件：logger.warning 一次；重复加载不再告警（不重复骚扰）。"""
    warnings: list[str] = []
    monkeypatch.setattr(prompts.logger, "warning", lambda *args: warnings.append(str(args)))
    loader = ReviewPromptLoader(tmp_path / "review_prompt.md")  # 文件不存在
    full, _ = loader.system_prompt("工具说明")
    assert full == "\n\n工具说明"  # 空正文兜底（不抛错，工具说明段仍组成可用 prompt）
    loader.system_prompt("工具说明")
    loader.system_prompt("工具说明")
    assert len(warnings) == 1


def test_existing_file_no_warning(tmp_path, monkeypatch):
    """文件存在：正常加载，不告警。"""
    warnings: list[str] = []
    monkeypatch.setattr(prompts.logger, "warning", lambda *args: warnings.append(str(args)))
    path = tmp_path / "review_prompt.md"
    path.write_text("# 复盘纪律", encoding="utf-8")
    full, _ = ReviewPromptLoader(path).system_prompt("工具说明")
    assert full == "# 复盘纪律\n\n工具说明"
    assert warnings == []
