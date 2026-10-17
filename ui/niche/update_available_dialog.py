from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSize, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from ui.core.brand import BrandColors
from ui.core.icons import IconType, IconUtils
from .update_method_dialog import UpdateMethodAvailability, UpdateMethodDialog, default_update_method_availability
from .update_git_instructions_dialog import UpdateGitInstructionsDialog
from .update_download_dialog import UpdateDownloadDialog


def _format_version(version: Optional[str]) -> str:
    value = (version or "").strip()
    if not value:
        return "unknown"
    if value.lower().startswith("v"):
        value = value[1:].strip() or "unknown"
    if value.lower() == "unknown":
        return "unknown"
    return f"v{value}"


@dataclass(frozen=True)
class UpdateAvailableInfo:
    local_version: str
    remote_version: str
    remote_auto_updateable: Optional[bool] = None
    remote_severity: Optional[int] = None
    remote_summary: Optional[str] = None
    local_update_archive: Optional[str] = None

    @property
    def is_local_update_debug(self) -> bool:
        return bool(str(self.local_update_archive or "").strip())

    @property
    def release_notes_url(self) -> str:
        if self.is_local_update_debug:
            return ""
        remote = (self.remote_version or "").strip()
        if remote.lower().startswith("v"):
            remote = remote[1:].strip()
        if not remote or remote.lower() == "unknown":
            return "https://github.com/Lemomation/irn/releases"
        return f"https://github.com/Lemomation/irn/releases/tag/v{remote}"


