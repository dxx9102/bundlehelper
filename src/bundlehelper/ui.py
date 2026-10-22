from __future__ import annotations

import copy
import re
import sys
import threading
import traceback
import uuid
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QSignalBlocker, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from .config import ConfigStore, CredentialStore, WindowsCredentialStore
from .errors import BundleHelperError, CancelledError
from .logging_service import OperationLogger, prune_logs, sanitize_text
from .models import AppSettings, AppState, ProjectConfig, RefManifest, default_bundle_dir, default_log_dir
from .process_runner import safe_remove_dir
from .ssh_service import HostKeyOption, SshService
from .sync_service import SyncService

APP_STYLE = """
QMainWindow, QDialog { background: #f4f6f8; }
QWidget { font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 10pt; color: #20262e; }
QGroupBox {
    background: white; border: 1px solid #d8dee6; border-radius: 7px;
    margin-top: 12px; padding-top: 10px; font-weight: 600;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; color: #2d5f8b; }
QLineEdit, QPlainTextEdit, QSpinBox, QListWidget {
    background: white; border: 1px solid #cbd3dc; border-radius: 5px; padding: 5px;
}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QListWidget:focus { border: 1px solid #3979b5; }
QPushButton { background: #ffffff; border: 1px solid #c7d0da; border-radius: 5px; padding: 6px 12px; }
QPushButton:hover { background: #edf4fb; border-color: #7fa8cf; }
QPushButton:pressed { background: #dceaf7; }
QPushButton:disabled { color: #98a2ad; background: #edf0f3; border-color: #d7dce2; }
QPushButton[primary="true"] { color: white; background: #2f6fa8; border-color: #2f6fa8; font-weight: 600; }
QPushButton[primary="true"]:hover { background: #275f91; }
QPushButton[danger="true"] { color: #a12626; }
QProgressBar { border: 1px solid #cbd3dc; border-radius: 5px; background: white; text-align: center; }
QProgressBar::chunk { background: #4c8bc2; border-radius: 4px; }
QListWidget::item { padding: 8px; border-radius: 4px; }
QListWidget::item:selected { color: white; background: #3e7fb7; }
"""


