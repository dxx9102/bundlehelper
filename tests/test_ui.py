from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QDialog, QMessageBox

from bundlehelper.config import ConfigStore, InMemoryCredentialStore
from bundlehelper.models import AppSettings, AppState, ProjectConfig, RefManifest
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

    window.project_list.setCurrentRow(1)
    qtbot.wait(20)
    assert window.name_edit.text() == "Second"
    assert window.bundle_override_edit.text() == ""
    assert str(tmp_path / "global bundles") in window.bundle_effective_label.text()
    assert window.delta_button.isEnabled()
    assert window.push_button.isEnabled()
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
