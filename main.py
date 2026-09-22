import sys
import asyncio
import uvicorn
import logging
import os
import json
import threading
import errno
from pathlib import Path
from typing import Callable
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QVBoxLayout, QWidget, QLabel, 
    QHBoxLayout, QSizePolicy, QSplitter, QMessageBox, QSystemTrayIcon, QMenu,
    QDialog, QLineEdit
)
from PySide6.QtCore import Signal, Slot, Qt, QProcess, QSize, QUrl, QEvent, QObject
from PySide6.QtCore import QTimer
from PySide6.QtGui import QPixmap, QDesktopServices, QIcon
import qasync

from drivers.factory import create_driver
from drivers.providers import DriverProvider, is_provider_locked, provider_lock_reason, provider_options
from api import API, DryRunCapture, DryRunDriver
from config.manager import ConfigManager
from config.loadouts import get_behavior_category_for_provider, get_loadout_field_defs
from remote_control import RemoteControlActions
from ui.windows.settings_window import SettingsWindow
from ui.windows.console_window import ConsoleWindow
from ui.windows.dry_run_window import DryRunWindow
from ui.windows.help_window import HelpWindow
from ui.windows.welcome_window import WelcomeWindow
from ui.widgets.mini_console import MiniConsole
from ui.widgets.request_queue_preview import RequestQueuePreview
from ui.widgets.split_button import SplitButton
from ui.widgets.badge_icon_button import BadgeIconButton
from ui.core.animation_settings import sync_animations_disabled_from_config
from ui.core.brand import BrandColors
from ui.core.icons import IconUtils, IconType
from ui.niche.hotswap_dialog import HotswapDialog, PROVIDER_ICON_MAP
from ui.niche.loadout_switch_dialog import LoadoutSwitchDialog
from ui.niche.post_update_functions import PostUpdateFunctionContext, run_post_update_function
from ui.niche.update_available_dialog import UpdateAvailableDialog, UpdateAvailableInfo
from ui.niche.update_installed_dialog import UpdateInstalledDialog, UpdateInstalledInfo
from drivers.parallel_manager import ParallelDriversManager
from utils.logger import Logger, LogLevel, LEVEL_NAME_MAP
from utils.news_state import NEWS_DOCS_URL, has_unviewed_news, mark_latest_news_viewed
from utils.providers_in_parallel import (
    get_current_provider,
    get_parallel_selected_providers,
    is_parallel_feature_enabled,
    is_parallel_runtime_active,
)
from utils.update_checker import check_for_updates
from utils.version_file import VersionFileInfo, parse_version_file
from utils.resource_path import resolve_resource_path
from utils.docs_links import DOCS_BASE_URL
from utils.diagnostics import (
    clear_prompt_snapshots,
    configure_internal_diagnostics_logging,
)
import shutil
import socket
import time
import traceback


def _parse_update_cleanup_args(
    argv: list[str],
) -> tuple[list[str], bool, str | None, bool, bool, str | None, str | None, bool, bool]:
    """
    Parse and remove internal startup args from argv.

    Supported forms:
      --deleteupdater
      --updaterpath <path>
      --updaterpath=<path>
      --clearFlags
      --fakeUpdate
      --localUpdateDebug <zipfile>
      --localUpdateDebug=<zipfile>
      --debugWidgetShows
      --extraDebugLogs
    """
    remaining: list[str] = []
    delete_updater = False
    updater_path: str | None = None
    clear_flags = False
    fake_update = False
    fake_pufref: str | None = None
    local_update_debug: str | None = None
    debug_widget_shows = False
    extra_debug_logs = False

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--deleteupdater":
            delete_updater = True
            i += 1
            continue

        if arg == "--updaterpath":
            if i + 1 < len(argv):
                updater_path = argv[i + 1]
                i += 2
                continue
            i += 1
            continue

        if arg.startswith("--updaterpath="):
            updater_path = arg.split("=", 1)[1] or None
            i += 1
            continue

        if arg.lower() == "--clearflags":
            clear_flags = True
            i += 1
            continue

        if arg.lower() == "--fakeupdate":
            fake_update = True
            i += 1
            continue

        if arg.lower() == "--fakepufref":
            fake_update = True
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                fake_pufref = argv[i + 1]
                i += 2
                continue
            i += 1
            continue

        if arg.lower().startswith("--fakepufref="):
            fake_pufref = arg.split("=", 1)[1] or None
            fake_update = True
            i += 1
            continue

        if arg.lower() == "--localupdatedebug":
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                local_update_debug = argv[i + 1]
                i += 2
                continue
            local_update_debug = ""
            i += 1
            continue

        if arg.lower().startswith("--localupdatedebug="):
            local_update_debug = arg.split("=", 1)[1]
            i += 1
            continue

        if arg.lower() == "--debugwidgetshows":
            debug_widget_shows = True
            i += 1
            continue

        if arg.lower() == "--extradebuglogs":
            extra_debug_logs = True
            i += 1
            continue

        remaining.append(arg)
        i += 1

    return (
        remaining,
        delete_updater,
        updater_path,
        clear_flags,
        fake_update,
        fake_pufref,
        local_update_debug,
        debug_widget_shows,
        extra_debug_logs,
    )


def _resolve_local_update_debug_archive(raw_path: str | None) -> Path | None:
    if raw_path is None:
        return None

    value = str(raw_path or "").strip()
    if not value:
        raise ValueError("--localUpdateDebug must point to a .zip file.")

    try:
        archive_path = Path(value).expanduser()
        try:
            archive_path = archive_path.resolve()
        except Exception:
            archive_path = archive_path.absolute()
    except Exception as exc:
        raise ValueError(f"Invalid --localUpdateDebug path: {value}") from exc

    if archive_path.suffix.lower() != ".zip":
        raise ValueError(f"--localUpdateDebug must point to a .zip file: {archive_path}")
    if not archive_path.is_file():
        raise ValueError(f"Local update debug ZIP not found: {archive_path}")
    return archive_path


def _delete_updater_best_effort(cleanup_path: Path) -> None:
    """
    Clean up the temp extraction directory left behind by the updater.
    
    The updater now passes its temp directory path directly (e.g.,
    C:/Users/.../Temp/intenserp-update-extract-xyz/), so we just delete it.
    """
    try:
        p = cleanup_path.expanduser()
        try:
            p = p.resolve()
        except Exception:
            p = p.absolute()
    except Exception:
        return

    if not p.exists():
        return

    # Wait a bit for the updater process to fully exit
    time.sleep(1.0)

    # Try to delete the entire temp directory tree
    for attempt in range(30):  # ~15 seconds of retries
        try:
            shutil.rmtree(p)
            return  # Success!
        except Exception:
            time.sleep(0.5)


def _clear_app_flags() -> bool:
    try:
        from config.app_flags import AppFlagsStore
        from config.location import get_active_config_dir

        config_dir = get_active_config_dir()
        store = AppFlagsStore(config_dir)
        ok = store.clear()
        if ok:
            print(f"App flags cleared: {config_dir / AppFlagsStore.FILENAME}")
        else:
            print("Failed to clear app flags.")
        return ok
    except Exception as exc:
        print(f"Failed to clear app flags: {exc}")
        return False


