"""单文件有效代码行门禁的统计口径与边界回归测试。"""

from pathlib import Path

import pytest

from scripts import check_file_size as file_size


def _write_source(path: Path, lines: int) -> None:
    """写入指定有效行数的可解析 Python 源码。

    参数：
        path: Path，待创建的 Python 文件路径
        lines: int，需要写入的有效代码行数

    返回：
        None，创建父目录并写入重复赋值语句
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("value = 1\n" * lines, encoding="utf-8")


def test_effective_lines_excludes_only_real_docstrings(tmp_path: Path) -> None:
    """确认模块、类、同步与异步函数文档会排除，控制流普通字符串仍计入代码。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，写入合成源码并断言有效代码行统计结果
    """
    source = '''"""模块说明。"""

class Sample:
    """类说明。"""

    def method(self):
        """同步方法说明。
        第二行。
        """
        return 1

async def fetch():
    """异步函数说明。"""
    return 2

if True:
    """控制流中的普通字符串表达式。"""
    value = 3

try:
    """异常块中的普通字符串表达式。"""
finally:
    value = 4
'''
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")

    _, effective = file_size.effective_lines(path)

    assert effective == 12


def test_effective_lines_distinguishes_comments_and_line_endings(tmp_path: Path) -> None:
    """确认纯注释与空行被排除，行尾注释保留且 CRLF 无末尾换行也能统计。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，写入不同换行形式的源码并断言总行数与有效行数
    """
    lf_path = tmp_path / "lf.py"
    lf_path.write_text("# 纯注释\nvalue = 1  # 行尾注释\n\n", encoding="utf-8")
    crlf_path = tmp_path / "crlf.py"
    crlf_path.write_bytes(b"# comment\r\nvalue = 1  # inline")

    assert file_size.effective_lines(lf_path) == (3, 1)
    assert file_size.effective_lines(crlf_path) == (2, 1)


def test_main_enforces_soft_and_hard_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """确认软上限只告警、硬上限等值仍通过而超过硬上限会失败。

    参数：
        tmp_path: Path，pytest 提供的临时项目根目录
        monkeypatch: pytest.MonkeyPatch，用于缩小测试中的门禁阈值和替换项目根目录
        capsys: pytest.CaptureFixture[str]，用于读取门禁输出

    返回：
        None，构造边界文件并断言退出码与告警、失败清单
    """
    monkeypatch.setattr(file_size, "ROOT", tmp_path)
    monkeypatch.setattr(file_size, "SOFT_LIMIT", 3)
    monkeypatch.setattr(file_size, "HARD_LIMIT", 5)
    monkeypatch.setattr(file_size, "BASELINE_OVERSIZE", {})
    _write_source(tmp_path / "src" / "soft_edge.py", 3)
    _write_source(tmp_path / "src" / "warning.py", 4)
    _write_source(tmp_path / "src" / "hard_edge.py", 5)
    _write_source(tmp_path / "src" / "failure.py", 6)

    assert file_size.main() == 1
    output = capsys.readouterr().out
    assert "warning.py" in output and "hard_edge.py" in output
    assert "failure.py" in output and "失败：" in output
    assert "soft_edge.py" not in output


def test_baseline_only_allows_registered_oversize_without_growth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """确认当前无伪豁免项，真实超限基线只能缩小且增加一行即失败。

    参数：
        tmp_path: Path，pytest 提供的临时项目根目录
        monkeypatch: pytest.MonkeyPatch，用于注入合成超限基线与项目根目录
        capsys: pytest.CaptureFixture[str]，用于清空两次门禁执行的输出

    返回：
        None，分别断言基线等值通过和超过登记值失败
    """
    assert file_size.BASELINE_OVERSIZE == {}
    monkeypatch.setattr(file_size, "ROOT", tmp_path)
    monkeypatch.setattr(file_size, "SOFT_LIMIT", 3)
    monkeypatch.setattr(file_size, "HARD_LIMIT", 5)
    monkeypatch.setattr(file_size, "BASELINE_OVERSIZE", {"src/legacy.py": 6})
    path = tmp_path / "src" / "legacy.py"
    _write_source(path, 6)

    assert file_size.main() == 0
    capsys.readouterr()
    _write_source(path, 7)
    assert file_size.main() == 1
    assert "豁免基线 6" in capsys.readouterr().out