class UpdateAvailableDialog(QDialog):
    def __init__(self, info: UpdateAvailableInfo, parent=None):
        super().__init__(parent)
        self._info = info
        self._install_button: QPushButton | None = None

        self.setWindowTitle("Debug Update Available" if info.is_local_update_debug else "Update Available")
        self.setModal(True)
        self.setFixedWidth(440)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)

        card = QFrame()
        card.setObjectName("updateCard")
        card.setStyleSheet(
            f"""
            QFrame#updateCard {{
                background-color: {BrandColors.WINDOW_BG};
                border: none;
            }}
            """
        )
        root_layout.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 22, 22, 18)
        layout.setSpacing(12)

        title = QLabel()
        title.setAlignment(Qt.AlignCenter)
        title.setTextFormat(Qt.RichText)
        title.setStyleSheet(
            f"""
            font-size: {BrandColors.FONT_SIZE_TITLE};
            font-weight: 800;
            background-color: transparent;
            """
        )
        title.setText(self._build_title_html())
        layout.addWidget(title)

        versions = self._build_version_row()
        layout.addWidget(versions, 0, Qt.AlignHCenter)

        if info.is_local_update_debug:
            archive_name = Path(str(info.local_update_archive or "")).name or "the selected ZIP"
            desc_text = (
                f"Debug update mode will stage {archive_name} and run the Auto-Update "
                "installer flow."
            )
        else:
            desc_text = "An update is available. You can install it or skip for now."
            if info.remote_auto_updateable is False:
                desc_text = (
                    "An update is available, but Auto-Update is disabled for this release. "
                    "Use Git (source runs) or download manually from the release page."
                )
            summary = str(info.remote_summary or "").strip()
            if summary:
                desc_text = f"{desc_text}\n\nThis update brings: {summary}"

        desc = QLabel(desc_text)
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignCenter)
        desc.setStyleSheet(
            f"""
            font-size: {BrandColors.FONT_SIZE_REGULAR};
            color: {BrandColors.TEXT_SECONDARY};
            background-color: transparent;
            padding: 4px 4px;
            """
        )
        layout.addWidget(desc)

        buttons = self._build_button_row()
        layout.addWidget(buttons)

        if not info.is_local_update_debug:
            view_release_notes = QPushButton("View Release Notes")
            view_release_notes.setCursor(Qt.PointingHandCursor)
            view_release_notes.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: transparent;
                    color: {BrandColors.TEXT_PRIMARY};
                    border: 1px solid {BrandColors.INPUT_BORDER};
                    padding: 10px 14px;
                    border-radius: 8px;
                    font-size: {BrandColors.FONT_SIZE_REGULAR};
                    font-weight: 700;
                }}
                QPushButton:hover {{
                    background-color: {BrandColors.ITEM_HOVER};
                    border: 1px solid {BrandColors.ACCENT};
                }}
                QPushButton:pressed {{
                    background-color: {BrandColors.SIDEBAR_BG};
                    border: 1px solid {BrandColors.INPUT_BORDER};
                }}
                """
            )
            view_release_notes.clicked.connect(self._open_release_notes)
            layout.addWidget(view_release_notes)

    def _build_version_row(self) -> QFrame:
        row = QFrame()
        row.setStyleSheet("background-color: transparent;")

        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        local_label = QLabel(_format_version(self._info.local_version))
        local_label.setStyleSheet(
            f"""
            font-size: {BrandColors.FONT_SIZE_LARGE};
            font-weight: 700;
            color: {BrandColors.TEXT_SECONDARY};
            background-color: transparent;
            """
        )
        layout.addWidget(local_label, 0, Qt.AlignVCenter)

        arrow = QLabel()
        arrow.setStyleSheet("background-color: transparent;")
        arrow_pixmap = IconUtils.get_pixmap(
            "chevron-right.svg",
            color=BrandColors.TEXT_SECONDARY,
            size=18,
            dpr=self.devicePixelRatioF(),
        )
        if not arrow_pixmap.isNull():
            arrow.setPixmap(arrow_pixmap)
        layout.addWidget(arrow, 0, Qt.AlignVCenter)

        remote_text = "Local ZIP" if self._info.is_local_update_debug else _format_version(self._info.remote_version)
        remote_label = QLabel(remote_text)
        remote_label.setStyleSheet(
            f"""
            font-size: {BrandColors.FONT_SIZE_LARGE};
            font-weight: 800;
            color: {BrandColors.TEXT_PRIMARY};
            background-color: transparent;
            """
        )
        layout.addWidget(remote_label, 0, Qt.AlignVCenter)

        return row

    def _build_title_html(self) -> str:
        if self._info.is_local_update_debug:
            return f'<span style="color: {BrandColors.TEXT_PRIMARY};">Debug Update Available</span>'

        label, label_color = self._severity_label_and_color()
        if label and label_color:
            return (
                f'<span style="color: {label_color};">{label}</span> '
                f'<span style="color: {BrandColors.TEXT_PRIMARY};">Update Available!</span>'
            )
        return f'<span style="color: {BrandColors.TEXT_PRIMARY};">Update Available!</span>'

    def _build_button_row(self) -> QFrame:
        row = QFrame()
        row.setStyleSheet("background-color: transparent;")

        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(10)

        not_yet = QPushButton("Not Yet")
        not_yet.setCursor(Qt.PointingHandCursor)
        not_yet.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {BrandColors.SIDEBAR_BG};
                color: {BrandColors.TEXT_PRIMARY};
                border: none;
                padding: 10px 14px;
                border-radius: 8px;
                font-size: {BrandColors.FONT_SIZE_REGULAR};
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {BrandColors.ITEM_HOVER};
            }}
            """
        )
        IconUtils.apply_icon(not_yet, IconType.CANCEL, BrandColors.TEXT_PRIMARY, size=14)
        not_yet.setIconSize(QSize(14, 14))
        not_yet.clicked.connect(self.reject)
        layout.addWidget(not_yet, 1)

        install = QPushButton("Install")
        install.setCursor(Qt.PointingHandCursor)
        install.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {BrandColors.ACCENT};
                color: {BrandColors.TEXT_PRIMARY};
                border: none;
                padding: 10px 14px;
                border-radius: 8px;
                font-size: {BrandColors.FONT_SIZE_REGULAR};
                font-weight: 800;
            }}
            QPushButton:hover {{
                background-color: #4a80e0;
            }}
            QPushButton:pressed {{
                background-color: #3c6ac3;
            }}
        """
        )
        IconUtils.apply_icon(install, IconType.BACKUP, BrandColors.TEXT_PRIMARY, size=16)
        install.setIconSize(QSize(16, 16))
        layout.addWidget(install, 1)
        install.clicked.connect(self._on_install_clicked)
        self._install_button = install

        return row

    def _severity_label_and_color(self) -> tuple[Optional[str], Optional[str]]:
        mapping = {
            1: ("Optional", BrandColors.ACCENT),
            3: ("Important", "#8854f0"),
            4: ("Critical", BrandColors.DANGER),
        }
        sev = self._info.remote_severity
        if sev is None:
            return None, None
        try:
            sev_int = int(sev)
        except Exception:
            return None, None
        return mapping.get(sev_int, (None, None))

    def _on_install_clicked(self) -> None:
        if self._info.is_local_update_debug:
            archive_path = str(self._info.local_update_archive or "").strip()
            if not archive_path:
                QMessageBox.warning(self, "Auto-Update", "No local update archive was provided.")
                return
            UpdateDownloadDialog(
                remote_version=self._info.remote_version,
                local_archive_path=archive_path,
                parent=self,
            ).exec()
            return

        availability = default_update_method_availability()
        if self._info.remote_auto_updateable is False:
            availability = UpdateMethodAvailability(
                git_enabled=availability.git_enabled,
                auto_enabled=False,
                git_reason=availability.git_reason,
                auto_reason="Auto-Update is disabled for this release.",
            )

        if not availability.auto_enabled and not availability.git_enabled:
            self._open_release_notes()
            return

        dialog = UpdateMethodDialog(availability=availability, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return

        method = (dialog.selected_method or "").strip().lower()
        if method == "git":
            UpdateGitInstructionsDialog(parent=self).exec()
            return

        if method == "auto":
            UpdateDownloadDialog(remote_version=self._info.remote_version, parent=self).exec()
            return

    def _open_release_notes(self) -> None:
        QDesktopServices.openUrl(QUrl(self._info.release_notes_url))

    # Icon rendering is handled centrally by IconUtils