def _install_widget_debug_logging(app: QApplication) -> Path | None:
    try:
        log_dir = Path(resolve_resource_path("logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = (log_dir / "widget_show_debug.log").resolve()
        log_file = open(log_path, "w", encoding="utf-8", buffering=1)
    except Exception as exc:
        print(f"Failed to open widget debug log: {exc}")
        return None

    def _log_line(text: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            log_file.write(f"[{stamp}] {text}\n")
        except Exception:
            pass

    class _WidgetDebugFilter(QObject):
        def __init__(self, parent=None):
            super().__init__(parent)

        def eventFilter(self, obj, event):
            try:
                if isinstance(obj, QWidget):
                    is_popup = bool(obj.windowFlags() & Qt.Popup)
                    if obj.isWindow() or is_popup:
                        if event.type() == QEvent.Show:
                            _log_line(
                                "EVENT Show "
                                f"{type(obj).__module__}.{type(obj).__name__} "
                                f"title={obj.windowTitle()!r} "
                                f"visible={obj.isVisible()} "
                                f"size={obj.size().width()}x{obj.size().height()} "
                                f"parent={(type(obj.parent()).__name__ if obj.parent() else None)!r} "
                                f"flags={int(obj.windowFlags())}"
                            )
                        elif event.type() == QEvent.Hide:
                            _log_line(
                                "EVENT Hide "
                                f"{type(obj).__module__}.{type(obj).__name__} "
                                f"title={obj.windowTitle()!r} "
                                f"size={obj.size().width()}x{obj.size().height()}"
                            )
            except Exception as exc:
                _log_line(f"FILTER ERROR {exc}")
            return False

    debug_filter = _WidgetDebugFilter(app)
    app.installEventFilter(debug_filter)
    app.setProperty("_widget_debug_filter", debug_filter)

    orig_show = QWidget.show

    def traced_show(widget):
        try:
            is_popup = bool(widget.windowFlags() & Qt.Popup)
            if widget.isWindow() or is_popup:
                stack = " | ".join(
                    f"{frame.name}@{Path(frame.filename).name}:{frame.lineno}"
                    for frame in traceback.extract_stack(limit=10)[:-1]
                )
                _log_line(
                    "CALL show "
                    f"{type(widget).__module__}.{type(widget).__name__} "
                    f"title={widget.windowTitle()!r} "
                    f"visible={widget.isVisible()} "
                    f"size={widget.size().width()}x{widget.size().height()} "
                    f"parent={(type(widget.parent()).__name__ if widget.parent() else None)!r} "
                    f"flags={int(widget.windowFlags())} "
                    f"stack={stack}"
                )
        except Exception as exc:
            _log_line(f"SHOW TRACE ERROR {exc}")
        return orig_show(widget)

    QWidget.show = traced_show

    from PySide6.QtWidgets import QComboBox

    orig_show_popup = QComboBox.showPopup

    def traced_show_popup(combo):
        try:
            _log_line(
                "CALL showPopup "
                f"{type(combo).__module__}.{type(combo).__name__} "
                f"objectName={combo.objectName()!r} "
                f"currentText={combo.currentText()!r} "
                f"size={combo.size().width()}x{combo.size().height()}"
            )
        except Exception as exc:
            _log_line(f"SHOWPOPUP TRACE ERROR {exc}")
        return orig_show_popup(combo)

    QComboBox.showPopup = traced_show_popup
    app.setProperty("_widget_debug_log_path", str(log_path))
    _log_line("Widget debug logging enabled")
    print(f"Widget debug logging enabled: {log_path}")
    return log_path


def get_version_info() -> VersionFileInfo:
    """Read version metadata from version.json."""
    version_file = resolve_resource_path("version.json")
    try:
        with open(version_file, "r", encoding="utf-8") as f:
            return parse_version_file(
                f.read(),
                default_version="unknown",
                default_auto_updateable=True,
                default_severity=2,
            )
    except FileNotFoundError:
        return parse_version_file(
            "",
            default_version="unknown",
            default_auto_updateable=True,
            default_severity=2,
        )


def get_version():
    """Read version from version.json file."""
    info = get_version_info()
    return info.version.strip() or "unknown"


POSTUPDATE_FLAG_FILENAME = "postupdate_notes_url.txt"
POSTUPDATE_CLEANUP_FILENAME = "postupdate_cleanup.json"


def _get_update_app_root() -> Path | None:
    try:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent
    except Exception:
        return None


def _release_notes_url_for_version(version: str) -> str:
    value = (version or "").strip()
    if value.lower().startswith("v"):
        value = value[1:].strip()
    if not value or value.lower() == "unknown":
        return "https://github.com/Phobeuscz/irn/releases"
    return f"https://github.com/Phobeuscz/irn/releases/tag/v{value}"


def _read_postupdate_cleanup_backup_dir(app_root: Path) -> tuple[Path, Path] | None:
    marker_path = app_root / POSTUPDATE_CLEANUP_FILENAME
    try:
        if not marker_path.is_file():
            return None
    except Exception:
        return None

    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        try:
            marker_path.unlink()
        except Exception:
            pass
        return None

    raw_backup_dir = ""
    if isinstance(payload, dict):
        raw_backup_dir = str(payload.get("backup_dir") or "").strip()
    if not raw_backup_dir:
        try:
            marker_path.unlink()
        except Exception:
            pass
        return None

    try:
        backup_dir = Path(raw_backup_dir).expanduser().resolve()
        app_root_resolved = app_root.resolve()
    except Exception:
        return None

    # The updater only creates backup folders next to the app root
    try:
        expected_prefix = f"{app_root_resolved.name}-backup"
        if backup_dir.parent != app_root_resolved.parent:
            return None
        if not backup_dir.name.startswith(expected_prefix):
            return None
        if backup_dir == app_root_resolved:
            return None
    except Exception:
        return None

    return backup_dir, marker_path


def _cleanup_postupdate_backup_best_effort() -> None:
    app_root = _get_update_app_root()
    if app_root is None:
        return

    cleanup_info = _read_postupdate_cleanup_backup_dir(app_root)
    if cleanup_info is None:
        return

    backup_dir, marker_path = cleanup_info
    if not backup_dir.exists():
        try:
            marker_path.unlink()
        except Exception:
            pass
        return

    def worker() -> None:
        # Let startup finish first. This backup is our rollback until relaunch.
        time.sleep(2.0)
        for attempt in range(60):
            try:
                if backup_dir.exists():
                    shutil.rmtree(backup_dir)
                try:
                    marker_path.unlink()
                except Exception:
                    pass
                Logger.info(f"Post-update backup cleaned up: {backup_dir}")
                return
            except Exception as exc:
                if attempt == 0:
                    Logger.warning(f"Post-update backup cleanup is waiting: {exc}")
                time.sleep(0.5)
        Logger.warning(f"Post-update backup cleanup failed: {backup_dir}")

    threading.Thread(target=worker, daemon=True).start()


def _consume_postupdate_installed_info() -> UpdateInstalledInfo | None:
    app_root = _get_update_app_root()
    if app_root is None:
        return None

    flag_path = app_root / POSTUPDATE_FLAG_FILENAME
    try:
        if not flag_path.is_file():
            return None
    except Exception:
        return None

    try:
        version_info = get_version_info()
    except Exception:
        version_info = parse_version_file(
            "",
            default_version="unknown",
            default_auto_updateable=True,
            default_severity=2,
        )

    version = version_info.version.strip() or "unknown"

    try:
        release_notes_url = _release_notes_url_for_version(version)
    except Exception:
        release_notes_url = "https://github.com/Phobeuscz/irn/releases"

    try:
        flag_path.unlink()
    except Exception:
        pass

    return UpdateInstalledInfo(
        version=version,
        release_notes_url=release_notes_url,
        post_update=version_info.post_update,
        post_update_function_ref=version_info.post_update_function_ref,
        summary=version_info.summary,
    )


class MainWindow(QMainWindow):
    update_available_found = Signal(object)
    dry_run_capture_received = Signal(object)
    DEFAULT_WINDOW_WIDTH = 450
    DEFAULT_WINDOW_HEIGHT = 520

    def __init__(
        self,
        *,
        fake_update: bool = False,
        fake_pufref: str | None = None,
        local_update_debug_archive: str | None = None,
        extra_debug_logs_cli: bool = False,
    ):
        super().__init__()
        self.setWindowTitle("IntenseRP Next")
        self.resize(self.DEFAULT_WINDOW_WIDTH, self.DEFAULT_WINDOW_HEIGHT)
        self.setStyleSheet(f"background-color: {BrandColors.WINDOW_BG}; color: {BrandColors.TEXT_PRIMARY};")

        self.config_manager = ConfigManager()
        self._extra_debug_logs_cli_enabled = bool(extra_debug_logs_cli)
        self._sync_extra_debug_logs_setting()
        sync_animations_disabled_from_config(self.config_manager)
        self._queue_preview_min_width = 300
        self._queue_preview_handle_width = 12

        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)

        outer_layout = QHBoxLayout(self.central_widget)
        outer_layout.setContentsMargins(16, 16, 16, 16)
        outer_layout.setSpacing(0)
        self._outer_layout = outer_layout

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(0)
        self.splitter.setStyleSheet(f"QSplitter::handle {{ background-color: {BrandColors.WINDOW_BG}; }}")
        outer_layout.addWidget(self.splitter)

        self.main_panel = QWidget()
        self.main_panel.setStyleSheet("background-color: transparent;")
        self.main_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.layout = QVBoxLayout(self.main_panel)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(12)

        self.queue_preview = RequestQueuePreview(self.central_widget)
        self.queue_preview.setVisible(False)
        self.queue_preview.setMinimumWidth(0)
        self.queue_preview.setMaximumWidth(0)
        self.queue_preview.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.queue_preview.stop_requested.connect(self._on_queue_preview_stop_requested)
        self.queue_preview.clear_after_current_requested.connect(self._on_queue_preview_clear_requested)
        self.queue_preview.request_action_requested.connect(self._on_queue_preview_request_action_requested)

        self.splitter.addWidget(self.main_panel)
        self.splitter.setCollapsible(0, False)
        self.splitter.splitterMoved.connect(self._on_splitter_moved)

        # 1. Title Area
        self._title_widget = QWidget()
        self._title_widget.setStyleSheet("background: transparent;")
        self.layout.addWidget(self._title_widget)
        self._build_title_area()

        # 1.5. Readiness Status
        self.status_label = QLabel("● Ready")
        self.status_label.setStyleSheet(f"""
            font-size: {BrandColors.FONT_SIZE_LARGE};
            font-weight: bold;
            color: {BrandColors.SUCCESS};
            padding: 8px;
            background-color: {BrandColors.SIDEBAR_BG};
            border-radius: 6px;
        """)
        self.status_label.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(self.status_label)

        # 2. Mini-Console Area
        self.mini_console = MiniConsole()
        self.mini_console.logging_level_selected.connect(self._on_mini_console_log_level_selected)
        self.mini_console.setMinimumHeight(250)
        self.mini_console.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.layout.addWidget(self.mini_console)

        # 3. Control Buttons
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        
        self.start_button = SplitButton("Start")
        self.start_button.setStyleSheet(f"""
            QPushButton {{
                background-color: {BrandColors.ACCENT};
                color: {BrandColors.TEXT_PRIMARY};
                border: none;
                padding: 12px 20px;
                border-radius: 6px;
                font-weight: bold;
                font-size: {BrandColors.FONT_SIZE_REGULAR};
            }}
            QPushButton:hover {{
                background-color: #4a80e0;
            }}
            QPushButton:disabled {{
                background-color: {BrandColors.TEXT_DISABLED};
            }}
        """)
        self.start_button.apply_icon(IconType.START, BrandColors.TEXT_PRIMARY)
        self.start_button.setCursor(Qt.PointingHandCursor)
        self.start_button.clicked.connect(self.on_start_clicked)
        button_layout.addWidget(self.start_button, 1)

        self.settings_button = QPushButton("Settings")
        self.settings_button.setStyleSheet(f"""
            QPushButton {{
                background-color: {BrandColors.SIDEBAR_BG};
                color: {BrandColors.TEXT_PRIMARY};
                border: none;
                padding: 12px 20px;
                border-radius: 6px;
                font-size: {BrandColors.FONT_SIZE_REGULAR};
            }}
            QPushButton:hover {{
                background-color: {BrandColors.ITEM_HOVER};
            }}
            QPushButton:disabled {{
                background-color: {BrandColors.SIDEBAR_BG};
                color: {BrandColors.TEXT_DISABLED};
            }}
        """)
        IconUtils.apply_icon(self.settings_button, IconType.SETTINGS, BrandColors.TEXT_PRIMARY)
        self.settings_button.setIconSize(QSize(16, 16))
        self.settings_button.setCursor(Qt.PointingHandCursor)
        self.settings_button.clicked.connect(self.open_settings)
        button_layout.addWidget(self.settings_button, 1)
        
        self.layout.addLayout(button_layout)

        help_row = QHBoxLayout()
        help_row.setSpacing(10)

        self.hotswap_button = QPushButton()
        self.hotswap_button.setFixedWidth(44)
        self.hotswap_button.setStyleSheet(f"""
            QPushButton {{
                background-color: {BrandColors.SIDEBAR_BG};
                color: {BrandColors.TEXT_PRIMARY};
                border: none;
                padding: 12px 0px;
                border-radius: 6px;
            }}
            QPushButton:hover {{
                background-color: {BrandColors.ITEM_HOVER};
            }}
        """)
        self.hotswap_button.setCursor(Qt.PointingHandCursor)
        self.hotswap_button.clicked.connect(self._on_hotswap)
        self.hotswap_button.setVisible(False)
        help_row.addWidget(self.hotswap_button)

        self.help_button = QPushButton("Tools")
        self.help_button.setStyleSheet(f"""
            QPushButton {{
                background-color: {BrandColors.SIDEBAR_BG};
                color: {BrandColors.TEXT_PRIMARY};
                border: none;
                padding: 12px 20px;
                border-radius: 6px;
                font-size: {BrandColors.FONT_SIZE_REGULAR};
            }}
            QPushButton:hover {{
                background-color: {BrandColors.ITEM_HOVER};
            }}
        """)
        IconUtils.apply_icon(self.help_button, IconType.HELP, BrandColors.TEXT_PRIMARY)
        self.help_button.setCursor(Qt.PointingHandCursor)
        self.help_button.clicked.connect(self.open_help)
        help_row.addWidget(self.help_button, 1)

        self.news_button = BadgeIconButton()
        self.news_button.setFixedWidth(44)
        self.news_button.setStyleSheet(f"""
            QPushButton {{
                background-color: {BrandColors.SIDEBAR_BG};
                color: {BrandColors.TEXT_PRIMARY};
                border: none;
                padding: 12px 0px;
                border-radius: 6px;
            }}
            QPushButton:hover {{
                background-color: {BrandColors.ITEM_HOVER};
            }}
        """)
        IconUtils.apply_icon(self.news_button, IconType.BELL, BrandColors.TEXT_PRIMARY, size=18)
        self.news_button.setIconSize(QSize(18, 18))
        self.news_button.setToolTip("Open News")
        self.news_button.setCursor(Qt.PointingHandCursor)
        self.news_button.clicked.connect(self._open_news_page)
        help_row.addWidget(self.news_button)

        self.layout.addLayout(help_row)

        self._queue_preview_enabled = False
        self._queue_preview_last_width = 360
        self._queue_preview_refresh_inflight = False
        self._queue_preview_refresh_pending = False
        self._queue_preview_refresh_posted = False
        self._queue_preview_last_rendered = None
        self._queue_preview_state_listener = self._on_queue_preview_state_changed
        self._queue_preview_listener_api: API | None = None

        self.driver = None
        self.api = None
        self.server = None
        self._stop_task: asyncio.Task | None = None
        self._shutdown_task: asyncio.Task | None = None
        self.settings_window = None
        self._settings_button_loading = False
        self.console_window = None
        self.dry_run_window = None
        self.help_window = None
        self._welcome_window = None
        self._main_logging_enabled = True
        self._console_log_level = LogLevel.DEBUG
        self._mini_console_log_level = LogLevel.SUCCESS
        self._news_unread = False

        self._settings_button_loading_timeout = QTimer(self)
        self._settings_button_loading_timeout.setSingleShot(True)
        self._settings_button_loading_timeout.setInterval(8000)
        self._settings_button_loading_timeout.timeout.connect(self._on_settings_button_loading_timeout)

        # Initialize logging based on settings
        self._setup_logging()
        
        # Always set the log callback for the mini-console
        Logger.set_console_callback(self._on_log_message)
        self.update_available_found.connect(self._show_update_available_dialog)
        self.dry_run_capture_received.connect(self._on_dry_run_capture_received)

        self._tray_icon = None
        self._tray_menu = None
        self._tray_restore_windows = []
        self._tray_action_hide = None
        self._tray_action_show = None
        self._tray_action_start = None
        self._tray_action_stop = None
        self._tray_action_restart = None
        self._tray_action_exit = None
        self._desktop_notifier = None
        self._desktop_notifier_unavailable = False
        self._open_notification_dialogs = []
        self._exit_requested = False
        self._setup_tray_icon()

        self._post_update_info = _consume_postupdate_installed_info()
        if self._post_update_info is None and fake_update:
            try:
                version_info = get_version_info()
            except Exception:
                version_info = parse_version_file(
                    "",
                    default_version="unknown",
                    default_auto_updateable=True,
                    default_severity=2,
                )

            version = version_info.version.strip() or "unknown"

            try:
                release_notes_url = _release_notes_url_for_version(version)
            except Exception:
                release_notes_url = "https://github.com/Phobeuscz/irn/releases"

            self._post_update_info = UpdateInstalledInfo(
                version=str(version or "unknown"),
                release_notes_url=str(release_notes_url or ""),
                post_update=version_info.post_update,
                post_update_function_ref=fake_pufref or version_info.post_update_function_ref,
                summary=version_info.summary,
            )
        self._maybe_show_update_installed_dialog()
        QTimer.singleShot(3000, _cleanup_postupdate_backup_best_effort)

        if local_update_debug_archive:
            self._maybe_show_local_update_debug_dialog(local_update_debug_archive)
        else:
            self._maybe_check_for_updates_on_startup()
        self._apply_queue_preview_setting(force=True)
        self._refresh_news_state()
        self._sync_news_button()
        self._sync_hotswap_button()
        self._maybe_show_welcome_window()

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def _cleanup_tray_icon(self) -> None:
        tray_icon = getattr(self, "_tray_icon", None)
        if not tray_icon:
            return

        try:
            tray_icon.hide()
        except Exception:
            pass

        try:
            tray_icon.setVisible(False)
        except Exception:
            pass

        try:
            tray_icon.deleteLater()
        except Exception:
            pass

        self._tray_icon = None
        tray_menu = getattr(self, "_tray_menu", None)
        if tray_menu is not None:
            try:
                tray_menu.deleteLater()
            except Exception:
                pass

        self._tray_menu = None
        self._tray_restore_windows = []
        self._tray_action_hide = None
        self._tray_action_show = None
        self._tray_action_start = None
        self._tray_action_stop = None
        self._tray_action_restart = None
        self._tray_action_exit = None

    def _setup_tray_icon(self) -> None:
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return

            tray_icon = QSystemTrayIcon(self)
            icon = self.windowIcon() if not self.windowIcon().isNull() else QApplication.windowIcon()
            if not icon.isNull():
                tray_icon.setIcon(icon)

            tray_icon.setToolTip(self.windowTitle())

            menu = QMenu(self)
            menu.aboutToShow.connect(self._update_tray_menu_state)

            hide_action = menu.addAction("Hide")
            hide_action.triggered.connect(self._hide_to_tray)

            show_action = menu.addAction("Show")
            show_action.triggered.connect(self._show_from_tray)

            menu.addSeparator()

            start_action = menu.addAction("Start")
            start_action.triggered.connect(self._tray_start_services)

            stop_action = menu.addAction("Stop")
            stop_action.triggered.connect(self._tray_stop_services)

            restart_action = menu.addAction("Restart")
            restart_action.triggered.connect(self._tray_restart_services)

            menu.addSeparator()

            exit_action = menu.addAction("Exit")
            exit_action.triggered.connect(self._tray_exit_requested)

            tray_icon.setContextMenu(menu)
            tray_icon.activated.connect(self._on_tray_icon_activated)
            tray_icon.show()

            self._tray_icon = tray_icon
            self._tray_menu = menu
            self._tray_action_hide = hide_action
            self._tray_action_show = show_action
            self._tray_action_start = start_action
            self._tray_action_stop = stop_action
            self._tray_action_restart = restart_action
            self._tray_action_exit = exit_action

            self._update_tray_menu_state()
        except Exception:
            self._tray_icon = None
            self._tray_menu = None

    def _iter_tray_windows(self):
        windows = [self]
        for attr in ("settings_window", "help_window", "console_window", "dry_run_window"):
            win = getattr(self, attr, None)
            if win is not None:
                windows.append(win)
        return windows

    def _hide_to_tray(self) -> None:
        restore = []
        for win in self._iter_tray_windows():
            try:
                if win.isVisible():
                    restore.append(win)
            except Exception:
                continue

        self._tray_restore_windows = restore

        for win in restore:
            try:
                win.hide()
            except Exception:
                pass

        self._update_tray_menu_state()

    def _show_from_tray(self) -> None:
        restore = list(getattr(self, "_tray_restore_windows", None) or [])
        targets = restore or [self]

        for win in targets:
            if win is None:
                continue
            try:
                win.show()
                if hasattr(win, "showNormal"):
                    win.showNormal()
                if hasattr(win, "raise_"):
                    win.raise_()
                if hasattr(win, "activateWindow"):
                    win.activateWindow()
            except Exception:
                pass

        self._tray_restore_windows = []
        self._update_tray_menu_state()

    def _on_tray_icon_activated(self, reason) -> None:
        if reason not in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            return

        if not self.isVisible():
            self._show_from_tray()
            return

        try:
            self.raise_()
            self.activateWindow()
        except Exception:
            pass

    def _are_services_running(self) -> bool:
        server_task = getattr(self, "server_task", None)
        if server_task is not None:
            try:
                if not server_task.done():
                    return True
            except Exception:
                return True

        return any(getattr(self, attr, None) is not None for attr in ("driver", "api", "server"))

    def _is_services_busy(self) -> bool:
        start_button = getattr(self, "start_button", None)
        main_button = getattr(start_button, "main_button", None)
        if main_button is not None:
            return not bool(main_button.isEnabled())
        if start_button is not None:
            try:
                return not bool(start_button.isEnabled())
            except Exception:
                return False
        return False

    def _can_switch_account(self) -> bool:
        driver = self._get_current_runtime_driver()
        if driver is None:
            return False

        try:
            mgr = driver._get_ece_manager()
            pairs = mgr.get_provider_pairs(driver.provider)
            return len(pairs) >= 2
        except Exception:
            return False

    def _get_current_runtime_driver(self):
        driver = getattr(self, "driver", None)
        if isinstance(driver, ParallelDriversManager):
            current_provider = get_current_provider(self.config_manager)
            return driver.get_driver(current_provider) or driver.get_current_driver()
        return driver

    def _get_runtime_driver_for_provider(self, provider: DriverProvider):
        driver = getattr(self, "driver", None)
        if isinstance(driver, ParallelDriversManager):
            return driver.get_driver(provider)

        runtime_driver = self._get_current_runtime_driver()
        runtime_provider = getattr(runtime_driver, "provider", None)
        if runtime_driver is not None and runtime_provider == provider:
            return runtime_driver
        return None

    def _iter_runtime_drivers(self) -> list[tuple[DriverProvider, object]]:
        driver = getattr(self, "driver", None)
        if isinstance(driver, ParallelDriversManager):
            return driver.iter_drivers()

        runtime_driver = self._get_current_runtime_driver()
        provider = getattr(runtime_driver, "provider", None)
        if runtime_driver is None or not isinstance(provider, DriverProvider):
            return []
        return [(provider, runtime_driver)]

    def _refresh_settings_dynamic_dropdown_options(self) -> None:
        settings_window = getattr(self, "settings_window", None)
        if settings_window is None or not settings_window.isVisible():
            return
        refresh = getattr(settings_window, "refresh_dynamic_dropdown_options", None)
        if callable(refresh):
            try:
                refresh()
            except Exception as exc:
                Logger.debug(f"Settings dynamic dropdown refresh skipped: {exc}")

    def _get_hotswap_targets(self) -> list[str]:
        current = self.config_manager.get_setting("providers_credentials", "provider") or "DeepSeek"
        current_provider = DriverProvider.from_setting(current)
        current_value = current_provider.value if current_provider else "DeepSeek"
        return [
            provider_name
            for provider_name in provider_options(
                include_locked=False,
                config_manager=self.config_manager,
            )
            if provider_name != current_value
        ]

    def _get_remote_model_switch_provider(self) -> DriverProvider:
        current = self.config_manager.get_setting("providers_credentials", "provider") or "DeepSeek"
        provider = DriverProvider.from_setting(current)
        return provider or DriverProvider.DEEPSEEK

    def _get_remote_model_options(self, provider: DriverProvider | None = None) -> list[str]:
        target_provider = provider or self._get_remote_model_switch_provider()
        model_field = get_loadout_field_defs(target_provider).get("model")
        raw_options = getattr(model_field, "options", None) or []
        options: list[str] = []
        seen: set[str] = set()
        for raw_option in raw_options:
            option = str(raw_option or "").strip()
            if option and option not in seen:
                seen.add(option)
                options.append(option)
        if target_provider == DriverProvider.HUGGINGCHAT:
            try:
                cache_path = (
                    Path(getattr(self.config_manager, "config_dir", "config_data"))
                    / "cache"
                    / "huggingchat_models.json"
                )
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                cached_models = payload.get("models") if isinstance(payload, dict) else []
            except Exception:
                cached_models = []
            for raw_model in cached_models if isinstance(cached_models, list) else []:
                model = str(raw_model or "").strip()
                if model and model not in seen:
                    seen.add(model)
                    options.append(model)
        return options

    def _get_remote_current_model(self, provider: DriverProvider | None = None) -> str:
        target_provider = provider or self._get_remote_model_switch_provider()
        behavior_category = get_behavior_category_for_provider(target_provider)
        if not behavior_category:
            return ""

        try:
            value = self.config_manager.get_setting(behavior_category, "model")
        except Exception:
            value = None
        return str(value or "").strip()

    def _get_remote_model_switch_context(self) -> dict[str, object]:
        if self._loadouts_feature_enabled():
            return {
                "supported": False,
                "parallel": False,
                "current_provider": "",
                "providers": [],
            }

        current_provider = get_current_provider(self.config_manager)
        runtime_providers = []
        for runtime_provider, _driver in self._iter_runtime_drivers():
            if runtime_provider not in runtime_providers:
                runtime_providers.append(runtime_provider)
        parallel_switch = (
            is_parallel_runtime_active(self.config_manager)
            and len(runtime_providers) >= 2
        )
        provider_candidates = runtime_providers if parallel_switch else [current_provider]

        provider_models: dict[DriverProvider, list[str]] = {}
        for provider in provider_candidates:
            if not isinstance(provider, DriverProvider):
                continue
            options = self._get_remote_model_options(provider)
            if options:
                provider_models[provider] = options

        initial_provider = (
            current_provider
            if current_provider in provider_models
            else next(iter(provider_models), current_provider)
        )

        providers_payload = []
        for provider, options in provider_models.items():
            providers_payload.append(
                {
                    "name": provider.value,
                    "current_model": self._get_remote_current_model(provider),
                    "options": [{"name": option} for option in options],
                }
            )

        return {
            "supported": bool(providers_payload),
            "parallel": parallel_switch and len(providers_payload) > 1,
            "current_provider": initial_provider.value,
            "providers": providers_payload,
        }

    def _get_remote_loadout_switch_context(self) -> dict[str, object]:
        if not self._loadouts_feature_enabled():
            return {
                "supported": False,
                "parallel": False,
                "current_provider": "",
                "providers": [],
            }

        current_provider = get_current_provider(self.config_manager)
        runtime_providers = []
        for runtime_provider, _driver in self._iter_runtime_drivers():
            if runtime_provider not in runtime_providers:
                runtime_providers.append(runtime_provider)
        parallel_switch = (
            is_parallel_runtime_active(self.config_manager)
            and len(runtime_providers) >= 2
        )
        provider_candidates = runtime_providers if parallel_switch else [current_provider]

        provider_loadouts = {}
        for provider in provider_candidates:
            if not isinstance(provider, DriverProvider):
                continue
            available = self.config_manager.get_loadouts(provider)
            if available:
                provider_loadouts[provider] = available

        initial_provider = (
            current_provider
            if current_provider in provider_loadouts
            else next(iter(provider_loadouts), current_provider)
        )

        providers_payload = []
        for provider, available in provider_loadouts.items():
            current_name = self.config_manager.get_runtime_active_loadout_name(provider)
            if not current_name:
                current_name = self.config_manager.get_preferred_loadout_name(
                    provider,
                    available,
                )
            providers_payload.append(
                {
                    "name": provider.value,
                    "current_loadout": current_name or "",
                    "options": [{"name": loadout.name} for loadout in available],
                }
            )

        return {
            "supported": bool(providers_payload),
            "parallel": parallel_switch and len(providers_payload) > 1,
            "current_provider": initial_provider.value,
            "providers": providers_payload,
        }

    def _loadouts_feature_enabled(self) -> bool:
        return bool(self.config_manager.get_setting("experimental", "enable_loadouts"))

    def _validate_runtime_loadouts(
        self,
        *,
        providers: list[DriverProvider] | tuple[DriverProvider, ...] | None = None,
        show_dialog: bool = True,
    ) -> bool:
        try:
            self.config_manager.prepare_runtime_loadouts(required_providers=list(providers or []))
            return True
        except ValueError as exc:
            message = str(exc)
        except Exception as exc:
            message = f"Failed to refresh loadouts: {exc}"

        Logger.error(f"Loadouts validation failed: {message}")
        if show_dialog:
            QMessageBox.warning(self, "Loadouts", message)
        return False

    def _get_remote_control_state(self) -> dict[str, object]:
        current = self.config_manager.get_setting("providers_credentials", "provider") or "DeepSeek"
        current_provider = DriverProvider.from_setting(current)
        current_value = current_provider.value if current_provider else "DeepSeek"
        model_switch = self._get_remote_model_switch_context()
        model_switch_provider = (
            DriverProvider.from_setting(model_switch.get("current_provider"))
            or current_provider
            or DriverProvider.DEEPSEEK
        )
        loadout_switch = self._get_remote_loadout_switch_context()
        return {
            "running": self._are_services_running(),
            "busy": self._is_services_busy(),
            "can_switch_account": self._can_switch_account(),
            "current_provider": current_value,
            "hotswap_targets": self._get_hotswap_targets(),
            "model_switch_supported": bool(model_switch.get("supported")),
            "model_switch_parallel": bool(model_switch.get("parallel")),
            "model_switch_current_provider": model_switch.get("current_provider"),
            "model_switch_current_model": self._get_remote_current_model(model_switch_provider),
            "model_switch_options": self._get_remote_model_options(model_switch_provider),
            "model_switch_providers": model_switch.get("providers"),
            "loadout_switch_supported": bool(loadout_switch.get("supported")),
            "loadout_switch_parallel": bool(loadout_switch.get("parallel")),
            "loadout_switch_current_provider": loadout_switch.get("current_provider"),
            "loadout_switch_providers": loadout_switch.get("providers"),
        }

    def _update_tray_menu_state(self) -> None:
        if not getattr(self, "_tray_icon", None):
            return

        hide_action = getattr(self, "_tray_action_hide", None)
        show_action = getattr(self, "_tray_action_show", None)
        start_action = getattr(self, "_tray_action_start", None)
        stop_action = getattr(self, "_tray_action_stop", None)
        restart_action = getattr(self, "_tray_action_restart", None)

        hidden = not bool(self.isVisible())
        if hide_action is not None:
            hide_action.setVisible(not hidden)
        if show_action is not None:
            show_action.setVisible(hidden)

        is_busy = self._is_services_busy()
        is_running = self._are_services_running()

        if start_action is not None:
            start_action.setEnabled((not is_busy) and (not is_running))
        if stop_action is not None:
            stop_action.setEnabled((not is_busy) and is_running)
        if restart_action is not None:
            restart_action.setEnabled((not is_busy) and is_running)

    def _tray_start_services(self) -> None:
        if self._are_services_running():
            return

        self.start_button.setEnabled(False)
        self._update_status("Starting...", "info")
        asyncio.create_task(self.start_services())
        self._update_tray_menu_state()

    def _tray_stop_services(self) -> None:
        if not self._are_services_running():
            return

        self.start_button.setEnabled(False)
        self._update_status("Stopping...", "info")
        asyncio.create_task(self.stop_services())
        self._update_tray_menu_state()

    def _tray_restart_services(self) -> None:
        if not self._are_services_running():
            return

        asyncio.create_task(self._restart_services_impl())
        self._update_tray_menu_state()

    def _tray_exit_requested(self) -> None:
        self._exit_requested = True
        try:
            should_close = bool(self.close())
        except Exception:
            should_close = False

        if should_close:
            return

        shutdown_task = getattr(self, "_shutdown_task", None)
        if shutdown_task and (not shutdown_task.done()):
            return

        self._exit_requested = False
        try:
            self._show_from_tray()
            settings_window = getattr(self, "settings_window", None)
            if settings_window is not None and getattr(settings_window, "unsaved_changes", False):
                settings_window.show()
                settings_window.raise_()
                settings_window.activateWindow()
        except Exception:
            pass

    def _request_application_quit(self, force_after_s: float = 8.0) -> None:
        """
        Best-effort request to quit both the Qt app and the qasync/asyncio loop.

        Some shutdown paths can leave the window closed but the process still alive;
        this makes quitting more deterministic and includes a last-resort forced exit.
        """
        import os
        import threading

        def _force_exit() -> None:
            try:
                os._exit(0)
            except Exception:
                return

        # Last-resort: hard-exit if we still haven't quit after a grace period
        try:
            timer = threading.Timer(float(force_after_s), _force_exit)
            timer.daemon = True
            timer.start()
        except Exception:
            pass

        try:
            loop = asyncio.get_event_loop()
            loop.call_soon(loop.stop)
        except Exception:
            pass

        try:
            app = QApplication.instance()
            if app is not None:
                app.quit()
        except Exception:
            pass

    def _tray_message_icon(self, level: str):
        level_norm = str(level or "info").strip().lower()
        if level_norm in {"warn", "warning"}:
            return QSystemTrayIcon.Warning
        if level_norm in {"err", "error", "critical"}:
            return QSystemTrayIcon.Critical
        return QSystemTrayIcon.Information

    def _show_tray_message(self, title: str, message: str, level: str = "info") -> bool:
        tray_icon = getattr(self, "_tray_icon", None)
        try:
            tray_available = bool(QSystemTrayIcon.isSystemTrayAvailable())
        except Exception:
            tray_available = False
        try:
            messages_supported = bool(QSystemTrayIcon.supportsMessages())
        except Exception:
            messages_supported = False
        tray_visible = bool(tray_icon and tray_icon.isVisible())
        Logger.extra_debug(
            "Tray notification request: "
            f"available={tray_available}, supportsMessages={messages_supported}, "
            f"icon_present={bool(tray_icon)}, icon_visible={tray_visible}, level={level}, "
            f"title={title!r}"
        )
        qt_invoked = False
        if tray_icon and tray_icon.isVisible():
            try:
                tray_icon.showMessage(title, message, self._tray_message_icon(level))
                qt_invoked = True
                Logger.extra_debug("Qt tray showMessage invoked.")
            except Exception as exc:
                Logger.extra_debug(f"Qt tray showMessage failed: {exc}")
        return qt_invoked

    def _get_desktop_notifier(self):
        if getattr(self, "_desktop_notifier_unavailable", False):
            return None
        notifier = getattr(self, "_desktop_notifier", None)
        if notifier is not None:
            return notifier
        try:
            from desktop_notifier import DesktopNotifier, Icon
        except Exception as exc:
            self._desktop_notifier_unavailable = True
            Logger.extra_debug(f"desktop-notifier import failed: {exc}")
            return None

        try:
            init_kwargs: dict[str, object] = {"app_name": "IntenseRP"}
            try:
                from utils.resource_path import resolve_resource_path

                icon_path = resolve_resource_path("ui", "assets", "brand", "newlogo.png")
                if icon_path.exists():
                    init_kwargs["app_icon"] = Icon(path=icon_path)
            except Exception as exc:
                Logger.extra_debug(f"desktop-notifier icon resolve failed: {exc}")

            notifier = DesktopNotifier(**init_kwargs)
            self._desktop_notifier = notifier
            Logger.extra_debug("desktop-notifier backend initialized.")
            return notifier
        except Exception as exc:
            self._desktop_notifier_unavailable = True
            Logger.extra_debug(f"desktop-notifier initialization failed: {exc}")
            return None

    @staticmethod
    def _desktop_notification_urgency(level: str):
        from desktop_notifier import Urgency

        level_norm = str(level or "info").strip().lower()
        if level_norm in {"warn", "warning", "err", "error", "critical"}:
            return Urgency.Critical
        return Urgency.Normal

    async def _send_desktop_notification(
        self, title: str, message: str, level: str = "info"
    ) -> bool:
        notifier = self._get_desktop_notifier()
        if notifier is None:
            return False

        try:
            from desktop_notifier import DEFAULT_SOUND

            notification_id = await notifier.send(
                title=str(title or ""),
                message=str(message or ""),
                urgency=self._desktop_notification_urgency(level),
                sound=DEFAULT_SOUND,
                thread=str(title or "") or None,
            )
            Logger.extra_debug(f"desktop-notifier sent notification id={notification_id!r}.")
            return True
        except Exception as exc:
            Logger.extra_debug(f"desktop-notifier send failed: {exc}")
            return False

    def _schedule_desktop_notification(
        self, title: str, message: str, level: str = "info"
    ) -> bool:
        if self._get_desktop_notifier() is None:
            return False

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False

        task = loop.create_task(self._send_desktop_notification(title, message, level))

        def _on_done(done_task: asyncio.Task) -> None:
            try:
                ok = bool(done_task.result())
            except asyncio.CancelledError:
                return
            except Exception as exc:
                Logger.extra_debug(f"desktop-notifier task failed: {exc}")
                ok = False
            if not ok:
                self._show_tray_message(title, message, level)

        task.add_done_callback(_on_done)
        return True

    def _flash_attention_window(self, widget: QWidget | None = None) -> None:
        target = widget or self
        try:
            QApplication.alert(target, 0)
        except Exception:
            pass

        if sys.platform != "win32":
            return

        try:
            import ctypes
            from ctypes import wintypes

            hwnd = int(target.winId())
            if not hwnd:
                return

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.UINT),
                    ("hwnd", wintypes.HWND),
                    ("dwFlags", wintypes.DWORD),
                    ("uCount", wintypes.UINT),
                    ("dwTimeout", wintypes.DWORD),
                ]

            flash_info = FLASHWINFO(
                ctypes.sizeof(FLASHWINFO),
                wintypes.HWND(hwnd),
                0x0000000F,
                8,
                0,
            )
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(flash_info))
            Logger.extra_debug("Windows FlashWindowEx invoked for attention request.")
        except Exception as exc:
            Logger.extra_debug(f"Windows FlashWindowEx failed: {exc}")

    def _open_notification_dialog(
        self,
        title: str,
        message: str,
        level: str,
        *,
        request_attention: bool = False,
    ) -> None:
        dialog = QMessageBox(self)
        level_norm = str(level or "info").strip().lower()
        if level_norm in {"warn", "warning"}:
            dialog.setIcon(QMessageBox.Warning)
        elif level_norm in {"err", "error", "critical"}:
            dialog.setIcon(QMessageBox.Critical)
        else:
            dialog.setIcon(QMessageBox.Information)
        dialog.setWindowTitle(str(title or "Notification"))
        dialog.setText(str(message or ""))
        dialog.setStandardButtons(QMessageBox.Ok)
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)

        self._open_notification_dialogs.append(dialog)

        def _forget_dialog(_result=None, current_dialog=dialog) -> None:
            try:
                self._open_notification_dialogs.remove(current_dialog)
            except ValueError:
                pass

        dialog.finished.connect(_forget_dialog)
        dialog.open()
        if request_attention:
            try:
                QApplication.beep()
            except Exception:
                pass
            self._flash_attention_window(dialog)

    def _notify_user(
        self,
        title: str,
        message: str,
        level: str = "info",
        dialog_message: str | None = None,
    ) -> None:
        title = str(title or "Notification")
        message = str(message or "")
        level_norm = str(level or "info").strip().lower()
        dialog_message = str(dialog_message or "").strip()

        is_focused = bool(self.isVisible() and self.isActiveWindow())
        if dialog_message:
            if not is_focused:
                if not self._schedule_desktop_notification(title, message, level_norm):
                    self._show_tray_message(title, message, level_norm)
            self._open_notification_dialog(
                title,
                dialog_message,
                level_norm,
                request_attention=not is_focused,
            )
            return

        if is_focused:
            self._open_notification_dialog(title, message, level_norm)
            return

        if self._schedule_desktop_notification(title, message, level_norm):
            return
        if self._show_tray_message(title, message, level_norm):
            return
        Logger.warning(f"{title}: {message}")

    async def _request_user_text(
        self,
        title: str,
        message: str,
        *,
        label: str = "Input",
        placeholder: str = "",
        max_length: int = 0,
        min_length: int = 0,
        digits_only: bool = False,
        level: str = "info",
        force_notify: bool = False,
    ) -> str | None:
        title = str(title or "Input Required")
        message = str(message or "")
        label = str(label or "Input")
        placeholder = str(placeholder or "")
        level_norm = str(level or "info").strip().lower()
        max_length = max(0, int(max_length or 0))
        min_length = max(0, int(min_length or 0))

        is_focused = bool(self.isVisible() and self.isActiveWindow())
        should_notify = bool(force_notify) or (not is_focused)

        if should_notify:
            shown = await self._send_desktop_notification(title, message, level_norm)
            if not shown:
                shown = self._show_tray_message(title, message, level_norm)
            Logger.extra_debug(
                "Request text prompt notification: "
                f"focused={is_focused}, force_notify={force_notify}, notification_sent={shown}. "
                "Delaying modal activation so the OS can surface the notification first."
            )
            try:
                QApplication.beep()
            except Exception:
                pass
            await asyncio.sleep(0.8)
            self._flash_attention_window(self)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[str | None] = loop.create_future()

        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setWindowModality(Qt.ApplicationModal)
        dialog.setWindowFlags(
            dialog.windowFlags()
            | Qt.Window
            | Qt.WindowStaysOnTopHint
            | Qt.WindowTitleHint
            | Qt.WindowCloseButtonHint
        )
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)

        layout = QVBoxLayout(dialog)
        message_label = QLabel(message)
        message_label.setWordWrap(True)
        layout.addWidget(message_label)

        input_label = QLabel(label)
        layout.addWidget(input_label)

        input_box = QLineEdit(dialog)
        input_box.setPlaceholderText(placeholder)
        input_box.setClearButtonEnabled(True)
        if max_length:
            input_box.setMaxLength(max_length)
        layout.addWidget(input_box)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_button = QPushButton("Cancel", dialog)
        submit_button = QPushButton("Continue", dialog)
        submit_button.setDefault(True)
        buttons.addWidget(cancel_button)
        buttons.addWidget(submit_button)
        layout.addLayout(buttons)

        normalizing = False

        def _normalized_text() -> str:
            text = input_box.text().strip()
            if digits_only:
                text = "".join(ch for ch in text if ch.isdigit())
            if max_length:
                text = text[:max_length]
            return text

        def _set_future(value: str | None) -> None:
            if not future.done():
                future.set_result(value)

        def _sync_text() -> None:
            nonlocal normalizing
            if normalizing:
                return
            normalized = _normalized_text()
            if input_box.text() != normalized:
                normalizing = True
                input_box.setText(normalized)
                normalizing = False
            submit_button.setEnabled(len(normalized) >= min_length)

        def _submit() -> None:
            value = _normalized_text()
            if len(value) < min_length:
                return
            _set_future(value)
            dialog.accept()

        def _cancel() -> None:
            _set_future(None)
            dialog.reject()

        input_box.textChanged.connect(lambda _text: _sync_text())
        input_box.returnPressed.connect(_submit)
        submit_button.clicked.connect(_submit)
        cancel_button.clicked.connect(_cancel)
        dialog.finished.connect(lambda _result: _set_future(None))
        _sync_text()

        dialog.open()
        input_box.setFocus(Qt.OtherFocusReason)
        dialog.raise_()
        dialog.activateWindow()
        self._flash_attention_window(dialog)
        if is_focused:
            try:
                QApplication.beep()
            except Exception:
                pass

        try:
            return await future
        except asyncio.CancelledError:
            if dialog.isVisible():
                dialog.close()
            raise

    def _maybe_show_update_installed_dialog(self) -> None:
        info = getattr(self, "_post_update_info", None)
        if info is None:
            return

        def show() -> None:
            if getattr(self, "_post_update_dialog_open", False):
                return

            if not self.isVisible():
                QTimer.singleShot(50, show)
                return

            self._post_update_dialog_open = True
            try:
                dialog = UpdateInstalledDialog(info, parent=self)
                dialog.exec()
            finally:
                self._post_update_dialog_open = False
                self._post_update_info = None

            QTimer.singleShot(0, lambda info=info: self._run_post_update_function(info))

        QTimer.singleShot(0, show)

    def _run_post_update_function(self, info: UpdateInstalledInfo) -> None:
        context = PostUpdateFunctionContext(
            parent=self,
            version=str(getattr(info, "version", "unknown") or "unknown"),
            open_browser_manager=self.open_browser_manager,
        )
        run_post_update_function(getattr(info, "post_update_function_ref", "none"), context)

    @Slot(object)
    def _show_update_available_dialog(self, info: UpdateAvailableInfo):
        if getattr(self, "_update_dialog_open", False):
            return

        def show():
            if getattr(self, "_update_dialog_open", False):
                return
            if not self.isVisible():
                return

            self._update_dialog_open = True
            try:
                dialog = UpdateAvailableDialog(
                    info,
                    parent=self,
                )
                dialog.exec()
            finally:
                self._update_dialog_open = False

        # make sure the dialog is shown from the running UI event loop to avoid
        # early-startup edge cases (startup update checks complete very quickly)
        QTimer.singleShot(0, show)

    def _maybe_check_for_updates_on_startup(self):
        try:
            enabled = bool(
                self.config_manager.get_setting(
                    "application_settings", "check_for_updates_on_startup"
                )
            )
        except Exception:
            enabled = False

        if not enabled:
            return

        def worker():
            result = check_for_updates()
            if result.error:
                Logger.warning(f"Update check failed: {result.error}")
                return

            if result.update_available:
                Logger.warning(
                    f"Update available: {result.local_version} -> {result.remote_version}"
                )
                if result.remote_version is not None:
                    self.update_available_found.emit(
                        UpdateAvailableInfo(
                            local_version=str(result.local_version or "unknown"),
                            remote_version=str(result.remote_version or "unknown"),
                            remote_auto_updateable=result.remote_auto_updateable,
                            remote_severity=result.remote_severity,
                            remote_summary=result.remote_summary,
                        )
                    )
                return

            Logger.info(f"Up to date (v{result.local_version}).")

        threading.Thread(target=worker, daemon=True).start()

    def _maybe_show_local_update_debug_dialog(self, archive_path: str) -> None:
        archive_path = str(archive_path or "").strip()
        if not archive_path:
            return

        try:
            local_version = get_version()
        except Exception:
            local_version = "unknown"

        Logger.warning(f"Local update debug enabled: {archive_path}")
        self.update_available_found.emit(
            UpdateAvailableInfo(
                local_version=str(local_version or "unknown"),
                remote_version="Debug",
                remote_auto_updateable=True,
                remote_severity=None,
                remote_summary=None,
                local_update_archive=archive_path,
            )
        )

    def _maybe_show_welcome_window(self) -> None:
        try:
            should_show_welcome = bool(self.config_manager.should_show_welcome())
        except Exception:
            should_show_welcome = bool(getattr(self.config_manager, "is_first_run", False))

        if not should_show_welcome:
            return

        def show() -> None:
            existing = getattr(self, "_welcome_window", None)
            if existing is not None and existing.isVisible():
                return

            try:
                should_show = bool(self.config_manager.should_show_welcome())
            except Exception:
                should_show = bool(getattr(self.config_manager, "is_first_run", False))
            if not should_show:
                return

            welcome = WelcomeWindow(self.config_manager, None)
            welcome.settings_applied.connect(lambda: self.on_settings_saved({"hotswap_button"}))
            welcome.show()
            try:
                welcome.raise_()
                welcome.activateWindow()
            except Exception:
                pass
            self._welcome_window = welcome

        # Defer until the UI loop is running
        QTimer.singleShot(0, show)

    def _sync_extra_debug_logs_setting(self):
        Logger.set_extra_debug_logs_enabled(
            bool(self.config_manager.get_setting("diagnostics", "extra_debug_logs"))
            or self._extra_debug_logs_cli_enabled
        )

    def _setup_logging(self):
        """Setup logging (console and file) based on settings."""
        def _coerce_int_setting(value, default: int, label: str) -> int:
            try:
                return int(value)
            except Exception:
                Logger.warning(
                    f"Invalid {label} value after settings reload ({value!r}); using {default}."
                )
                return default

        # Console window (separate from mini-console)
        enable_console = self.config_manager.get_setting("console_settings", "enable_console")

        # Routing options (only user-toggleable when console is enabled)
        log_to_main = self.config_manager.get_effective_setting("console_settings", "log_to_main")
        log_to_stdout = self.config_manager.get_effective_setting("console_settings", "log_to_stdout")

        Logger.set_stdout_enabled(bool(log_to_stdout))
        self._main_logging_enabled = bool(log_to_main)
        self.mini_console.set_main_logging_enabled(self._main_logging_enabled)
        if enable_console:
            self._show_console()
        else:
            self._hide_console()
            
        # File Logging
        enable_files = self.config_manager.get_setting("logfiles", "enable_logfiles")
        log_dir = self.config_manager.get_setting("logfiles", "log_dir")
        max_files = self.config_manager.get_setting("logfiles", "max_files")
        max_size_val = self.config_manager.get_setting("logfiles", "size_val")
        size_unit = self.config_manager.get_setting("logfiles", "size_unit")
        
        # Defaults
        if log_dir is None: log_dir = "logs"
        if max_files is None: max_files = 5
        if max_size_val is None: max_size_val = 10
        if size_unit is None: size_unit = "MB"

        max_files_int = _coerce_int_setting(max_files, 5, "logfiles.max_files")
        max_size_int = _coerce_int_setting(max_size_val, 10, "logfiles.size_val")
        Logger.configure_file_logging(bool(enable_files), str(log_dir), max_files_int, max_size_int, str(size_unit))
        configure_internal_diagnostics_logging(self.config_manager)
        if not bool(self.config_manager.get_setting("diagnostics", "save_last_prompt")):
            clear_prompt_snapshots(self.config_manager.config_dir)
        self._sync_extra_debug_logs_setting()

        # Logging levels
        stdout_lvl = self.config_manager.get_setting("system_settings", "stdout_log_level") or "Debug"
        console_lvl = self.config_manager.get_setting("system_settings", "console_log_level") or "Debug"
        mini_lvl = self.config_manager.get_setting("system_settings", "mini_console_log_level") or "Success"
        file_lvl = self.config_manager.get_setting("system_settings", "logfile_log_level") or "Debug"
        if mini_lvl not in LEVEL_NAME_MAP:
            mini_lvl = "Success"

        Logger.set_stdout_level(LEVEL_NAME_MAP.get(stdout_lvl, LogLevel.DEBUG))
        Logger.set_file_level(LEVEL_NAME_MAP.get(file_lvl, LogLevel.DEBUG))
        self._console_log_level = LEVEL_NAME_MAP.get(console_lvl, LogLevel.DEBUG)
        self._mini_console_log_level = LEVEL_NAME_MAP.get(mini_lvl, LogLevel.SUCCESS)
        if hasattr(self, "mini_console"):
            self.mini_console.set_logging_level(mini_lvl)

    def _on_mini_console_log_level_selected(self, level_name: str):
        level_name = str(level_name or "").strip()
        if level_name not in LEVEL_NAME_MAP:
            return

        current_level = (
            self.config_manager.get_setting("system_settings", "mini_console_log_level")
            or "Success"
        )
        if level_name == current_level:
            self.mini_console.set_logging_level(level_name)
            return

        self.config_manager.set_setting("system_settings", "mini_console_log_level", level_name)
        self.config_manager.save_settings()
        self._setup_logging()
        self._sync_settings_window_mini_console_log_level(level_name)

    def _sync_settings_window_mini_console_log_level(self, level_name: str):
        settings_window = getattr(self, "settings_window", None)
        if not settings_window:
            return

        field_widgets = getattr(settings_window, "field_widgets", {})
        widget = field_widgets.get("system_settings.mini_console_log_level")
        if widget is None or not hasattr(widget, "setCurrentText"):
            return

        previous_state = widget.blockSignals(True)
        try:
            widget.setCurrentText(level_name)
        finally:
            widget.blockSignals(previous_state)

        update_dirty_markers = getattr(settings_window, "_update_dirty_markers", None)
        if callable(update_dirty_markers):
            update_dirty_markers()
    
    def _show_console(self):
        """Show the console window."""
        if not self.console_window:
            self.console_window = ConsoleWindow(self.config_manager)
        
        self.console_window.show()
        Logger.info("Console initialized.")
    
    def _hide_console(self):
        """Hide the console window."""
        if self.console_window:
            self.console_window.force_close()
            self.console_window = None
    
    def _on_log_message(self, level: LogLevel, message: str):
        """Callback for logger to send messages to console."""
        if self.console_window and Logger.should_log(level, self._console_log_level):
            self.console_window.append_log(level.value, message)

        if self._main_logging_enabled and Logger.should_log(level, self._mini_console_log_level):
            self.mini_console.add_log(level, message)
    
    def _update_status(self, text: str, status_type: str = "info"):
        """Update the status label with appropriate styling."""
        def _to_status_bar_text(raw: str, level: str) -> str:
            raw_text = str(raw or "")
            is_multiline = ("\n" in raw_text) or ("\r" in raw_text)
            one_liner = " ".join(raw_text.split())

            max_len = 80
            if level == "error":
                if is_multiline or ("Call log:" in raw_text) or ("Traceback" in raw_text) or (len(one_liner) > max_len):
                    return "Unexpected Error"
                return one_liner

            if len(one_liner) > max_len:
                return one_liner[: max_len - 3].rstrip() + "..."
            return one_liner

        status_type = str(status_type or "info").strip().lower()
        color_map = {
            "ready": BrandColors.SUCCESS,
            "running": BrandColors.ACCENT,
            "warning": BrandColors.WARNING,
            "error": BrandColors.DANGER,
            "info": BrandColors.TEXT_SECONDARY,
        }
        color = color_map.get(status_type, BrandColors.TEXT_SECONDARY)
        
        # Add status indicator dot
        dot = "●" if status_type in ["ready", "running"] else "○"
        
        self.status_label.setText(f"{dot} {_to_status_bar_text(text, status_type)}")
        self.status_label.setStyleSheet(f"""
            font-size: {BrandColors.FONT_SIZE_LARGE};
            font-weight: bold;
            color: {color};
            padding: 8px;
            background-color: {BrandColors.SIDEBAR_BG};
            border-radius: 6px;
        """)

    def _on_splitter_moved(self, pos: int, index: int):
        if not getattr(self, "queue_preview", None):
            return
        if not self.queue_preview.isVisible():
            return

        try:
            sizes = self.splitter.sizes()
            if len(sizes) >= 2:
                self._queue_preview_last_width = max(self._queue_preview_min_width, int(sizes[0]))
        except Exception:
            pass

    def _rebuild_splitter(self, include_queue_preview: bool) -> None:
        outer_layout = getattr(self, "_outer_layout", None)
        if outer_layout is None:
            return

        old_splitter = getattr(self, "splitter", None)
        if old_splitter is not None:
            # Detach widgets we keep so deleting the splitter does not delete them
            for widget in (getattr(self, "main_panel", None), getattr(self, "queue_preview", None)):
                if widget is not None and widget.parent() is old_splitter:
                    widget.setParent(self.central_widget)

            outer_layout.removeWidget(old_splitter)
            old_splitter.setParent(None)
            old_splitter.deleteLater()

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(self._queue_preview_handle_width if include_queue_preview else 0)
        splitter.setStyleSheet(f"QSplitter::handle {{ background-color: {BrandColors.WINDOW_BG}; }}")
        splitter.splitterMoved.connect(self._on_splitter_moved)
        outer_layout.addWidget(splitter)
        self.splitter = splitter

        if include_queue_preview:
            splitter.addWidget(self.queue_preview)
            splitter.addWidget(self.main_panel)
            splitter.setCollapsible(0, False)
            splitter.setCollapsible(1, False)
            return

        splitter.addWidget(self.main_panel)
        splitter.setCollapsible(0, False)

    def _build_title_area(self):
        """Build (or rebuild) the title area inside self._title_widget."""
        version = get_version()
        old_layout = self._title_widget.layout()
        if old_layout is not None:
            while old_layout.count():
                item = old_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
                elif item.layout():
                    while item.layout().count():
                        child = item.layout().takeAt(0)
                        if child.widget():
                            child.widget().deleteLater()
                    item.layout().deleteLater()
            QWidget().setLayout(old_layout)

        classic = self.config_manager.get_setting("experimental", "classic_title")
        if classic:
            layout = QHBoxLayout(self._title_widget)
            layout.setContentsMargins(0, 0, 0, 0)
            title_label = QLabel(f"Welcome to IntenseRP Next (v{version})!")
            title_label.setStyleSheet(f"""
                font-size: {BrandColors.FONT_SIZE_TITLE};
                font-weight: bold;
                color: {BrandColors.TEXT_PRIMARY};
            """)
            title_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(title_label)
        else:
            layout = QHBoxLayout(self._title_widget)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(10)

            layout.addStretch()

            logo_path = os.path.join(os.path.dirname(__file__), "ui", "assets", "brand", "newlogo-nobg.png")
            logo_label = QLabel()
            logo_pixmap = QPixmap(logo_path)
            if not logo_pixmap.isNull():
                scaled = logo_pixmap.scaled(80, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                scaled.setDevicePixelRatio(2.0)
                logo_label.setPixmap(scaled)
            logo_label.setFixedSize(40, 40)
            logo_label.setStyleSheet("background: transparent;")
            layout.addWidget(logo_label, alignment=Qt.AlignVCenter)

            title_label = QLabel()
            title_label.setTextFormat(Qt.RichText)
            title_label.setText(
                f'<span style="font-size: 28px; font-weight: bold; color: {BrandColors.TEXT_PRIMARY};">IntenseRP </span>'
                f'<span style="font-size: 28px; font-weight: bold; color: {BrandColors.ACCENT};">Next</span>'
            )
            title_label.setStyleSheet("background: transparent;")
            layout.addWidget(title_label, alignment=Qt.AlignVCenter)

            version_label = QLabel(f"v{version}")
            version_label.setStyleSheet(f"""
                font-size: {BrandColors.FONT_SIZE_SMALL};
                font-weight: bold;
                color: {BrandColors.TEXT_SECONDARY};
                background: transparent;
                padding-top: 4px;
            """)
            layout.addWidget(version_label, alignment=Qt.AlignVCenter)

            layout.addStretch()

    def _apply_queue_preview_setting(self, force: bool = False):
        enabled = bool(self.config_manager.get_setting("system_settings", "show_request_queue_preview"))

        if (not force) and enabled == getattr(self, "_queue_preview_enabled", False):
            return

        was_visible = bool(getattr(self, "queue_preview", None) and self.queue_preview.isVisible())
        self._queue_preview_enabled = enabled

        if enabled:
            desired_width = max(int(getattr(self, "_queue_preview_last_width", 360) or 360), self._queue_preview_min_width)
            if not was_visible:
                self.resize(self.width() + desired_width, self.height())

            if self.splitter.indexOf(self.queue_preview) == -1:
                self.splitter.insertWidget(0, self.queue_preview)
                self.splitter.setCollapsible(0, False)
                self.splitter.setCollapsible(1, False)

            self.queue_preview.setMaximumWidth(16777215)
            self.queue_preview.setMinimumWidth(self._queue_preview_min_width)
            self.queue_preview.setVisible(True)
            self.splitter.setHandleWidth(self._queue_preview_handle_width)
            self._queue_preview_last_rendered = None
            self._schedule_queue_preview_refresh()

            def apply_sizes():
                if not self.queue_preview.isVisible():
                    return
                total = sum(self.splitter.sizes())
                main_width = max(0, total - desired_width)
                self.splitter.setSizes([desired_width, main_width])

            QTimer.singleShot(0, apply_sizes)
            return

        # Disabled
        try:
            sizes = self.splitter.sizes()
            if self.queue_preview.isVisible() and len(sizes) >= 2:
                self._queue_preview_last_width = max(self._queue_preview_min_width, int(sizes[0]))
        except Exception:
            pass

        self.queue_preview.setVisible(False)
        self.queue_preview.setMinimumWidth(0)
        self.queue_preview.setMaximumWidth(0)
        self._queue_preview_refresh_pending = False
        self._queue_preview_refresh_posted = False
        self._queue_preview_last_rendered = None

        # Rebuild the splitter without the preview widget to avoid stale size/minimum constraints
        # keeping the main window wide after the panel is disabled
        self._rebuild_splitter(include_queue_preview=False)

        # Clear any layout-driven minimums that may have been raised while the preview was present
        try:
            self.central_widget.setMinimumSize(0, 0)
            self.splitter.setMinimumSize(0, 0)
        except Exception:
            pass

        QTimer.singleShot(
            0,
            lambda: self.resize(self.DEFAULT_WINDOW_WIDTH, self.DEFAULT_WINDOW_HEIGHT),
        )

    def _set_queue_preview_api(self, api: API | None) -> None:
        listener = getattr(self, "_queue_preview_state_listener", None)
        attached_api = getattr(self, "_queue_preview_listener_api", None)
        if attached_api is api:
            return

        if attached_api is not None and listener is not None:
            try:
                attached_api.remove_queue_state_listener(listener)
            except Exception:
                pass

        self._queue_preview_listener_api = api
        if api is not None and listener is not None:
            try:
                api.add_queue_state_listener(listener)
            except Exception as e:
                Logger.debug(f"Queue preview: Failed to attach queue state listener: {e}")

        self._schedule_queue_preview_refresh()

    def _on_queue_preview_state_changed(self) -> None:
        if getattr(self, "_queue_preview_refresh_posted", False):
            return

        self._queue_preview_refresh_posted = True
        QTimer.singleShot(0, self._flush_queue_preview_refresh_request)

    def _flush_queue_preview_refresh_request(self) -> None:
        self._queue_preview_refresh_posted = False
        self._schedule_queue_preview_refresh()

    def _schedule_queue_preview_refresh(self):
        if not getattr(self, "_queue_preview_enabled", False):
            return
        if not getattr(self, "queue_preview", None) or not self.queue_preview.isVisible():
            return

        self._queue_preview_refresh_pending = True
        if getattr(self, "_queue_preview_refresh_inflight", False):
            return

        self._queue_preview_refresh_inflight = True
        asyncio.create_task(self._refresh_queue_preview())

    async def _refresh_queue_preview(self):
        try:
            while True:
                self._queue_preview_refresh_pending = False

                if not getattr(self, "_queue_preview_enabled", False):
                    return
                if not getattr(self, "queue_preview", None) or not self.queue_preview.isVisible():
                    return

                api = getattr(self, "api", None)
                entries = await api.snapshot_requests() if api is not None else []

                rendered = []
                for idx, (status, entry) in enumerate(entries, start=1):
                    req = getattr(entry, "request", None)
                    request_type = str(getattr(entry, "request_type", "chat") or "chat").strip().lower()
                    target_provider = getattr(entry, "target_provider", None)
                    provider_name = (
                        target_provider.value
                        if isinstance(target_provider, DriverProvider)
                        else "Unknown"
                    )
                    try:
                        if request_type == "text":
                            prompt_value = getattr(req, "prompt", "") if req else ""
                            if isinstance(prompt_value, list):
                                prompt_value = prompt_value[0] if prompt_value else ""
                            msg_count = 1 if str(prompt_value or "") else 0
                            prompt_length = len(str(prompt_value or ""))
                        else:
                            msg_count = len(getattr(req, "messages", []) or [])
                            prompt_length = 0
                    except Exception:
                        msg_count = 0
                        prompt_length = 0

                    rendered.append(
                        {
                            "position": idx,
                            "id": getattr(entry, "id", ""),
                            "queued_at": getattr(entry, "queued_at", 0.0),
                            "status": status,
                            "request_type": request_type,
                            "provider": provider_name,
                            "slot_label": getattr(entry, "target_slot_label", None),
                            "message_count": msg_count,
                            "prompt_length": prompt_length,
                            "api_key_name": getattr(entry, "api_key_name", None),
                            "model": getattr(req, "model", "") if req else "",
                            "stream": bool(getattr(req, "stream", False)) if req else False,
                        }
                    )

                if getattr(self, "_queue_preview_last_rendered", None) != rendered:
                    self._queue_preview_last_rendered = rendered
                    self.queue_preview.set_requests(rendered)

                if not getattr(self, "_queue_preview_refresh_pending", False):
                    return
        finally:
            self._queue_preview_refresh_inflight = False
            if (
                getattr(self, "_queue_preview_refresh_pending", False)
                and getattr(self, "_queue_preview_enabled", False)
                and getattr(self, "queue_preview", None)
                and self.queue_preview.isVisible()
            ):
                self._schedule_queue_preview_refresh()

    def _on_queue_preview_stop_requested(self) -> None:
        asyncio.create_task(self._abort_queue_current_request())

    def _on_queue_preview_clear_requested(self) -> None:
        asyncio.create_task(self._cancel_queue_after_current())

    def _on_queue_preview_request_action_requested(self, request_id: str) -> None:
        asyncio.create_task(self._cancel_queue_request(request_id))

    async def _abort_queue_current_request(self) -> None:
        api = getattr(self, "api", None)
        if api is None:
            return

        try:
            aborted = await api.abort_current_request(reason="Request aborted from UI.")
            if aborted:
                Logger.warning("Queue: Current request aborted from UI.")
        except Exception as e:
            Logger.error(f"Queue: Failed to abort current request: {e}")

        self._schedule_queue_preview_refresh()

    async def _cancel_queue_after_current(self) -> None:
        api = getattr(self, "api", None)
        if api is None:
            return

        try:
            cancelled = await api.cancel_queued_requests(reason="Request queue cleared from UI.")
            if cancelled:
                Logger.warning(f"Queue: Cancelled {cancelled} queued request(s) from UI.")
        except Exception as e:
            Logger.error(f"Queue: Failed to clear request queue: {e}")

        self._schedule_queue_preview_refresh()

    async def _cancel_queue_request(self, request_id: str) -> None:
        normalized_id = str(request_id or "").strip()
        if not normalized_id:
            return

        api = getattr(self, "api", None)
        if api is None:
            return

        try:
            cancelled = await api.cancel_request(
                normalized_id,
                reason=f"Request {normalized_id} cancelled from UI.",
            )
            if cancelled:
                Logger.warning(f"Queue: Cancelled request {normalized_id} from UI.")
        except Exception as e:
            Logger.error(f"Queue: Failed to cancel request {normalized_id}: {e}")

        self._schedule_queue_preview_refresh()

    async def _wait_for_provider_queue_head_to_clear(
        self,
        provider: DriverProvider,
        timeout_s: float = 5.0,
    ) -> bool:
        api = getattr(self, "api", None)
        if api is None:
            return True

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(float(timeout_s), 0.0)
        while True:
            is_processing = getattr(api, "is_provider_processing", None)
            if callable(is_processing):
                try:
                    provider_busy = bool(is_processing(provider))
                except Exception:
                    provider_busy = False
            else:
                provider_busy = False
                current_entries = getattr(api, "current_entries_by_slot_id", None) or {}
                get_slots = getattr(api, "_get_execution_slots_for_provider", None)
                if callable(get_slots):
                    try:
                        provider_busy = any(
                            getattr(slot, "id", None) in current_entries
                            for slot in get_slots(provider)
                        )
                    except Exception:
                        provider_busy = False
            if not provider_busy:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.1)

    def _set_settings_button_loading(self, loading: bool) -> None:
        loading = bool(loading)
        if self._settings_button_loading == loading:
            return

        self._settings_button_loading = loading
        if loading:
            self.settings_button.setEnabled(False)
            self.settings_button.setCursor(Qt.ArrowCursor)
            self.settings_button.setText("Loading")
            self.settings_button.setIcon(QIcon())
            self._settings_button_loading_timeout.start()
            return

        self._settings_button_loading_timeout.stop()
        self.settings_button.setText("Settings")
        self.settings_button.setEnabled(True)
        self.settings_button.setCursor(Qt.PointingHandCursor)
        self.settings_button.setIconSize(QSize(16, 16))
        IconUtils.apply_icon(self.settings_button, IconType.SETTINGS, BrandColors.TEXT_PRIMARY)

    def _on_settings_button_loading_timeout(self) -> None:
        if not self._settings_button_loading:
            return
        Logger.warning("Settings button loading state timed out. Resetting button state.")
        self._set_settings_button_loading(False)

    def _finish_open_settings(self) -> None:
        if not self._settings_button_loading:
            return

        try:
            if not self.settings_window:
                # Pass None as parent to make it a top-level window with its own taskbar icon
                self.settings_window = SettingsWindow(self.config_manager, None)
                self.settings_window.settings_saved.connect(self.on_settings_saved)
                self.settings_window.restart_requested.connect(self.on_restart_requested)
            elif not self.settings_window.isVisible():
                self.settings_window.refresh_from_config()
            self.settings_window.present()
        except Exception as exc:
            Logger.error(f"Failed to open Settings: {exc}")
            QMessageBox.warning(
                self,
                "Settings",
                "Failed to open the Settings window.\n\n"
                f"{exc}",
            )
        finally:
            self._set_settings_button_loading(False)

    def open_settings(self):
        if self._settings_button_loading:
            return

        if self.settings_window and self.settings_window.isVisible():
            self.settings_window.present()
            return

        self._set_settings_button_loading(True)
        QApplication.processEvents()
        QTimer.singleShot(60, self._finish_open_settings)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._schedule_queue_preview_refresh)

    def open_help(self):
        if not self.help_window:
            self.help_window = HelpWindow(self.config_manager, None, main_window=self)
            self.help_window.settings_reloaded.connect(self.on_settings_reloaded)
        self.help_window.show()
        self.help_window.activateWindow()

    def open_browser_manager(self) -> None:
        self.open_help()
        help_window = getattr(self, "help_window", None)
        if help_window is not None:
            help_window.show_browser_manager()

    def _open_docs_home(self) -> bool:
        return bool(QDesktopServices.openUrl(QUrl(DOCS_BASE_URL)))

    def _handle_global_docs_shortcut(self) -> bool:
        active_window = QApplication.activeWindow()
        if isinstance(active_window, SettingsWindow):
            try:
                if active_window.open_docs_for_shortcut():
                    return True
            except Exception:
                pass
        return self._open_docs_home()

    def eventFilter(self, watched, event):
        if (
            event is not None
            and event.type() == QEvent.KeyPress
            and event.key() == Qt.Key_F1
            and event.modifiers() == Qt.NoModifier
            and (not event.isAutoRepeat())
        ):
            if self._handle_global_docs_shortcut():
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def on_settings_saved(self, affected=None):
        affected = affected or set()
        Logger.info("Settings saved.")
        sync_animations_disabled_from_config(self.config_manager)
        # Handle logging toggle
        self._setup_logging()
        self._apply_queue_preview_setting()

        # Update console settings if it exists
        # Rule 43 of The Internet: If it exists, then it exists
        if self.console_window:
            self.console_window.apply_settings()

        # Refresh affected UI components
        if "chevron_dropdown" in affected and self.start_button.text() == "Stop":
            self._refresh_chevron_menu()
        elif self._loadouts_feature_enabled() and self.start_button.text() == "Stop":
            self._refresh_chevron_menu()

        if "hotswap_button" in affected:
            self._sync_hotswap_button()

        if "news_button" in affected:
            self._sync_news_button()

        if "title_bar" in affected:
            self._build_title_area()

        runtime_providers = [provider for provider, _driver in self._iter_runtime_drivers()]
        if self._loadouts_feature_enabled():
            try:
                self.config_manager.prepare_runtime_loadouts(required_providers=runtime_providers)
            except Exception as exc:
                Logger.warning(f"Loadouts refresh after settings save failed: {exc}")
        else:
            self.config_manager.clear_runtime_loadouts()

        # If driver is running, it will pick up changes on next generation
        # All thanks to the config manager being dynamic

    # ------------------------------------------------------------------
    # Chevron dropdown menu + handlers
    # ------------------------------------------------------------------

    def _refresh_chevron_menu(self):
        menu = self.start_button.menu
        menu.clear()

        restart_action = menu.addAction("Restart")
        restart_action.triggered.connect(self._on_restart_services)

        account_action = menu.addAction("Switch Account")
        account_action.triggered.connect(self._on_account_switch)

        # Disable if fewer than 2 accounts for the current provider
        if not self._can_switch_account():
            account_action.setEnabled(False)

        if self._loadouts_feature_enabled():
            loadout_action = menu.addAction("Switch Loadout")
            loadout_action.triggered.connect(self._on_switch_loadout)

    def _on_restart_services(self):
        asyncio.create_task(self._restart_services_impl())

    async def _restart_services_impl(self):
        self.start_button.setEnabled(False)
        self._update_status("Restarting...", "info")
        self._update_tray_menu_state()
        try:
            await self.stop_services()
        except Exception as e:
            Logger.error(f"Restart: stop failed: {e}")

        # stop_services resets the UI; keep the app in a "busy" state while restarting
        self.start_button.setEnabled(False)
        self._update_status("Restarting...", "info")
        self._update_tray_menu_state()

        try:
            await self.start_services()
        finally:
            self._update_tray_menu_state()

    def _normalize_runtime_provider_list(self, providers) -> list[DriverProvider]:
        normalized: list[DriverProvider] = []
        for raw_provider in providers or []:
            provider = (
                raw_provider
                if isinstance(raw_provider, DriverProvider)
                else DriverProvider.from_setting(raw_provider)
            )
            if provider is not None and provider not in normalized:
                normalized.append(provider)
        return normalized

    def _running_status_text(self) -> str:
        port_setting = self.config_manager.get_setting("network_settings", "port")
        try:
            port = int(port_setting) if port_setting else 7777
        except (TypeError, ValueError):
            port = 7777
        return f"Running (Port {port})"

    def _dry_run_mode_enabled(self) -> bool:
        try:
            return bool(self.config_manager.get_setting("network_settings", "dry_run_mode"))
        except Exception:
            return False

    def _dry_run_status_text(self, port: int | None = None) -> str:
        if port is None:
            port_setting = self.config_manager.get_setting("network_settings", "port")
            try:
                port = int(port_setting) if port_setting else 7777
            except (TypeError, ValueError):
                port = 7777
        return f"Dry Run (Port {port})"

    def _emit_dry_run_capture(self, capture: DryRunCapture) -> None:
        self.dry_run_capture_received.emit(capture)

    def _show_dry_run_window(self) -> None:
        window = getattr(self, "dry_run_window", None)
        if window is None:
            window = DryRunWindow(None)
            window.stop_requested.connect(self._on_dry_run_window_stop_requested)
            self.dry_run_window = window

        window.show_waiting()
        window.present()

    def _close_dry_run_window(self) -> None:
        window = getattr(self, "dry_run_window", None)
        if window is None:
            return

        try:
            window.force_close()
        except Exception:
            try:
                window.close()
            except Exception:
                pass
        finally:
            self.dry_run_window = None

    def _on_dry_run_window_stop_requested(self) -> None:
        if getattr(self, "_stop_task", None) is not None:
            return
        asyncio.create_task(self.stop_services())

    def _on_dry_run_capture_received(self, capture: DryRunCapture) -> None:
        window = getattr(self, "dry_run_window", None)
        if window is None:
            window = DryRunWindow(None)
            window.stop_requested.connect(self._on_dry_run_window_stop_requested)
            self.dry_run_window = window

        window.set_capture(capture)
        window.present()

    def _reset_start_controls_to_idle(self) -> None:
        self.start_button.setText("Start")
        self.start_button.apply_icon(IconType.START, BrandColors.TEXT_PRIMARY)
        self.start_button.setEnabled(True)
        self.start_button.set_chevron_visible(False)
        self._sync_hotswap_button()
        self._update_tray_menu_state()

    def _show_provider_locked_notice(self, provider: DriverProvider) -> None:
        reason = provider_lock_reason(provider) or f"{provider.value} is currently locked."
        Logger.warning(f"Provider locked: {reason}")
        self._update_status(f"{provider.value} is locked", "warning")
        self._notify_user("Provider Locked", reason, level="warning")

    async def _restart_runtime_providers_impl(
        self,
        providers,
        *,
        reason: str,
        allow_full_fallback: bool = True,
    ) -> bool:
        target_providers = self._normalize_runtime_provider_list(providers)
        if not target_providers:
            return True

        if not self._are_services_running():
            return True

        runtime_entries = [
            (runtime_provider, runtime_driver)
            for runtime_provider, runtime_driver in self._iter_runtime_drivers()
            if runtime_provider in target_providers and runtime_driver is not None
        ]
        covered_providers = {runtime_provider for runtime_provider, _driver in runtime_entries}
        missing_providers = [
            provider for provider in target_providers if provider not in covered_providers
        ]
        if missing_providers:
            missing_text = ", ".join(provider.value for provider in missing_providers)
            if not allow_full_fallback:
                Logger.warning(f"Selective restart unavailable for {missing_text}.")
                return False
            Logger.warning(
                f"Selective restart unavailable for {missing_text}; falling back to full restart."
            )
            await self._restart_services_impl()
            return True

        runtime_providers = [
            runtime_provider for runtime_provider, _driver in self._iter_runtime_drivers()
        ]
        if self._loadouts_feature_enabled() and not self._validate_runtime_loadouts(
            providers=runtime_providers,
        ):
            self._update_status("Loadouts validation failed", "error")
            return False

        provider_text = ", ".join(provider.value for provider in target_providers)
        self.start_button.setEnabled(False)
        self._update_status(f"Restarting {provider_text}...", "info")
        self._update_tray_menu_state()

        try:
            for _provider, runtime_driver in runtime_entries:
                try:
                    runtime_driver.is_running = False
                except Exception:
                    pass

            api = getattr(self, "api", None)
            if api is not None:
                cancel_queued_for_provider = getattr(
                    api,
                    "cancel_queued_requests_for_provider",
                    None,
                )
                for provider in target_providers:
                    if callable(cancel_queued_for_provider):
                        cancelled = await cancel_queued_for_provider(
                            provider,
                            reason=(
                                f"Queued {provider.value} request cancelled due to "
                                f"{reason}."
                            ),
                        )
                        if cancelled:
                            Logger.warning(
                                f"Queue: Cancelled {cancelled} queued {provider.value} "
                                f"request(s) before {reason}."
                            )

                    aborted = await api.abort_current_request_for_provider(
                        provider,
                        reason=(
                            f"Current {provider.value} request aborted due to {reason}."
                        ),
                    )
                    if aborted:
                        Logger.warning(
                            f"Queue: Aborted current {provider.value} request before {reason}."
                        )
                        cleared = await self._wait_for_provider_queue_head_to_clear(
                            provider,
                            timeout_s=5.0,
                        )
                        if not cleared:
                            Logger.warning(
                                f"Queue: Timed out waiting for {provider.value} request "
                                f"to clear before {reason}."
                            )

            for provider, runtime_driver in runtime_entries:
                label = getattr(runtime_driver, "provider_label", None) or provider.value
                Logger.info(f"Selective restart: closing {label}...")
                await asyncio.wait_for(runtime_driver.close(), timeout=20.0)

                Logger.info(f"Selective restart: starting {label}...")
                await runtime_driver.start(
                    status_callback=(
                        lambda msg, current_label=label: self._update_status(
                            f"{current_label}: {msg}",
                            "info",
                        )
                    )
                )
                if not await self._ensure_driver_ui_language_is_compatible(runtime_driver):
                    raise RuntimeError(
                        f"{label} UI language is not compatible after restart."
                    )

            Logger.success(f"Selective restart completed for {provider_text}.")
            self._update_status(self._running_status_text(), "running")
            self._schedule_queue_preview_refresh()
            return True
        except Exception as exc:
            Logger.error(f"Selective restart failed for {provider_text}: {exc}")
            for provider, runtime_driver in runtime_entries:
                if bool(getattr(runtime_driver, "is_running", False)):
                    continue
                try:
                    await asyncio.wait_for(runtime_driver.close(), timeout=20.0)
                except Exception as close_error:
                    Logger.debug(
                        f"Selective restart cleanup failed for {provider.value}: {close_error}"
                    )
            self._update_status(f"Provider restart failed: {exc}", "error")
            return False
        finally:
            self.start_button.setEnabled(True)
            self._refresh_chevron_menu()
            self._sync_hotswap_button()
            self._update_tray_menu_state()

    def _on_account_switch(self):
        asyncio.create_task(self._account_switch_impl())

    def _on_switch_loadout(self):
        provider = get_current_provider(self.config_manager)
        runtime_providers = []
        for runtime_provider, _driver in self._iter_runtime_drivers():
            if runtime_provider not in runtime_providers:
                runtime_providers.append(runtime_provider)
        parallel_switch = is_parallel_runtime_active(self.config_manager) and len(runtime_providers) >= 2

        if parallel_switch:
            provider_loadouts = {
                runtime_provider: self.config_manager.get_loadouts(runtime_provider)
                for runtime_provider in runtime_providers
            }
            provider_loadouts = {
                runtime_provider: available
                for runtime_provider, available in provider_loadouts.items()
                if available
            }
            if not provider_loadouts:
                QMessageBox.information(
                    self,
                    "Loadouts",
                    "No loadouts are available for the active parallel providers yet.",
                )
                return

            initial_provider = (
                provider
                if provider in provider_loadouts
                else next(iter(provider_loadouts))
            )
            current_names = {
                runtime_provider: self.config_manager.get_runtime_active_loadout_name(runtime_provider)
                for runtime_provider in provider_loadouts
            }
            dialog = LoadoutSwitchDialog(
                initial_provider.value,
                provider_loadouts.get(initial_provider, []),
                current_names.get(initial_provider),
                parent=self,
                provider_loadouts=provider_loadouts,
                current_loadout_names=current_names,
                initial_provider=initial_provider,
            )
            if dialog.exec() != LoadoutSwitchDialog.Accepted:
                return

            selected_names = dialog.selected_loadout_names_by_provider
            changes = {
                runtime_provider: selected_name
                for runtime_provider, selected_name in selected_names.items()
                if selected_name and selected_name != current_names.get(runtime_provider)
            }
            if not changes:
                return

            try:
                for runtime_provider, selected_name in changes.items():
                    self.config_manager.set_preferred_loadout_name(runtime_provider, selected_name)
            except Exception as exc:
                QMessageBox.warning(self, "Loadouts", f"Failed to switch loadout.\n\n{exc}")
                return

            if len(changes) == 1:
                changed_provider, selected_name = next(iter(changes.items()))
                Logger.info(f"Loadouts: selected '{selected_name}' for {changed_provider.value}.")
            else:
                changed_names = ", ".join(
                    f"{runtime_provider.value}: {selected_name}"
                    for runtime_provider, selected_name in changes.items()
                )
                Logger.info(f"Loadouts: selected parallel loadouts ({changed_names}).")

            asyncio.create_task(
                self._restart_runtime_providers_impl(
                    list(changes.keys()),
                    reason="loadout switch",
                )
            )
            return

        available = self.config_manager.get_loadouts(provider)
        if not available:
            QMessageBox.information(
                self,
                "Loadouts",
                f"No loadouts are available for {provider.value} yet.",
            )
            return

        if self._are_services_running():
            current_name = self.config_manager.get_runtime_active_loadout_name(provider)
        else:
            current_name = self.config_manager.get_preferred_loadout_name(provider, available)

        dialog = LoadoutSwitchDialog(provider.value, available, current_name, parent=self)
        if dialog.exec() != LoadoutSwitchDialog.Accepted:
            return

        selected_name = dialog.selected_loadout_name
        if not selected_name or selected_name == current_name:
            return

        try:
            self.config_manager.set_preferred_loadout_name(provider, selected_name)
        except Exception as exc:
            QMessageBox.warning(self, "Loadouts", f"Failed to switch loadout.\n\n{exc}")
            return

        Logger.info(f"Loadouts: selected '{selected_name}' for {provider.value}.")
        if self._are_services_running():
            asyncio.create_task(
                self._restart_runtime_providers_impl(
                    [provider],
                    reason="loadout switch",
                )
            )
        else:
            self._update_status(f"Selected loadout: {selected_name}", "info")

    async def _account_switch_impl(self):
        self.start_button.setEnabled(False)
        self._update_status("Switching account...", "info")
        driver = self._get_current_runtime_driver()
        if not driver:
            Logger.warning("No driver available for account switch.")
            self.start_button.setEnabled(True)
            return
        provider = getattr(driver, "provider", None)
        if isinstance(provider, DriverProvider) and (not self._validate_runtime_loadouts(providers=[provider])):
            self.start_button.setEnabled(True)
            self._update_status("Loadouts validation failed", "error")
            return
        try:
            api = getattr(self, "api", None)
            if api is not None and isinstance(provider, DriverProvider):
                aborted = await api.abort_current_request_for_provider(
                    provider,
                    reason="Request aborted due to manual account switch.",
                )
                if aborted:
                    Logger.warning(
                        f"Queue: Aborted current {provider.value} request before account switch."
                    )
                    cleared = await self._wait_for_provider_queue_head_to_clear(provider, timeout_s=5.0)
                    if not cleared:
                        Logger.warning(
                            f"Queue: Timed out waiting for {provider.value} request to clear before account switch."
                        )
            success = await driver.ece_restart_with_rotation(
                reason="manual account switch",
                status_callback=lambda msg: self._update_status(msg, "info"),
            )
            if success:
                port_setting = self.config_manager.get_setting("network_settings", "port")
                try:
                    port = int(port_setting) if port_setting else 7777
                except (TypeError, ValueError):
                    port = 7777
                self._update_status(f"Running (Port {port})", "running")
                Logger.success("Account switch completed.")
            else:
                Logger.warning("Account switch failed (no alternative identity available).")
                self._update_status("Account switch failed", "warning")
            self.start_button.setEnabled(True)
        except Exception as e:
            Logger.error(f"Account switch failed: {e}")
            self._update_status(f"Account switch failed: {e}", "error")
            await self.stop_services()

    # ------------------------------------------------------------------
    # News button
    # ------------------------------------------------------------------

    def _refresh_news_state(self) -> None:
        self._news_unread = has_unviewed_news(self.config_manager.config_dir)

    def _sync_news_button(self) -> None:
        enabled = self.config_manager.get_setting("experimental", "changelog_button")
        show = True if enabled is None else bool(enabled)

        self.news_button.setVisible(show)
        self.news_button.set_badge_visible(show and self._news_unread)
        if show and self._news_unread:
            self.news_button.setToolTip("Open News (new items available)")
        else:
            self.news_button.setToolTip("Open News")

    def _open_news_page(self) -> None:
        QDesktopServices.openUrl(QUrl(NEWS_DOCS_URL))
        if mark_latest_news_viewed(self.config_manager.config_dir):
            self._refresh_news_state()
            self._sync_news_button()

    # ------------------------------------------------------------------
    # Hotswap
    # ------------------------------------------------------------------

    def _sync_hotswap_button(self):
        """Show or hide the discrete hotswap button based on setting + running state."""
        mode = self.config_manager.get_setting("application_settings", "hotswap_experience")
        running = self.start_button.text() == "Stop"
        dry_run_active = isinstance(getattr(self, "driver", None), DryRunDriver)
        show = (not dry_run_active) and (
            (mode == "Persistent Discrete") or ((mode == "Discrete") and running)
        )

        self.hotswap_button.setVisible(show)
        if show:
            provider = self.config_manager.get_setting("providers_credentials", "provider") or "DeepSeek"
            icon_file = PROVIDER_ICON_MAP.get(provider)
            if icon_file:
                icon = IconUtils.get_icon(
                    icon_file, color=BrandColors.TEXT_PRIMARY, size=18,
                    widget=self.hotswap_button,
                )
                if not icon.isNull():
                    self.hotswap_button.setIcon(icon)
                    self.hotswap_button.setIconSize(QSize(18, 18))

    def _on_hotswap(self):
        current = self.config_manager.get_setting("providers_credentials", "provider") or "DeepSeek"
        dialog = HotswapDialog(
            current,
            parent=self,
            providers=self._get_hotswap_targets(),
        )
        if dialog.exec() != HotswapDialog.Accepted:
            return
        new_provider = dialog.selected_provider
        if not new_provider or new_provider == current:
            return

        asyncio.create_task(self._hotswap_to_provider_impl(new_provider))

    async def _hotswap_to_provider_impl(self, new_provider: str) -> None:
        current = self.config_manager.get_setting("providers_credentials", "provider") or "DeepSeek"
        normalized_provider = DriverProvider.from_setting(new_provider)
        if normalized_provider is None:
            raise ValueError(f"Unknown provider: {new_provider}")
        if is_provider_locked(normalized_provider, self.config_manager):
            self._show_provider_locked_notice(normalized_provider)
            return

        target_provider = normalized_provider.value
        if target_provider == current:
            return

        Logger.info(f"Hotswap: {current} -> {target_provider}")
        self.config_manager.set_setting("providers_credentials", "provider", target_provider)
        self.config_manager.save_settings()
        running = self.start_button.text() == "Stop"
        if running:
            await self._restart_services_impl()
        else:
            self._sync_hotswap_button()

    async def _remote_stop_services(self) -> None:
        if not self._are_services_running():
            return

        self.start_button.setEnabled(False)
        self._update_status("Stopping...", "info")
        self._update_tray_menu_state()
        await self.stop_services()

    async def _remote_restart_services(self) -> None:
        if not self._are_services_running():
            return
        await self._restart_services_impl()

    async def _remote_switch_account(self) -> None:
        if not self._can_switch_account():
            return
        await self._account_switch_impl()

    async def _remote_hotswap(self, provider_name: str) -> None:
        await self._hotswap_to_provider_impl(provider_name)

    async def _remote_switch_loadout(self, selected_loadouts: dict[str, str]) -> None:
        if not self._loadouts_feature_enabled():
            raise RuntimeError("Loadouts are not enabled.")

        context = self._get_remote_loadout_switch_context()
        providers = context.get("providers")
        if not isinstance(providers, list) or not providers:
            raise RuntimeError("No loadouts are available right now.")

        providers_by_name = {
            str(provider_info.get("name") or ""): provider_info
            for provider_info in providers
            if isinstance(provider_info, dict)
        }
        changes: dict[DriverProvider, str] = {}

        for raw_provider, raw_loadout in (selected_loadouts or {}).items():
            provider = DriverProvider.from_setting(raw_provider)
            if provider is None:
                raise RuntimeError(f"Unknown provider: {raw_provider}")

            provider_info = providers_by_name.get(provider.value)
            if provider_info is None:
                raise RuntimeError(f"{provider.value} is not available for loadout switching.")

            selected_name = str(raw_loadout or "").strip()
            allowed_names = {
                str(item.get("name") or "")
                for item in (provider_info.get("options") or [])
                if isinstance(item, dict)
            }
            if selected_name not in allowed_names:
                raise RuntimeError(
                    f"Loadout '{selected_name}' is not available for {provider.value}."
                )

            current_name = str(provider_info.get("current_loadout") or "").strip()
            if selected_name != current_name:
                changes[provider] = selected_name

        if not changes:
            return

        for provider, selected_name in changes.items():
            self.config_manager.set_preferred_loadout_name(provider, selected_name)

        if len(changes) == 1:
            changed_provider, selected_name = next(iter(changes.items()))
            Logger.info(
                f"Remote Control: selected loadout '{selected_name}' for {changed_provider.value}."
            )
        else:
            changed_names = ", ".join(
                f"{provider.value}: {selected_name}"
                for provider, selected_name in changes.items()
            )
            Logger.info(f"Remote Control: selected parallel loadouts ({changed_names}).")

        restarted = await self._restart_runtime_providers_impl(
            list(changes.keys()),
            reason="remote loadout switch",
            allow_full_fallback=False,
        )
        if not restarted:
            raise RuntimeError("Loadout switch was saved, but provider restart failed.")

    async def _apply_remote_model_switch(
        self,
        provider: DriverProvider,
        runtime_driver: object,
        desired_model: str,
    ) -> None:
        desired = str(desired_model or "").strip()
        if not desired:
            raise RuntimeError("Model name cannot be empty.")

        if provider == DriverProvider.GLM_CHAT:
            apply_model = getattr(runtime_driver, "apply_configured_model", None)
            if not callable(apply_model):
                raise RuntimeError("GLM Chat does not expose model switching right now.")

            await apply_model(wait_until_ready=True)

            read_label = getattr(runtime_driver, "_read_current_glm_model_label", None)
            normalize_label = getattr(runtime_driver, "_normalize_model_label", None)
            if callable(read_label) and callable(normalize_label):
                current_label = str(await read_label() or "").strip()
                if normalize_label(current_label) != normalize_label(desired):
                    shown = current_label or "Unknown"
                    raise RuntimeError(
                        f"GLM Chat did not confirm the requested model switch (still showing '{shown}')."
                    )
            return

        if provider == DriverProvider.QWEN_LM:
            apply_model = getattr(runtime_driver, "apply_configured_model", None)
            if not callable(apply_model):
                raise RuntimeError("QwenLM does not expose model switching right now.")

            await apply_model()

            read_label = getattr(runtime_driver, "_read_current_qwen_model_label", None)
            canonicalize_label = getattr(runtime_driver, "_canonicalize_model_label", None)
            if callable(read_label) and callable(canonicalize_label):
                current_label = str(await read_label() or "").strip()
                if canonicalize_label(current_label) != canonicalize_label(desired):
                    shown = current_label or "Unknown"
                    raise RuntimeError(
                        f"QwenLM did not confirm the requested model switch (still showing '{shown}')."
                    )
            return

        if provider == DriverProvider.PERPLEXITY:
            apply_model = getattr(runtime_driver, "apply_configured_model", None)
            if not callable(apply_model):
                raise RuntimeError("Perplexity does not expose model switching right now.")

            await apply_model()

            read_label = getattr(runtime_driver, "_read_current_model_selection", None)
            canonicalize_label = getattr(runtime_driver, "_canonicalize_model_label", None)
            if callable(read_label) and callable(canonicalize_label):
                current_label = str(await read_label() or "").strip()
                if canonicalize_label(current_label) != canonicalize_label(desired):
                    shown = current_label or "Unknown"
                    raise RuntimeError(
                        f"Perplexity did not confirm the requested model switch (still showing '{shown}')."
                    )
            return

        if provider == DriverProvider.HUGGINGCHAT:
            apply_model = getattr(runtime_driver, "apply_configured_model", None)
            if not callable(apply_model):
                raise RuntimeError("HuggingChat does not expose model switching right now.")

            await apply_model()
            if desired == "Current HuggingChat selection":
                return

            read_info = getattr(runtime_driver, "_read_current_model_info", None)
            canonicalize_label = getattr(runtime_driver, "_canonicalize_model_id", None)
            if callable(read_info) and callable(canonicalize_label):
                current_info = await read_info()
                current_label = ""
                if isinstance(current_info, dict):
                    current_label = str(current_info.get("model") or "").strip()
                if canonicalize_label(current_label) != canonicalize_label(desired):
                    shown = current_label or "Unknown"
                    raise RuntimeError(
                        f"HuggingChat did not confirm the requested model switch (still showing '{shown}')."
                    )
            return

        if provider == DriverProvider.AI_STUDIO:
            apply_model = getattr(runtime_driver, "apply_configured_model", None)
            if not callable(apply_model):
                raise RuntimeError("Google AI Studio does not expose model switching right now.")

            await apply_model()

            read_label = getattr(runtime_driver, "_read_current_model_id", None)
            canonicalize_text = getattr(runtime_driver, "_canonicalize_text", None)
            model_config_for_label = getattr(runtime_driver, "_model_config_for_label", None)
            expected_label = desired
            if callable(model_config_for_label):
                expected_config = model_config_for_label(desired)
                expected_label = str(expected_config.get("base_id") or desired).strip()

            if callable(read_label) and callable(canonicalize_text):
                current_label = str(await read_label() or "").strip()
                if canonicalize_text(current_label) != canonicalize_text(expected_label):
                    shown = current_label or "Unknown"
                    raise RuntimeError(
                        "Google AI Studio did not confirm the requested model switch "
                        f"(still showing '{shown}')."
                    )
            return

        if provider == DriverProvider.MIMO:
            apply_model = getattr(runtime_driver, "apply_configured_model", None)
            if not callable(apply_model):
                raise RuntimeError("Xiaomi MiMo does not expose model switching right now.")

            await apply_model()

            read_label = getattr(runtime_driver, "_read_current_mimo_model_label", None)
            canonicalize_label = getattr(runtime_driver, "_canonicalize_model_label", None)
            if callable(read_label) and callable(canonicalize_label):
                current_label = str(await read_label() or "").strip()
                fallback_map = getattr(runtime_driver, "MIMO_RETIRED_MODEL_FALLBACKS", {})
                expected_target = fallback_map.get(desired, desired) if isinstance(fallback_map, dict) else desired
                if canonicalize_label(current_label) != canonicalize_label(expected_target):
                    shown = current_label or "Unknown"
                    raise RuntimeError(
                        f"Xiaomi MiMo did not confirm the requested model switch (still showing '{shown}')."
                    )
            return

        raise RuntimeError(f"{provider.value} does not support remote model switching.")

    async def _remote_switch_model(self, selected_models: dict[str, str] | str) -> None:
        if isinstance(selected_models, str):
            selected_models = {
                self._get_remote_model_switch_provider().value: selected_models,
            }

        context = self._get_remote_model_switch_context()
        providers = context.get("providers")
        if not isinstance(providers, list) or not providers:
            raise RuntimeError("Switch Models is not available right now.")

        providers_by_name = {
            str(provider_info.get("name") or ""): provider_info
            for provider_info in providers
            if isinstance(provider_info, dict)
        }
        changes: dict[DriverProvider, str] = {}

        for raw_provider, raw_model in (selected_models or {}).items():
            provider = DriverProvider.from_setting(raw_provider)
            if provider is None:
                raise RuntimeError(f"Unknown provider: {raw_provider}")

            provider_info = providers_by_name.get(provider.value)
            if provider_info is None:
                raise RuntimeError(f"{provider.value} is not available for model switching.")

            desired_model = str(raw_model or "").strip()
            allowed_models = {
                str(item.get("name") or "")
                for item in (provider_info.get("options") or [])
                if isinstance(item, dict)
            }
            if desired_model not in allowed_models:
                raise RuntimeError(
                    f"'{desired_model}' is not available for {provider.value}."
                )

            current_model = str(provider_info.get("current_model") or "").strip()
            if desired_model != current_model:
                changes[provider] = desired_model

        if not changes:
            return

        switch_jobs: list[tuple[DriverProvider, str, object, str, object]] = []
        for provider, desired_model in changes.items():
            behavior_category = get_behavior_category_for_provider(provider)
            if not behavior_category:
                raise RuntimeError(f"{provider.value} does not expose a switchable model setting.")

            runtime_driver = self._get_runtime_driver_for_provider(provider)
            runtime_provider = getattr(runtime_driver, "provider", None)
            if runtime_driver is None or runtime_provider != provider:
                raise RuntimeError(
                    f"{provider.value} is not an active runtime driver, so its model cannot be switched right now."
                )

            previous_model = self.config_manager.get_setting(behavior_category, "model")
            switch_jobs.append(
                (provider, behavior_category, runtime_driver, desired_model, previous_model)
            )

        if len(switch_jobs) == 1:
            provider, _category, _driver, desired_model, _previous_model = switch_jobs[0]
            Logger.info(f"Remote Control: switching {provider.value} model to '{desired_model}'.")
        else:
            changed_models = ", ".join(
                f"{provider.value}: {desired_model}"
                for provider, _category, _driver, desired_model, _previous_model in switch_jobs
            )
            Logger.info(f"Remote Control: switching parallel models ({changed_models}).")

        for provider, behavior_category, runtime_driver, desired_model, previous_model in switch_jobs:
            self.config_manager.set_setting(behavior_category, "model", desired_model)
            self.config_manager.save_settings()

            try:
                await self._apply_remote_model_switch(provider, runtime_driver, desired_model)
            except Exception:
                self.config_manager.set_setting(behavior_category, "model", previous_model)
                self.config_manager.save_settings()
                raise

    def on_settings_reloaded(self):
        Logger.info("Settings reloaded.")
        try:
            sync_animations_disabled_from_config(self.config_manager)
            self._setup_logging()
            self._refresh_news_state()
            self._sync_news_button()

            if self.console_window:
                self.console_window.apply_settings()

            if self.settings_window and self.settings_window.isVisible():
                # importing settings is effectively an external change -=> force refresh
                self.settings_window.refresh_from_config(force=True)
        except Exception as exc:
            Logger.error(f"Failed to apply reloaded settings: {exc}")

    def on_restart_requested(self):
        asyncio.create_task(self._restart_application())

    async def _restart_application(self):
        Logger.info("Restarting application...")
        try:
            await asyncio.wait_for(self.stop_services(), timeout=10)
        except asyncio.TimeoutError:
            Logger.warning("Restart cleanup timed out; forcing restart.")
        except Exception as e:
            Logger.error(f"Error during restart cleanup: {e}")

        try:
            # Prefer replacing the current process to avoid orphaned/lingering windows.
            if getattr(sys, "frozen", False):
                argv = [sys.executable] + sys.argv[1:]
            else:
                script = Path(sys.argv[0]).expanduser()
                try:
                    script = script.resolve()
                except Exception:
                    script = script.absolute()
                argv = [sys.executable, str(script)] + sys.argv[1:]

            os.execv(argv[0], argv)
        except Exception as e:
            Logger.error(f"execv restart failed: {e}")

        # Fallback: spawn a detached process and hard-exit this one.
        try:
            if getattr(sys, "frozen", False):
                program = sys.executable
                args = sys.argv[1:]
            else:
                program = sys.executable
                args = [str(Path(sys.argv[0]).expanduser().resolve())] + sys.argv[1:]

            QProcess.startDetached(program, args)
        finally:
            os._exit(0)

    @Slot()
    def on_start_clicked(self):
        if self.start_button.text() == "Start":
            self.start_button.setEnabled(False)
            self._update_status("Starting...", "info")
            self._update_tray_menu_state()
            # Schedule the start_services coroutine
            asyncio.create_task(self.start_services())
        else:
            self.start_button.setEnabled(False)
            self._update_status("Stopping...", "info")
            self._update_tray_menu_state()
            asyncio.create_task(self.stop_services())

    def _is_port_in_use_error(self, exc: OSError) -> bool:
        if exc is None:
            return False

        in_use_errnos = {
            getattr(errno, "EADDRINUSE", None),
            getattr(errno, "WSAEADDRINUSE", None),
        }
        if exc.errno in in_use_errnos:
            return True

        winerror = getattr(exc, "winerror", None)
        if winerror in in_use_errnos:
            return True

        msg = str(exc).lower()
        return ("address already in use" in msg) or ("only one usage of each socket address" in msg)

    def _is_tcp_port_listening(self, host: str, port: int, timeout_s: float = 0.25) -> bool:
        host = str(host or "").strip()
        if not host:
            return False

        try:
            family = socket.AF_INET6 if ":" in host else socket.AF_INET
            with socket.socket(family=family, type=socket.SOCK_STREAM) as sock:
                sock.settimeout(float(timeout_s))
                return sock.connect_ex((host, int(port))) == 0
        except Exception:
            return False

    def _has_listening_socket_for_port_psutil(self, port: int) -> bool:
        try:
            import psutil  # type: ignore
        except Exception:
            return False

        try:
            target_port = int(port)
        except Exception:
            return False

        try:
            connections = psutil.net_connections(kind="inet")
        except Exception:
            return False

        for conn in connections or []:
            try:
                if getattr(conn, "status", None) != psutil.CONN_LISTEN:
                    continue

                laddr = getattr(conn, "laddr", None)
                if not laddr:
                    continue

                port_val = getattr(laddr, "port", None)
                if port_val is None and isinstance(laddr, (list, tuple)) and len(laddr) >= 2:
                    port_val = laddr[1]

                if int(port_val) == target_port:
                    return True
            except Exception:
                continue

        return False

    def _check_port_available(self, host: str, port: int) -> tuple[bool, str]:
        """
        Returns (ok, reason).

        When ok is False:
          - reason == "in_use" indicates the port is already in use.
          - otherwise, reason contains a short error message.
        """
        if not isinstance(port, int) or port < 1 or port > 65535:
            return False, f"Invalid port: {port}. Choose a number between 1 and 65535."

        probe_hosts = ["127.0.0.1", "::1"]
        if host and host not in {"127.0.0.1", "0.0.0.0", "::"}:
            probe_hosts.insert(0, host)

        for probe_host in probe_hosts:
            if self._is_tcp_port_listening(probe_host, port):
                return False, "in_use"

        try:
            family = socket.AF_INET6 if (host and ":" in host) else socket.AF_INET
            with socket.socket(family=family, type=socket.SOCK_STREAM) as sock:
                sock.bind((host, port))
        except OSError as exc:
            if self._is_port_in_use_error(exc):
                if self._has_listening_socket_for_port_psutil(port):
                    return False, "in_use"

                try:
                    family = socket.AF_INET6 if (host and ":" in host) else socket.AF_INET
                    with socket.socket(family=family, type=socket.SOCK_STREAM) as sock:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        sock.bind((host, port))
                    return True, ""
                except OSError as exc2:
                    if self._is_port_in_use_error(exc2):
                        return False, "in_use"
                    return False, str(exc2)
            return False, str(exc)

        return True, ""

    def _open_settings_to(
        self,
        category_key: str,
        field_key: str,
        *,
        configure: Callable[[object], None] | None = None,
        attempts: int = 8,
    ) -> None:
        try:
            self.open_settings()
        except Exception:
            return

        category_key = str(category_key or "").strip()
        field_key = str(field_key or "").strip()
        if not category_key or not field_key:
            return

        def focus(attempt: int = 0) -> None:
            settings_window = getattr(self, "settings_window", None)
            if settings_window is None:
                if attempt < attempts:
                    QTimer.singleShot(80, lambda: focus(attempt + 1))
                return

            try:
                settings_window.present()
            except Exception:
                pass

            if callable(configure):
                try:
                    configure(settings_window)
                except Exception:
                    pass

            try:
                if hasattr(settings_window, "focus_setting"):
                    settings_window.focus_setting(category_key, field_key)
            except Exception:
                pass

        QTimer.singleShot(0, lambda: focus(0))

    def _open_settings_to_network_port(self) -> None:
        self._open_settings_to("network_settings", "port")

    def _warn_port_in_use(self, port: int) -> None:
        title = "Port In Use"
        text = f"Port {port} is already in use."
        details = "Free the port (close the other application) or change it in Settings -> API Server."

        if not (self.isVisible() and self.isActiveWindow()):
            self._notify_user(title, f"{text}\n\n{details}", level="warning")
            return

        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Warning)
        dialog.setWindowTitle(title)
        dialog.setText(text)
        dialog.setInformativeText(details)

        open_settings = dialog.addButton("Open Settings", QMessageBox.ActionRole)
        dialog.addButton(QMessageBox.Ok)

        def on_clicked(button) -> None:
            if button is open_settings:
                self._open_settings_to_network_port()

        dialog.buttonClicked.connect(on_clicked)
        dialog.open()

    def _open_settings_to_saved_sessions(self, provider_key: str | None = None) -> None:
        provider_key = str(provider_key or "").strip()

        def configure(settings_window: object) -> None:
            if provider_key and hasattr(settings_window, "set_profile_clear_provider_selection"):
                settings_window.set_profile_clear_provider_selection([provider_key])

        self._open_settings_to(
            "system_settings",
            "clear_persistent_profiles_row",
            configure=configure,
        )

    def _disable_profile_compatibility_warnings(self) -> None:
        try:
            self.config_manager.set_setting("system_settings", "profile_compatibility_warnings", False)
            self.config_manager.save_settings()
        except Exception as exc:
            Logger.warning(f"Failed to disable profile compatibility warnings: {exc}")
            return

        settings_window = getattr(self, "settings_window", None)
        if settings_window is not None and settings_window.isVisible():
            try:
                settings_window.refresh_from_config(force=False)
            except Exception:
                pass

    def _show_profile_compatibility_warning(self, payload: dict) -> None:
        try:
            warnings_enabled = bool(
                self.config_manager.get_setting("system_settings", "profile_compatibility_warnings")
            )
        except Exception:
            warnings_enabled = True
        if not warnings_enabled:
            return

        provider_label = str(payload.get("provider_label") or "this provider").strip()
        provider_key = str(payload.get("provider_key") or "").strip()
        created_version = str(payload.get("profile_created_by_version") or "unknown").strip() or "unknown"
        last_version = str(payload.get("profile_last_chrome_version") or "unknown").strip() or "unknown"
        current_title = str(payload.get("current_browser_title") or "Chromium").strip() or "Chromium"
        current_version = str(payload.get("current_browser_version") or "unknown").strip() or "unknown"
        auth_error = str(payload.get("auth_error") or "").strip()

        title = "Saved Profile May Need Reset"
        text = f"{provider_label} login failed while using a saved profile from a different browser version."
        details = (
            f"The saved profile was created with browser version {created_version} "
            f"(last used with {last_version}), but IntenseRP is now using "
            f"{current_title} {current_version}.\n\n"
            "If the provider keeps returning to login, delete that provider's saved profile and start again. "
            "Auto Login can then create a fresh browser profile."
        )
        if auth_error:
            details = f"{details}\n\nLogin error: {auth_error}"

        is_focused = bool(self.isVisible() and self.isActiveWindow())
        if not is_focused:
            notification_text = (
                f"{provider_label} login failed with a saved profile from a different browser version. "
                "Open Settings -> Provider and Login -> Saved Sessions to clear it."
            )
            if not self._schedule_desktop_notification(title, notification_text, "warning"):
                if not self._show_tray_message(title, notification_text, "warning"):
                    Logger.warning(f"{title}: {notification_text}")
            self._flash_attention_window(self)

        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Warning)
        dialog.setWindowTitle(title)
        dialog.setText(text)
        dialog.setInformativeText(details)

        take_me_there = dialog.addButton("Take Me There", QMessageBox.ActionRole)
        dont_show_again = dialog.addButton("Don't Show Again", QMessageBox.DestructiveRole)
        dialog.addButton(QMessageBox.Ok)

        def on_clicked(button) -> None:
            if button is take_me_there:
                self._open_settings_to_saved_sessions(provider_key)
            elif button is dont_show_again:
                self._disable_profile_compatibility_warnings()

        dialog.buttonClicked.connect(on_clicked)
        dialog.open()

    async def start_services(self):
        if (
            getattr(self, "api", None) is not None
            or getattr(self, "server", None) is not None
            or getattr(self, "server_task", None) is not None
            or getattr(self, "driver", None) is not None
        ):
            Logger.warning("Start requested while services still exist; cleaning up first.")
            try:
                await self.stop_services(update_ui=False)
            except Exception as e:
                Logger.error(f"Pre-start cleanup failed: {e}")

        try:
            # Configure Uvicorn
            port_setting = self.config_manager.get_setting("network_settings", "port")
            try:
                port = int(port_setting) if port_setting else 7777
            except (TypeError, ValueError):
                port = 7777

            available_on_lan = self.config_manager.get_setting("network_settings", "available_on_lan")
            host = "0.0.0.0" if available_on_lan else "127.0.0.1"

            port_ok, reason = self._check_port_available(host, port)
            if not port_ok:
                if reason == "in_use":
                    Logger.warning(f"Port {port} is already in use; refusing to start services.")
                    self._update_status(f"Port {port} is already in use", "warning")
                    self._warn_port_in_use(port)
                else:
                    Logger.error(f"Port {port} is not available; refusing to start services. ({reason})")
                    self._update_status(f"Port {port} is not available", "error")
                    self._notify_user(
                        "Port Unavailable",
                        f"Port {port} is not available.\n\n{reason}\n\nFree the port or change it in Settings -> API Server.",
                        level="error",
                    )

                self.start_button.setText("Start")
                self.start_button.apply_icon(IconType.START, BrandColors.TEXT_PRIMARY)
                self.start_button.setEnabled(True)
                self.start_button.set_chevron_visible(False)
                self._sync_hotswap_button()
                self._update_tray_menu_state()
                return

            if self._dry_run_mode_enabled():
                Logger.info("Dry Run Mode active: starting API server without launching a provider browser.")
                self.driver = DryRunDriver(self.config_manager)

                for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
                    logger = logging.getLogger(logger_name)
                    logger.handlers = [logging.NullHandler()]
                    logger.setLevel(logging.CRITICAL)
                    logger.propagate = False
                    logger.disabled = True

                self.api = API(
                    self.driver,
                    dry_run=True,
                    dry_run_capture_callback=self._emit_dry_run_capture,
                )
                self._set_queue_preview_api(self.api)

                config = uvicorn.Config(
                    app=self.api.app,
                    host=host,
                    port=port,
                    log_level="critical",
                    log_config=None,
                    access_log=False,
                )

                self.server = uvicorn.Server(config)

                self._update_status("Starting Dry Run API Server...", "info")
                self.server_task = asyncio.create_task(self.server.serve())

                self._show_dry_run_window()
                self._update_status(self._dry_run_status_text(port), "running")

                if self.config_manager.get_setting("network_settings", "show_ip"):
                    addrs = [f"http://127.0.0.1:{port}"]
                    if available_on_lan:
                        try:
                            hostname = socket.gethostname()
                            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                                ip = info[4][0]
                                if not ip.startswith("127."):
                                    addrs.append(f"http://{ip}:{port}")
                        except Exception:
                            pass
                    for addr in set(addrs):
                        Logger.success(f"Dry run server running at {addr}")

                self.start_button.setText("Stop")
                self.start_button.apply_icon(IconType.STOP, BrandColors.TEXT_PRIMARY)
                self.start_button.setEnabled(True)
                self.start_button.set_chevron_visible(False)
                self._sync_hotswap_button()
                self._update_tray_menu_state()
                return

            current_provider = get_current_provider(self.config_manager)
            if is_provider_locked(current_provider, self.config_manager):
                self._show_provider_locked_notice(current_provider)
                self._reset_start_controls_to_idle()
                return

            required_providers = (
                get_parallel_selected_providers(self.config_manager)
                if is_parallel_feature_enabled(self.config_manager)
                else [current_provider]
            )
            locked_required_providers = [
                provider
                for provider in required_providers
                if is_provider_locked(provider, self.config_manager)
            ]
            if locked_required_providers:
                self._show_provider_locked_notice(locked_required_providers[0])
                self._reset_start_controls_to_idle()
                return

            if not self._validate_runtime_loadouts(providers=required_providers):
                self.start_button.setText("Start")
                self.start_button.apply_icon(IconType.START, BrandColors.TEXT_PRIMARY)
                self.start_button.setEnabled(True)
                self.start_button.set_chevron_visible(False)
                self._sync_hotswap_button()
                self._update_tray_menu_state()
                self._update_status("Loadouts validation failed", "error")
                return

            # Pass config manager to the runtime driver
            if is_parallel_runtime_active(self.config_manager):
                self.driver = ParallelDriversManager(self.config_manager)
            else:
                self.driver = create_driver(self.config_manager)
            self.driver.notify_user_callback = self._notify_user
            self.driver.request_user_text_callback = self._request_user_text
            self.driver.profile_compatibility_warning_callback = self._show_profile_compatibility_warning
            self.driver.on_crash_callback = self.on_browser_crashed

            # Silence uvicorn loggers to avoid noisy console output.
            for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
                logger = logging.getLogger(logger_name)
                logger.handlers = [logging.NullHandler()]
                logger.setLevel(logging.CRITICAL)
                logger.propagate = False
                logger.disabled = True
            
            # Start Driver (with status callback for browser installation/launch updates)
            self._update_status("Launching Browser...", "info")
            await self.driver.start(status_callback=lambda msg: self._update_status(msg, "info"))
            self._refresh_settings_dynamic_dropdown_options()

            # Optional provider UI language check (provider-specific enforcement; safe to no-op)
            # If the user cancels, stop startup gracefully
            if not await self._ensure_runtime_ui_languages_are_compatible():
                await self.stop_services()
                return

            self.api = API(
                self.driver,
                remote_actions=RemoteControlActions(
                    stop=self._remote_stop_services,
                    restart=self._remote_restart_services,
                    switch_account=self._remote_switch_account,
                    hotswap=self._remote_hotswap,
                    switch_loadout=self._remote_switch_loadout,
                    switch_model=self._remote_switch_model,
                    get_state=self._get_remote_control_state,
                ),
            )
            self._set_queue_preview_api(self.api)

            # Configure Uvicorn with log_config=None to avoid "Unable to configure formatter 'default'" error
            config = uvicorn.Config(
                app=self.api.app,
                host=host,
                port=port,
                log_level="critical",
                log_config=None,
                access_log=False,
            )

            self.server = uvicorn.Server(config)
            
            # Start API Server
            self._update_status("Starting API Server...", "info")
            # We run server.serve() as a task because it blocks
            self.server_task = asyncio.create_task(self.server.serve())
            
            self._update_status(f"Running (Port {port})", "running")

            if self.config_manager.get_setting("network_settings", "show_ip"):
                addrs = [f"http://127.0.0.1:{port}"]
                if available_on_lan:
                    try:
                        hostname = socket.gethostname()
                        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                            ip = info[4][0]
                            if not ip.startswith("127."):
                                addrs.append(f"http://{ip}:{port}")
                    except Exception:
                        pass
                for addr in set(addrs):
                    Logger.success(f"Server running at {addr}")
                if self.config_manager.get_setting("experimental", "enable_remote_control"):
                    for addr in set(addrs):
                        Logger.success(f"Remote control available at {addr}/remote")

            self.start_button.setText("Stop")
            self.start_button.apply_icon(IconType.STOP, BrandColors.TEXT_PRIMARY)
            self.start_button.setEnabled(True)
            self.start_button.set_chevron_visible(True)
            self._refresh_chevron_menu()
            self._sync_hotswap_button()
            self._update_tray_menu_state()

        except Exception as e:
            self._update_status(f"Error: {e}", "error")
            Logger.error(f"Error starting services: {e}")
            try:
                await self.stop_services(update_ui=False)
            except Exception as cleanup_error:
                Logger.error(f"Error cleaning up after failed start: {cleanup_error}")
            self.start_button.setText("Start")
            self.start_button.apply_icon(IconType.START, BrandColors.TEXT_PRIMARY)
            self.start_button.setEnabled(True)
            self.start_button.set_chevron_visible(False)
            self._sync_hotswap_button()
            self._update_tray_menu_state()

    async def _ensure_runtime_ui_languages_are_compatible(self) -> bool:
        runtime_drivers = self._iter_runtime_drivers()
        if not runtime_drivers:
            return True

        for _provider, driver in runtime_drivers:
            if not await self._ensure_driver_ui_language_is_compatible(driver):
                return False

        return True

    async def _ensure_driver_ui_language_is_compatible(self, driver=None) -> bool:
        driver = driver or self._get_current_runtime_driver()
        if not driver:
            return True

        async def _exec_message_box(dialog: QMessageBox) -> int:
            loop = asyncio.get_running_loop()
            future: asyncio.Future[int] = loop.create_future()

            def on_button_clicked(button) -> None:
                if future.done():
                    return
                try:
                    future.set_result(int(dialog.standardButton(button)))
                except Exception:
                    future.set_result(int(QMessageBox.Cancel))

            def on_finished(_result: int) -> None:
                if future.done():
                    return
                future.set_result(int(QMessageBox.Cancel))

            dialog.buttonClicked.connect(on_button_clicked)
            dialog.finished.connect(on_finished)
            dialog.open()

            try:
                return await future
            finally:
                try:
                    dialog.buttonClicked.disconnect(on_button_clicked)
                except Exception:
                    pass
                try:
                    dialog.finished.disconnect(on_finished)
                except Exception:
                    pass
                dialog.deleteLater()

        provider_label = getattr(driver, "provider_label", None) or "Provider"
        required_label = getattr(driver, "required_ui_language_label", None) or "English (en-US)"

        while True:
            try:
                is_ok = await driver.check_ui_language()
            except Exception as e:
                Logger.warning(f"Failed to detect {provider_label} UI language: {e}")
                return True

            if is_ok:
                return True

            detected = getattr(driver, "last_document_lang", None) or "<unset>"
            self._update_status(f"{provider_label} UI language must be {required_label}", "warning")

            dialog = QMessageBox(self)
            dialog.setIcon(QMessageBox.Warning)
            dialog.setWindowTitle(f"{provider_label} UI Language")
            dialog.setText(f"{provider_label} UI language is not {required_label}.")
            dialog.setInformativeText(
                f"Detected <html lang>: {detected}\n\n"
                f"IntenseRP currently requires the {provider_label} UI language to be {required_label}. "
                "Some automation relies on expected UI text, and an unsupported interface language can make it break.\n\n"
                f"Please change the language to {required_label} in the {provider_label} browser window, then click Retry."
            )
            dialog.setStandardButtons(QMessageBox.Retry | QMessageBox.Cancel)
            dialog.setDefaultButton(QMessageBox.Retry)

            choice = await _exec_message_box(dialog)
            if choice == int(QMessageBox.Cancel):
                Logger.warning(f"Start cancelled: {provider_label} UI language is not {required_label}.")
                return False

            # Give the page a moment in case changing language triggers a reload
            await asyncio.sleep(0.25)

    async def stop_services(self, update_ui: bool = True):
        existing = getattr(self, "_stop_task", None)
        if existing and (not existing.done()):
            await existing
            return

        async def _stop_impl() -> None:
            Logger.info("Stopping services...")
            had_warnings = False

            api = getattr(self, "api", None)
            if api is not None:
                try:
                    await asyncio.wait_for(api.stop(), timeout=5.0)
                except asyncio.TimeoutError:
                    had_warnings = True
                    Logger.warning("Timeout while stopping API worker.")
                except asyncio.CancelledError:
                    had_warnings = True
                    Logger.warning("API stop cancelled.")
                except Exception as e:
                    had_warnings = True
                    Logger.error(f"Error stopping API worker: {e}")
                finally:
                    self.api = None
                    self._set_queue_preview_api(None)

            server = getattr(self, "server", None)
            server_task = getattr(self, "server_task", None)
            if server is not None:
                try:
                    server.should_exit = True
                except Exception:
                    pass

            if server_task is not None:
                try:
                    await asyncio.wait_for(server_task, timeout=5.0)
                except asyncio.TimeoutError:
                    had_warnings = True
                    Logger.warning("Timeout while stopping API server; cancelling server task.")
                    try:
                        server_task.cancel()
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(server_task, timeout=2.0)
                    except asyncio.TimeoutError:
                        Logger.warning("Server task did not cancel in time.")
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        Logger.debug(f"Error while cancelling server task: {e}")
                except asyncio.CancelledError:
                    had_warnings = True
                    Logger.warning("API server stop cancelled.")
                except Exception as e:
                    had_warnings = True
                    Logger.error(f"Error stopping API server: {e}")
                finally:
                    self.server_task = None

            self.server = None

            driver = getattr(self, "driver", None)
            if driver is not None:
                try:
                    await asyncio.wait_for(driver.close(), timeout=20.0)
                except asyncio.TimeoutError:
                    had_warnings = True
                    Logger.warning("Timeout while closing browser driver.")
                except asyncio.CancelledError:
                    had_warnings = True
                    Logger.warning("Driver close cancelled.")
                except Exception as e:
                    had_warnings = True
                    Logger.error(f"Error closing driver: {e}")
                finally:
                    self.driver = None

            self._close_dry_run_window()

            if update_ui:
                self._update_status("Stopped", "ready")
                self.start_button.setText("Start")
                self.start_button.apply_icon(IconType.START, BrandColors.TEXT_PRIMARY)
                self.start_button.setEnabled(True)
                self.start_button.set_chevron_visible(False)
                self._sync_hotswap_button()
            if had_warnings:
                Logger.warning("Services stopped with warnings.")
            else:
                Logger.success("Services stopped.")
            self._update_tray_menu_state()

        self._stop_task = asyncio.create_task(_stop_impl())
        try:
            await self._stop_task
        finally:
            self._stop_task = None

    async def on_browser_crashed(self):
        """
        Callback for when the browser crashes or is closed manually.
        """
        Logger.warning("Browser crash callback received.")
        self._update_status("Browser Closed/Crashed", "warning")
        
        # We need to clean up all services including playwright
        try:
            api = getattr(self, "api", None)
            if api is not None:
                try:
                    await asyncio.wait_for(api.stop(), timeout=5.0)
                except asyncio.TimeoutError:
                    Logger.warning("Timeout while stopping API worker after crash.")
                except asyncio.CancelledError:
                    Logger.warning("API stop cancelled after crash.")
                except Exception as e:
                    Logger.error(f"Error stopping API worker after crash: {e}")
                finally:
                    self.api = None
                    self._set_queue_preview_api(None)

            server = getattr(self, "server", None)
            server_task = getattr(self, "server_task", None)
            if server is not None:
                try:
                    server.should_exit = True
                except Exception:
                    pass

            if server_task is not None:
                try:
                    await asyncio.wait_for(server_task, timeout=5.0)
                except asyncio.TimeoutError:
                    Logger.warning("Timeout while stopping API server after crash; cancelling server task.")
                    try:
                        server_task.cancel()
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(server_task, timeout=2.0)
                    except asyncio.TimeoutError:
                        Logger.warning("Server task did not cancel in time after crash.")
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        Logger.debug(f"Error while cancelling server task after crash: {e}")
                except asyncio.CancelledError:
                    Logger.warning("API server stop cancelled after crash.")
                except Exception as e:
                    Logger.error(f"Error stopping API server after crash: {e}")
                finally:
                    self.server_task = None

            self.server = None
            
            # The driver's close() method handles None checks for context/browser, theoretically that is enough
            if self.driver:
                try:
                    await asyncio.wait_for(self.driver.close(), timeout=20.0)
                except asyncio.TimeoutError:
                    Logger.warning("Timeout while closing driver after crash.")
                except asyncio.CancelledError:
                    Logger.warning("Driver close cancelled after crash.")
                except Exception as e:
                    Logger.error(f"Error closing driver after crash: {e}")
                finally:
                    self.driver = None
            
            # Reset UI
            self.start_button.setText("Start")
            self.start_button.apply_icon(IconType.START, BrandColors.TEXT_PRIMARY)
            self.start_button.setEnabled(True)
            self.start_button.set_chevron_visible(False)
            self._sync_hotswap_button()
            self._update_tray_menu_state()

        except Exception as e:
            Logger.error(f"Error handling crash cleanup: {e}")
            self._update_status(f"Error: {e}", "error")
            self.start_button.setEnabled(True)
            self._update_tray_menu_state()

    def closeEvent(self, event):
        tray_icon = getattr(self, "_tray_icon", None)
        exit_requested = bool(getattr(self, "_exit_requested", False))
        collapse_to_tray = False

        if (not exit_requested) and tray_icon and tray_icon.isVisible():
            try:
                collapse_to_tray = bool(
                    self.config_manager.get_setting(
                        "application_settings", "collapse_to_tray_on_close"
                    )
                )
            except Exception:
                collapse_to_tray = False

        if collapse_to_tray:
            Logger.info("Close requested; collapsing to tray.")
            self._hide_to_tray()
            event.ignore()
            return

        # Cleanup on close
        Logger.info("Window closing, shutting down...")
        # qasync loop runs until the window closes usually, but we need to await the cleanup.
        
        # If the settings window is open, close it too. If the user cancels the
        # "unsaved changes" prompt, abort quitting the app.
        if self.settings_window:
            if not self.settings_window.close():
                self._exit_requested = False
                event.ignore()
                return
        
        status_text = self.status_label.text()
        if any(state in status_text for state in ["Stopped", "Ready", "Browser Closed/Crashed"]):
            # Close console window if open
            if self.console_window:
                self.console_window.force_close()
                self.console_window = None

            self._cleanup_tray_icon()
            self._request_application_quit()
            event.accept()
            return

        shutdown_task = getattr(self, "_shutdown_task", None)
        if shutdown_task and (not shutdown_task.done()):
            event.ignore()
            return

        event.ignore()
        self._update_status("Shutting down...", "info")
        
        async def cleanup_and_close():
            await self.stop_services()
            # Now we can close
            # We need to call close again, but bypass this check
            # We can reset the status label
            self._update_status("Stopped", "ready")
            
            # Close console window if open
            if self.console_window:
                self.console_window.force_close()
                self.console_window = None
            
            # If the settings window got opened during shutdown, try to close it.
            if self.settings_window and self.settings_window.isVisible():
                if not self.settings_window.close():
                    return

            self.close()
            
        self._shutdown_task = asyncio.create_task(cleanup_and_close())

