from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QDialog, QMessageBox

from bundlehelper.config import ConfigStore, InMemoryCredentialStore
from bundlehelper.models import AppSettings, AppState, ProjectConfig, RefManifest
from bundlehelper.sync_service import AdoptionReport
from bundlehelper.ui import MainWindow, SettingsDialog


def make_state(tmp_path: Path) -> AppState:
    baseline = RefManifest(head_oid="a" * 40, head_branch="main", heads={"main": "a" * 40}, tags={})
    settings = AppSettings(
        bundle_dir=str(tmp_path / "global bundles"),
        remote_temp_dir="/tmp/global",
        log_dir=str(tmp_path / "global logs"),
        log_retention_days=30,
    )
    first = ProjectConfig(
        id="first",
        name="First",
        github_url="https://github.com/acme/first",
        local_repo_path=str(tmp_path / "first"),
        server_repo_path="/srv/first",
        bundle_dir_override=str(tmp_path / "first bundles"),
        remote_temp_dir_override="/tmp/first",
    )
    first.ssh.host = "example.com"
    first.ssh.user = "dev"
    second = ProjectConfig(
        id="second",
        name="Second",
        github_url="https://github.com/acme/second",
        local_repo_path=str(tmp_path / "second"),
        server_repo_path="/srv/second",
        baseline=baseline,
        setup_complete=True,
    )
    second.ssh.host = "example.org"
    second.ssh.user = "ops"
    return AppState(settings=settings, projects=[first, second])


def test_project_switch_resolves_override_and_inherited_directories(qtbot, tmp_path: Path) -> None:
    state = make_state(tmp_path)
    window = MainWindow(
        ConfigStore(tmp_path / "config" / "projects.json"),
        InMemoryCredentialStore(),
        state,
    )
    qtbot.addWidget(window)
    window.show()
    assert window.name_edit.text() == "First"
    assert str(tmp_path / "first bundles") in window.bundle_effective_label.text()
    assert not window.delta_button.isEnabled()
    assert window.full_button.isEnabled()
    assert window.adopt_button.isEnabled()

    window.name_edit.setText("First renamed")
    assert window.project_list.item(0).text() == "First renamed"

    window.project_list.setCurrentRow(1)
    qtbot.wait(20)
    assert window.name_edit.text() == "Second"
    assert [window.project_list.item(row).text() for row in range(2)] == [
        "First renamed",
        "Second",
    ]
    assert window.bundle_override_edit.text() == ""
    assert str(tmp_path / "global bundles") in window.bundle_effective_label.text()
    assert window.delta_button.isEnabled()
    assert window.push_button.isEnabled()
    assert not window.adopt_button.isEnabled()
    window.close()


