"""config_io.set_env_keys 测试：.env 逐行替换/追加、注释与其他行保留、空值跳过。

密钥铁规：返回值只含 key 名，永不携带 value。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.config_io import ConfigError, set_env_keys


@pytest.fixture(autouse=True)
def _clean_env():
    """用例前后恢复环境变量原状（set_env_keys 直接写 os.environ，须手动快照恢复）。"""
    names = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    saved = {k: os.environ.get(k) for k in names}
    for k in names:
        os.environ.pop(k, None)
    yield
    for k in names:
        if saved[k] is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = saved[k]  # type: ignore[index]


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    """预置一份含注释/空行/已注释 key 的 .env。"""
    path = tmp_path / ".env"
    path.write_text(
        "# Gate.io APIv4 密钥\n"
        "GATE_API_KEY=gate-old\n"
        "\n"
        "# LLM Provider 密钥\n"
        "ANTHROPIC_API_KEY=ant-old\n"
        "# OPENAI_API_KEY=commented-out\n",
        encoding="utf-8",
    )
    return path


def test_replaces_existing_key(env_file: Path):
    """已存在的 key：替换该行（不新增重复行），并同步 os.environ。"""
    assert set_env_keys({"ANTHROPIC_API_KEY": "ant-new"}, env_file) == ["ANTHROPIC_API_KEY"]
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines.count("ANTHROPIC_API_KEY=ant-new") == 1
    assert "ANTHROPIC_API_KEY=ant-old" not in lines
    assert os.environ["ANTHROPIC_API_KEY"] == "ant-new"


def test_appends_missing_key_at_eof(env_file: Path):
    """缺失的 key：文件末尾追加，已有行原样保留。"""
    assert set_env_keys({"OPENAI_API_KEY": "oai-new"}, env_file) == ["OPENAI_API_KEY"]
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == "OPENAI_API_KEY=oai-new"
    assert "GATE_API_KEY=gate-old" in lines  # 其他行不动


def test_preserves_comments_and_other_lines(env_file: Path):
    """注释行（含 # KEY= 形式）与非目标行逐字保留，原内容一行不动。"""
    before = env_file.read_text(encoding="utf-8").splitlines()
    set_env_keys({"OPENAI_API_KEY": "oai-new"}, env_file)
    after = env_file.read_text(encoding="utf-8").splitlines()
    assert after[: len(before)] == before
    assert "# OPENAI_API_KEY=commented-out" in after  # 注释不被当成目标行替换


def test_skips_empty_values(tmp_path: Path):
    """空值跳过：不写文件（不存在则不创建）、不同步 environ、不进返回值。"""
    path = tmp_path / ".env"
    assert set_env_keys({"OPENAI_API_KEY": ""}, path) == []
    assert not path.exists()
    assert "OPENAI_API_KEY" not in os.environ


def test_returns_only_key_names(env_file: Path):
    """返回值只含 key 名，永不携带 value（密钥铁规）。"""
    written = set_env_keys({"OPENAI_API_KEY": "oai-秘密值"}, env_file)
    assert written == ["OPENAI_API_KEY"]
    assert "oai-秘密值" not in str(written)


def test_rejects_control_chars_in_value(env_file: Path):
    """防御纵深：set_env_keys 边界拒绝换行注入（即使调用方未校验）。"""
    with pytest.raises(ConfigError, match="控制字符"):
        set_env_keys({"OPENAI_API_KEY": "sk-x\nLLM_MOCK=1"}, env_file)
    # 文件未被污染
    assert "LLM_MOCK" not in env_file.read_text(encoding="utf-8")


def test_rejects_control_chars_in_key(env_file: Path):
    """key 含控制字符同样拒绝。"""
    with pytest.raises(ConfigError, match="控制字符"):
        set_env_keys({"OPENAI_API_KEY\r": "x"}, env_file)
