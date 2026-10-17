from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QLabel, QMessageBox, QFileDialog,
    QFrame, QLayout, QSizePolicy
)
from PySide6.QtCore import Qt, Signal, QUrl, QPoint, QRect, QSize
from PySide6.QtGui import QDesktopServices

from ui.core.brand import BrandColors
from ui.core.icons import IconUtils, IconType
from ui.niche.backup_import_window import BackupImportWindow
from ui.niche.browser_manager_window import BrowserManagerWindow
from ui.niche.diagnostics_bundle_window import DiagnosticsBundleWindow
from ui.windows.contributors_window import ContributorsWindow
from ui.niche.stmp_patcher_window import STMPPatcherWindow
from ui.widgets.smooth_scroll_area import SmoothScrollArea
from utils.v1_migrator import V1Migrator
from utils.logger import Logger


DISCORD_INVITE_URL = "https://discord.gg/4Gvjk2RdsK"
REPOSITORY_URL = "https://github.com/Lemomation/irn"
DOCS_HOME_URL = "https://lemomation.github.io/irn/"


class _TileFlowLayout(QLayout):
    def __init__(self, parent=None, margin=0, spacing=12):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientations()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool):
        x = rect.x()
        y = rect.y()
        line_height = 0

        for item in self._items:
            widget = item.widget()
            if widget is not None and not widget.isVisible():
                continue

            item_size = item.sizeHint()
            next_x = x + item_size.width() + self.spacing()
            if line_height > 0 and next_x - self.spacing() > rect.right() and x > rect.x():
                x = rect.x()
                y = y + line_height + self.spacing()
                next_x = x + item_size.width() + self.spacing()
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item_size))

            x = next_x
            line_height = max(line_height, item_size.height())

        return y + line_height - rect.y()