class OperationWorker(QThread):
    progress = Signal(int, str)
    output = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str, str)

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task = task
        self.cancel_event = threading.Event()

    def request_cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            result = self.task(self.progress.emit, self.output.emit, self.cancel_event)
            self.succeeded.emit(result)
        except Exception as exc:
            title = "操作已取消" if isinstance(exc, CancelledError) else (str(exc).strip() or type(exc).__name__)
            detail = f"{type(exc).__name__}: {exc}".strip()[:4000]
            self.failed.emit(title, detail)


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("全局设置")
        self.setMinimumWidth(650)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.bundle_edit = QLineEdit(settings.bundle_dir)
        self.remote_edit = QLineEdit(settings.remote_temp_dir)
        self.log_edit = QLineEdit(settings.log_dir)
        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(1, 3650)
        self.retention_spin.setSuffix(" 天")
        self.retention_spin.setValue(settings.log_retention_days)
        form.addRow("本地 bundle 临时目录：", self._directory_row(self.bundle_edit, self._browse_bundle))
        form.addRow("服务器临时目录：", self.remote_edit)
        form.addRow("日志目录：", self._directory_row(self.log_edit, self._browse_log))
        form.addRow("日志保留：", self.retention_spin)
        layout.addLayout(form)
        note = QLabel("项目中的目录覆盖留空时使用以上全局值。服务器临时目录必须是 Linux 绝对路径。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #5d6975; padding: 8px 0;")
        layout.addWidget(note)
        buttons = QDialogButtonBox()
        reset_button = QPushButton("恢复默认路径")
        reset_button.clicked.connect(self._reset_defaults)
        buttons.addButton(reset_button, QDialogButtonBox.ButtonRole.ResetRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _directory_row(edit: QLineEdit, callback: Callable[[], None]) -> QWidget:
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        button = QPushButton("浏览…")
        button.clicked.connect(callback)
        row.addWidget(button)
        return widget

    def _browse_bundle(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "选择本地 bundle 临时目录", self.bundle_edit.text())
        if value:
            self.bundle_edit.setText(value)

    def _browse_log(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "选择日志目录", self.log_edit.text())
        if value:
            self.log_edit.setText(value)

    def _reset_defaults(self) -> None:
        self.bundle_edit.setText(default_bundle_dir())
        self.remote_edit.setText("/tmp/bundlehelper")
        self.log_edit.setText(default_log_dir())
        self.retention_spin.setValue(30)

    def _accept(self) -> None:
        try:
            self.settings().validate()
        except Exception as exc:
            QMessageBox.warning(self, "设置无效", str(exc))
            return
        self.accept()

    def settings(self) -> AppSettings:
        return AppSettings(
            bundle_dir=self.bundle_edit.text().strip(),
            remote_temp_dir=self.remote_edit.text().strip(),
            log_dir=self.log_edit.text().strip(),
            log_retention_days=int(self.retention_spin.value()),
        )


class MainWindow(QMainWindow):
    def __init__(
        self,
        config_store: ConfigStore | None = None,
        credential_store: CredentialStore | None = None,
        state: AppState | None = None,
        sync_factory: Callable[[Path], SyncService] | None = None,
    ):
        super().__init__()
        self.config_store = config_store or ConfigStore()
        self.credential_store = credential_store or WindowsCredentialStore()
        self.state = state if state is not None else self.config_store.load()
        if not self.state.projects:
            self.state.projects.append(ProjectConfig(name="新项目"))
        self._current_project_id = self.state.projects[0].id
        self._sync_factory = sync_factory or (lambda known_hosts: SyncService(known_hosts_path=known_hosts))
        self._active_worker: OperationWorker | None = None
        self._active_on_success: Callable[[object], None] | None = None
        self._active_log_path: str | None = None
        self._active_kind = ""
        self._active_on_success = None
        self._active_secrets: tuple[str, ...] = ()
        self._active_cancel_until: int | None = None
        self._worker_outcome: tuple[str, object] | None = None
        self._loading_form = False
        self._lockable_widgets: list[QWidget] = []
        self.setWindowTitle("BundleHelper - Git Bundle 离线同步")
        self.resize(1240, 860)
        self.setMinimumSize(980, 680)
        self._build_ui()
        self._reload_project_list(self._current_project_id)
        self._load_current_project()
        self._refresh_action_state()
        self._prune_startup_logs()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("BundleHelper")
        title.setStyleSheet("font-size: 21pt; font-weight: 700; color: #254f75;")
        subtitle = QLabel("通过 Git bundle 在本地仓库、离线 Linux 服务器与 GitHub 之间安全同步")
        subtitle.setStyleSheet("color: #66727e;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        self.settings_button = QPushButton("全局设置")
        self.settings_button.clicked.connect(self._open_settings)
        header.addWidget(self.settings_button)
        root.addLayout(header)
        body_splitter = QSplitter()
        body_splitter.setChildrenCollapsible(False)
        root.addWidget(body_splitter, 1)
        left = QWidget()
        left.setMinimumWidth(210)
        left.setMaximumWidth(280)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        project_label = QLabel("项目")
        project_label.setStyleSheet("font-weight: 600;")
        left_layout.addWidget(project_label)
        self.project_list = QListWidget()
        self.project_list.currentRowChanged.connect(self._on_project_row_changed)
        left_layout.addWidget(self.project_list, 1)
        left_buttons = QHBoxLayout()
        self.add_project_button = QPushButton("新建")
        self.delete_project_button = QPushButton("删除")
        self.add_project_button.clicked.connect(self._add_project)
        self.delete_project_button.clicked.connect(self._delete_project)
        left_buttons.addWidget(self.add_project_button)
        left_buttons.addWidget(self.delete_project_button)
        left_layout.addLayout(left_buttons)
        self.project_panel = left
        body_splitter.addWidget(left)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.setChildrenCollapsible(False)
        right_layout.addWidget(right_splitter, 1)
        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        form_widget = QWidget()
        self.form_layout = QVBoxLayout(form_widget)
        self.form_layout.setContentsMargins(0, 0, 8, 0)
        self.form_layout.setSpacing(8)
        self._build_project_form()
        for edit in (
            self.name_edit, self.github_edit, self.local_edit, self.server_repo_edit,
            self.host_edit, self.user_edit, self.key_edit,
            self.bundle_override_edit, self.remote_override_edit, self.log_override_edit,
        ):
            edit.textChanged.connect(self._on_config_edited)
        self.port_spin.valueChanged.connect(self._on_config_edited)
        form_scroll.setWidget(form_widget)
        self.form_panel = form_scroll
        right_splitter.addWidget(form_scroll)
        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 8, 0, 0)
        log_header = QHBoxLayout()
        log_title = QLabel("运行日志（敏感信息已脱敏）")
        log_title.setStyleSheet("font-weight: 600;")
        log_header.addWidget(log_title)
        log_header.addStretch(1)
        self.open_logs_button = QPushButton("打开日志目录")
        self.open_logs_button.clicked.connect(self._open_log_dir)
        log_header.addWidget(self.open_logs_button)
        log_layout.addLayout(log_header)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setPlaceholderText("操作输出将显示在这里。")
        log_layout.addWidget(self.log_view, 1)
        right_splitter.addWidget(log_panel)
        right_splitter.setSizes([640, 190])
        body_splitter.addWidget(right)
        body_splitter.setSizes([230, 990])
        status_row = QHBoxLayout()
        self.save_button = QPushButton("保存配置")
        self.save_button.clicked.connect(self._save_clicked)
        self.temp_button = QPushButton("打开临时目录")
        self.temp_button.clicked.connect(self._open_temp_dir)
        self.clean_temp_button = QPushButton("清理本机临时包")
        self.clean_temp_button.clicked.connect(self._clean_local_temp_dirs)
        status_row.addWidget(self.save_button)
        status_row.addWidget(self.temp_button)
        status_row.addWidget(self.clean_temp_button)
        status_row.addStretch(1)
        self.setup_label = QLabel()
        self.setup_label.setStyleSheet("color: #5d6975;")
        status_row.addWidget(self.setup_label)
        root.addLayout(status_row)
        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet("color: #4b5966;")
        root.addWidget(self.status_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        root.addWidget(self.progress_bar)
        actions = QHBoxLayout()
        self.check_ssh_button = QPushButton("检查 SSH")
        self.check_ssh_button.clicked.connect(self._check_ssh)
        self.full_button = QPushButton("首次全量部署")
        self.full_button.setProperty("primary", True)
        self.full_button.clicked.connect(self._start_full_bootstrap)
        self.delta_button = QPushButton("拉取服务器增量")
        self.delta_button.setProperty("primary", True)
        self.delta_button.clicked.connect(self._start_delta_sync)
        self.push_button = QPushButton("推送全部分支和 Tag 到 GitHub")
        self.push_button.clicked.connect(self._start_push)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setProperty("danger", True)
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel_active)
        actions.addWidget(self.check_ssh_button)
        actions.addWidget(self.full_button)
        actions.addWidget(self.delta_button)
        actions.addWidget(self.push_button)
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)
        root.addLayout(actions)
        self._lockable_widgets = [
            self.project_panel, self.form_panel, self.settings_button, self.save_button,
            self.temp_button, self.clean_temp_button, self.open_logs_button,
            self.check_ssh_button, self.full_button, self.delta_button, self.push_button,
        ]

    def _build_project_form(self) -> None:
        repo_group = QGroupBox("仓库配置")
        repo_form = QFormLayout(repo_group)
        repo_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.name_edit = QLineEdit()
        self.github_edit = QLineEdit()
        self.github_edit.setPlaceholderText("https://github.com/owner/repository")
        self.local_edit = QLineEdit()
        local_row = QWidget()
        local_layout = QHBoxLayout(local_row)
        local_layout.setContentsMargins(0, 0, 0, 0)
        local_layout.addWidget(self.local_edit, 1)
        local_browse = QPushButton("浏览…")
        local_browse.clicked.connect(lambda: self._browse_directory(self.local_edit, "选择本地仓库目录"))
        local_layout.addWidget(local_browse)
        self.server_repo_edit = QLineEdit()
        self.server_repo_edit.setPlaceholderText("/home/user/repositories/project")
        repo_form.addRow("项目名称：", self.name_edit)
        repo_form.addRow("GitHub 地址：", self.github_edit)
        repo_form.addRow("本地仓库目录：", local_row)
        repo_form.addRow("服务器仓库目录：", self.server_repo_edit)
        self.form_layout.addWidget(repo_group)
        ssh_group = QGroupBox("SSH 服务器")
        ssh_form = QFormLayout(ssh_group)
        ssh_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        host_row = QWidget()
        host_layout = QHBoxLayout(host_row)
        host_layout.setContentsMargins(0, 0, 0, 0)
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("server.example.com")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(22)
        self.port_spin.setMaximumWidth(105)
        host_layout.addWidget(self.host_edit, 1)
        host_layout.addWidget(QLabel("端口"))
        host_layout.addWidget(self.port_spin)
        self.user_edit = QLineEdit()
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("留空使用 ssh-agent")
        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.addWidget(self.key_edit, 1)
        key_browse = QPushButton("浏览…")
        key_browse.clicked.connect(lambda: self._browse_file(self.key_edit, "选择 SSH 私钥"))
        key_layout.addWidget(key_browse)
        ssh_form.addRow("主机：", host_row)
        ssh_form.addRow("用户名：", self.user_edit)
        ssh_form.addRow("私钥文件：", key_row)
        self.form_layout.addWidget(ssh_group)
        override_group = QGroupBox("目录覆盖")
        override_form = QFormLayout(override_group)
        override_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.bundle_override_edit = self._override_row(override_form, "本地 bundle 临时目录：", "bundle")
        self.bundle_effective_label = QLabel()
        self.bundle_effective_label.setStyleSheet("color: #71808e;")
        override_form.addRow("", self.bundle_effective_label)
        self.remote_override_edit = self._override_row(override_form, "服务器临时目录：", "remote")
        self.remote_effective_label = QLabel()
        self.remote_effective_label.setStyleSheet("color: #71808e;")
        override_form.addRow("", self.remote_effective_label)
        self.log_override_edit = self._override_row(override_form, "日志目录：", "log")
        self.log_effective_label = QLabel()
        self.log_effective_label.setStyleSheet("color: #71808e;")
        override_form.addRow("", self.log_effective_label)
        override_note = QLabel("留空表示继承全局设置。操作开始后这些字段会锁定，避免执行中切换目录。")
        override_note.setWordWrap(True)
        override_note.setStyleSheet("color: #71808e;")
        override_form.addRow("", override_note)
        self.form_layout.addWidget(override_group)
        token_group = QGroupBox("GitHub Token")
        token_layout = QHBoxLayout(token_group)
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("输入 GitHub Personal Access Token")
        self.save_token_button = QPushButton("保存到凭据管理器")
        self.clear_token_button = QPushButton("清除 Token")
        self.save_token_button.clicked.connect(self._save_token)
        self.clear_token_button.clicked.connect(self._clear_token)
        token_layout.addWidget(self.token_edit, 1)
        token_layout.addWidget(self.save_token_button)
        token_layout.addWidget(self.clear_token_button)
        self.form_layout.addWidget(token_group)
        self.token_status_label = QLabel()
        self.token_status_label.setStyleSheet("color: #71808e;")
        self.form_layout.addWidget(self.token_status_label)
        self.form_layout.addStretch(1)
        for edit in (self.bundle_override_edit, self.remote_override_edit, self.log_override_edit):
            edit.textChanged.connect(self._refresh_effective_paths)

    def _override_row(self, form: QFormLayout, label: str, kind: str) -> QLineEdit:
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit()
        edit.setPlaceholderText("留空则继承全局设置")
        row.addWidget(edit, 1)
        button = QPushButton("浏览…")
        if kind == "remote":
            button.setText("使用全局")
            button.clicked.connect(edit.clear)
        elif kind == "bundle":
            button.clicked.connect(lambda: self._browse_directory(edit, "选择本地 bundle 临时目录"))
        else:
            button.clicked.connect(lambda: self._browse_directory(edit, "选择日志目录"))
        row.addWidget(button)
        form.addRow(label, widget)
        return edit

    def _browse_directory(self, edit: QLineEdit, title: str) -> None:
        value = QFileDialog.getExistingDirectory(self, title, edit.text())
        if value:
            edit.setText(value)

    def _browse_file(self, edit: QLineEdit, title: str) -> None:
        value, _ = QFileDialog.getOpenFileName(self, title, edit.text(), "所有文件 (*)")
        if value:
            edit.setText(value)

    def _current_project(self) -> ProjectConfig:
        for project in self.state.projects:
            if project.id == self._current_project_id:
                return project
        self._current_project_id = self.state.projects[0].id
        return self.state.projects[0]

    def _apply_form_to_current(self) -> None:
        if self._loading_form:
            return
        project = self._current_project()
        project.name = self.name_edit.text().strip() or "未命名项目"
        project.github_url = self.github_edit.text().strip()
        project.local_repo_path = self.local_edit.text().strip()
        project.server_repo_path = self.server_repo_edit.text().strip()
        project.ssh.host = self.host_edit.text().strip()
        project.ssh.port = int(self.port_spin.value())
        project.ssh.user = self.user_edit.text().strip()
        project.ssh.key_path = self.key_edit.text().strip()
        project.bundle_dir_override = self.bundle_override_edit.text().strip() or None
        project.remote_temp_dir_override = self.remote_override_edit.text().strip() or None
        project.log_dir_override = self.log_override_edit.text().strip() or None
        current_row = self.project_list.currentRow()
        if 0 <= current_row < self.project_list.count():
            item = self.project_list.item(current_row)
            item.setText(project.name)
            item.setData(0x0100, project.id)

    def _load_current_project(self) -> None:
        project = self._current_project()
        self._loading_form = True
        try:
            self.name_edit.setText(project.name)
            self.github_edit.setText(project.github_url)
            self.local_edit.setText(project.local_repo_path)
            self.server_repo_edit.setText(project.server_repo_path)
            self.host_edit.setText(project.ssh.host)
            self.port_spin.setValue(project.ssh.port)
            self.user_edit.setText(project.ssh.user)
            self.key_edit.setText(project.ssh.key_path)
            self.bundle_override_edit.setText(project.bundle_dir_override or "")
            self.remote_override_edit.setText(project.remote_temp_dir_override or "")
            self.log_override_edit.setText(project.log_dir_override or "")
            self.bundle_override_edit.setPlaceholderText("留空继承：" + self.state.settings.bundle_dir)
            self.remote_override_edit.setPlaceholderText("留空继承：" + self.state.settings.remote_temp_dir)
            self.log_override_edit.setPlaceholderText("留空继承：" + self.state.settings.log_dir)
        finally:
            self._loading_form = False
        self._refresh_effective_paths()
        self._refresh_setup_status()
        self._refresh_token_status()
        self._refresh_action_state()

    def _reload_project_list(self, select_id: str | None = None) -> None:
        self._loading_form = True
        try:
            with QSignalBlocker(self.project_list):
                self.project_list.clear()
                selected_row = 0
                for index, project in enumerate(self.state.projects):
                    item = QListWidgetItem(project.name)
                    item.setData(0x0100, project.id)
                    self.project_list.addItem(item)
                    if project.id == select_id:
                        selected_row = index
                self.project_list.setCurrentRow(selected_row)
        finally:
            self._loading_form = False

    def _on_project_row_changed(self, row: int) -> None:
        if self._loading_form or row < 0 or row >= len(self.state.projects):
            return
        self._apply_form_to_current()
        self._persist_state(show_error=False)
        self._current_project_id = self.state.projects[row].id
        self._load_current_project()

    def _add_project(self) -> None:
        self._apply_form_to_current()
        index = len(self.state.projects) + 1
        project = ProjectConfig(name=f"项目 {index}")
        self.state.projects.append(project)
        self._persist_state(show_error=True)
        self._reload_project_list(project.id)
        self._current_project_id = project.id
        self._load_current_project()

    def _delete_project(self) -> None:
        if len(self.state.projects) <= 1:
            QMessageBox.information(self, "无法删除", "至少需要保留一个项目。")
            return
        project = self._current_project()
        answer = QMessageBox.question(
            self, "删除项目",
            f"确定删除项目“{project.name}”吗？\n\n不会删除本地仓库、服务器仓库或已有日志。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        index = self.state.projects.index(project)
        self.state.projects.pop(index)
        next_index = min(index, len(self.state.projects) - 1)
        self._current_project_id = self.state.projects[next_index].id
        self._persist_state(show_error=True)
        self._reload_project_list(self._current_project_id)
        self._load_current_project()

    def _persist_state(self, *, show_error: bool = False) -> bool:
        self._apply_form_to_current()
        try:
            self.config_store.save(self.state)
            return True
        except Exception as exc:
            if show_error:
                QMessageBox.critical(self, "保存失败", f"无法保存配置：\n{exc}")
            self._append_log(f"保存配置失败：{exc}")
            return False

    def _on_config_edited(self, *_args: object) -> None:
        if self._loading_form:
            return
        self._refresh_setup_status()
        self._refresh_action_state()

    def _save_clicked(self) -> None:
        self._apply_form_to_current()
        try:
            self.state.settings.validate()
        except Exception as exc:
            QMessageBox.warning(self, "配置无效", str(exc))
            return
        if not self._persist_state(show_error=True):
            return
        self._refresh_effective_paths()
        self._refresh_setup_status()
        self._refresh_action_state()
        self._append_log(f"配置已保存：{self.config_store.path}")
        self.status_label.setText("配置已保存")

    def _refresh_effective_paths(self) -> None:
        settings = self.state.settings
        values = (
            (self.bundle_override_edit, self.bundle_effective_label, settings.bundle_dir, "本地 bundle 临时目录"),
            (self.remote_override_edit, self.remote_effective_label, settings.remote_temp_dir, "服务器临时目录"),
            (self.log_override_edit, self.log_effective_label, settings.log_dir, "日志目录"),
        )
        for edit, label, inherited, title in values:
            override = edit.text().strip()
            if override:
                label.setText(f"{title}：使用项目覆盖 {override}")
            else:
                label.setText(f"{title}：继承全局 {inherited}")

    def _refresh_setup_status(self) -> None:
        project = self._current_project()
        if project.setup_complete and project.baseline:
            stamp = project.baseline.captured_at.replace("T", " ")[:19]
            self.setup_label.setText(f"同步基线：{stamp} UTC · {len(project.baseline.heads)} 个分支 · {len(project.baseline.tags)} 个 Tag")
        else:
            self.setup_label.setText("同步基线：尚未完成首次全量部署")

    def _refresh_action_state(self) -> None:
        active = self._active_worker is not None
        for widget in self._lockable_widgets:
            widget.setEnabled(not active)
        if active:
            for button in (self.check_ssh_button, self.full_button, self.delta_button, self.push_button):
                button.setEnabled(False)
            return

        project = self._current_project()
        ssh_ready = bool(project.ssh.host.strip() and project.ssh.user.strip())
        repo_ready = bool(
            project.github_url.strip() and project.local_repo_path.strip()
            and project.server_repo_path.strip()
        )
        self.check_ssh_button.setEnabled(ssh_ready)
        self.full_button.setEnabled(repo_ready and ssh_ready)
        self.delta_button.setEnabled(repo_ready and ssh_ready and project.setup_complete and project.baseline is not None)
        self.push_button.setEnabled(bool(project.github_url.strip() and project.local_repo_path.strip()))
        self.temp_button.setEnabled(bool(project.resolved_bundle_dir(self.state.settings).strip()))
        self.open_logs_button.setEnabled(bool(project.resolved_log_dir(self.state.settings).strip()))

    def _refresh_token_status(self) -> None:
        try:
            token = self.credential_store.get_token()
        except Exception as exc:
            self.token_status_label.setText(f"Token 状态：无法读取 Windows 凭据管理器（{exc}）")
            return
        if token:
            self.token_status_label.setText("Token 状态：已保存在 Windows 凭据管理器（全局共用）；不会写入配置文件或日志。")
        else:
            self.token_status_label.setText("Token 状态：未保存（全局共用）。私有仓库克隆及 GitHub Push 需要 Token。")

    def _save_token(self) -> None:
        token = self.token_edit.text().strip()
        if not token:
            QMessageBox.warning(self, "Token 为空", "请输入 GitHub Personal Access Token。")
            return
        try:
            self.credential_store.set_token(token)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", f"无法写入 Windows 凭据管理器：\n{exc}")
            return
        self.token_edit.clear()
        self._refresh_token_status()
        self._append_log("GitHub Token 已保存到 Windows 凭据管理器。")
        self.status_label.setText("Token 已保存")

    def _clear_token(self) -> None:
        answer = QMessageBox.question(
            self,
            "清除 Token",
            "确定从 Windows 凭据管理器删除全局 GitHub Token 吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.credential_store.set_token(None)
        except Exception as exc:
            QMessageBox.critical(self, "清除失败", f"无法删除 Token：\n{exc}")
            return
        self._refresh_token_status()
        self._append_log("GitHub Token 已从 Windows 凭据管理器清除。")
        self.status_label.setText("Token 已清除")

    def _open_settings(self) -> None:
        if self._active_worker is not None:
            return
        self._apply_form_to_current()
        dialog = SettingsDialog(copy.deepcopy(self.state.settings), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.state.settings = dialog.settings()
        self._persist_state(show_error=True)
        self.bundle_override_edit.setPlaceholderText("留空继承：" + self.state.settings.bundle_dir)
        self.remote_override_edit.setPlaceholderText("留空继承：" + self.state.settings.remote_temp_dir)
        self.log_override_edit.setPlaceholderText("留空继承：" + self.state.settings.log_dir)
        self._refresh_effective_paths()
        self._refresh_action_state()
        self._append_log("全局目录设置已更新。")

    @staticmethod
    def _open_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve()))):
            raise BundleHelperError(f"无法打开目录：{path}")

    def _open_log_dir(self) -> None:
        project = self._current_project()
        directory = Path(project.resolved_log_dir(self.state.settings)).expanduser()
        try:
            self._open_directory(directory)
        except Exception as exc:
            QMessageBox.warning(self, "无法打开日志目录", str(exc))

    def _open_temp_dir(self) -> None:
        project = self._current_project()
        directory = Path(project.resolved_bundle_dir(self.state.settings)).expanduser() / project.id
        try:
            self._open_directory(directory)
        except Exception as exc:
            QMessageBox.warning(self, "无法打开临时目录", str(exc))

    def _clean_local_temp_dirs(self) -> None:
        if self._active_worker is not None:
            return
        project = self._current_project()
        base = Path(project.resolved_bundle_dir(self.state.settings)).expanduser().resolve()
        project_root = base / project.id
        if not project_root.is_dir():
            QMessageBox.information(self, "没有临时文件", "当前项目没有可清理的本机临时目录。")
            return
        candidates = [
            item for item in project_root.iterdir()
            if item.is_dir() and re.fullmatch(r"[0-9a-f]{12}", item.name)
        ]
        if not candidates:
            QMessageBox.information(self, "没有临时文件", "当前项目没有符合 BundleHelper 命名规则的临时包目录。")
            return
        answer = QMessageBox.question(
            self,
            "清理本机临时包",
            f"将删除当前项目下 {len(candidates)} 个操作临时目录。\n\n"
            "只删除项目临时根目录内名称符合 12 位操作 ID 的目录，不会删除配置父目录或其他文件。\n"
            "失败操作的诊断包删除后无法恢复，是否继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = 0
        failures: list[str] = []
        for item in candidates:
            try:
                if safe_remove_dir(project_root, item, item.name):
                    removed += 1
            except Exception as exc:
                failures.append(f"{item.name}: {exc}")
        self._append_log(f"手动清理本机临时目录：已删除 {removed} 个，失败 {len(failures)} 个。")
        for failure in failures:
            self._append_log("清理失败：" + failure)
        if failures:
            QMessageBox.warning(self, "部分清理失败", "\n".join(failures[:10]))
        else:
            self.status_label.setText(f"已清理 {removed} 个本机临时目录")

    def _prune_startup_logs(self) -> None:
        directories = {self.state.settings.log_dir}
        directories.update(
            project.log_dir_override for project in self.state.projects
            if project.log_dir_override and project.log_dir_override.strip()
        )
        removed = 0
        for raw in directories:
            try:
                removed += prune_logs(
                    Path(raw).expanduser(),
                    self.state.settings.log_retention_days,
                )
            except Exception:
                continue
        if removed:
            self._append_log(f"启动清理：已删除 {removed} 个超过保留期的 BundleHelper 日志。")
    def _begin_sync_operation(
        self,
        operation: str,
        project: ProjectConfig,
        settings: AppSettings,
        runner: Callable[[OperationLogger, Callable[[int, str], None], Callable[[str], None], threading.Event], object],
        on_success: Callable[[object], str | None],
        *,
        token: str | None = None,
        cancel_until: int | None = None,
    ) -> None:
        if self._active_worker is not None:
            return
        operation_id = uuid.uuid4().hex[:12]
        holder: dict[str, OperationWorker] = {}

        def emit_output(line: str) -> None:
            worker = holder.get("worker")
            if worker is not None:
                worker.output.emit(line)

        try:
            logger = OperationLogger(
                project.resolved_log_dir(settings),
                project.name,
                operation,
                operation_id,
                on_output=emit_output,
                secrets=(token,) if token else (),
            )
        except Exception as exc:
            QMessageBox.critical(self, "无法创建日志", f"无法创建操作日志：\n{exc}")
            return

        def wrapped(
            progress: Callable[[int, str], None],
            output: Callable[[str], None],
            cancel_event: threading.Event,
        ) -> object:
            try:
                return runner(logger, progress, output, cancel_event)
            except Exception:
                logger.error(traceback.format_exc())
                raise

        worker = OperationWorker(wrapped, self)
        holder["worker"] = worker
        self._active_worker = worker
        self._active_on_success = on_success
        self._active_log_path = str(logger.path)
        self._active_kind = operation
        self._active_secrets = (token,) if token else ()
        self._active_cancel_until = cancel_until
        self._worker_outcome = None
        worker.progress.connect(self._on_progress)
        worker.output.connect(self._append_log)
        worker.succeeded.connect(self._on_worker_succeeded)
        worker.failed.connect(self._on_worker_failed)
        worker.finished.connect(self._on_worker_finished)
        self.progress_bar.setValue(0)
        self.status_label.setText(f"正在执行：{operation}")
        self.cancel_button.setVisible(cancel_until is not None)
        self.cancel_button.setEnabled(cancel_until is not None)
        self.cancel_button.setToolTip("仅在 bundle 传输和本地引用修改开始前可取消")
        self._refresh_action_state()
        self._append_log(f"开始 {operation}；日志：{logger.path}")
        worker.start()

    def _run_worker(self, *args: object, **kwargs: object) -> None:
        self._begin_sync_operation(*args, **kwargs)  # type: ignore[arg-type]

    def _check_ssh(self) -> None:
        if self._active_worker is not None:
            return
        self._apply_form_to_current()
        project = copy.deepcopy(self._current_project())
        settings = copy.deepcopy(self.state.settings)
        try:
            project.ssh.validate()
            settings.validate()
        except Exception as exc:
            QMessageBox.warning(self, "SSH 配置无效", str(exc))
            return
        self._persist_state(show_error=False)

        def runner(
            logger: OperationLogger,
            progress: Callable[[int, str], None],
            _output: Callable[[str], None],
            _cancel_event: threading.Event,
        ) -> object:
            progress(10, "检查 OpenSSH 工具")
            ssh = SshService(project.ssh, self.config_store.known_hosts_path)
            ssh.check_available()
            progress(38, "检查已知主机")
            if ssh.is_host_known():
                progress(55, "验证服务器 Git 环境")
                ssh.check_server(logger=logger)
                progress(100, "SSH 检查通过")
                return {"known": True}
            progress(55, "扫描服务器主机密钥")
            options = ssh.scan_host_keys()
            progress(100, "等待确认主机指纹")
            return {"known": False, "options": options}

        def on_success(result: object) -> str | None:
            data = result if isinstance(result, dict) else {}
            if data.get("known"):
                return "SSH 连接检查通过，服务器 Git 和 sha256sum 可用。"
            options = data.get("options") or []
            self._confirm_ssh_keys(project, settings, options)
            return None

        self._begin_sync_operation("检查 SSH", project, settings, runner, on_success)

    def _confirm_ssh_keys(
        self,
        project: ProjectConfig,
        settings: AppSettings,
        options: object,
    ) -> None:
        if not isinstance(options, list) or not options:
            QMessageBox.warning(self, "主机密钥不可用", "没有收到可确认的服务器主机密钥。")
            return
        lines = [
            f"主机：{project.ssh.user}@{project.ssh.host}:{project.ssh.port}",
            "",
            "将保存以下 SSH 主机密钥：",
        ]
        for option in options:
            if isinstance(option, HostKeyOption):
                lines.append(f"  · {option.key_type}  {option.fingerprint}")
        lines.extend([
            "",
            "请与服务器管理员提供的 SHA-256 指纹核对。确认后，本工具只在专属 known_hosts 文件中信任这些密钥，"
            "后续连接将严格校验。",
        ])
        answer = QMessageBox.question(
            self,
            "确认 SSH 主机指纹",
            "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._append_log("用户取消了 SSH 主机密钥确认。")
            self.status_label.setText("SSH 主机密钥未保存")
            return

        def runner(
            logger: OperationLogger,
            progress: Callable[[int, str], None],
            _output: Callable[[str], None],
            _cancel_event: threading.Event,
        ) -> object:
            progress(15, "保存主机密钥")
            ssh = SshService(project.ssh, self.config_store.known_hosts_path)
            ssh.clear_host_key()
            ssh.trust_host_keys(options)
            progress(55, "验证 SSH 与服务器环境")
            ssh.check_server(logger=logger)
            progress(100, "SSH 检查通过")
            return True

        self._begin_sync_operation(
            "确认 SSH 主机密钥",
            project,
            settings,
            runner,
            lambda _result: "SSH 主机密钥已保存，连接检查通过。",
        )

    def _start_full_bootstrap(self) -> None:
        if self._active_worker is not None:
            return
        self._apply_form_to_current()
        project = copy.deepcopy(self._current_project())
        settings = copy.deepcopy(self.state.settings)
        try:
            settings.validate()
            project.validate()
        except Exception as exc:
            QMessageBox.warning(self, "配置无效", str(exc))
            return
        self._persist_state(show_error=False)
        try:
            token = self.credential_store.get_token()
        except Exception as exc:
            QMessageBox.critical(self, "无法读取 Token", f"无法访问 Windows 凭据管理器：\n{exc}")
            return
        answer = QMessageBox.question(
            self,
            "首次全量部署",
            "即将执行以下操作：\n\n"
            f"GitHub：{project.github_url}\n"
            f"本地仓库：{project.local_repo_path}\n"
            f"服务器：{project.ssh.user}@{project.ssh.host}:{project.ssh.port}\n"
            f"服务器仓库：{project.server_repo_path}\n\n"
            "服务器仓库将先从 bundle 克隆（不会从服务器访问 GitHub）。"
            "如果服务器目标目录已存在且引用不同，工具会在修改前停止。是否继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def runner(
            logger: OperationLogger,
            progress: Callable[[int, str], None],
            _output: Callable[[str], None],
            cancel_event: threading.Event,
        ) -> object:
            service = self._sync_factory(self.config_store.known_hosts_path)
            return service.bootstrap_full(
                project,
                settings,
                token=token,
                logger=logger,
                progress=progress,
                cancel_event=cancel_event,
            )

        def on_success(result: object) -> str:
            return self._record_manifest(project.id, result, "首次全量部署完成")

        self._begin_sync_operation(
            "首次全量部署",
            project,
            settings,
            runner,
            on_success,
            token=token,
            cancel_until=30,
        )

    def _start_delta_sync(self) -> None:
        if self._active_worker is not None:
            return
        self._apply_form_to_current()
        project = copy.deepcopy(self._current_project())
        settings = copy.deepcopy(self.state.settings)
        try:
            settings.validate()
            project.validate()
        except Exception as exc:
            QMessageBox.warning(self, "配置无效", str(exc))
            return
        if not project.setup_complete or project.baseline is None:
            QMessageBox.information(self, "尚未部署", "请先完成首次全量部署。")
            return
        self._persist_state(show_error=False)

        def runner(
            logger: OperationLogger,
            progress: Callable[[int, str], None],
            _output: Callable[[str], None],
            cancel_event: threading.Event,
        ) -> object:
            service = self._sync_factory(self.config_store.known_hosts_path)
            return service.sync_server_delta(
                project,
                settings,
                logger=logger,
                progress=progress,
                cancel_event=cancel_event,
            )

        def on_success(result: object) -> str:
            return self._record_manifest(project.id, result, "服务器增量同步完成")

        self._begin_sync_operation(
            "拉取服务器增量",
            project,
            settings,
            runner,
            on_success,
            cancel_until=45,
        )

    def _start_push(self) -> None:
        if self._active_worker is not None:
            return
        self._apply_form_to_current()
        project = copy.deepcopy(self._current_project())
        settings = copy.deepcopy(self.state.settings)
        try:
            settings.validate()
            project.validate()
            token = self.credential_store.get_token()
        except Exception as exc:
            QMessageBox.warning(self, "配置无效", str(exc))
            return
        if not token:
            QMessageBox.warning(
                self,
                "需要 GitHub Token",
                "Push 前请先在“GitHub Token”区域保存 Personal Access Token。"
                "Token 只保存在 Windows 凭据管理器中。",
            )
            return
        answer = QMessageBox.question(
            self,
            "推送全部分支和 Tag",
            f"即将向以下仓库执行 git push --all 和 git push --tags：\n\n{project.github_url}\n\n"
            "操作不会使用 force，也不会因 Push 失败修改服务器同步基线。是否继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._persist_state(show_error=False)

        def runner(
            logger: OperationLogger,
            progress: Callable[[int, str], None],
            _output: Callable[[str], None],
            cancel_event: threading.Event,
        ) -> object:
            service = self._sync_factory(self.config_store.known_hosts_path)
            return service.push_github(
                project,
                settings,
                token=token,
                logger=logger,
                progress=progress,
                cancel_event=cancel_event,
            )

        def on_success(_result: object) -> str:
            self._append_log("全部分支和 Tag 已推送到 GitHub。")
            return "全部分支和 Tag 已推送到 GitHub。"

        self._begin_sync_operation(
            "推送到 GitHub",
            project,
            settings,
            runner,
            on_success,
            token=token,
            cancel_until=None,
        )

    def _record_manifest(self, project_id: str, result: object, message: str) -> str:
        if not isinstance(result, RefManifest):
            raise BundleHelperError("同步操作未返回有效的引用基线。")
        for project in self.state.projects:
            if project.id == project_id:
                project.baseline = copy.deepcopy(result)
                project.setup_complete = True
                break
        if not self._persist_state(show_error=True):
            raise BundleHelperError("同步已完成，但无法保存新的引用基线；请检查配置文件权限。")
        self._refresh_setup_status()
        self._refresh_action_state()
        self._append_log(
            f"{message}：{len(result.heads)} 个分支，{len(result.tags)} 个 Tag，"
            f"HEAD {result.head_branch}@{result.head_oid[:12]}。"
        )
        return message + "，同步基线已更新。"
    @Slot(int, str)
    def _on_progress(self, value: int, message: str) -> None:
        self.progress_bar.setValue(max(0, min(100, value)))
        self.status_label.setText(message)
        if self._active_cancel_until is not None:
            self.cancel_button.setEnabled(value < self._active_cancel_until)
            if value >= self._active_cancel_until:
                self.cancel_button.setToolTip("操作已进入传输或修改阶段，不能再取消")

    @Slot(str)
    def _append_log(self, line: str) -> None:
        text = sanitize_text(str(line).rstrip("\r\n"), self._active_secrets)
        if text:
            self.log_view.appendPlainText(text)

    @Slot(object)
    def _on_worker_succeeded(self, result: object) -> None:
        self._worker_outcome = ("success", result)

    @Slot(str, str)
    def _on_worker_failed(self, title: str, detail: str) -> None:
        self._worker_outcome = ("failed", (title, detail))

    @Slot()
    def _on_worker_finished(self) -> None:
        worker = self._active_worker
        outcome = self._worker_outcome
        callback = self._active_on_success
        log_path = self._active_log_path
        if worker is not None:
            worker.deleteLater()
        self._active_worker = None
        self._active_on_success = None
        self._active_log_path = None
        self._active_kind = ""
        self._active_cancel_until = None
        self._worker_outcome = None
        self.cancel_button.setVisible(False)
        self.cancel_button.setEnabled(False)
        self._refresh_action_state()

        if outcome is None:
            self._append_log("后台线程已结束，但没有返回操作结果。")
            self.status_label.setText("操作状态未知")
            return

        status, payload = outcome
        if status == "success":
            message = ""
            try:
                if callback is not None:
                    message = callback(payload) or ""
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                self._append_log("操作完成后的配置更新失败：" + detail)
                self.status_label.setText("操作完成，但配置保存失败")
                QMessageBox.critical(
                    self,
                    "配置保存失败",
                    f"后台操作已经完成，但无法保存同步基线：\n\n{detail}\n\n日志：{log_path or '未创建'}",
                )
                return
            if self._active_worker is not None:
                return
            self.progress_bar.setValue(100)
            self.status_label.setText(message or "操作完成")
            self._append_log(f"操作完成。日志：{log_path or '未创建'}")
            if message:
                QMessageBox.information(self, "操作完成", message)
            return

        title, detail = payload  # type: ignore[misc]
        clean_detail = sanitize_text(str(detail), self._active_secrets).strip()
        self.status_label.setText(title)
        self._append_log(f"操作失败：{title}")
        if log_path:
            self._append_log(f"保留诊断文件。日志：{log_path}")
        suffix = f"\n\n详细诊断日志：\n{log_path}" if log_path else ""
        QMessageBox.critical(self, "操作失败", f"{title}\n\n{clean_detail[-3000:]}{suffix}")

    def _cancel_active(self) -> None:
        if self._active_worker is None:
            return
        if self._active_cancel_until is None:
            QMessageBox.information(self, "无法取消", "当前阶段不支持取消。")
            return
        if self.progress_bar.value() >= self._active_cancel_until:
            QMessageBox.information(self, "无法取消", "操作已进入传输或修改阶段，为保护仓库一致性不能再取消。")
            return
        self._active_worker.request_cancel()
        self.cancel_button.setEnabled(False)
        self.status_label.setText("正在取消…")
        self._append_log("已请求取消当前操作。")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._active_worker is not None:
            if (
                self._active_cancel_until is not None
                and self.progress_bar.value() < self._active_cancel_until
            ):
                answer = QMessageBox.question(
                    self,
                    "操作进行中",
                    "当前操作仍可取消。是否请求取消并退出？",
                )
                if answer == QMessageBox.StandardButton.Yes:
                    self._active_worker.request_cancel()
                    self._append_log("关闭窗口前已请求取消当前操作。")
                event.ignore()
                return
            QMessageBox.warning(
                self,
                "无法退出",
                "操作已进入传输或修改阶段。请等待操作完成后关闭窗口。",
            )
            event.ignore()
            return
        self._apply_form_to_current()
        self._persist_state(show_error=False)
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("BundleHelper")
    app.setApplicationDisplayName("BundleHelper")
    app.setOrganizationName("BundleHelper")
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLE)
    try:
        window = MainWindow()
    except Exception as exc:
        QMessageBox.critical(None, "BundleHelper 启动失败", f"无法加载应用配置：\n{exc}")
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())