from __future__ import annotations

from dataclasses import dataclass
import threading

import requests
from PySide6.QtCore import Qt, Signal, Slot, QUrl, QSize
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from ui.core.brand import BrandColors
from ui.core.icons import IconType, IconUtils


REMOTE_LATEST_SURVEY_URL = (
    "https://raw.githubusercontent.com/Lemomation/irn/refs/heads/v2-rewrite/latestsurvey.txt"
)
DISCORD_INVITE_URL = "https://discord.gg/4Gvjk2RdsK"
_POST_UPDATE_ACTIONS = {"discord", "survey", "none"}


def _format_version(version: str) -> str:
    value = (version or "").strip()
    if not value:
        return "unknown"
    if value.lower().startswith("v"):
        value = value[1:].strip() or "unknown"
    if value.lower() == "unknown":
        return "unknown"
    return f"v{value}"


@dataclass(frozen=True)
class UpdateInstalledInfo:
    version: str
    release_notes_url: str
    post_update: str = "survey"
    post_update_function_ref: str = "none"
    summary: str = ""


class UpdateInstalledDialog(QDialog):
    _survey_link_ready = Signal(object, object)  # url, error_text

    def __init__(self, info: UpdateInstalledInfo, parent=None):
        super().__init__(parent)
        self._info = info
        self._post_update_button: QPushButton | None = None
        self._post_update_button_original_text: str = ""
        self._survey_link_ready.connect(self._on_survey_link_ready, Qt.QueuedConnection)

        self.setWindowTitle("Update Installed")
        self.setModal(True)
        self.setFixedWidth(440)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)

        card = QFrame()
        card.setObjectName("updateInstalledCard")
        card.setStyleSheet(
            f"""
            QFrame#updateInstalledCard {{
                background-color: {BrandColors.WINDOW_BG};
                border: none;
            }}
            """
        )
        root_layout.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 22, 22, 18)
        layout.setSpacing(12)

        title = QLabel("Update Installed!")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"""
            font-size: {BrandColors.FONT_SIZE_TITLE};
            font-weight: 800;
            color: {BrandColors.TEXT_PRIMARY};
            background-color: transparent;
            """
        )
        layout.addWidget(title)

        subtitle = QLabel(self._build_subtitle_text())
        subtitle.setTextFormat(Qt.PlainText)
        subtitle.setWordWrap(True)
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet(
            f"""
            font-size: {BrandColors.FONT_SIZE_REGULAR};
            color: {BrandColors.TEXT_SECONDARY};
            background-color: transparent;
            padding: 4px 4px;
            """
        )
        layout.addWidget(subtitle)

        layout.addWidget(self._build_button_row())

        post_update_row = self._build_post_update_row()
        if post_update_row is not None:
            layout.addWidget(post_update_row)

    def _build_subtitle_text(self) -> str:
        version_text = _format_version(self._info.version)
        text = f"Update {version_text} successfully installed."
        summary = str(getattr(self._info, "summary", "") or "").strip()
        if summary:
            text = f"{text} This update brings: {summary}"
        return text

    def _build_button_row(self) -> QFrame:
        row = QFrame()
        row.setStyleSheet("background-color: transparent;")

        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(10)

        close_btn = QPushButton("Close")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(
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
        close_btn.clicked.connect(self.reject)
        layout.addWidget(close_btn, 1)

        notes_btn = QPushButton("Release Notes")
        notes_btn.setCursor(Qt.PointingHandCursor)
        notes_btn.setStyleSheet(
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
        notes_btn.clicked.connect(self._on_release_notes_clicked)
        IconUtils.apply_icon(notes_btn, IconType.HELP, BrandColors.TEXT_PRIMARY, size=16)
        notes_btn.setIconSize(QSize(16, 16))
        layout.addWidget(notes_btn, 1)

        return row

    def _build_post_update_row(self) -> QFrame | None:
        post_update_button = self._build_post_update_button()
        if post_update_button is None:
            return None

        row = QFrame()
        row.setStyleSheet("background-color: transparent;")

        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(post_update_button, 1)

        return row

    def _build_post_update_button(self) -> QPushButton | None:
        action = _normalize_post_update_action(self._info.post_update)
        if action == "none":
            self._post_update_button = None
            return None

        button_text = "Join our Discord!" if action == "discord" else "Vote for the Next Update"

        button = QPushButton(button_text)
        button.setCursor(Qt.PointingHandCursor)
        button.setMinimumHeight(42)
        button.setStyleSheet(_post_update_button_style())

        if action == "discord":
            IconUtils.apply_icon(button, IconType.DISCORD, BrandColors.TEXT_PRIMARY, size=16)
            button.setIconSize(QSize(16, 16))
        else:
            icon = IconUtils.get_icon(
                "external-link.svg",
                color=BrandColors.TEXT_PRIMARY,
                size=16,
                widget=button,
            )
            if not icon.isNull():
                button.setIcon(icon)
                button.setIconSize(QSize(16, 16))

        button.clicked.connect(self._on_post_update_clicked)
        self._post_update_button = button
        return button

    def _on_release_notes_clicked(self) -> None:
        url = (self._info.release_notes_url or "").strip()
        if url:
            QDesktopServices.openUrl(QUrl(url))
        self.accept()

    def _on_post_update_clicked(self) -> None:
        action = _normalize_post_update_action(self._info.post_update)
        if action == "discord":
            QDesktopServices.openUrl(QUrl(DISCORD_INVITE_URL))
            return

        btn = getattr(self, "_post_update_button", None)
        if btn is None:
            return

        btn.setEnabled(False)
        self._post_update_button_original_text = btn.text()
        btn.setText("Loading survey link...")

        def worker() -> None:
            url: str | None = None
            error_text: str | None = None
            try:
                url = _fetch_latest_survey_url()
            except Exception as exc:
                error_text = str(exc)

            self._survey_link_ready.emit(url, error_text)

        threading.Thread(target=worker, daemon=True).start()

    @Slot(object, object)
    def _on_survey_link_ready(self, url: object, error_text: object) -> None:
        btn = getattr(self, "_post_update_button", None)
        if btn is None:
            return

        btn.setEnabled(True)
        btn.setText(self._post_update_button_original_text or "Vote for the Next Update")

        url_str = str(url or "").strip()
        if url_str:
            QDesktopServices.openUrl(QUrl(url_str))
            return

        msg = "Couldn't fetch the survey link right now. Please try again later."
        err = str(error_text or "").strip()
        if err:
            msg = f"{msg}\n\n{err}"
        QMessageBox.warning(self, "Survey Link Unavailable", msg)


def _parse_first_url(text: str) -> str | None:
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith(("http://", "https://")):
            return line
        return None
    return None


def _fetch_latest_survey_url(timeout_s: float = 5.0) -> str:
    response = requests.get(
        REMOTE_LATEST_SURVEY_URL,
        timeout=timeout_s,
        headers={"User-Agent": "IntenseRP-Next-SurveyLink"},
    )
    response.raise_for_status()
    url = _parse_first_url(response.text)
    if not url:
        raise ValueError("The survey link file is empty or invalid.")
    return url


def _post_update_button_style(*, square: bool = False) -> str:
    padding = "0px" if square else "10px 14px"
    return f"""
        QPushButton {{
            background-color: transparent;
            color: {BrandColors.TEXT_PRIMARY};
            border: 1px solid {BrandColors.INPUT_BORDER};
            padding: {padding};
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
        QPushButton:disabled {{
            color: {BrandColors.TEXT_DISABLED};
        }}
    """


def _normalize_post_update_action(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in _POST_UPDATE_ACTIONS:
        return normalized
    return "survey"