class _TileFlowHost(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._flow_layout = _TileFlowLayout(self, margin=0, spacing=12)
        self.setLayout(self._flow_layout)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    @property
    def flow_layout(self):
        return self._flow_layout

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._flow_layout.heightForWidth(width)

    def sizeHint(self):
        parent_width = self.parentWidget().contentsRect().width() if self.parentWidget() is not None else 0
        min_width = self._flow_layout.minimumSize().width()
        width = max(min_width, int(parent_width or self.width() or 0))
        height = self._flow_layout.heightForWidth(width)
        return QSize(width, height)

    def minimumSizeHint(self):
        minimum = self._flow_layout.minimumSize()
        return QSize(minimum.width(), minimum.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._flow_layout.invalidate()
        self.updateGeometry()


class HelpTile(QFrame):
    clicked = Signal()
    TILE_WIDTH = 136
    TILE_HEIGHT = 104

    def __init__(self, label: str, icon_type: IconType | str, tooltip: str = "", parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        if tooltip:
            self.setToolTip(tooltip)

        self.setStyleSheet(f"""
            HelpTile {{
                background-color: {BrandColors.SIDEBAR_BG};
                border-radius: 8px;
                border: 1px solid transparent;
            }}
            HelpTile:hover {{
                background-color: {BrandColors.ITEM_HOVER};
                border: 1px solid {BrandColors.ACCENT};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setContentsMargins(10, 16, 10, 12)
        layout.setSpacing(8)

        # Icon (large, centered)
        icon_label = QLabel()
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setStyleSheet("background-color: transparent;")
        pixmap = IconUtils.get_pixmap(
            icon_type,
            color=BrandColors.TEXT_PRIMARY,
            size=36,
            dpr=self.devicePixelRatioF(),
        )
        icon_label.setPixmap(pixmap)
        layout.addWidget(icon_label)

        # Text label (centered, below icon)
        text_label = QLabel(label)
        text_label.setAlignment(Qt.AlignCenter)
        text_label.setWordWrap(True)
        text_label.setStyleSheet(f"""
            font-size: {BrandColors.FONT_SIZE_SMALL};
            font-weight: 600;
            color: {BrandColors.TEXT_PRIMARY};
            background-color: transparent;
        """)
        layout.addWidget(text_label)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def sizeHint(self):
        return QSize(self.TILE_WIDTH, self.TILE_HEIGHT)

    def minimumSizeHint(self):
        return self.sizeHint()


class HelpWindow(QMainWindow):
    settings_reloaded = Signal()

    def __init__(self, config_manager, parent=None, main_window=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self.main_window = main_window
        self.setWindowTitle("Help & Extras")
        self.resize(680, 560)
        self.setMinimumSize(520, 420)
        self.setStyleSheet(f"background-color: {BrandColors.WINDOW_BG};")

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)

        header = QWidget()
        header.setStyleSheet("background-color: transparent;")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(4)

        title = QLabel("Help & Extras")
        title.setStyleSheet(f"""
            font-size: 20px;
            font-weight: bold;
            color: {BrandColors.TEXT_PRIMARY};
        """)
        title.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(title)

        desc = QLabel("Migration tools, utilities, community, and project resources.")
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignCenter)
        desc.setStyleSheet(f"""
            font-size: {BrandColors.FONT_SIZE_REGULAR};
            color: {BrandColors.TEXT_SECONDARY};
            background-color: transparent;
            padding: 0px 4px;
        """)
        header_layout.addWidget(desc)
        layout.addWidget(header)

        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"""
            QScrollArea {{
                border: none;
                background-color: {BrandColors.WINDOW_BG};
            }}
            QScrollArea > QWidget > QWidget {{
                background-color: {BrandColors.WINDOW_BG};
            }}
            QScrollBar:vertical {{
                border: none;
                background: {BrandColors.WINDOW_BG};
                width: 12px;
                margin: 0px;
                border-radius: 6px;
            }}
            QScrollBar::handle:vertical {{
                background: #555555;
                min-height: 20px;
                border-radius: 6px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: #666666;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
        """)
        layout.addWidget(scroll, 1)

        scroll_content = QWidget()
        scroll.setWidget(scroll_content)
        section_layout = QVBoxLayout(scroll_content)
        section_layout.setContentsMargins(0, 2, 0, 0)
        section_layout.setSpacing(24)

        sections = [
            (
                "Utilities",
                [
                    ("Backup / Import", IconType.BACKUP, "Backup or restore your config directory (settings/key/profiles) using a .zip file.", self.show_backup_import),
                    ("Browser Manager", IconType.BROWSER_MANAGER, "Install, reinstall, or remove the Playwright Chromium browser used by IntenseRP.", self.show_browser_manager),
                    ("STMP Patcher", IconType.PATCHER, "Patches RossAscends's STMP to include per-message names.", self.show_stmp_patcher),
                    ("Migrate from v1", IconType.MIGRATE, "", self.start_migration),
                ],
            ),
            (
                "Help & Diagnostics",
                [
                    ("Bug Report", IconType.BUG_REPORT, "Create a diagnostics .zip bundle from the private internal log and latest saved prompts.", self.show_bug_report_bundle),
                    ("Docs", IconType.DOCS, "Open the IntenseRP Next documentation.", self.open_docs),
                ],
            ),
            (
                "Community & Project",
                [
                    ("Contributors", IconType.CONTRIBUTORS, "", self.show_contributors),
                    ("Discord Server", IconType.DISCORD, "Join the IntenseRP Next Discord server for community help and quick questions.", self.open_discord),
                    ("GitHub", IconType.GITHUB, "Open the IntenseRP Next GitHub repository.", self.open_github),
                ],
            ),
        ]

        for section_title, tiles in sections:
            self._add_tile_section(section_layout, section_title, tiles)

        self.contributors_window = None
        self.stmp_patcher_window = None
        self.backup_import_window = None
        self.browser_manager_window = None
        self.diagnostics_bundle_window = None

    def _add_tile_section(self, parent_layout, title: str, tiles):
        section = QWidget()
        section.setStyleSheet("background-color: transparent;")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        label = QLabel(title)
        label.setStyleSheet(f"""
            font-size: {BrandColors.FONT_SIZE_XLARGE};
            font-weight: 700;
            color: {BrandColors.TEXT_PRIMARY};
            background-color: transparent;
        """)
        layout.addWidget(label)

        flow_host = _TileFlowHost(section)
        for tile_label, icon_type, tooltip, handler in tiles:
            tile = HelpTile(tile_label, icon_type, tooltip=tooltip, parent=self)
            tile.clicked.connect(handler)
            flow_host.flow_layout.addWidget(tile)

        layout.addWidget(flow_host)
        parent_layout.addWidget(section)

    def start_migration(self):
        msg = QMessageBox(self)
        msg.setWindowTitle("Migrate from v1")
        msg.setText("This tool will migrate settings from an old IntenseRP (v1.5.3+) installation.\n\n"
                    "Please select the root directory of your old installation.\n"
                    "This directory must contain the 'save' folder with 'config.enc' and 'secret.key'.")
        msg.setIcon(QMessageBox.Information)
        msg.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)

        if msg.exec() != QMessageBox.Ok:
            return

        directory = QFileDialog.getExistingDirectory(self, "Select v1 Installation Directory")
        if not directory:
            return

        migrator = V1Migrator(self.config_manager)
        success, message = migrator.migrate(directory)

        result_msg = QMessageBox(self)
        result_msg.setWindowTitle("Migration Result")
        result_msg.setText(message)
        result_msg.setIcon(QMessageBox.Information if success else QMessageBox.Warning)
        result_msg.exec()

    def show_stmp_patcher(self):
        if not self.stmp_patcher_window:
            self.stmp_patcher_window = STMPPatcherWindow(None)  # Top level
        self.stmp_patcher_window.show()
        self.stmp_patcher_window.activateWindow()

    def show_contributors(self):
        if not self.contributors_window:
            self.contributors_window = ContributorsWindow(None) # Top level
        self.contributors_window.show()
        self.contributors_window.activateWindow()

    def show_backup_import(self):
        if not self.backup_import_window:
            self.backup_import_window = BackupImportWindow(self.config_manager, None)  # Top level
            self.backup_import_window.settings_reloaded.connect(self.settings_reloaded.emit)
        self.backup_import_window.show()
        self.backup_import_window.activateWindow()

    def show_bug_report_bundle(self):
        if not self.diagnostics_bundle_window:
            self.diagnostics_bundle_window = DiagnosticsBundleWindow(self.config_manager, None)  # Top level
        self.diagnostics_bundle_window.show()
        self.diagnostics_bundle_window.activateWindow()

    def show_browser_manager(self):
        if not self.browser_manager_window:
            self.browser_manager_window = BrowserManagerWindow(main_window=self.main_window, parent=None)  # Top level
        self.browser_manager_window.show()
        self.browser_manager_window.activateWindow()

    def open_discord(self):
        QDesktopServices.openUrl(QUrl(DISCORD_INVITE_URL))

    def open_github(self):
        QDesktopServices.openUrl(QUrl(REPOSITORY_URL))

    def open_docs(self):
        QDesktopServices.openUrl(QUrl(DOCS_HOME_URL))