def main():
    import os
    import signal
    # If frozen, force Playwright/Patchright to use the global cache directory
    # instead of looking for bundled browsers in the internal _MEIPASS/package directories.
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            # Windows: %LOCALAPPDATA%\ms-playwright
            local_app_data = os.environ.get("LOCALAPPDATA")
            if local_app_data:
                global_path = os.path.join(local_app_data, "ms-playwright")
                if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
                    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = global_path
                if "PATCHRIGHT_BROWSERS_PATH" not in os.environ:
                    os.environ["PATCHRIGHT_BROWSERS_PATH"] = global_path
        else:
            # Linux/macOS: ~/.cache/ms-playwright
            home = Path.home()
            global_path = str(home / ".cache" / "ms-playwright")
            if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = global_path
            if "PATCHRIGHT_BROWSERS_PATH" not in os.environ:
                os.environ["PATCHRIGHT_BROWSERS_PATH"] = global_path

    # Handle module execution request (e.g. for patchright subprocesses in frozen app)
    if len(sys.argv) > 2 and sys.argv[1] == "-m":
        import runpy
        # Remove exe and -m, keeping module name and args
        # argv becomes ['module_name', 'arg1', ...]
        sys.argv = sys.argv[2:]
        module_name = sys.argv[0]
        try:
            # We must use alter_sys=True so the module sees the correct argv
            runpy.run_module(module_name, run_name="__main__", alter_sys=True)
            sys.exit(0)
        except SystemExit:
            raise
        except Exception as e:
            print(f"Failed to run module {module_name}: {e}")
            sys.exit(1)

    (
        remaining_args,
        delete_updater,
        updater_path,
        clear_flags,
        fake_update,
        fake_pufref,
        local_update_debug,
        debug_widget_shows,
        extra_debug_logs,
    ) = _parse_update_cleanup_args(sys.argv[1:])
    sys.argv = [sys.argv[0]] + remaining_args
    Logger.set_extra_debug_logs_enabled(extra_debug_logs)

    if clear_flags:
        sys.exit(0 if _clear_app_flags() else 1)

    app = QApplication(sys.argv)
    try:
        local_update_debug_archive = _resolve_local_update_debug_archive(local_update_debug)
    except ValueError as exc:
        QMessageBox.warning(None, "Local Update Debug", str(exc))
        sys.exit(2)

    if debug_widget_shows:
        _install_widget_debug_logging(app)

    from ui.core.app_icon import get_app_icon
    from PySide6.QtWidgets import QStyleFactory

    # Force a consistent style between running-from-source and packaged(PyInstaller) builds. 
    # Style availability can differ when plugins aren't bundled the same way.
    available_styles = {name.lower(): name for name in QStyleFactory.keys()}
    for preferred in ("fusion", "windowsvista", "windows"):
        style_name = available_styles.get(preferred.lower())
        if style_name:
            app.setStyle(style_name)
            break

    app_icon = get_app_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
    
    # Load Fonts
    from PySide6.QtGui import QFontDatabase, QFont
    import os
    
    font_dir = resolve_resource_path("ui", "fonts")
    if font_dir.exists():
        for filename in os.listdir(font_dir):
            if filename.endswith(".ttf"):
                QFontDatabase.addApplicationFont(str(font_dir / filename))
    
    # Set Global Font
    font = QFont(BrandColors.FONT_FAMILY)
    app.setFont(font)
    
    # Enforce Dark Mode Palette (or try to)
    app.setStyleSheet(f"""
        QWidget {{
            font-family: '{BrandColors.FONT_FAMILY}';
            background-color: {BrandColors.WINDOW_BG};
            color: {BrandColors.TEXT_PRIMARY};
        }}
    """)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MainWindow(
        fake_update=fake_update,
        fake_pufref=fake_pufref,
        local_update_debug_archive=(
            str(local_update_debug_archive) if local_update_debug_archive is not None else None
        ),
        extra_debug_logs_cli=extra_debug_logs,
    )
    window.show()

    def _request_quit() -> None:
        try:
            window._exit_requested = True
            if window.close():
                return
            window._exit_requested = False
            return
        except Exception:
            pass

        try:
            app.quit()
        except Exception:
            pass

    try:
        app.aboutToQuit.connect(window._cleanup_tray_icon)
    except Exception:
        pass

    def _sigint_handler(_signum, _frame) -> None:
        try:
            QTimer.singleShot(0, _request_quit)
        except Exception:
            try:
                app.quit()
            except Exception:
                pass

    try:
        signal.signal(signal.SIGINT, _sigint_handler)
    except Exception:
        pass

    if delete_updater and updater_path:
        try:
            target = Path(updater_path)

            def worker() -> None:
                _delete_updater_best_effort(target)

            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            pass

    with loop:
        loop.run_forever()

if __name__ == "__main__":
    main()