def test_delete_project_keeps_remaining_project_configuration(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config" / "projects.json"
    state = make_state(tmp_path)
    window = MainWindow(
        ConfigStore(config_path),
        InMemoryCredentialStore(),
        state,
    )
    qtbot.addWidget(window)
    window.project_list.setCurrentRow(1)

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    window._delete_project()

    assert [project.id for project in window.state.projects] == ["first"]
    remaining = window.state.projects[0]
    assert remaining.name == "First"
    assert remaining.github_url == "https://github.com/acme/first"
    assert remaining.local_repo_path == str(tmp_path / "first")
    assert remaining.ssh.host == "example.com"
    assert window.name_edit.text() == "First"
    assert window.project_list.item(0).text() == "First"

    reloaded = ConfigStore(config_path).load()
    assert [project.id for project in reloaded.projects] == ["first"]
    assert reloaded.projects[0].github_url == "https://github.com/acme/first"
    assert reloaded.projects[0].ssh.host == "example.com"
    window.close()


def test_new_project_name_avoids_existing_default_names(qtbot, tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.projects[0].name = "项目 1"
    state.projects[1].name = "项目 3"
    window = MainWindow(
        ConfigStore(tmp_path / "config" / "projects.json"),
        InMemoryCredentialStore(),
        state,
    )
    qtbot.addWidget(window)

    window._add_project()

    assert window.state.projects[-1].name == "项目 4"
    assert [window.project_list.item(row).text() for row in range(3)] == [
        "项目 1",
        "项目 3",
        "项目 4",
    ]
    window.close()


def test_adopt_button_requires_complete_configuration(qtbot, tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.projects[0].github_url = ""
    window = MainWindow(
        ConfigStore(tmp_path / "config" / "projects.json"),
        InMemoryCredentialStore(),
        state,
    )
    qtbot.addWidget(window)

    assert not window.adopt_button.isEnabled()
    window.close()


def test_operation_uses_configuration_snapshot(qtbot, tmp_path: Path, monkeypatch) -> None:
    state = make_state(tmp_path)
    window = MainWindow(
        ConfigStore(tmp_path / "config" / "projects.json"),
        InMemoryCredentialStore(),
        state,
    )
    qtbot.addWidget(window)
    captured: dict[str, object] = {}

    def fake_begin(operation, project, settings, runner, on_success, **kwargs):
        captured.update(
            operation=operation,
            project=project,
            settings=settings,
            runner=runner,
            on_success=on_success,
            kwargs=kwargs,
        )

    monkeypatch.setattr(window, "_begin_sync_operation", fake_begin)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    window._start_full_bootstrap()
    assert captured["operation"] == "\u9996\u6b21\u5168\u91cf\u90e8\u7f72"
    snapshot_project = captured["project"]
    snapshot_settings = captured["settings"]
    assert isinstance(snapshot_project, ProjectConfig)
    assert isinstance(snapshot_settings, AppSettings)
    old_bundle = snapshot_project.resolved_bundle_dir(snapshot_settings)

    window.state.settings.bundle_dir = str(tmp_path / "changed global")
    window.state.projects[0].bundle_dir_override = str(tmp_path / "changed project")
    assert snapshot_project.resolved_bundle_dir(snapshot_settings) == old_bundle
    window.close()


def test_token_status_and_log_redaction_do_not_expose_secret(qtbot, tmp_path: Path) -> None:
    token = "github_pat_SECRET_123"
    state = make_state(tmp_path)
    window = MainWindow(
        ConfigStore(tmp_path / "config" / "projects.json"),
        InMemoryCredentialStore(token),
        state,
    )
    qtbot.addWidget(window)
    assert token not in window.token_status_label.text()
    assert "\u5df2\u4fdd\u5b58" in window.token_status_label.text()
    window._active_secrets = (token,)
    window._append_log("Authorization: Bearer " + token + " BUNDLE_HELPER_GITHUB_TOKEN=" + token)
    log_text = window.log_view.toPlainText()
    assert token not in log_text
    assert "***" in log_text
    window.close()


def test_settings_dialog_returns_configured_values(qtbot, tmp_path: Path) -> None:
    settings = AppSettings(
        bundle_dir=str(tmp_path / "bundles"),
        remote_temp_dir="/srv/tmp",
        log_dir=str(tmp_path / "logs"),
        log_retention_days=12,
    )
    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)
    assert dialog.bundle_edit.text() == str(tmp_path / "bundles")
    assert dialog.remote_edit.text() == "/srv/tmp"
    assert dialog.retention_spin.value() == 12
    assert dialog.settings().log_dir == str(tmp_path / "logs")
    dialog.reject()

def test_adopt_inspection_blocks_when_report_has_issues(qtbot, tmp_path: Path, monkeypatch) -> None:
    state = make_state(tmp_path)
    window = MainWindow(
        ConfigStore(tmp_path / "config" / "projects.json"),
        InMemoryCredentialStore(),
        state,
    )
    qtbot.addWidget(window)
    calls: list[dict[str, object]] = []
    warnings: list[str] = []

    def fake_begin(operation, project, settings, runner, on_success, **kwargs):
        calls.append(
            {
                "operation": operation,
                "project": project,
                "settings": settings,
                "runner": runner,
                "on_success": on_success,
                "kwargs": kwargs,
            }
        )

    def fake_warning(_parent, _title, text):
        warnings.append(text)
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(window, "_begin_sync_operation", fake_begin)
    monkeypatch.setattr(QMessageBox, "warning", fake_warning)
    window._start_adopt_existing()

    assert [call["operation"] for call in calls] == ["检查已有仓库"]
    local = RefManifest(head_oid="a" * 40, head_branch="main", heads={"main": "a" * 40})
    server = RefManifest(head_oid="b" * 40, head_branch="main", heads={"main": "b" * 40})
    report = AdoptionReport(
        local_manifest=local,
        server_manifest=server,
        issues=["分支 main 在本地和服务器已分叉，请先手工合并"],
    )

    result = calls[0]["on_success"](report)  # type: ignore[operator]

    assert result == ""
    assert len(calls) == 1
    assert "分支 main 在本地和服务器已分叉" in warnings[-1]
    window.close()


def test_adopt_confirmation_rechecks_and_records_server_baseline(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    state = make_state(tmp_path)
    window = MainWindow(
        ConfigStore(tmp_path / "config" / "projects.json"),
        InMemoryCredentialStore(),
        state,
    )
    qtbot.addWidget(window)
    local = RefManifest(head_oid="a" * 40, head_branch="main", heads={"main": "a" * 40})
    server = RefManifest(
        head_oid="c" * 40,
        head_branch="main",
        heads={"main": "c" * 40, "feature/server": "d" * 40},
        tags={"v2.0.0": "e" * 40},
    )
    report = AdoptionReport(
        local_manifest=local,
        server_manifest=server,
        fast_forward_heads={"main": ("a" * 40, "c" * 40)},
        new_heads={"feature/server": "d" * 40},
        new_tags={"v2.0.0": "e" * 40},
    )

    class FakeSync:
        def __init__(self) -> None:
            self.inspect_calls = 0
            self.expected: AdoptionReport | None = None

        def inspect_existing_repos(self, *_args, **_kwargs):
            self.inspect_calls += 1
            return report

        def adopt_existing_repos(self, *_args, expected, **_kwargs):
            self.expected = expected
            return server

    fake_sync = FakeSync()
    calls: list[dict[str, object]] = []
    questions: list[str] = []

    def fake_begin(operation, project, settings, runner, on_success, **kwargs):
        calls.append(
            {
                "operation": operation,
                "project": project,
                "settings": settings,
                "runner": runner,
                "on_success": on_success,
                "kwargs": kwargs,
            }
        )

    def fake_question(_parent, _title, text, *_args, **_kwargs):
        questions.append(text)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(window, "_sync_factory", lambda _known_hosts: fake_sync)
    monkeypatch.setattr(window, "_begin_sync_operation", fake_begin)
    monkeypatch.setattr(QMessageBox, "question", fake_question)
    window._start_adopt_existing()

    assert [call["operation"] for call in calls] == ["检查已有仓库"]
    inspected = calls[0]["runner"](  # type: ignore[operator]
        object(),
        lambda _value, _message: None,
        lambda _line: None,
        object(),
    )
    assert fake_sync.inspect_calls == 1
    assert calls[0]["on_success"](inspected) == ""  # type: ignore[operator]

    assert [call["operation"] for call in calls] == ["检查已有仓库", "接入已有仓库"]
    question_text = questions[-1]
    assert "可快进分支：main" in question_text
    assert "服务器新增分支：1 个" in question_text
    assert "服务器新增 Tag：1 个" in question_text

    baseline = calls[1]["runner"](  # type: ignore[operator]
        object(),
        lambda _value, _message: None,
        lambda _line: None,
        object(),
    )
    assert fake_sync.expected is report
    outcome = calls[1]["on_success"](baseline)  # type: ignore[operator]

    current = window.state.projects[0]
    assert current.setup_complete
    assert current.baseline is not None
    assert current.baseline.heads == server.heads
    assert current.baseline.tags == server.tags
    assert "已有仓库接入完成，同步基线已更新" in outcome
    assert not window.adopt_button.isEnabled()
    assert window.delta_button.isEnabled()
    window.close()
