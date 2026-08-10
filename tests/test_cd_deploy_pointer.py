"""CD 生产指针契约：只有服务器健康部署成功后才快进 deploy。"""

from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "cd.yml"
UPDATER = ROOT / "scripts" / "update_deploy_ref.sh"


def test_deploy_pointer_step_runs_after_server_deploy() -> None:
    """校验 CD 工作流中生产基线指针的更新排在服务器部署之后。

    参数：无

    返回：
        None，断言 cd.yml 中"更新生产基线指针"步骤位于"服务器执行部署脚本"之后、
        指针更新前已注入 DEPLOY_COMMIT、指针步骤调用 update_deploy_ref.sh，
        且 deploy 作业在下载制品前以 fetch-depth: 0 拉取完整历史
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    deploy_job = text[text.index("  deploy:") :]
    server = text.index("- name: 服务器执行部署脚本")
    pointer = text.index("- name: 更新生产基线指针")
    assert server < pointer
    assert "DEPLOY_COMMIT" in text[:pointer]
    assert "scripts/update_deploy_ref.sh" in text[pointer:]
    assert "fetch-depth: 0" in deploy_job[: deploy_job.index("actions/download-artifact")]


def test_deploy_pointer_update_is_verified_fast_forward_without_force() -> None:
    """校验指针更新脚本只做带验证的快进推送，绝不强推。

    参数：无

    返回：
        None，断言 update_deploy_ref.sh 用 merge-base --is-ancestor 检查快进、
        通过 git push origin 推送、不含 --force、用 git ls-remote 复核远端对齐、
        未对齐时输出中文告警并按 seq 1 3 重试
    """
    text = UPDATER.read_text(encoding="utf-8")
    assert "git merge-base --is-ancestor" in text
    assert "git push origin" in text
    assert "--force" not in text
    assert "git ls-remote" in text
    assert "远端 deploy 与服务器实际 SHA 未对齐" in text
    assert "seq 1 3" in text
