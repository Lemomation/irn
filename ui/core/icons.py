from enum import Enum
import os
from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractButton
from ui.core.brand import BrandColors
from utils.logger import Logger
from utils.resource_path import resolve_resource_path

class IconType(Enum):
    START = "play.svg"
    STOP = "square.svg"
    CONFIRM = "check.svg"
    CANCEL = "x.svg"
    SETTINGS = "settings.svg"
    SEARCH = "search.svg"
    PLUS = "plus.svg"
    HELP = "help-circle.svg"
    BELL = "bell.svg"
    SUPPORT = "support.svg"
    MIGRATE = "truck.svg"
    CONTRIBUTORS = "user-check.svg"
    PATCHER = "terminal.svg"
    BACKUP = "download-cloud.svg"
    BUG_REPORT = "bug.svg"
    BROWSER_MANAGER = "globe.svg"
    DISCORD = "discord.svg"
    GITHUB = "github.svg"
    DOCS = "book-open-check.svg"
    KOFI = "kofi.svg"

class IconUtils:
    _SVG_TEXT_CACHE: dict[str, str] = {}
    _PIXMAP_CACHE: dict[tuple[str, str, int, float, int], QPixmap] = {}

    @staticmethod
    def _build_icon(normal_pixmap: QPixmap, disabled_pixmap: QPixmap | None = None) -> QIcon:
        if normal_pixmap.isNull():
            return QIcon()

        icon = QIcon()
        for mode in (QIcon.Normal, QIcon.Active, QIcon.Selected):
            icon.addPixmap(normal_pixmap, mode, QIcon.Off)
            icon.addPixmap(normal_pixmap, mode, QIcon.On)

        if disabled_pixmap is not None and not disabled_pixmap.isNull():
            icon.addPixmap(disabled_pixmap, QIcon.Disabled, QIcon.Off)
            icon.addPixmap(disabled_pixmap, QIcon.Disabled, QIcon.On)
        return icon

    @staticmethod
    def _icon_path(filename: str, *, subdir: str | None = None) -> str:
        parts = ["ui", "assets", "icons"]
        if subdir:
            parts.append(subdir)
        parts.append(filename)
        return str(resolve_resource_path(*parts))

    @staticmethod
    def _read_svg_text(path: str) -> str:
        cached = IconUtils._SVG_TEXT_CACHE.get(path)
        if cached is not None:
            return cached

        try:
            with open(path, "r", encoding="utf-8") as f:
                svg = f.read()
        except Exception:
            svg = ""

        IconUtils._SVG_TEXT_CACHE[path] = svg
        return svg

    @staticmethod
    def _render_svg_to_pixmap(
        svg: str,
        *,
        color: str | None,
        size: int,
        dpr: float,
        y_offset: int = 0,
    ) -> QPixmap:
        if not svg:
            return QPixmap()

        if color:
            svg = svg.replace("currentColor", str(color))

        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))

        px = max(1, int(round(size * dpr)))
        pixmap = QPixmap(px, px)
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        renderer.render(painter, QRectF(0, 0, px, px))
        painter.end()

        pixmap.setDevicePixelRatio(dpr)
        return pixmap

    @staticmethod
    def get_pixmap(
        icon: "IconType | str",
        *,
        color: str | None,
        size: int,
        dpr: float,
        y_offset: int = 0,
        subdir: str | None = None,
    ) -> QPixmap:
        filename = icon.value if isinstance(icon, IconType) else str(icon)
        icon_path = IconUtils._icon_path(filename, subdir=subdir)

        cache_key = (icon_path, str(color or ""), int(size), round(float(dpr), 2), int(y_offset))
        cached = IconUtils._PIXMAP_CACHE.get(cache_key)
        if cached is not None:
            return cached

        if not os.path.exists(icon_path):
            Logger.warning(f"Icon file not found: {icon_path}")
            return QPixmap()

        svg = IconUtils._read_svg_text(icon_path)
        pixmap = IconUtils._render_svg_to_pixmap(svg, color=color, size=size, dpr=dpr, y_offset=y_offset)
        IconUtils._PIXMAP_CACHE[cache_key] = pixmap
        return pixmap

    @staticmethod
    def get_icon(
        icon: "IconType | str",
        *,
        color: str | None,
        size: int,
        widget: object | None = None,
        y_offset: int = 0,
        subdir: str | None = None,
        include_disabled: bool = False,
    ) -> QIcon:
        dpr = float(widget.devicePixelRatioF()) if widget is not None and hasattr(widget, "devicePixelRatioF") else 1.0
        normal_pixmap = IconUtils.get_pixmap(
            icon,
            color=color,
            size=size,
            dpr=dpr,
            y_offset=y_offset,
            subdir=subdir,
        )
        if normal_pixmap.isNull():
            return QIcon()

        disabled_pixmap = None
        if include_disabled and color is not None:
            disabled_pixmap = IconUtils.get_pixmap(
                icon,
                color=BrandColors.TEXT_DISABLED,
                size=size,
                dpr=dpr,
                y_offset=y_offset,
                subdir=subdir,
            )

        return IconUtils._build_icon(normal_pixmap, disabled_pixmap)

    @staticmethod
    def apply_icon(
        button: QAbstractButton,
        icon_type: "IconType | str",
        color: str = None,
        size: int = 16,
        y_offset: int = 0,
        include_disabled: bool = False,
    ):
        """
        Applies an SVG icon to a button.
        """
        icon = IconUtils.get_icon(
            icon_type,
            color=color,
            size=size,
            widget=button,
            y_offset=y_offset,
            include_disabled=include_disabled,
        )
        if icon.isNull():
            return

        button.setIcon(icon)
        # Intentionally do not touch the button iconSize here
        # Many callsites either rely on the platform default or set their own iconSize
