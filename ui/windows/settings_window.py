from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QListWidget,
    QScrollArea, QLabel, QPushButton, QFrame, QMessageBox, QDialog, QListWidgetItem,
    QLineEdit, QTextEdit, QComboBox, QSizePolicy, QLayout, QLayoutItem,
    QGraphicsOpacityEffect
)
from PySide6.QtCore import Qt, Signal, QTimer, QSize, QRect, Property, QPoint, QPropertyAnimation, QEasingCurve, QSequentialAnimationGroup, QParallelAnimationGroup, QEvent, QUrl
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QBrush, QDesktopServices, QKeySequence, QShortcut, QPolygon, QCursor
from difflib import SequenceMatcher
import copy
from typing import Any
import threading
import os
import shutil
import re
import json
from pathlib import Path
from config.formatting_presets import FORMATTING_PRESET_TEMPLATES
from config.loadouts import (
    LoadoutDefinition,
    build_visual_loadout_settings,
    get_behavior_category_for_provider,
    get_loadout_field_bindings,
    serialize_settings_loadouts,
)
from config.manager import ConfigManager
from config.location import infer_preset_from_config_dir, migrate_config_dir, resolve_config_dir, write_pointer_file
from config.schema import (
    PROVIDER_BEHAVIOR_GROUPS,
    RUNTIME_PARALLEL_MODE_OPTIONS,
    SCHEMA,
    SETTINGS_CARDS,
    SETTINGS_SECTIONS,
    SettingType,
)
from drivers.providers import DriverProvider, provider_options
from ui.core.brand import BrandColors
from ui.widgets.components import Tumbler, StyledLineEdit, StyledTextEdit, StyledComboBox, Divider, Description, HintCard, StyledButton, MultiColumnRow, SettingRow, ToggleRow, InputPairsWidget, InputListWidget, DirectoryEntry, SegmentedSwitcher, configure_deprecated_combobox_items
from ui.widgets.marshmallow_dropdown import MarshmallowDropdown, MarshmallowMultiSelectDropdown, MarshmallowOption
from ui.widgets.redirect_card import RedirectCard
from ui.widgets.runtime_parallelization import RuntimeProviderLaneDropdown
from ui.widgets.tooltip_text import render_tooltip_text
from ui.widgets.smooth_scroll_area import SmoothScrollArea
from ui.ece.credential_manager_dialog import CredentialManagerDialog
from ui.core.icons import IconUtils, IconType
from ui.core.animation_settings import animations_disabled
from ui.niche.update_available_dialog import UpdateAvailableDialog, UpdateAvailableInfo
from utils.logger import Logger
from utils.api_key_generator import generate_api_key
from utils.ip_utils import normalize_ip_list
from utils.update_checker import check_for_updates, read_local_version
from utils.docs_links import build_docs_url
from utils.providers_in_parallel import (
    MAX_FULL_PARALLEL_PROVIDER_INSTANCES,
)

INFO_BUBBLE_HOVER_EVENTS = frozenset({
    QEvent.Enter,
    QEvent.HoverEnter,
    QEvent.MouseMove,
    QEvent.HoverMove,
})

INFO_BUBBLE_HIDE_EVENTS = frozenset({
    QEvent.Leave,
    QEvent.HoverLeave,
})

INFO_BUBBLE_TRIGGER_EVENTS = INFO_BUBBLE_HOVER_EVENTS | INFO_BUBBLE_HIDE_EVENTS | frozenset({
    QEvent.MouseButtonPress,
    QEvent.FocusIn,
    QEvent.KeyPress,
    QEvent.Wheel,
})

class _SearchHighlightOverlay(QWidget):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._flash_target_widget = None
        self._persistent_widget = None
        self._pulse = 0.0
        self._padding = 4
        self._radius = 8

        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background-color: transparent;")
        self.hide()

        self._anim_group = QSequentialAnimationGroup(self)

        anim_in = QPropertyAnimation(self, b"pulse")
        anim_in.setDuration(140)
        anim_in.setEasingCurve(QEasingCurve.OutCubic)
        anim_in.setStartValue(0.0)
        anim_in.setEndValue(1.0)

        anim_out = QPropertyAnimation(self, b"pulse")
        anim_out.setDuration(420)
        anim_out.setEasingCurve(QEasingCurve.OutCubic)
        anim_out.setStartValue(1.0)
        anim_out.setEndValue(0.0)

        self._anim_group.addAnimation(anim_in)
        self._anim_group.addAnimation(anim_out)
        self._anim_group.finished.connect(self._on_anim_finished)

    def _get_pulse(self) -> float:
        return float(self._pulse)

    def _set_pulse(self, value: float):
        value = max(0.0, min(float(value), 1.0))
        if value == self._pulse:
            return
        self._pulse = value
        self.update()

    pulse = Property(float, _get_pulse, _set_pulse)

    def _on_anim_finished(self) -> None:
        self._flash_target_widget = None
        self._sync_visibility()

    def clear(self) -> None:
        self.clear_flash()
        self.set_persistent_widget(None)

    def clear_flash(self) -> None:
        self._anim_group.stop()
        self._flash_target_widget = None
        self._pulse = 0.0
        self._sync_visibility()
        self.update()

    def set_persistent_widget(self, widget: QWidget | None) -> None:
        self._persistent_widget = widget
        self._sync_visibility()
        self.update()

    def pulse_widget(self, widget: QWidget) -> None:
        if widget is None:
            return

        self._flash_target_widget = widget
        parent = self.parent()
        if isinstance(parent, QWidget):
            self.setGeometry(parent.rect())
        self._sync_visibility()

        self._anim_group.stop()
        self._pulse = 0.0
        self.update()
        self._anim_group.start()

    def update_target_geometry(self, *_args) -> None:
        if not (self._has_visible_target(self._flash_target_widget) or self._has_visible_target(self._persistent_widget)):
            return
        self._sync_visibility()
        self.update()

    def eventFilter(self, obj, event):
        if obj is self.parent() and event.type() == QEvent.Resize:
            parent = self.parent()
            if isinstance(parent, QWidget):
                self.setGeometry(parent.rect())
        return super().eventFilter(obj, event)

    def _has_visible_target(self, widget: QWidget | None) -> bool:
        if widget is None:
            return False
        try:
            return bool(widget.isVisible())
        except RuntimeError:
            return False

    def _sync_visibility(self) -> None:
        parent = self.parent()
        if isinstance(parent, QWidget):
            self.setGeometry(parent.rect())

        has_target = self._has_visible_target(self._flash_target_widget) or self._has_visible_target(self._persistent_widget)
        if has_target:
            self.show()
            self.raise_()
        else:
            self.hide()

    def _target_rect_for_widget(self, widget: QWidget | None) -> QRect | None:
        if not self._has_visible_target(widget):
            return None

        try:
            top_left = self.mapFromGlobal(widget.mapToGlobal(QPoint(0, 0)))
            rect = widget.rect().translated(top_left).adjusted(
                -self._padding, -self._padding, self._padding, self._padding
            )
        except RuntimeError:
            return None

        if not rect.intersects(self.rect()):
            return None
        return rect

    def paintEvent(self, event):
        persistent_rect = self._target_rect_for_widget(self._persistent_widget)
        flash_rect = self._target_rect_for_widget(self._flash_target_widget)
        if persistent_rect is None and (flash_rect is None or self._pulse <= 0.001):
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        accent = QColor(BrandColors.ACCENT)

        if flash_rect is not None and self._pulse > 0.001:
            pulse = float(self._pulse)

            fill = QColor(accent)
            fill.setAlpha(int(round(26 * pulse)))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(fill))
            painter.drawRoundedRect(flash_rect, self._radius, self._radius)

            outer = QColor(accent)
            outer.setAlpha(int(round(80 * pulse)))
            outer_pen = QPen(outer, 6)
            outer_pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(outer_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(flash_rect, self._radius, self._radius)

            inner = QColor(accent)
            inner.setAlpha(int(round(180 * pulse)))
            inner_pen = QPen(inner, 2)
            inner_pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(inner_pen)
            painter.drawRoundedRect(flash_rect, self._radius, self._radius)

        if persistent_rect is not None:
            border_pen = QPen(accent, 2)
            border_pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(border_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(persistent_rect, self._radius, self._radius)

        painter.end()


class _SidebarSectionWidget(QWidget):
    section_requested = Signal(str)
    card_requested = Signal(str, str)

    def __init__(self, section_key: str, title: str, icon_file: str, icon_loader, parent=None):
        super().__init__(parent)
        self.section_key = str(section_key)
        self._icon_file = str(icon_file or "")
        self._icon_loader = icon_loader
        self._active = False
        self._card_buttons = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.top_button = QPushButton(str(title or ""))
        self.top_button.setCursor(Qt.PointingHandCursor)
        self.top_button.setFlat(True)
        self.top_button.setCheckable(False)
        self.top_button.setAttribute(Qt.WA_Hover, True)
        self.top_button.clicked.connect(lambda: self.section_requested.emit(self.section_key))
        layout.addWidget(self.top_button)

        self.children = QWidget()
        self.children.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.children_layout = QVBoxLayout(self.children)
        self.children_layout.setContentsMargins(20, 2, 0, 4)
        self.children_layout.setSpacing(1)
        layout.addWidget(self.children)

        self._update_top_button_style()
        self.set_cards([])
        self.set_expanded(False)

    def set_cards(self, cards: list[tuple[str, str]]) -> None:
        while self.children_layout.count():
            item = self.children_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._card_buttons = {}

        for card_key, card_title in cards:
            button = QPushButton(str(card_title or ""))
            button.setCursor(Qt.PointingHandCursor)
            button.setFlat(True)
            button.setAttribute(Qt.WA_Hover, True)
            button.clicked.connect(
                lambda _checked=False, c=card_key: self.card_requested.emit(self.section_key, c)
            )
            button.setStyleSheet(
                f"""
                QPushButton {{
                    text-align: left;
                    color: {BrandColors.TEXT_SOFT};
                    background-color: transparent;
                    border: none;
                    padding: 7px 14px;
                    font-size: {BrandColors.FONT_SIZE_REGULAR};
                    font-family: {BrandColors.FONT_FAMILY};
                }}
                QPushButton:hover {{
                    color: {BrandColors.TEXT_PRIMARY};
                }}
                """
            )
            self.children_layout.addWidget(button)
            self._card_buttons[str(card_key)] = button
        self.children.adjustSize()

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        self._update_top_button_style()

    def set_active_card(self, card_key: str | None) -> None:
        active_card = str(card_key or "").strip()
        for key, button in self._card_buttons.items():
            if key == active_card:
                button.setStyleSheet(
                    f"""
                    QPushButton {{
                        text-align: left;
                        color: {BrandColors.TEXT_PRIMARY};
                        background-color: transparent;
                        border: none;
                        padding: 7px 14px;
                        font-size: {BrandColors.FONT_SIZE_REGULAR};
                        font-family: {BrandColors.FONT_FAMILY};
                        font-weight: 700;
                    }}
                    QPushButton:hover {{
                        color: {BrandColors.TEXT_PRIMARY};
                    }}
                    """
                )
            else:
                button.setStyleSheet(
                    f"""
                    QPushButton {{
                        text-align: left;
                        color: {BrandColors.TEXT_SOFT};
                        background-color: transparent;
                        border: none;
                        padding: 7px 14px;
                        font-size: {BrandColors.FONT_SIZE_REGULAR};
                        font-family: {BrandColors.FONT_FAMILY};
                    }}
                    QPushButton:hover {{
                        color: {BrandColors.TEXT_PRIMARY};
                    }}
                    """
                )

    def set_expanded(self, expanded: bool) -> None:
        self.children.setVisible(bool(expanded) and bool(self._card_buttons))

    def _update_top_button_style(self) -> None:
        if self._active:
            bg = BrandColors.CATEGORY_ACTIVE_BG
            border = BrandColors.CATEGORY_ACTIVE_BORDER
            fg = BrandColors.TEXT_PRIMARY
            weight = "700"
        else:
            bg = "transparent"
            border = "transparent"
            fg = BrandColors.TEXT_SECONDARY
            weight = "500"

        self.top_button.setStyleSheet(
            f"""
            QPushButton {{
                text-align: left;
                background-color: {bg};
                color: {fg};
                border: 1px solid {border};
                border-radius: 8px;
                padding: 12px 14px;
                font-size: {BrandColors.FONT_SIZE_LARGE};
                font-family: {BrandColors.FONT_FAMILY};
                font-weight: {weight};
            }}
            QPushButton:hover {{
                background-color: {BrandColors.ITEM_HOVER if not self._active else BrandColors.CATEGORY_ACTIVE_BG};
                color: {BrandColors.TEXT_PRIMARY};
            }}
            """
        )

        if self._icon_file:
            icon_color = BrandColors.TEXT_PRIMARY if self._active else BrandColors.TEXT_SECONDARY
            self.top_button.setIcon(self._icon_loader(self._icon_file, icon_color))
            self.top_button.setIconSize(QSize(18, 18))


class _FlowLayout(QLayout):
    def __init__(self, parent=None, margin=0, spacing=8):
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
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

            next_x = x + item.sizeHint().width() + self.spacing()
            if line_height > 0 and next_x - self.spacing() > rect.right() and x > rect.x():
                x = rect.x()
                y = y + line_height + self.spacing()
                next_x = x + item.sizeHint().width() + self.spacing()
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))

            x = next_x
            line_height = max(line_height, item.sizeHint().height())

        return y + line_height - rect.y()


class _FlowLayoutHost(QWidget):
    def __init__(self, spacing: int = 8, parent=None):
        super().__init__(parent)
        self._flow_layout = _FlowLayout(self, margin=0, spacing=spacing)
        self.setLayout(self._flow_layout)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    @property
    def flow_layout(self) -> _FlowLayout:
        return self._flow_layout

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._flow_layout.heightForWidth(width)

    def sizeHint(self):
        parent_width = self.parentWidget().contentsRect().width() if self.parentWidget() is not None else 0
        min_width = self._flow_layout.minimumSize().width()
        width = max(min_width, int(parent_width or self.width() or 0))
        height = max(42, self._flow_layout.heightForWidth(width))
        return QSize(width, height)

    def minimumSizeHint(self):
        min_width = self._flow_layout.minimumSize().width()
        min_height = max(42, self._flow_layout.heightForWidth(max(1, min_width)))
        return QSize(min_width, min_height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._flow_layout.invalidate()
        self.updateGeometry()


class _BehaviorSectionDivider(QWidget):
    def __init__(self, title: str, icon_file: str, parent=None):
        super().__init__(parent)
        self._title = str(title or "")
        self._icon_file = str(icon_file or "")
        self.setMinimumHeight(44)

        self._icon_label = QLabel(self)
        self._icon_label.setStyleSheet("background-color: transparent;")
        self._icon_label.setFixedSize(18, 18)

        self._text_label = QLabel(self._title, self)
        self._text_label.setStyleSheet(
            f"""
            color: {BrandColors.TEXT_PRIMARY};
            font-size: {BrandColors.FONT_SIZE_LARGE};
            font-weight: 700;
            background-color: {BrandColors.SIDEBAR_BG};
            padding: 0 6px;
            """
        )

        pixmap = IconUtils.get_pixmap(
            self._icon_file,
            color=BrandColors.ACCENT,
            size=18,
            dpr=self.devicePixelRatioF(),
        )
        if not pixmap.isNull():
            self._icon_label.setPixmap(pixmap)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        inset = max(48, int(self.width() * 0.15))
        text_size = self._text_label.sizeHint()
        icon_size = self._icon_label.size()
        total_width = icon_size.width() + 10 + text_size.width()
        y = (self.height() - max(icon_size.height(), text_size.height())) // 2
        self._icon_label.move(inset, y + max(0, (text_size.height() - icon_size.height()) // 2))
        self._text_label.setGeometry(
            inset + icon_size.width() + 10,
            y,
            text_size.width(),
            text_size.height(),
        )
        self._content_bounds = QRect(inset, y, total_width, max(icon_size.height(), text_size.height()))

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(QColor(BrandColors.INPUT_BORDER), 1))

        gap_rect = getattr(self, "_content_bounds", QRect())
        gap_left = max(0, gap_rect.left() - 12)
        gap_right = min(self.width(), gap_rect.right() + 12)
        center_y = self.height() // 2

        if gap_left > 0:
            painter.drawLine(0, center_y, gap_left, center_y)
        if gap_right < self.width():
            painter.drawLine(gap_right, center_y, self.width(), center_y)
        painter.end()


class _SettingInfoBubble(QWidget):
    clicked = Signal(str)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._docs_url = ""
        self._arrow_edge = "bottom"
        self._arrow_x = 24
        self._arrow_size = 10
        self._current_anchor = None

        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_NoMousePropagation, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.hide()

        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity_effect)

        self._pos_anim = QPropertyAnimation(self, b"pos")
        self._pos_anim.setDuration(160)
        self._pos_anim.setEasingCurve(QEasingCurve.OutCubic)

        self._opacity_anim = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._opacity_anim.setDuration(160)
        self._opacity_anim.setEasingCurve(QEasingCurve.OutCubic)

        self._anim_group = QParallelAnimationGroup(self)
        self._anim_group.addAnimation(self._pos_anim)
        self._anim_group.addAnimation(self._opacity_anim)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(16, 14, 16, 14 + self._arrow_size)
        self._layout.setSpacing(6)

        self._title = QLabel("")
        self._title.setWordWrap(True)
        self._title.setTextFormat(Qt.PlainText)
        self._title.setStyleSheet(
            f"""
            color: {BrandColors.ACCENT};
            font-size: {BrandColors.FONT_SIZE_LARGE};
            font-weight: 700;
            background-color: transparent;
            """
        )
        self._title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._layout.addWidget(self._title)

        self._body = QLabel("")
        self._body.setWordWrap(True)
        self._body.setTextFormat(Qt.RichText)
        self._body.setStyleSheet(
            f"""
            color: {BrandColors.TEXT_SOFT};
            font-size: {BrandColors.FONT_SIZE_SMALL};
            background-color: transparent;
            """
        )
        self._body.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._layout.addWidget(self._body)

        footer = QWidget()
        footer.setStyleSheet("background-color: #111214;")
        footer.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 4, 0, 0)
        footer_layout.setSpacing(6)

        self._footer_text = QLabel("Click for More Info")
        self._footer_text.setStyleSheet(
            f"""
            color: {BrandColors.TEXT_SECONDARY};
            font-size: {BrandColors.FONT_SIZE_SMALL};
            background-color: #111214;
            """
        )
        self._footer_text.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        footer_layout.addWidget(self._footer_text, 0)

        self._footer_icon = QLabel()
        self._footer_icon.setStyleSheet("background-color: #111214;")
        self._footer_icon.setFixedSize(12, 12)
        self._footer_icon.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        footer_layout.addWidget(self._footer_icon, 0)
        footer_layout.addStretch(1)
        self._layout.addWidget(footer)

    def _resize_to_content_width(self, width: int) -> QSize:
        margins = self._layout.contentsMargins()
        content_width = max(1, int(width) - margins.left() - margins.right())

        for label in (self._title, self._body):
            # Clear the old height first cause otherwise QLabel keeps reporting the
            # previous tall fixed height when the next message is shorter
            label.setMinimumHeight(0)
            label.setMaximumHeight(16777215)
            label.setFixedWidth(content_width)
            if label.hasHeightForWidth():
                label.setFixedHeight(max(0, label.heightForWidth(content_width)))
            else:
                label.setFixedHeight(max(0, label.sizeHint().height()))
            label.updateGeometry()

        self._layout.invalidate()
        self._layout.activate()
        height = self._layout.sizeHint().height()
        self.setFixedSize(int(width), height)
        return QSize(int(width), height)

    def set_anchor(self, anchor_widget: QWidget | None):
        self._current_anchor = anchor_widget

    def show_for(
        self,
        anchor_widget: QWidget,
        title: str,
        body: str,
        docs_url: str | None,
        preferred_global_pos: QPoint | None = None,
    ) -> None:
        self._current_anchor = anchor_widget
        self._docs_url = str(docs_url or "").strip()
        self._title.setText(str(title or ""))
        self._body.setText(render_tooltip_text(body))
        self._footer_text.setVisible(bool(self._docs_url))
        self._footer_icon.setVisible(bool(self._docs_url))

        icon = IconUtils.get_pixmap(
            "external-link.svg",
            color=BrandColors.TEXT_SECONDARY,
            size=12,
            dpr=self.devicePixelRatioF(),
        )
        if not icon.isNull():
            self._footer_icon.setPixmap(icon)

        parent = self.parentWidget()
        if parent is None:
            return

        width = min(360, max(280, parent.width() - 24))
        self._layout.setContentsMargins(16, 14, 16, 14 + self._arrow_size)
        bubble_size = self._resize_to_content_width(width)

        anchor_rect = QRect(
            parent.mapFromGlobal(anchor_widget.mapToGlobal(QPoint(0, 0))),
            anchor_widget.size(),
        )
        pointer_local = parent.mapFromGlobal(preferred_global_pos or QCursor.pos())
        spacing = 12

        place_above = (pointer_local.y() + spacing + bubble_size.height()) > parent.height()
        if place_above:
            self._arrow_edge = "bottom"
            final_y = min(anchor_rect.top() - bubble_size.height() - spacing, pointer_local.y() - bubble_size.height() - 14)
            final_y = max(8, final_y)
            self._layout.setContentsMargins(16, 14, 16, 14 + self._arrow_size)
        else:
            self._arrow_edge = "top"
            final_y = max(anchor_rect.bottom() + spacing, pointer_local.y() + 14)
            final_y = min(parent.height() - bubble_size.height() - 8, final_y)
            self._layout.setContentsMargins(16, 14 + self._arrow_size, 16, 14)

        final_x = pointer_local.x() - min(36, bubble_size.width() // 5)
        final_x = max(8, min(final_x, parent.width() - bubble_size.width() - 8))
        self._arrow_x = max(18, min(pointer_local.x() - final_x, bubble_size.width() - 18))

        final_pos = QPoint(final_x, final_y)
        start_pos = QPoint(final_x, final_y + 8)

        self.move(start_pos)
        self.show()
        self.raise_()

        self._pos_anim.stop()
        self._opacity_anim.stop()
        self._pos_anim.setStartValue(start_pos)
        self._pos_anim.setEndValue(final_pos)
        self._opacity_anim.setStartValue(0.0)
        self._opacity_anim.setEndValue(1.0)
        self._anim_group.start()
        self.update()

    def hide_now(self) -> None:
        self._anim_group.stop()
        self.hide()
        self._current_anchor = None

    def contains_global(self, global_pos: QPoint) -> bool:
        local = self.mapFromGlobal(global_pos)
        return self.rect().contains(local)

    def mousePressEvent(self, event):
        event.accept()
        if self._docs_url:
            self.clicked.emit(self._docs_url)

    def mouseReleaseEvent(self, event):
        event.accept()

    def mouseMoveEvent(self, event):
        event.accept()

    def enterEvent(self, event):
        event.accept()
        super().enterEvent(event)

    def leaveEvent(self, event):
        event.accept()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        bg = QColor("#111214")
        border = QColor(BrandColors.INPUT_BORDER)
        rect = self.rect()

        if self._arrow_edge == "top":
            bubble_rect = rect.adjusted(0, self._arrow_size, 0, 0)
            triangle = QPolygon(
                [
                    QPoint(self._arrow_x, 0),
                    QPoint(self._arrow_x - self._arrow_size, self._arrow_size),
                    QPoint(self._arrow_x + self._arrow_size, self._arrow_size),
                ]
            )
        else:
            bubble_rect = rect.adjusted(0, 0, 0, -self._arrow_size)
            triangle = QPolygon(
                [
                    QPoint(self._arrow_x, rect.height()),
                    QPoint(self._arrow_x - self._arrow_size, rect.height() - self._arrow_size),
                    QPoint(self._arrow_x + self._arrow_size, rect.height() - self._arrow_size),
                ]
            )

        painter.setPen(QPen(border, 1))
        painter.setBrush(QBrush(bg))
        painter.drawRoundedRect(bubble_rect, 10, 10)
        painter.drawPolygon(triangle)
        painter.end()

class SettingsWindow(QMainWindow):
    settings_saved = Signal(set)
    restart_requested = Signal()
    update_check_finished = Signal(object, str)
    MIN_WINDOW_WIDTH = 780
    MIN_WINDOW_HEIGHT = 560
    SIDEBAR_WIDTH_MAXIMIZED = 320
    SIDEBAR_WIDTH_WIDE = 296
    SIDEBAR_WIDTH_DEFAULT = 280
    SIDEBAR_WIDTH_COMPACT = 256

    SIDEBAR_ICON_MAP = {
        "providers_credentials": "key.svg",
        "formatting": "type.svg",
        "deepseek_behavior": "providers/deepseek.svg",
        "glm_behavior": "providers/zai.svg",
        "moonshot_behavior": "providers/moonshot.svg",
        "qwen_behavior": "providers/qwen.svg",
        "perplexity_behavior": "providers/perplexity.svg",
        "huggingchat_behavior": "providers/huggingface.svg",
        "aistudio_behavior": "providers/aistudio.svg",
        "mimo_behavior": "providers/xiaomi.svg",
        "diagnostics": "bug.svg",
        "logfiles": "file.svg",
        "application_settings": "settings.svg",
        "system_settings": "monitor.svg",
        "console_settings": "terminal.svg",
        "console_dumping": "download.svg",
        "network_settings": "share-2.svg",
        "experimental": "flask-conical.svg",
        "runtime": "circle-gauge.svg",
    }

    BEHAVIOR_CATEGORY_BY_PROVIDER = {
        provider: get_behavior_category_for_provider(provider)
        for provider in DriverProvider
        if get_behavior_category_for_provider(provider)
    }
    PROVIDER_BY_BEHAVIOR_CATEGORY = {
        behavior_key: provider
        for provider, behavior_key in BEHAVIOR_CATEGORY_BY_PROVIDER.items()
    }


    def __init__(self, config_manager: ConfigManager, parent=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self.setWindowTitle("Settings")
        self.resize(900, 700)
        self.setMinimumSize(self.MIN_WINDOW_WIDTH, self.MIN_WINDOW_HEIGHT)
        self.setStyleSheet(f"background-color: {BrandColors.WINDOW_BG}; color: {BrandColors.TEXT_PRIMARY};")
        
        self.unsaved_changes = False
        self.field_widgets = {} # Map "category.key" -> widget
        self.setting_rows = {} # Map "category.key" -> SettingRow (for dependency toggling)
        self.category_widgets = {}
        self.category_widgets_by_key = {}
        self.category_items_by_key = {}
        self._built_category_keys = set()
        self._pending_category_build_keys = []
        self._category_build_timer = QTimer(self)
        self._category_build_timer.setSingleShot(True)
        self._category_build_timer.timeout.connect(self._build_next_queued_category)
        self._category_defs_by_key = {category.key: category for category in SCHEMA}
        self._category_order = [category.key for category in SCHEMA]
        self._section_defs = list(SETTINGS_SECTIONS)
        self._section_defs_by_key = {section.key: section for section in self._section_defs}
        self._card_defs_by_key = dict(SETTINGS_CARDS)
        self._section_widgets = {}
        self._section_layouts = {}
        self._card_widgets = {}
        self._card_content_layouts = {}
        self._card_next_field_index = {}
        self._card_build_complete_keys = set()
        self._built_section_keys = set()
        self._pending_section_preload_cards = []
        self._sections_needing_activation_sync = set()
        self._settings_values_loaded = False
        self._sidebar_sections = {}
        self._selected_section_key = None
        self._selected_card_key = None
        self._field_locations = {}
        self._field_display_key = {}
        self._display_rows = {}
        self._dynamic_card_titles = {}
        self._provider_behavior_buttons = []
        self._provider_behavior_selected_key = None
        self._provider_behavior_user_selected = False
        self._provider_behavior_selector_card_key = "provider_defaults"
        self._provider_behavior_group_card_keys = {}
        self._provider_behavior_complete_keys = set()
        self._provider_behavior_selector_instances = []
        self._pending_provider_behavior_preload_keys = []
        self._loadout_dropdown_instances = []
        self._loadout_base_values_cache = {}
        self._loadout_editor_selected_names = {}
        self._loadout_editor_draft_by_provider = {}
        self._loadout_editor_formatting_card_key = "loadout_editor"
        self._runtime_parallelization_widgets = {}
        self._persistent_profile_entries = {}
        self._persistent_profile_options_loaded = False
        self._active_docs_focus_container = None
        self._suppress_dirty_tracking = False
        self._sidebar_icon_subdir_cache = {}
        self._info_anchor_by_object_id = {}
        self._info_bubble_widget_ids = set()
        self._navigation_anchor_by_object_id = {}
        self._active_navigation_anchor = None
        self._last_focus_reason = None

        self._init_ui()
        self._load_values()
        self.update_check_finished.connect(self._handle_update_check_result)
        self._update_check_in_progress = False
        self._sync_application_settings_info()

        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._on_app_focus_changed)
            app.installEventFilter(self)

    def _get_sidebar_icon(self, icon_file: str, color: str, size: int = 18) -> QIcon:
        use_sidebar_subdir = ("/" not in icon_file) and ("\\" not in icon_file)
        sidebar_subdir = None
        if use_sidebar_subdir:
            cached_subdir = self._sidebar_icon_subdir_cache.get(icon_file)
            if cached_subdir is None:
                cached_subdir = "sidebar" if Path(IconUtils._icon_path(icon_file, subdir="sidebar")).exists() else ""
                self._sidebar_icon_subdir_cache[icon_file] = cached_subdir
            sidebar_subdir = cached_subdir or None
        icon = IconUtils.get_icon(
            icon_file,
            color=color,
            size=size,
            widget=self,
            subdir=sidebar_subdir,
        )
        if icon.isNull() and use_sidebar_subdir and sidebar_subdir:
            icon = IconUtils.get_icon(
                icon_file,
                color=color,
                size=size,
                widget=self,
            )
        return icon

    def _create_card_header(self, category_key: str, title: str) -> QWidget:
        header = QWidget()
        header.setStyleSheet("background-color: transparent;")

        layout = QHBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        icon_file = self.SIDEBAR_ICON_MAP.get(category_key)
        if icon_file:
            icon_size = 20
            icon_label = QLabel()
            icon_label.setStyleSheet("background-color: transparent;")
            icon_label.setFixedSize(icon_size, icon_size)
            icon = self._get_sidebar_icon(icon_file, BrandColors.TEXT_PRIMARY, size=icon_size)
            icon_label.setPixmap(icon.pixmap(icon_size, icon_size))
            layout.addWidget(icon_label, 0, Qt.AlignVCenter)

        title_label = QLabel(title)
        title_label.setStyleSheet(f"""
            font-size: {BrandColors.FONT_SIZE_TITLE};
            font-weight: 700;
            letter-spacing: 0.5px;
            color: {BrandColors.TEXT_PRIMARY};
            background-color: transparent;
        """)
        layout.addWidget(title_label, 1, Qt.AlignVCenter)

        return header

    def _has_immediate_subdivider(self, fields) -> bool:
        """
        Returns True when the first meaningful field in a category is a subsection divider.
        In that case, rendering a header underline tends to look like a duplicated divider.
        """
        for field in fields or []:
            if field.type == SettingType.DESCRIPTION:
                continue
            return field.type == SettingType.DIVIDER
        return False

    def _apply_category_item_icon(self, item: QListWidgetItem, active: bool):
        if not item:
            return

        icon_file = item.data(Qt.UserRole + 1)
        if not icon_file:
            return

        color = BrandColors.TEXT_PRIMARY if active else BrandColors.TEXT_SECONDARY
        item.setIcon(self._get_sidebar_icon(icon_file, color))

    def _on_category_selection_changed(self, current: QListWidgetItem, previous: QListWidgetItem):
        current_key = str(current.data(Qt.UserRole)) if current is not None else ""
        if current_key:
            self._ensure_category_built(current_key, refresh_profiles=(current_key == "system_settings"))
        self._apply_category_item_icon(previous, active=False)
        self._apply_category_item_icon(current, active=True)
        self._sync_paged_settings_view(scroll_to_top=self._should_use_paged_settings_view())
        self._queue_visible_category_builds()

    def _get_field_docs_url(self, field) -> str | None:
        docs_path = str(getattr(field, "docs_path", "") or "").strip()
        if not docs_path:
            return None
        docs_anchor = str(getattr(field, "docs_anchor", "") or "").strip() or None
        return build_docs_url(docs_path, docs_anchor)

    def _get_field_front_tooltip(self, field) -> str:
        return str(getattr(field, "front_tooltip", "") or "").strip()

    def _get_field_inline_description(self, field, *, fallback_to_tooltip: bool = False) -> str | None:
        front_tooltip = self._get_field_front_tooltip(field)
        if front_tooltip:
            return front_tooltip
        if fallback_to_tooltip:
            tooltip = str(getattr(field, "tooltip", "") or "").strip()
            if tooltip:
                return tooltip
        return None

    def _tag_docs_widget(self, widget: QWidget | None, docs_url: str | None) -> None:
        if widget is None or not docs_url:
            return
        widget.setProperty("docsUrl", docs_url)

    def _open_docs_from_sender(self) -> bool:
        sender = self.sender()
        docs_url = str(sender.property("docsUrl") or "").strip() if sender is not None else ""
        if not docs_url:
            return False
        return self._open_docs_url(docs_url)

    def _open_docs_url(self, docs_url: str | None) -> bool:
        prepared = str(docs_url or "").strip()
        if not prepared:
            return False
        return bool(QDesktopServices.openUrl(QUrl(prepared)))

    def _docs_url_for_widget(self, widget: QWidget | None) -> str | None:
        current = widget
        while current is not None:
            docs_url = str(current.property("docsUrl") or "").strip()
            if docs_url:
                return docs_url
            current = current.parentWidget()
        return None

    def _docs_container_for_widget(self, widget: QWidget | None):
        current = widget
        while current is not None:
            if hasattr(current, "set_help_focus_visible") and hasattr(current, "help_button"):
                return current
            current = current.parentWidget()
        return None

    def _on_app_focus_changed(self, _old, new) -> None:
        next_container = self._docs_container_for_widget(new)
        previous_container = getattr(self, "_active_docs_focus_container", None)
        if previous_container is next_container:
            self._sync_active_navigation_highlight(new)
            return

        if previous_container is not None and hasattr(previous_container, "set_help_focus_visible"):
            previous_container.set_help_focus_visible(False)

        self._active_docs_focus_container = next_container
        if next_container is not None and hasattr(next_container, "set_help_focus_visible"):
            next_container.set_help_focus_visible(True)
        self._sync_active_navigation_highlight(new)

    def _register_navigation_anchor(self, widget: QWidget | None) -> None:
        if widget is None:
            return

        widget_id = id(widget)
        self._navigation_anchor_by_object_id[widget_id] = widget
        widget.destroyed.connect(lambda *_args, wid=widget_id: self._on_navigation_anchor_destroyed(wid))

    def _on_navigation_anchor_destroyed(self, widget_id: int) -> None:
        anchor = self._navigation_anchor_by_object_id.pop(widget_id, None)
        if anchor is not None and anchor is self._active_navigation_anchor:
            self._set_active_navigation_highlight(None)

    def _resolve_navigation_anchor(self, widget: QWidget | None):
        current = widget
        while current is not None:
            anchor = self._navigation_anchor_by_object_id.get(id(current))
            if anchor is not None:
                return anchor
            current = current.parentWidget()
        return None

    def _should_auto_scroll_for_focus_reason(self, reason) -> bool:
        return self._should_show_navigation_highlight_for_focus_reason(reason)

    def _should_show_navigation_highlight_for_focus_reason(self, reason) -> bool:
        return reason in {
            Qt.TabFocusReason,
            Qt.BacktabFocusReason,
            Qt.ShortcutFocusReason,
            Qt.OtherFocusReason,
        }

    def _pulse_highlight_if_current(self, widget: QWidget) -> None:
        if widget is None or widget is not self._active_navigation_anchor:
            return
        self._flash_widget(widget)

    def _set_active_navigation_highlight(
        self,
        widget: QWidget | None,
        *,
        auto_scroll: bool = False,
        pulse: bool = False,
    ) -> None:
        overlay = getattr(self, "_highlight_overlay", None)
        if not overlay or not isinstance(overlay, _SearchHighlightOverlay):
            return

        if widget is None:
            self._active_navigation_anchor = None
            overlay.set_persistent_widget(None)
            return

        try:
            if widget.isHidden():
                return
        except RuntimeError:
            return

        same_target = widget is self._active_navigation_anchor
        self._active_navigation_anchor = widget
        overlay.set_persistent_widget(widget)

        duration_ms = 280
        started = False
        if auto_scroll and not same_target:
            started = self._smooth_ensure_visible(widget, y_margin=80, duration_ms=duration_ms)

        if pulse:
            if started:
                QTimer.singleShot(duration_ms, lambda w=widget: self._pulse_highlight_if_current(w))
            else:
                self._pulse_highlight_if_current(widget)

    def _sync_active_navigation_highlight(self, widget: QWidget | None) -> None:
        if widget is None:
            self._set_active_navigation_highlight(None)
            return

        if bool(widget.windowFlags() & Qt.Popup):
            return

        anchor = self._resolve_navigation_anchor(widget)
        if anchor is not None:
            if self._should_show_navigation_highlight_for_focus_reason(self._last_focus_reason):
                self._set_active_navigation_highlight(anchor)
            else:
                self._set_active_navigation_highlight(None)
            return

        if widget is self or self.isAncestorOf(widget):
            self._set_active_navigation_highlight(None)
            return

        self._set_active_navigation_highlight(None)

    def _open_docs_for_focused_widget(self) -> bool:
        focus_widget = self.focusWidget()
        docs_url = self._docs_url_for_widget(focus_widget)
        return self._open_docs_url(docs_url)

    def open_docs_for_shortcut(self) -> bool:
        return self._open_docs_for_focused_widget()

    def present(self) -> None:
        maximize = bool(self.config_manager.get_setting("application_settings", "open_settings_full_screen"))
        if not self.isVisible():
            if maximize:
                self.showMaximized()
            else:
                self.showNormal()
                self.show()
        else:
            if maximize and not self.isMaximized():
                self.showMaximized()
        self.activateWindow()
        self.raise_()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._update_responsive_layout()
        self._sync_section_visibility()
        timer = getattr(self, "_section_preload_timer", None)
        if self._pending_section_preload_cards and isinstance(timer, QTimer):
            timer.start(80)
        timer = getattr(self, "_provider_behavior_preload_timer", None)
        if self._pending_provider_behavior_preload_keys and isinstance(timer, QTimer):
            timer.start(120)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_responsive_layout()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            self._update_responsive_layout()
            QTimer.singleShot(0, self._update_responsive_layout)

    def _sidebar_width_for_window(self) -> int:
        if self.isFullScreen() or self.isMaximized():
            return self.SIDEBAR_WIDTH_MAXIMIZED

        width = max(0, int(self.width()))
        if width >= 1180:
            return self.SIDEBAR_WIDTH_WIDE
        if width >= 940:
            return self.SIDEBAR_WIDTH_DEFAULT
        return self.SIDEBAR_WIDTH_COMPACT

    def _update_responsive_layout(self) -> None:
        sidebar = getattr(self, "_sidebar_panel", None)
        if sidebar is None:
            return

        desired_width = self._sidebar_width_for_window()
        if sidebar.width() != desired_width:
            sidebar.setFixedWidth(desired_width)

    def _focus_search_input(self) -> None:
        self.search_input.setFocus(Qt.ShortcutFocusReason)
        self.search_input.selectAll()

    def _dropdown_options_for_field(self, field, category_key: str) -> list[str]:
        options = [str(option) for option in (field.options or [])]
        if category_key == "providers_credentials" and field.key == "provider":
            return provider_options(include_locked=False, config_manager=self.config_manager)
        if category_key == "huggingchat_behavior" and field.key == "model":
            unavailable = "Model list unavailable, please successfully log into HuggingChat at least once"
            try:
                cache_path = Path(getattr(self.config_manager, "config_dir", "config_data")) / "cache" / "huggingchat_models.json"
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                cached_models = payload.get("models") if isinstance(payload, dict) else []
            except Exception:
                cached_models = []

            base_option = options[0] if options else "Current HuggingChat selection"
            dynamic_options = [base_option]
            seen = set(dynamic_options)
            for raw_model in cached_models if isinstance(cached_models, list) else []:
                model = str(raw_model or "").strip()
                if model and model not in seen:
                    seen.add(model)
                    dynamic_options.append(model)
            if len(dynamic_options) == 1:
                dynamic_options.append(unavailable)
            return dynamic_options

        deprecated_options = getattr(field, "deprecated_options", None)
        if deprecated_options and field.key == "model":
            dep_set = {str(o).strip().lower() for o in deprecated_options}
            toggle_widget = self.field_widgets.get(f"{category_key}.show_deprecated_models")
            if toggle_widget is not None and hasattr(toggle_widget, "isChecked"):
                show_deprecated = toggle_widget.isChecked()
            else:
                show_deprecated = bool(self.config_manager.get_setting(category_key, "show_deprecated_models"))

            saved_model = str(self.config_manager.get_setting(category_key, field.key) or "").strip().lower()
            current_widget = self.field_widgets.get(f"{category_key}.{field.key}")
            current_model = (
                current_widget.currentText().strip().lower()
                if current_widget is not None and hasattr(current_widget, "currentText")
                else ""
            )
            saved_is_deprecated = (saved_model in dep_set) or (current_model in dep_set)

            if not show_deprecated and not saved_is_deprecated:
                options = [opt for opt in options if str(opt).strip().lower() not in dep_set]

        return options

    def _refresh_dropdown_options_for_field(self, category_key: str, field) -> None:
        if field.type != SettingType.DROPDOWN:
            return
        full_key = f"{category_key}.{field.key}"
        widget = self.field_widgets.get(full_key)
        if not isinstance(widget, StyledComboBox):
            return

        options = self._dropdown_options_for_field(field, category_key)
        existing = [widget.itemText(index) for index in range(widget.count())]
        if existing == options:
            return

        current_text = widget.currentText()
        saved_value = str(self.config_manager.get_setting(category_key, field.key) or "")
        widget.blockSignals(True)
        try:
            widget.clear()
            for option in options:
                widget.addItem(str(option))
            deprecated_opts = getattr(field, "deprecated_options", None)
            if deprecated_opts:
                tooltip = getattr(field, "deprecated_tooltip", "Model has been retired") or "Model has been retired"
                configure_deprecated_combobox_items(widget, deprecated_opts, tooltip=tooltip)
            if current_text and widget.findText(current_text) >= 0:
                widget.setCurrentText(current_text)
            elif saved_value and widget.findText(saved_value) >= 0:
                widget.setCurrentText(saved_value)
        finally:
            widget.blockSignals(False)

    def refresh_dynamic_dropdown_options(self) -> None:
        for full_key, field in (self.field_defs or {}).items():
            try:
                category_key, _field_key = full_key.split(".", 1)
            except ValueError:
                continue
            self._refresh_dropdown_options_for_field(category_key, field)

    def _create_field_widget(self, field, category_key):
        widget = None
        docs_url = self._get_field_docs_url(field)
        if field.type == SettingType.BOOLEAN:
            widget = Tumbler()
            widget.stateChanged.connect(self._on_setting_changed)
            if (category_key == "experimental") and (field.key == "enable_loadouts"):
                widget.stateChanged.connect(self._on_loadouts_toggle_changed)
        elif field.type == SettingType.DIRECTORY:
            dialog_title = f"Select {field.label}" if field.label else "Select Directory"
            widget = DirectoryEntry(dialog_title=dialog_title)
            if field.key == "config_storage_custom_path":
                widget.setPlaceholderText("Custom config directory...")
            elif field.key == "condump_directory":
                widget.setPlaceholderText("Ask (leave blank)...")
            elif field.key == "log_dir":
                widget.setPlaceholderText("Default (logs)...")
            widget.textChanged.connect(self._on_setting_changed)
        elif field.type in [SettingType.STRING, SettingType.PASSWORD, SettingType.INTEGER]:
            widget = StyledLineEdit()
            if field.type == SettingType.PASSWORD:
                widget.setEchoMode(QLineEdit.Password)
            elif field.type == SettingType.INTEGER:
                from PySide6.QtGui import QIntValidator
                widget.setValidator(QIntValidator())

            if field.key == "config_storage_custom_path":
                widget.setPlaceholderText("Custom config directory...")
            elif field.key == "condump_directory":
                widget.setPlaceholderText("Ask (leave blank)...")
            widget.textChanged.connect(self._on_setting_changed)
        elif field.type == SettingType.TEXTAREA:
            widget = StyledTextEdit()
            widget.textChanged.connect(self._on_setting_changed)
        elif field.type == SettingType.DROPDOWN:
            widget = StyledComboBox()
            dropdown_options = self._dropdown_options_for_field(field, category_key)
            if dropdown_options:
                for option in dropdown_options:
                    widget.addItem(str(option))
            deprecated_opts = getattr(field, "deprecated_options", None)
            if deprecated_opts:
                tooltip = getattr(field, "deprecated_tooltip", "Model has been retired") or "Model has been retired"
                configure_deprecated_combobox_items(widget, deprecated_opts, tooltip=tooltip)
            if not getattr(field, "transient", False):
                widget.currentTextChanged.connect(self._on_setting_changed)

            if category_key == "providers_credentials" and field.key == "provider":
                widget.setIconSize(QSize(16, 16))
                for index in range(widget.count()):
                    provider_name = widget.itemText(index)
                    icon_file = {
                        "DeepSeek": "providers/deepseek.svg",
                        "GLM Chat": "providers/zai.svg",
                        "Moonshot": "providers/moonshot.svg",
                        "QwenLM": "providers/qwen.svg",
                        "Perplexity": "providers/perplexity.svg",
                        "HuggingChat": "providers/huggingface.svg",
                        "Google AI Studio": "providers/aistudio.svg",
                        "Xiaomi MiMo": "providers/xiaomi.svg",
                    }.get(provider_name)
                    if not icon_file:
                        continue
                    icon = IconUtils.get_icon(
                        icon_file,
                        color=BrandColors.TEXT_PRIMARY,
                        size=16,
                        widget=widget,
                    )
                    if not icon.isNull():
                        widget.setItemIcon(index, icon)
                widget.currentTextChanged.connect(lambda *_: self._sync_provider_behavior_default_page())
            
            # Specific logic for formatting preset
            if field.key == "formatting_preset":
                widget.currentTextChanged.connect(self._on_preset_changed)
            elif field.key == "config_storage_location":
                widget.currentTextChanged.connect(self._on_config_storage_location_changed)
            elif (category_key == "system_settings") and (field.key == "persistent_profile_to_delete"):
                widget.addItem("(Click to load saved profiles...)", "")
                widget.popupAboutToShow.connect(
                    lambda: self._maybe_refresh_persistent_profile_options(
                        "provider_login",
                        force=not self._persistent_profile_options_loaded,
                    )
                )
        elif field.type == SettingType.SWITCHER:
            widget = SegmentedSwitcher(
                field.options or [],
                default_value=str(field.default or ""),
            )
            if not getattr(field, "transient", False):
                widget.valueChanged.connect(self._on_setting_changed)
        elif field.type == SettingType.INPUT_PAIR:
            widget = InputPairsWidget(alternative_actions=field.alternative_actions)
            widget.pairsChanged.connect(self._on_setting_changed)
            widget.alternativeActionTriggered.connect(
                lambda action_name, field_key=field.key, widget=widget: self._on_input_pair_alternative_action(
                    category_key, field_key, widget, action_name
                )
            )
        elif field.type == SettingType.INPUT_LIST:
            placeholder = "account@example.com" if (
                category_key == "aistudio_behavior"
                and field.key == "paid_model_access_emails"
            ) else "127.0.0.1"
            widget = InputListWidget(placeholder=placeholder)
            widget.itemsChanged.connect(self._on_setting_changed)

        elif field.type == SettingType.MULTI_SELECT_DROPDOWN:
            options = []
            for option in field.options or []:
                option_label = str(option or "").strip()
                if not option_label:
                    continue
                option_key = option_label
                icon_file = None
                is_provider_selector = (
                    (
                        category_key == "network_settings"
                        and field.key == "accept_reasoning_effort_providers"
                    )
                    or (
                        category_key == "system_settings"
                        and field.key == "clear_persistent_profile_providers"
                    )
                )
                if is_provider_selector:
                    provider = DriverProvider.from_setting(option_label)
                    if provider is not None:
                        option_key = provider.value
                        option_label = provider.value
                        icon_file = self._provider_icon_file(provider)
                options.append(MarshmallowOption(key=option_key, label=option_label, icon_file=icon_file))

            if (category_key == "network_settings") and (field.key == "accept_reasoning_effort_providers"):
                widget = MarshmallowMultiSelectDropdown(
                    placeholder=self._format_reasoning_effort_provider_summary(0),
                    button_icon_file="sidebar/brain.svg",
                    popup_title="Select providers for API reasoning effort",
                    summary_formatter=self._format_reasoning_effort_provider_summary,
                )
            elif (category_key == "system_settings") and (field.key == "clear_persistent_profile_providers"):
                widget = MarshmallowMultiSelectDropdown(
                    placeholder=self._format_profile_clear_provider_summary(0),
                    button_icon_file="trash-2.svg",
                    popup_title="Select providers to clear",
                    summary_formatter=self._format_profile_clear_provider_summary,
                )
            else:
                widget = MarshmallowMultiSelectDropdown(
                    placeholder=f"Select {str(field.label or 'items').lower()}",
                )
            widget.set_options(options)
            if getattr(field, "transient", False):
                widget.set_selected_keys(field.default or [])
            if (category_key == "system_settings") and (field.key == "clear_persistent_profile_providers"):
                widget.selectionChanged.connect(lambda *_: self._sync_profile_clear_button_label())
            widget.selectionChanged.connect(self._on_setting_changed)

        elif field.type == SettingType.REDIRECT:
            btn_text = str(field.default) if field.default else "Open"
            display_description = self._get_field_inline_description(field, fallback_to_tooltip=True) or ""
            widget = RedirectCard(
                field.label,
                display_description,
                btn_text,
                info_body=field.tooltip or display_description,
                docs_url=docs_url,
                docs_handler=self._open_docs_from_sender,
            )
            if field.action == "open_credential_manager":
                widget.clicked.connect(self._open_credential_manager)
                
        elif field.type == SettingType.BUTTON:
            widget = StyledButton(field.label)
            # use the default value as button text if provided, else label
            btn_text = str(field.default) if field.default else field.label
            widget.setText(btn_text)
            if str(getattr(field, "button_height", "") or "").strip().lower() == "row":
                widget.setProperty("matchRowHeight", True)
            
            if field.action == "reset_injection":
                widget.clicked.connect(self._reset_injection)
            elif field.action == "reset_formatting":
                widget.clicked.connect(self._reset_formatting)
            elif field.action == "delete_selected_persistent_profile":
                widget.clicked.connect(self._delete_selected_persistent_profile)
                widget.setEnabled(self._persistent_profile_options_loaded)
            elif field.action == "clear_all_persistent_profiles":
                widget.clicked.connect(self._clear_all_persistent_profiles)
            elif field.action == "clear_selected_persistent_profiles":
                widget.clicked.connect(self._clear_selected_persistent_profiles)
            elif field.action == "check_for_updates":
                widget.clicked.connect(self._check_for_updates)
        
        if widget:
            full_key = f"{category_key}.{field.key}"
            self.field_widgets[full_key] = widget
            widget.setProperty("fullKey", full_key)
            self._tag_docs_widget(widget, docs_url)
            
        return widget

    def _iter_fields(self, fields):
        for field in fields:
            yield field
            if field.type == SettingType.ROW:
                yield from self._iter_fields(field.sub_fields)

    def _resolve_category_insert_index(self, category_key: str) -> int:
        insert_index = 0
        for key in self._category_order:
            if key == category_key:
                break
            if key in self.category_widgets_by_key:
                insert_index += 1
        return insert_index

    def _remember_field_location(
        self,
        section_key: str,
        card_key: str,
        category_key: str,
        field,
        *,
        provider_key: str | None = None,
    ) -> None:
        location = {
            "section_key": section_key,
            "card_key": card_key,
            "provider_key": provider_key,
        }
        self._field_locations[f"{category_key}.{field.key}"] = location
        if field.type == SettingType.ROW:
            for sub in field.sub_fields or []:
                self._field_locations[f"{category_key}.{sub.key}"] = location

    def _prime_field_navigation_metadata(self) -> None:
        for section in self._section_defs:
            if section.key == "provider_behavior":
                for behavior_key, groups in PROVIDER_BEHAVIOR_GROUPS.items():
                    for index, group in enumerate(groups):
                        card_key = f"provider_behavior::{behavior_key}::{index}"
                        self._dynamic_card_titles.setdefault(card_key, str(group.get("title") or "Provider"))
                        for field_key in group.get("fields", []):
                            field = self._resolve_field_def(behavior_key, field_key)
                            if field is None:
                                continue
                            self._remember_field_location(
                                section.key,
                                card_key,
                                behavior_key,
                                field,
                                provider_key=behavior_key,
                            )
                            self._register_search_target_for_field(
                                section.key,
                                card_key,
                                behavior_key,
                                field,
                                provider_key=behavior_key,
                            )
                continue

            for card_key in section.card_keys:
                card_def = self._card_defs_by_key.get(card_key)
                if card_def is None:
                    continue
                for category_key, field_key in list(getattr(card_def, "field_refs", None) or []):
                    field = self._resolve_field_def(category_key, field_key)
                    if field is None:
                        continue
                    self._remember_field_location(section.key, card_key, category_key, field)
                    self._register_search_target_for_field(section.key, card_key, category_key, field)

    def _register_search_target(self, category, field) -> None:
        extra_labels = ""
        if field.type == SettingType.ROW and field.sub_fields:
            extra_labels = " ".join(sub.label for sub in field.sub_fields if sub.label)

        self.search_targets.append({
            "label_lower": (field.label or "").lower(),
            "key_lower": (field.key or "").lower(),
            "category_lower": (category.name or "").lower(),
            "category_key_lower": (category.key or "").lower(),
            "category_key": category.key,
            "extra_lower": extra_labels.lower(),
            "full_key": f"{category.key}.{field.key}",
        })

    def _apply_field_value(self, category, field) -> None:
        if getattr(field, "transient", False):
            return

        key = f"{category.key}.{field.key}"
        widget = self.field_widgets.get(key)
        if widget is None:
            return

        value = self.config_manager.get_setting(category.key, field.key)
        widget.blockSignals(True)
        try:
            if field.type == SettingType.BOOLEAN:
                widget.setChecked(bool(value))
            elif field.type in [SettingType.STRING, SettingType.DIRECTORY, SettingType.PASSWORD, SettingType.INTEGER]:
                widget.setText(str(value) if value is not None else "")
            elif field.type == SettingType.TEXTAREA:
                widget.setPlainText(str(value) if value is not None else "")
            elif field.type == SettingType.DROPDOWN:
                widget.blockSignals(False)
                self._refresh_dropdown_options_for_field(category.key, field)
                widget.blockSignals(True)
                if value:
                    value_text = str(value)
                    if widget.findText(value_text) >= 0:
                        widget.setCurrentText(value_text)
            elif field.type == SettingType.SWITCHER and isinstance(widget, SegmentedSwitcher):
                widget.set_value(str(value or field.default or ""), emit=False, animate=False)
            elif field.type == SettingType.INPUT_PAIR:
                widget.set_pairs(value or [])
            elif field.type == SettingType.INPUT_LIST:
                widget.set_items(value or [])
            elif field.type == SettingType.MULTI_SELECT_DROPDOWN:
                widget.set_selected_keys(value or [])
        finally:
            widget.blockSignals(False)

    def _apply_category_values(self, category) -> None:
        for field in self._iter_fields(category.fields):
            self._apply_field_value(category, field)

    def _refresh_loaded_state(self, *, refresh_profiles: bool = False) -> None:
        self._update_dependencies()

        preset_widget = self.field_widgets.get("formatting.formatting_preset")
        if preset_widget:
            self._on_preset_changed(preset_widget.currentText())

        self._sync_config_storage_from_active_dir()
        self._sync_provider_behavior_default_page(force=True)
        self._sync_runtime_parallelization_widgets_from_config()
        self._refresh_loadout_editor_widgets()
        self._sync_application_settings_info()
        if refresh_profiles:
            self._maybe_refresh_persistent_profile_options(force=True)
        self._update_dirty_markers()

    def _build_category_card(self, category_key: str) -> QWidget | None:
        category = self._category_defs_by_key.get(category_key)
        if category is None:
            return None

        existing = self.category_widgets_by_key.get(category_key)
        if existing is not None:
            return existing

        card = QWidget()
        card.setStyleSheet(f"""
            QWidget {{
                background-color: {BrandColors.SIDEBAR_BG};
                border-radius: 8px;
            }}
        """)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(BrandColors.CARD_PADDING, 18, BrandColors.CARD_PADDING, BrandColors.CARD_PADDING)
        card_layout.setSpacing(4)  # now SettingRow/ToggleRow have their own internal padding

        self.category_widgets[category.name] = card
        self.category_widgets_by_key[category.key] = card

        header = self._create_card_header(category.key, category.name)
        card_layout.addWidget(header)

        if not self._has_immediate_subdivider(category.fields):
            divider = QFrame()
            divider.setFrameShape(QFrame.HLine)
            divider.setFrameShadow(QFrame.Sunken)
            divider.setFixedHeight(1)
            divider.setStyleSheet(f"background-color: {BrandColors.INPUT_BORDER}; border: none;")
            card_layout.addWidget(divider)
            card_layout.addSpacing(6)

        for field in category.fields:
            docs_url = self._get_field_docs_url(field)

            if field.type == SettingType.DIVIDER:
                widget = Divider(field.label)
                card_layout.addWidget(widget)
                continue

            if field.type == SettingType.DESCRIPTION:
                widget = Description(field.default)
                self.field_widgets[f"{category.key}.{field.key}"] = widget
                card_layout.addWidget(widget)
                continue

            if field.type == SettingType.HINT:
                widget = HintCard(
                    field.label,
                    field.default,
                    variant=getattr(field, "hint_variant", None) or "info",
                )
                self.field_widgets[f"{category.key}.{field.key}"] = widget
                self._tag_docs_widget(widget, docs_url)
                card_layout.addWidget(widget)
                continue

            if field.type == SettingType.REDIRECT:
                widget = self._create_field_widget(field, category.key)
                if widget:
                    self.setting_rows[f"{category.key}.{field.key}"] = widget
                    self._register_navigation_anchor(widget)
                    card_layout.addWidget(widget)
                continue

            if field.type == SettingType.ROW:
                sub_widgets = []
                if field.sub_fields:
                    for sub in field.sub_fields:
                        sub_w = self._create_field_widget(sub, category.key)
                        sub_widgets.append(sub_w)
                widget = MultiColumnRow(sub_widgets, field.ratios)
                inline_description = self._get_field_inline_description(field, fallback_to_tooltip=True) or ""
                widget.setToolTip(render_tooltip_text(inline_description))
                self._tag_docs_widget(widget, docs_url)

                self.field_widgets[f"{category.key}.{field.key}"] = widget
                row = SettingRow(
                    field.label,
                    widget,
                    field.tooltip,
                    description=self._get_field_inline_description(field),
                    docs_url=docs_url,
                    docs_handler=self._open_docs_from_sender,
                )
                self.setting_rows[f"{category.key}.{field.key}"] = row
                self._register_navigation_anchor(row)
                card_layout.addWidget(row)
                continue

            widget = self._create_field_widget(field, category.key)
            if widget:
                if field.type == SettingType.BOOLEAN:
                    row = ToggleRow(
                        field.label,
                        widget,
                        field.tooltip,
                        description=self._get_field_inline_description(field, fallback_to_tooltip=True),
                        docs_url=docs_url,
                        docs_handler=self._open_docs_from_sender,
                    )
                else:
                    row = SettingRow(
                        field.label,
                        widget,
                        field.tooltip,
                        description=self._get_field_inline_description(field),
                        docs_url=docs_url,
                        docs_handler=self._open_docs_from_sender,
                    )
                self.setting_rows[f"{category.key}.{field.key}"] = row
                self._register_navigation_anchor(row)
                card_layout.addWidget(row)

        item = self.category_items_by_key.get(category.key)
        if item and item.isHidden():
            card.setHidden(True)

        insert_index = self._resolve_category_insert_index(category.key)
        self.scroll_layout.insertWidget(insert_index, card)
        self._built_category_keys.add(category.key)
        return card

    def _ensure_category_built(self, category_key: str | None, *, refresh_profiles: bool = False) -> QWidget | None:
        normalized_key = str(category_key or "").strip()
        if not normalized_key:
            return None

        card = self.category_widgets_by_key.get(normalized_key)
        if card is None:
            card = self._build_category_card(normalized_key)
            category = self._category_defs_by_key.get(normalized_key)
            if card is not None and category is not None:
                previous_suppress = self._suppress_dirty_tracking
                self._suppress_dirty_tracking = True
                try:
                    self._apply_category_values(category)
                    self._refresh_loaded_state(refresh_profiles=refresh_profiles)
                finally:
                    self._suppress_dirty_tracking = previous_suppress
        elif refresh_profiles:
            self._maybe_refresh_persistent_profile_options(normalized_key, force=True)

        return card

    def _is_category_visible_in_sidebar(self, category_key: str) -> bool:
        item = self.category_items_by_key.get(category_key)
        return (item is None) or (not item.isHidden())

    def _queue_visible_category_builds(self) -> None:
        timer = getattr(self, "_category_build_timer", None)
        if not isinstance(timer, QTimer):
            return

        if self._should_use_paged_settings_view():
            self._pending_category_build_keys = []
            timer.stop()
            return

        visible_keys = [
            key for key in self._category_order
            if key not in self._built_category_keys and self._is_category_visible_in_sidebar(key)
        ]
        if not visible_keys:
            self._pending_category_build_keys = []
            timer.stop()
            return

        merged = [key for key in self._pending_category_build_keys if key in visible_keys]
        merged_set = set(merged)
        merged.extend(key for key in visible_keys if key not in merged_set)
        self._pending_category_build_keys = merged

        if not timer.isActive():
            timer.start(30 if not self.isVisible() else 0)

    def _build_next_queued_category(self) -> None:
        if self._should_use_paged_settings_view():
            self._pending_category_build_keys = []
            return

        while self._pending_category_build_keys:
            next_key = self._pending_category_build_keys.pop(0)
            if next_key in self._built_category_keys:
                continue
            if not self._is_category_visible_in_sidebar(next_key):
                continue

            self._ensure_category_built(next_key, refresh_profiles=False)
            if self._pending_category_build_keys and isinstance(getattr(self, "_category_build_timer", None), QTimer):
                self._category_build_timer.start(0)
            return

    def _resolve_search_target_widget(self, target: dict, *, build: bool = False):
        full_key = str(target.get("full_key") or "").strip()
        if not full_key:
            return None

        widget = self.setting_rows.get(full_key) or self.field_widgets.get(full_key)
        if widget is not None or not build:
            return widget

        category_key = str(target.get("category_key") or "").strip()
        if category_key:
            self._ensure_category_built(category_key, refresh_profiles=(category_key == "system_settings"))
        return self.setting_rows.get(full_key) or self.field_widgets.get(full_key)

    def _maybe_refresh_persistent_profile_options(self, category_key: str | None = None, *, force: bool = False) -> None:
        resolved_key = str(category_key or self._get_selected_category_key() or "").strip()
        if resolved_key not in {"system_settings", "provider_login"}:
            return
        if (not force) and self._persistent_profile_options_loaded:
            return

        select_widget = self.field_widgets.get("system_settings.persistent_profile_to_delete")
        if not isinstance(select_widget, StyledComboBox):
            return

        self._refresh_persistent_profile_options()

    def _preload_initial_categories(self, target_count: int = 2) -> None:
        desired = max(1, int(target_count))
        if self._should_use_paged_settings_view():
            desired = 1

        built = 0
        for key in self._category_order:
            if not self._is_category_visible_in_sidebar(key):
                continue
            card = self._ensure_category_built(key, refresh_profiles=(key == "system_settings"))
            if card is None:
                continue
            built += 1
            if built >= desired:
                break

    def _init_ui(self):
        self.search_targets = []
        self._search_target_keys = set()
        self.field_defs = {}
        self._dep_override_cache = {}
        self.is_auto_scrolling = False
        self._auto_scroll_reset_timer = QTimer()
        self._auto_scroll_reset_timer.setSingleShot(True)
        self._auto_scroll_reset_timer.timeout.connect(self._end_auto_scroll)

        for category in SCHEMA:
            for field in self._iter_fields(category.fields):
                self.field_defs[f"{category.key}.{field.key}"] = field
        self._prime_field_navigation_metadata()

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        left_widget = QWidget()
        self._sidebar_panel = left_widget
        left_widget.setStyleSheet(f"background-color: {BrandColors.SIDEBAR_BG};")
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(12, 12, 12, 0)
        left_layout.setSpacing(0)
        self._update_responsive_layout()

        self.sidebar_scroll = QScrollArea()
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setFrameShape(QFrame.NoFrame)
        self.sidebar_scroll.setStyleSheet(
            f"""
            QScrollArea {{
                background-color: {BrandColors.SIDEBAR_BG};
                border: none;
            }}
            QScrollBar:vertical {{
                border: none;
                background: {BrandColors.SIDEBAR_BG};
                width: 10px;
                margin: 0px;
            }}
            QScrollBar::handle:vertical {{
                background: #555555;
                border-radius: 5px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
            """
        )
        self.sidebar_content = QWidget()
        self.sidebar_layout = QVBoxLayout(self.sidebar_content)
        self.sidebar_layout.setContentsMargins(0, 0, 0, 12)
        self.sidebar_layout.setSpacing(4)
        self.sidebar_scroll.setWidget(self.sidebar_content)
        left_layout.addWidget(self.sidebar_scroll, 1)

        self.search_nav = QWidget()
        self.search_nav.setStyleSheet("background-color: transparent;")
        search_nav_layout = QHBoxLayout(self.search_nav)
        search_nav_layout.setContentsMargins(0, 0, 0, 6)
        search_nav_layout.setSpacing(6)
        search_nav_layout.addStretch(1)

        compact_button_style = f"""
            QPushButton {{
                background-color: {BrandColors.INPUT_BG};
                color: {BrandColors.TEXT_PRIMARY};
                border: 1px solid {BrandColors.INPUT_BORDER};
                border-radius: 6px;
                padding: 5px;
            }}
            QPushButton:hover {{
                border: 1px solid {BrandColors.CATEGORY_ACTIVE_BORDER};
                background-color: {BrandColors.ITEM_HOVER};
            }}
            QPushButton:disabled {{
                color: {BrandColors.TEXT_DISABLED};
                border: 1px solid {BrandColors.INPUT_BORDER};
            }}
        """

        self.search_prev_btn = QPushButton()
        self.search_prev_btn.setCursor(Qt.PointingHandCursor)
        self.search_prev_btn.setFixedSize(28, 28)
        self.search_prev_btn.setStyleSheet(compact_button_style)
        self.search_prev_btn.setIcon(
            IconUtils.get_icon("chevron-left.svg", color=BrandColors.TEXT_PRIMARY, size=14, widget=self.search_prev_btn)
        )
        self.search_prev_btn.clicked.connect(self._goto_previous_search_match)
        search_nav_layout.addWidget(self.search_prev_btn)

        self.search_status_label = QLabel("0 / 0")
        self.search_status_label.setStyleSheet(
            f"""
            color: {BrandColors.TEXT_SOFT};
            font-size: {BrandColors.FONT_SIZE_REGULAR};
            font-family: {BrandColors.FONT_FAMILY};
            padding: 0 2px;
            min-width: 52px;
            """
        )
        self.search_status_label.setAlignment(Qt.AlignCenter)
        search_nav_layout.addWidget(self.search_status_label)

        self.search_next_btn = QPushButton()
        self.search_next_btn.setCursor(Qt.PointingHandCursor)
        self.search_next_btn.setFixedSize(28, 28)
        self.search_next_btn.setStyleSheet(compact_button_style)
        self.search_next_btn.setIcon(
            IconUtils.get_icon("chevron-right.svg", color=BrandColors.TEXT_PRIMARY, size=14, widget=self.search_next_btn)
        )
        self.search_next_btn.clicked.connect(self._goto_next_search_match)
        search_nav_layout.addWidget(self.search_next_btn)
        self.search_nav.hide()
        left_layout.addWidget(self.search_nav, 0)

        self.search_bar = QWidget()
        self.search_bar.setStyleSheet(
            f"""
            QWidget {{
                background-color: {BrandColors.SIDEBAR_BG};
                border-top: 1px solid {BrandColors.INPUT_BORDER};
            }}
            """
        )
        search_layout = QHBoxLayout(self.search_bar)
        search_layout.setContentsMargins(0, 8, 0, 8)
        search_layout.setSpacing(6)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search settings...")
        self.search_input.setStyleSheet(
            f"""
            QLineEdit {{
                background-color: {BrandColors.INPUT_BG};
                color: {BrandColors.TEXT_PRIMARY};
                border: 2px solid {BrandColors.INPUT_BORDER};
                border-radius: 6px;
                padding: 7px 10px 7px 28px;
                font-size: {BrandColors.FONT_SIZE_REGULAR};
                font-family: {BrandColors.FONT_FAMILY};
            }}
            QLineEdit:focus {{
                border: 2px solid {BrandColors.ACCENT};
            }}
            """
        )
        self.search_input.addAction(
            IconUtils.get_icon(IconType.SEARCH, color=BrandColors.TEXT_SECONDARY, size=16, widget=self.search_input),
            QLineEdit.LeadingPosition,
        )
        self.search_clear_action = self.search_input.addAction(
            IconUtils.get_icon("x.svg", color=BrandColors.TEXT_SECONDARY, size=16, widget=self.search_input),
            QLineEdit.TrailingPosition,
        )
        self.search_clear_action.setVisible(False)
        self.search_clear_action.triggered.connect(self._clear_search)
        self.search_input.textChanged.connect(self._on_search_text_changed)
        search_layout.addWidget(self.search_input, 1)
        left_layout.addWidget(self.search_bar, 0)
        main_layout.addWidget(left_widget, 0)

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(30, 24, 30, 24)
        right_layout.setSpacing(0)

        self.scroll_area = SmoothScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._hide_info_bubble)
        self.scroll_area.setStyleSheet(
            f"""
            QScrollArea {{
                background-color: {BrandColors.WINDOW_BG};
                border: none;
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
            """
        )

        self.scroll_content = QWidget()
        self.scroll_content.setMaximumWidth(BrandColors.CONTENT_MAX_WIDTH)
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(0, 0, 10, 0)
        self.scroll_layout.setSpacing(BrandColors.CARD_SPACING)
        self.scroll_layout.setAlignment(Qt.AlignTop)
        self.scroll_area.setWidget(self.scroll_content)
        self.scroll_area.setAlignment(Qt.AlignHCenter)
        right_layout.addWidget(self.scroll_area, 1)

        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 18, 0, 0)
        button_layout.setSpacing(12)
        button_layout.addStretch(1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setCursor(Qt.PointingHandCursor)
        self.cancel_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {BrandColors.SIDEBAR_BG};
                color: {BrandColors.TEXT_PRIMARY};
                border: none;
                padding: 10px 20px;
                border-radius: 6px;
                font-size: {BrandColors.FONT_SIZE_REGULAR};
            }}
            QPushButton:hover {{
                background-color: {BrandColors.ITEM_HOVER};
            }}
            """
        )
        IconUtils.apply_icon(self.cancel_btn, IconType.CANCEL, BrandColors.TEXT_PRIMARY, size=16, y_offset=2)
        self.cancel_btn.clicked.connect(self.close)
        button_layout.addWidget(self.cancel_btn)

        self.save_btn = QPushButton("Save")
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {BrandColors.ACCENT};
                color: {BrandColors.TEXT_PRIMARY};
                border: none;
                padding: 10px 20px;
                border-radius: 6px;
                font-weight: 700;
                font-size: {BrandColors.FONT_SIZE_REGULAR};
            }}
            QPushButton:hover {{
                background-color: #4a80e0;
            }}
            """
        )
        IconUtils.apply_icon(self.save_btn, IconType.CONFIRM, BrandColors.TEXT_PRIMARY, size=16, y_offset=2)
        self.save_btn.clicked.connect(self.save_settings)
        button_layout.addWidget(self.save_btn)
        right_layout.addLayout(button_layout)
        main_layout.addWidget(right_widget, 1)

        self.update_timer = QTimer()
        self.update_timer.setSingleShot(True)
        self.update_timer.setInterval(100)
        self.update_timer.timeout.connect(self._update_dependencies)

        self.search_timer = QTimer()
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(260)
        self.search_timer.timeout.connect(self._perform_search)

        self._provider_behavior_preload_timer = QTimer(self)
        self._provider_behavior_preload_timer.setSingleShot(True)
        self._provider_behavior_preload_timer.timeout.connect(self._preload_next_provider_behavior_group)

        self._section_preload_timer = QTimer(self)
        self._section_preload_timer.setSingleShot(True)
        self._section_preload_timer.timeout.connect(self._preload_next_section_card)

        self.search_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        self.search_shortcut.activated.connect(self._focus_search_input)

        self._highlight_overlay = _SearchHighlightOverlay(self.scroll_area.viewport())
        self._highlight_overlay.setGeometry(self.scroll_area.viewport().rect())
        self._highlight_overlay.raise_()
        self.scroll_area.viewport().installEventFilter(self._highlight_overlay)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._highlight_overlay.update_target_geometry)

        self._info_bubble = _SettingInfoBubble(self.scroll_area.viewport())
        self._info_bubble.clicked.connect(self._open_docs_url)
        bubble_widgets = [self._info_bubble, *self._info_bubble.findChildren(QWidget)]
        self._info_bubble_widget_ids = {id(widget) for widget in bubble_widgets}
        for widget in bubble_widgets:
            widget.installEventFilter(self)
        self._info_timer = QTimer(self)
        self._info_timer.setSingleShot(True)
        self._info_timer.setInterval(360)
        self._info_timer.timeout.connect(self._show_pending_info_bubble)
        self._info_hide_timer = QTimer(self)
        self._info_hide_timer.setSingleShot(True)
        self._info_hide_timer.setInterval(140)
        self._info_hide_timer.timeout.connect(self._hide_info_bubble)
        self._pending_info_anchor = None
        self._active_info_anchor = None
        self._pending_info_pos = None
        self._search_matches = []
        self._search_match_index = -1

        self._build_sections()
        first_section = self._section_defs[0].key if self._section_defs else None
        if first_section:
            self._set_active_section(first_section, scroll_to_top=False)

    def _build_sections(self) -> None:
        for section in self._section_defs:
            sidebar = _SidebarSectionWidget(
                section.key,
                section.label,
                section.icon,
                self._get_sidebar_icon,
                parent=self.sidebar_content,
            )
            sidebar.set_cards(self._sidebar_cards_for_section(section.key))
            sidebar.section_requested.connect(self._on_sidebar_section_requested)
            sidebar.card_requested.connect(self._on_sidebar_card_requested)
            self.sidebar_layout.addWidget(sidebar)
            self._sidebar_sections[section.key] = sidebar

            section_widget = QWidget()
            section_layout = QVBoxLayout(section_widget)
            section_layout.setContentsMargins(0, 0, 0, 0)
            section_layout.setSpacing(BrandColors.CARD_SPACING)
            self.scroll_layout.addWidget(section_widget)
            self._section_widgets[section.key] = section_widget
            self._section_layouts[section.key] = section_layout

            section_layout.addStretch(1)

        self.sidebar_layout.addStretch(1)

    def _apply_card_field_values(self, category_key: str, field) -> None:
        category = self._category_defs_by_key.get(category_key)
        if category is None or field is None:
            return
        for item in self._iter_fields([field]):
            self._apply_field_value(category, item)

    def _apply_card_values(self, card_def) -> None:
        for category_key, field_key in list(getattr(card_def, "field_refs", None) or []):
            field = self._resolve_field_def(category_key, field_key)
            self._apply_card_field_values(category_key, field)

    def _is_standard_card(self, card_def) -> bool:
        return not bool(getattr(card_def, "special", None))

    def _build_next_standard_card_field(self, section_key: str, card_def, *, refresh_values: bool) -> bool:
        card_key = str(getattr(card_def, "key", "") or "")
        content_layout = self._card_content_layouts.get(card_key)
        if content_layout is None:
            return False

        refs = list(getattr(card_def, "field_refs", None) or [])
        next_index = int(self._card_next_field_index.get(card_key, 0) or 0)
        if next_index >= len(refs):
            self._card_build_complete_keys.add(card_key)
            return False

        category_key, field_key = refs[next_index]
        field = self._resolve_field_def(category_key, field_key)
        if field is not None:
            entry = self._build_field_entry(field, category_key, section_key, card_key)
            if entry is not None:
                content_layout.addWidget(entry)
            if refresh_values:
                previous_suppress = self._suppress_dirty_tracking
                self._suppress_dirty_tracking = True
                try:
                    self._apply_card_field_values(category_key, field)
                finally:
                    self._suppress_dirty_tracking = previous_suppress

        next_index += 1
        self._card_next_field_index[card_key] = next_index
        if next_index >= len(refs):
            self._card_build_complete_keys.add(card_key)
        return True

    def _finish_standard_card_fields(self, section_key: str, card_def, *, refresh_values: bool) -> None:
        card_key = str(getattr(card_def, "key", "") or "")
        while card_key not in self._card_build_complete_keys:
            if not self._build_next_standard_card_field(
                section_key,
                card_def,
                refresh_values=refresh_values,
            ):
                break

    def _mark_section_built_if_complete(self, section_key: str) -> None:
        section = self._section_defs_by_key.get(section_key)
        if section is not None and all(key in self._card_build_complete_keys for key in section.card_keys):
            self._built_section_keys.add(section_key)

    def _insert_card_widget(self, section_key: str, card_key: str, card_widget: QWidget) -> None:
        section_layout = self._section_layouts.get(section_key)
        if section_layout is None:
            return
        insert_index = max(0, section_layout.count() - 1)
        section_layout.insertWidget(insert_index, card_widget)
        self._card_widgets[card_key] = card_widget

    def _ensure_card_built(
        self,
        section_key: str,
        card_key: str,
        *,
        refresh_values: bool = False,
        background: bool = False,
    ) -> QWidget | None:
        existing = self._card_widgets.get(card_key)
        card_def = self._card_defs_by_key.get(card_key)
        if existing is not None:
            if card_def is not None and card_key not in self._card_build_complete_keys:
                if background and self._is_standard_card(card_def):
                    self._build_next_standard_card_field(
                        section_key,
                        card_def,
                        refresh_values=refresh_values,
                    )
                elif not background:
                    self._finish_standard_card_fields(section_key, card_def, refresh_values=refresh_values)
                self._mark_section_built_if_complete(section_key)
            return existing

        section_layout = self._section_layouts.get(section_key)
        if section_layout is None or card_def is None:
            return None

        if background and self._is_standard_card(card_def):
            card_widget = self._build_standard_card(section_key, card_def, field_refs=[])
            self._card_next_field_index[card_key] = 0
            self._insert_card_widget(section_key, card_key, card_widget)
            if not list(getattr(card_def, "field_refs", None) or []):
                self._card_build_complete_keys.add(card_key)
            else:
                self._build_next_standard_card_field(
                    section_key,
                    card_def,
                    refresh_values=refresh_values,
                )
        else:
            card_widget = self._build_card_widget(section_key, card_def)
            self._insert_card_widget(section_key, card_key, card_widget)
            self._card_build_complete_keys.add(card_key)

        if refresh_values and card_key in self._card_build_complete_keys:
            previous_suppress = self._suppress_dirty_tracking
            self._suppress_dirty_tracking = True
            try:
                self._apply_card_values(card_def)
                self._sync_application_settings_info()
                self._sync_profile_clear_button_label()
            finally:
                self._suppress_dirty_tracking = previous_suppress

        if background:
            self._sections_needing_activation_sync.add(section_key)

        self._mark_section_built_if_complete(section_key)

        return card_widget

    def _ensure_section_built(
        self,
        section_key: str | None,
        *,
        refresh_values: bool = False,
        background: bool = False,
    ) -> bool:
        resolved_key = str(section_key or "").strip()
        section = self._section_defs_by_key.get(resolved_key)
        if section is None:
            return False

        built_any = False
        for card_key in section.card_keys:
            if card_key in self._card_build_complete_keys:
                continue
            if self._ensure_card_built(
                resolved_key,
                card_key,
                refresh_values=refresh_values,
                background=background,
            ) is not None:
                built_any = True

        self._mark_section_built_if_complete(resolved_key)

        return built_any

    def _sync_section_after_activation(self, section_key: str, *, force: bool = False) -> None:
        resolved_key = str(section_key or "").strip()
        if not resolved_key:
            return
        needs_sync = force or (resolved_key in self._sections_needing_activation_sync)
        if not needs_sync:
            return

        previous_suppress = self._suppress_dirty_tracking
        self._suppress_dirty_tracking = True
        try:
            preset_widget = self.field_widgets.get("formatting.formatting_preset")
            if preset_widget:
                self._on_preset_changed(preset_widget.currentText())
            self._sync_config_storage_from_active_dir()
            self._sync_application_settings_info()
            self._sync_profile_clear_button_label()
            if resolved_key in {"formatting", "provider_behavior"}:
                self._refresh_loadout_editor_widgets()
            else:
                self._update_dependencies()
        finally:
            self._suppress_dirty_tracking = previous_suppress
        self._sections_needing_activation_sync.discard(resolved_key)

    def _queue_section_preload(self, *, exclude: str | None = None) -> None:
        excluded_key = str(exclude or "").strip()
        pending: list[tuple[str, str]] = []
        for section in self._section_defs:
            if section.key == excluded_key:
                continue
            for card_key in section.card_keys:
                if card_key not in self._card_build_complete_keys:
                    pending.append((section.key, card_key))

        self._pending_section_preload_cards = pending

        timer = getattr(self, "_section_preload_timer", None)
        if not isinstance(timer, QTimer):
            return
        if not pending:
            timer.stop()
            return

        timer.start(80 if self.isVisible() else 180)

    def _preload_next_section_card(self) -> None:
        if not self.isVisible():
            self._queue_section_preload(exclude=self._selected_section_key)
            return

        while self._pending_section_preload_cards:
            section_key, card_key = self._pending_section_preload_cards.pop(0)
            if section_key == self._selected_section_key:
                continue
            if card_key in self._card_build_complete_keys:
                continue
            self._ensure_card_built(
                section_key,
                card_key,
                refresh_values=self._settings_values_loaded,
                background=True,
            )
            if card_key not in self._card_build_complete_keys:
                self._pending_section_preload_cards.insert(0, (section_key, card_key))
            break

        if self._pending_section_preload_cards:
            timer = getattr(self, "_section_preload_timer", None)
            if isinstance(timer, QTimer):
                timer.start(24)

    def _sidebar_cards_for_section(self, section_key: str) -> list[tuple[str, str]]:
        section = self._section_defs_by_key.get(section_key)
        if section is None:
            return []

        if section_key == "formatting":
            cards: list[tuple[str, str]] = []
            for card_key in section.card_keys:
                if card_key not in self._card_defs_by_key:
                    continue
                widget = self._card_widgets.get(card_key)
                if widget is not None and widget.isHidden():
                    continue
                cards.append((card_key, self._card_defs_by_key[card_key].title))
            return cards

        if section_key != "provider_behavior":
            return [
                (card_key, self._card_defs_by_key[card_key].title)
                for card_key in section.card_keys
                if card_key in self._card_defs_by_key
            ]

        behavior_key = self._provider_behavior_selected_key or self.BEHAVIOR_CATEGORY_BY_PROVIDER.get(self._get_selected_provider()) or "deepseek_behavior"
        cards: list[tuple[str, str]] = []
        selector_def = self._card_defs_by_key.get(self._provider_behavior_selector_card_key)
        if selector_def is not None:
            cards.append((self._provider_behavior_selector_card_key, selector_def.title))
        for card_key in self._provider_behavior_group_card_keys.get(behavior_key, []):
            title = str(self._card_widgets.get(card_key).property("sidebarTitle") or "").strip()
            if title:
                cards.append((card_key, title))
        return cards

    def _build_card_widget(self, section_key: str, card_def):
        if getattr(card_def, "special", None) == "loadout_editor":
            return self._build_loadout_editor_card(section_key, card_def)
        if getattr(card_def, "special", None) == "provider_behavior":
            return self._build_provider_behavior_card(section_key, card_def)
        if getattr(card_def, "special", None) == "runtime_parallelization":
            return self._build_runtime_parallelization_card(section_key, card_def)
        return self._build_standard_card(section_key, card_def)

    def _provider_icon_file(self, provider: DriverProvider | None) -> str | None:
        return {
            DriverProvider.DEEPSEEK: "providers/deepseek.svg",
            DriverProvider.GLM_CHAT: "providers/zai.svg",
            DriverProvider.MOONSHOT: "providers/moonshot.svg",
            DriverProvider.QWEN_LM: "providers/qwen.svg",
            DriverProvider.PERPLEXITY: "providers/perplexity.svg",
            DriverProvider.HUGGINGCHAT: "providers/huggingface.svg",
            DriverProvider.AI_STUDIO: "providers/aistudio.svg",
            DriverProvider.MIMO: "providers/xiaomi.svg",
        }.get(provider)

    def _all_runtime_parallel_providers(self) -> list[DriverProvider]:
        return [
            DriverProvider.DEEPSEEK,
            DriverProvider.GLM_CHAT,
            DriverProvider.MOONSHOT,
            DriverProvider.QWEN_LM,
            DriverProvider.PERPLEXITY,
            DriverProvider.HUGGINGCHAT,
            DriverProvider.AI_STUDIO,
            DriverProvider.MIMO,
        ]

    def _available_runtime_parallel_providers(self) -> list[DriverProvider]:
        allowed = {
            DriverProvider.from_setting(option)
            for option in provider_options(include_locked=False, config_manager=self.config_manager)
        }
        current = self._get_selected_provider()
        if current is not None:
            allowed.add(current)
        return [provider for provider in self._all_runtime_parallel_providers() if provider in allowed]

    def _runtime_parallel_mode_from_config(self) -> str:
        saved_mode = str(self.config_manager.get_setting("runtime", "parallelization_mode") or "").strip()
        valid_modes = {key for key, _label in RUNTIME_PARALLEL_MODE_OPTIONS}
        if saved_mode in valid_modes:
            return saved_mode
        for key, label in RUNTIME_PARALLEL_MODE_OPTIONS:
            if saved_mode == label:
                return key
        return "provider_lanes"

    def _runtime_parallel_mode_key(self) -> str:
        combo = self._runtime_parallelization_widgets.get("mode")
        if isinstance(combo, StyledComboBox):
            data = combo.currentData()
            if data:
                return str(data)
            text = combo.currentText()
            for key, label in RUNTIME_PARALLEL_MODE_OPTIONS:
                if label == text:
                    return key
        return self._runtime_parallel_mode_from_config()

    def _set_combo_current_data(self, combo: StyledComboBox, key: str) -> None:
        for index in range(combo.count()):
            if str(combo.itemData(index) or "") == str(key or ""):
                combo.setCurrentIndex(index)
                return

    def _normalize_runtime_lane_value(self, value) -> tuple[list[DriverProvider], dict[DriverProvider, int]]:
        if not isinstance(value, dict):
            value = {}

        raw_providers = value.get("providers")
        if not isinstance(raw_providers, list):
            raw_providers = []

        raw_instances = value.get("instances")
        if not isinstance(raw_instances, dict):
            raw_instances = {}

        provider_set: set[DriverProvider] = set()
        for raw_provider in raw_providers:
            provider = DriverProvider.from_setting(raw_provider)
            if provider is not None:
                provider_set.add(provider)

        selected = [
            provider
            for provider in self._all_runtime_parallel_providers()
            if provider in provider_set
        ]

        counts: dict[DriverProvider, int] = {}
        for provider in self._all_runtime_parallel_providers():
            raw_count = raw_instances.get(provider.value, raw_instances.get(provider.name, 1))
            try:
                count = int(raw_count)
            except (TypeError, ValueError):
                count = 1
            counts[provider] = max(1, min(MAX_FULL_PARALLEL_PROVIDER_INSTANCES, count))

        return selected, counts

    def _runtime_lane_value_from_widget(self, lanes: RuntimeProviderLaneDropdown) -> dict:
        current_provider = self._get_selected_provider()
        selected = [
            provider.value
            for provider in lanes.selected_providers()
            if provider != current_provider
        ]
        counts = {
            provider.value: max(1, min(MAX_FULL_PARALLEL_PROVIDER_INSTANCES, int(count)))
            for provider, count in lanes.instance_counts().items()
        }
        return {"providers": selected, "instances": counts}

    def _normalized_runtime_lane_value_for_compare(self, value) -> dict:
        selected, counts = self._normalize_runtime_lane_value(value)
        return {
            "providers": [provider.value for provider in selected],
            "instances": {
                provider.value: counts.get(provider, 1)
                for provider in self._all_runtime_parallel_providers()
            },
        }

    def _selected_parallel_providers_from_config(self) -> list[DriverProvider]:
        current = self._get_selected_provider()
        selected, _counts = self._normalize_runtime_lane_value(
            self.config_manager.get_setting("runtime", "parallel_provider_lanes")
        )
        if current is not None and current not in selected:
            selected.append(current)
        return selected

    def _parallel_instance_counts_from_config(self) -> dict[DriverProvider, int]:
        _selected, counts = self._normalize_runtime_lane_value(
            self.config_manager.get_setting("runtime", "parallel_provider_lanes")
        )
        return counts

    def _build_runtime_parallelization_card(self, section_key: str, card_def):
        card = QWidget()
        card.setStyleSheet(
            f"""
            QWidget {{
                background-color: {BrandColors.SIDEBAR_BG};
                border-radius: 8px;
            }}
            """
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(BrandColors.CARD_PADDING + 4, 18, BrandColors.CARD_PADDING + 4, BrandColors.CARD_PADDING)
        layout.setSpacing(6)

        title = QLabel(card_def.title)
        title.setStyleSheet(
            f"""
            color: {BrandColors.TEXT_PRIMARY};
            font-size: {BrandColors.FONT_SIZE_TITLE};
            font-weight: 700;
            background-color: transparent;
            """
        )
        layout.addWidget(title)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background-color: {BrandColors.INPUT_BORDER}; border: none;")
        layout.addWidget(divider)

        content_widget = QWidget()
        content_widget.setStyleSheet("background-color: transparent;")
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(6)
        self._card_content_layouts[card_def.key] = content_layout

        if getattr(card_def, "description", None):
            desc = QLabel(render_tooltip_text(card_def.description))
            desc.setWordWrap(True)
            desc.setTextFormat(Qt.RichText)
            desc.setStyleSheet(
                f"""
                color: {BrandColors.TEXT_SECONDARY};
                font-size: {BrandColors.FONT_SIZE_REGULAR};
                background-color: transparent;
                padding-top: 4px;
                padding-bottom: 6px;
                """
            )
            content_layout.addWidget(desc)

        master_field = self._resolve_field_def("runtime", "providers_in_parallel")
        if master_field is not None:
            master_entry = self._build_field_entry(master_field, "runtime", section_key, card_def.key)
            if master_entry is not None:
                content_layout.addWidget(master_entry)
            master_widget = self.field_widgets.get("runtime.providers_in_parallel")
            if isinstance(master_widget, Tumbler):
                master_widget.stateChanged.connect(self._on_runtime_parallelization_master_changed)

        mode_field = self._resolve_field_def("runtime", "parallelization_mode")
        lanes_field = self._resolve_field_def("runtime", "parallel_provider_lanes")
        if mode_field is None or lanes_field is None:
            Logger.warning("Runtime parallelization schema fields are missing")
            layout.addWidget(content_widget)
            self._register_runtime_parallelization_field_locations(section_key, card_def.key)
            return card

        mode_docs_url = self._get_field_docs_url(mode_field)
        mode_label = mode_field.label
        mode_tooltip = mode_field.tooltip or ""
        mode_description = self._get_field_inline_description(mode_field)

        mode = StyledComboBox()
        for key, label in RUNTIME_PARALLEL_MODE_OPTIONS:
            mode.addItem(label, key)
        mode.setProperty("fullKey", "runtime.parallelization_mode")
        mode.setProperty("settingInfoTitle", mode_label)
        mode.setProperty("settingInfoBody", mode_tooltip or mode_description or "")
        mode.setProperty("docsUrl", mode_docs_url)
        self.field_widgets["runtime.parallelization_mode"] = mode
        self._tag_docs_widget(mode, mode_docs_url)
        mode.currentIndexChanged.connect(self._on_runtime_parallelization_mode_changed)
        mode_row = SettingRow(
            mode_label,
            mode,
            mode_tooltip,
            description=mode_description,
            docs_url=mode_docs_url,
            docs_handler=self._open_docs_from_sender,
        )
        self._runtime_parallelization_widgets["mode"] = mode
        self._runtime_parallelization_widgets["mode_row"] = mode_row
        self.setting_rows["runtime.parallelization_mode"] = mode_row
        self._register_navigation_anchor(mode_row)
        self._install_info_filters(mode_row)
        content_layout.addWidget(mode_row)

        lanes_docs_url = self._get_field_docs_url(lanes_field)
        lanes_label = lanes_field.label
        lanes_tooltip = lanes_field.tooltip or ""
        lanes_description = self._get_field_inline_description(lanes_field)

        lanes = RuntimeProviderLaneDropdown(max_instances=MAX_FULL_PARALLEL_PROVIDER_INSTANCES)
        lanes.setProperty("fullKey", "runtime.parallel_provider_lanes")
        lanes.setProperty("settingInfoTitle", lanes_label)
        lanes.setProperty("settingInfoBody", lanes_tooltip or lanes_description or "")
        lanes.setProperty("docsUrl", lanes_docs_url)
        self.field_widgets["runtime.parallel_provider_lanes"] = lanes
        self._tag_docs_widget(lanes, lanes_docs_url)
        lanes.stateChanged.connect(self._on_runtime_parallelization_lane_changed)
        lanes_row = SettingRow(
            lanes_label,
            lanes,
            lanes_tooltip,
            description=lanes_description,
            docs_url=lanes_docs_url,
            docs_handler=self._open_docs_from_sender,
        )
        self._runtime_parallelization_widgets["lanes"] = lanes
        self._runtime_parallelization_widgets["lanes_row"] = lanes_row
        self.setting_rows["runtime.parallel_provider_lanes"] = lanes_row
        self._register_navigation_anchor(lanes_row)
        self._install_info_filters(lanes_row)
        content_layout.addWidget(lanes_row)

        startup_divider = Divider("Startup")
        self._runtime_parallelization_widgets["startup_divider"] = startup_divider
        content_layout.addWidget(startup_divider)
        for field_key in (
            "parallel_concurrent_launch",
            "parallel_launch_in_batches",
            "parallel_launch_batch_size",
        ):
            field = self._resolve_field_def("runtime", field_key)
            if field is None:
                continue
            entry = self._build_field_entry(field, "runtime", section_key, card_def.key)
            if entry is not None:
                content_layout.addWidget(entry)

        layout.addWidget(content_widget)
        self._sync_runtime_parallelization_widgets_from_config()
        self._register_runtime_parallelization_field_locations(section_key, card_def.key)
        return card

    def _register_runtime_parallelization_field_locations(self, section_key: str, card_key: str) -> None:
        mode_display_key = "runtime.parallelization_mode"
        lanes_display_key = "runtime.parallel_provider_lanes"
        mode_row = self._runtime_parallelization_widgets.get("mode_row")
        lanes_row = self._runtime_parallelization_widgets.get("lanes_row")
        if mode_row is not None:
            self._display_rows[mode_display_key] = mode_row
        if lanes_row is not None:
            self._display_rows[lanes_display_key] = lanes_row

    def _sync_runtime_parallelization_widgets_from_config(self) -> None:
        if not self._runtime_parallelization_widgets:
            return

        previous_suppress = self._suppress_dirty_tracking
        self._suppress_dirty_tracking = True
        try:
            master = self._runtime_parallelization_widgets.get("master")
            if master is None:
                master = self.field_widgets.get("runtime.providers_in_parallel")
                if master is not None:
                    self._runtime_parallelization_widgets["master"] = master
            if isinstance(master, Tumbler):
                master.blockSignals(True)
                master.setChecked(bool(self.config_manager.get_setting("runtime", "providers_in_parallel")))
                master.blockSignals(False)

            mode = self._runtime_parallelization_widgets.get("mode")
            if isinstance(mode, StyledComboBox):
                mode.blockSignals(True)
                self._set_combo_current_data(mode, self._runtime_parallel_mode_from_config())
                mode.blockSignals(False)

            lanes = self._runtime_parallelization_widgets.get("lanes")
            if isinstance(lanes, RuntimeProviderLaneDropdown):
                lanes.blockSignals(True)
                lanes.set_providers(self._available_runtime_parallel_providers())
                lanes.set_current_provider(self._get_selected_provider())
                lanes.set_full_mode(self._runtime_parallel_mode_key() == "full_parallel_lanes")
                lanes.set_state(
                    selected=self._selected_parallel_providers_from_config(),
                    counts=self._parallel_instance_counts_from_config(),
                )
                lanes.blockSignals(False)

            for field_key in (
                "parallel_concurrent_launch",
                "parallel_launch_in_batches",
                "parallel_launch_batch_size",
            ):
                field = self._resolve_field_def("runtime", field_key)
                if field is not None:
                    self._apply_field_value(self._category_defs_by_key["runtime"], field)

            self._sync_runtime_parallelization_enabled_state()
        finally:
            self._suppress_dirty_tracking = previous_suppress

    def _sync_runtime_parallelization_current_provider(self) -> None:
        lanes = self._runtime_parallelization_widgets.get("lanes")
        if not isinstance(lanes, RuntimeProviderLaneDropdown):
            return
        lanes.blockSignals(True)
        try:
            lanes.set_providers(self._available_runtime_parallel_providers())
            lanes.set_current_provider(self._get_selected_provider())
        finally:
            lanes.blockSignals(False)
        self._update_dirty_markers()

    def _sync_runtime_parallelization_enabled_state(self) -> None:
        master = self.field_widgets.get("runtime.providers_in_parallel")
        enabled = bool(master.isChecked()) if isinstance(master, Tumbler) else bool(
            self.config_manager.get_setting("runtime", "providers_in_parallel")
        )

        mode = self._runtime_parallelization_widgets.get("mode")
        mode_row = self._runtime_parallelization_widgets.get("mode_row")
        lanes = self._runtime_parallelization_widgets.get("lanes")
        lanes_row = self._runtime_parallelization_widgets.get("lanes_row")
        startup_divider = self._runtime_parallelization_widgets.get("startup_divider")

        target = mode_row if isinstance(mode_row, QWidget) else mode
        self._set_widget_enabled_if_changed(target, enabled)
        target = lanes_row if isinstance(lanes_row, QWidget) else lanes
        self._set_widget_enabled_if_changed(target, enabled)
        self._set_widget_visible_if_changed(startup_divider, enabled)

        if isinstance(lanes, RuntimeProviderLaneDropdown):
            lanes.set_full_mode(self._runtime_parallel_mode_key() == "full_parallel_lanes")

    def _on_runtime_parallelization_master_changed(self) -> None:
        if self._suppress_dirty_tracking:
            return
        QTimer.singleShot(0, self._sync_runtime_parallelization_enabled_state)
        QTimer.singleShot(0, self._update_dirty_markers)

    def _on_runtime_parallelization_mode_changed(self, *_args) -> None:
        lanes = self._runtime_parallelization_widgets.get("lanes")
        if isinstance(lanes, RuntimeProviderLaneDropdown):
            lanes.set_full_mode(self._runtime_parallel_mode_key() == "full_parallel_lanes")
        if not self._suppress_dirty_tracking:
            QTimer.singleShot(0, self._update_dirty_markers)

    def _on_runtime_parallelization_lane_changed(self) -> None:
        if not self._suppress_dirty_tracking:
            QTimer.singleShot(0, self._update_dirty_markers)

    def _runtime_parallelization_dirty_rows(self) -> set[str]:
        dirty_rows: set[str] = set()

        mode = self._runtime_parallelization_widgets.get("mode")
        if isinstance(mode, StyledComboBox):
            if self._runtime_parallel_mode_key() != self._runtime_parallel_mode_from_config():
                dirty_rows.add("runtime.parallelization_mode")

        lanes = self._runtime_parallelization_widgets.get("lanes")
        if isinstance(lanes, RuntimeProviderLaneDropdown):
            expected = self._runtime_lane_value_from_widget(lanes)
            saved = self._normalized_runtime_lane_value_for_compare(
                self.config_manager.get_setting("runtime", "parallel_provider_lanes")
            )
            if expected != saved:
                dirty_rows.add("runtime.parallel_provider_lanes")

        return dirty_rows

    def _update_runtime_parallelization_dirty_markers(self) -> bool:
        dirty_rows = self._runtime_parallelization_dirty_rows()
        for key in ("runtime.parallelization_mode", "runtime.parallel_provider_lanes"):
            row = self._display_rows.get(key)
            if hasattr(row, "set_dirty"):
                row.set_dirty(key in dirty_rows)
        return bool(dirty_rows)

    def _apply_runtime_parallelization_to_config(self) -> None:
        mode = self._runtime_parallelization_widgets.get("mode")
        if isinstance(mode, StyledComboBox):
            self.config_manager.set_setting("runtime", "parallelization_mode", self._runtime_parallel_mode_key())

        lanes = self._runtime_parallelization_widgets.get("lanes")
        if isinstance(lanes, RuntimeProviderLaneDropdown):
            self.config_manager.set_setting(
                "runtime",
                "parallel_provider_lanes",
                self._runtime_lane_value_from_widget(lanes),
            )

    @staticmethod
    def _format_reasoning_effort_provider_summary(count: int) -> str:
        return f"Select providers ({max(0, int(count or 0))} enabled)"

    @staticmethod
    def _format_profile_clear_provider_summary(count: int) -> str:
        return f"Select Providers ({max(0, int(count or 0))} selected)"

    def _clone_loadout(self, loadout: LoadoutDefinition) -> LoadoutDefinition:
        return LoadoutDefinition(
            name=loadout.name,
            provider=loadout.provider,
            settings=copy.deepcopy(loadout.settings),
            meta_comment=loadout.meta_comment,
        )

    def _flatten_loadout_editor_draft(self) -> list[LoadoutDefinition]:
        ordered: list[LoadoutDefinition] = []
        for provider in (
            DriverProvider.DEEPSEEK,
            DriverProvider.GLM_CHAT,
            DriverProvider.MOONSHOT,
            DriverProvider.QWEN_LM,
            DriverProvider.PERPLEXITY,
            DriverProvider.HUGGINGCHAT,
            DriverProvider.AI_STUDIO,
            DriverProvider.MIMO,
        ):
            behavior_key = self.BEHAVIOR_CATEGORY_BY_PROVIDER.get(provider)
            if not behavior_key:
                continue
            for loadout in self._loadout_editor_draft_by_provider.get(behavior_key, []):
                ordered.append(self._clone_loadout(loadout))
        return ordered

    def _normalize_loadout_sequence(self, loadouts: list[LoadoutDefinition]) -> list[LoadoutDefinition]:
        grouped: dict[str, list[LoadoutDefinition]] = {}
        for loadout in loadouts or []:
            behavior_key = self.BEHAVIOR_CATEGORY_BY_PROVIDER.get(loadout.provider)
            if not behavior_key:
                continue
            grouped.setdefault(behavior_key, []).append(self._clone_loadout(loadout))

        ordered: list[LoadoutDefinition] = []
        for provider in (
            DriverProvider.DEEPSEEK,
            DriverProvider.GLM_CHAT,
            DriverProvider.MOONSHOT,
            DriverProvider.QWEN_LM,
            DriverProvider.PERPLEXITY,
            DriverProvider.HUGGINGCHAT,
            DriverProvider.AI_STUDIO,
            DriverProvider.MIMO,
        ):
            behavior_key = self.BEHAVIOR_CATEGORY_BY_PROVIDER.get(provider)
            if not behavior_key:
                continue
            ordered.extend(grouped.get(behavior_key, []))
        return ordered

    def _iter_saved_loadout_controlled_fields(self):
        for full_key, field_def in (self.field_defs or {}).items():
            if getattr(field_def, "transient", False):
                continue
            if not self._is_loadout_controlled_full_key(full_key):
                continue
            if field_def.type in {
                SettingType.BUTTON,
                SettingType.DESCRIPTION,
                SettingType.DIVIDER,
                SettingType.HINT,
                SettingType.REDIRECT,
                SettingType.ROW,
            }:
                continue
            yield full_key, field_def

    def _build_base_controlled_state_from_config(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        for full_key, _field_def in self._iter_saved_loadout_controlled_fields():
            category_key, field_key = full_key.split(".", 1)
            state[full_key] = copy.deepcopy(self.config_manager.get_setting(category_key, field_key))
        return state

    def _load_loadout_editor_state_from_config(self) -> None:
        draft_by_provider: dict[str, list[LoadoutDefinition]] = {}
        for loadout in self.config_manager.get_loadouts():
            behavior_key = self.BEHAVIOR_CATEGORY_BY_PROVIDER.get(loadout.provider)
            if not behavior_key:
                continue
            draft_by_provider.setdefault(behavior_key, []).append(self._clone_loadout(loadout))

        self._loadout_editor_draft_by_provider = draft_by_provider
        self._loadout_base_values_cache = self._build_base_controlled_state_from_config()
        self._loadout_editor_selected_names = {}

        for behavior_key, provider in self.PROVIDER_BY_BEHAVIOR_CATEGORY.items():
            available = draft_by_provider.get(behavior_key, [])
            preferred = self.config_manager.get_preferred_loadout_name(provider, available)
            if preferred and any(loadout.name == preferred for loadout in available):
                self._loadout_editor_selected_names[behavior_key] = preferred
            elif available:
                self._loadout_editor_selected_names[behavior_key] = available[0].name
            else:
                self._loadout_editor_selected_names[behavior_key] = None

    def _provider_for_behavior_key(self, behavior_key: str | None) -> DriverProvider | None:
        return self.PROVIDER_BY_BEHAVIOR_CATEGORY.get(str(behavior_key or "").strip())

    def _current_editor_behavior_key(self) -> str:
        return (
            self._provider_behavior_selected_key
            or self.BEHAVIOR_CATEGORY_BY_PROVIDER.get(self._get_selected_provider())
            or "deepseek_behavior"
        )

    def _draft_loadouts_for_behavior(self, behavior_key: str | None) -> list[LoadoutDefinition]:
        return list(self._loadout_editor_draft_by_provider.get(str(behavior_key or "").strip(), []))

    def _selected_loadout_name_for_behavior(self, behavior_key: str | None) -> str | None:
        key = str(behavior_key or "").strip()
        available = self._draft_loadouts_for_behavior(key)
        selected = str(self._loadout_editor_selected_names.get(key) or "").strip() or None
        if selected and any(loadout.name == selected for loadout in available):
            return selected
        fallback = available[0].name if available else None
        self._loadout_editor_selected_names[key] = fallback
        return fallback

    def _selected_loadout_for_behavior(self, behavior_key: str | None) -> LoadoutDefinition | None:
        selected_name = self._selected_loadout_name_for_behavior(behavior_key)
        if not selected_name:
            return None
        for loadout in self._draft_loadouts_for_behavior(behavior_key):
            if loadout.name == selected_name:
                return loadout
        return None

    def _build_loadout_dropdown_options(self, behavior_key: str | None) -> list[MarshmallowOption]:
        provider = self._provider_for_behavior_key(behavior_key)
        icon_file = self._provider_icon_file(provider)
        return [
            MarshmallowOption(
                key=loadout.name,
                label=loadout.name,
                icon_file=icon_file,
            )
            for loadout in self._draft_loadouts_for_behavior(behavior_key)
        ]

    def _build_reusable_provider_switch(self) -> QWidget:
        selector_wrap = _FlowLayoutHost(spacing=8)
        instance_buttons: dict[str, QPushButton] = {}

        for provider in (
            DriverProvider.DEEPSEEK,
            DriverProvider.GLM_CHAT,
            DriverProvider.MOONSHOT,
            DriverProvider.QWEN_LM,
            DriverProvider.PERPLEXITY,
            DriverProvider.HUGGINGCHAT,
            DriverProvider.AI_STUDIO,
            DriverProvider.MIMO,
        ):
            behavior_key = self.BEHAVIOR_CATEGORY_BY_PROVIDER.get(provider)
            if not behavior_key:
                continue

            button = QPushButton(f"   {provider.value}")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            icon_file = self._provider_icon_file(provider)
            if icon_file:
                icon = IconUtils.get_icon(
                    icon_file,
                    color=BrandColors.TEXT_PRIMARY,
                    size=16,
                    widget=button,
                )
                if not icon.isNull():
                    button.setIcon(icon)
                    button.setIconSize(QSize(16, 16))
            button.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: #1d1f23;
                    color: {BrandColors.TEXT_SECONDARY};
                    border: 1px solid {BrandColors.INPUT_BORDER};
                    border-radius: 8px;
                    padding: 8px 14px;
                    font-size: {BrandColors.FONT_SIZE_REGULAR};
                    font-family: {BrandColors.FONT_FAMILY};
                    font-weight: 600;
                }}
                QPushButton:hover {{
                    color: {BrandColors.TEXT_PRIMARY};
                    border: 1px solid {BrandColors.CATEGORY_ACTIVE_BORDER};
                }}
                QPushButton:checked {{
                    background-color: {BrandColors.CATEGORY_ACTIVE_BG};
                    color: {BrandColors.TEXT_PRIMARY};
                    border: 1px solid {BrandColors.CATEGORY_ACTIVE_BORDER};
                }}
                """
            )
            button.clicked.connect(
                lambda _checked=False, key=behavior_key: self._on_loadout_provider_requested(key)
            )
            selector_wrap.flow_layout.addWidget(button)
            instance_buttons[behavior_key] = button

        self._provider_behavior_buttons.append(instance_buttons)
        return selector_wrap

    def _build_loadout_selector_block(self, *, section_key: str) -> QWidget:
        block = QWidget()
        block.setStyleSheet("background-color: transparent;")
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layout.addWidget(self._build_reusable_provider_switch())

        dropdown = MarshmallowDropdown(placeholder="No loadouts yet")
        dropdown.currentKeyChanged.connect(self._on_loadout_selected_from_dropdown)
        dropdown.addRequested.connect(self._on_loadout_add_requested)
        dropdown.deleteRequested.connect(self._on_loadout_delete_requested)
        dropdown.setProperty("settingInfoTitle", "Current Loadout")
        dropdown.setProperty(
            "settingInfoBody",
            "Pick which loadout you are editing for the selected provider. This list only shows loadouts for that provider.",
        )
        dropdown.setProperty(
            "docsUrl",
            build_docs_url("experimental/loadouts/", "how-editing-works-now"),
        )
        dropdown_row = QWidget(block)
        dropdown_row.setStyleSheet("background-color: transparent;")
        row_layout = QVBoxLayout(dropdown_row)
        row_layout.setContentsMargins(0, 10, 0, 10)
        row_layout.setSpacing(6)

        dropdown_label = QLabel(
            f"<span style='font-size: {BrandColors.FONT_SIZE_REGULAR}; "
            f"font-weight: 500; color: {BrandColors.TEXT_SECONDARY};'>Current Loadout</span>",
            dropdown_row,
        )
        dropdown_label.setTextFormat(Qt.RichText)
        dropdown_label.setStyleSheet("background-color: transparent;")
        dropdown_label.setAttribute(Qt.WA_Hover, True)
        dropdown_label.setProperty("settingInfoTitle", "Current Loadout")
        dropdown_label.setProperty(
            "settingInfoBody",
            "Pick which loadout you are editing for the selected provider. This list only shows loadouts for that provider.",
        )
        dropdown_label.setProperty(
            "docsUrl",
            build_docs_url("experimental/loadouts/", "how-editing-works-now"),
        )
        row_layout.addWidget(dropdown_label)
        row_layout.addWidget(dropdown)
        if section_key == "provider_behavior":
            dropdown_row.setVisible(self._is_loadouts_enabled_in_ui())
        layout.addWidget(dropdown_row)

        self._loadout_dropdown_instances.append(
            {
                "section_key": section_key,
                "widget": dropdown,
                "row": dropdown_row,
                "label": dropdown_label,
            }
        )
        self._register_navigation_anchor(dropdown_row)
        self._install_info_filters(dropdown_label)
        self._install_info_filters(dropdown)
        return block

    def _sync_loadout_selector_instances(self) -> None:
        current_behavior_key = self._current_editor_behavior_key()
        loadouts_enabled = self._is_loadouts_enabled_in_ui()
        current_name = self._selected_loadout_name_for_behavior(current_behavior_key) if loadouts_enabled else None
        options = self._build_loadout_dropdown_options(current_behavior_key) if loadouts_enabled else []

        for instance in list(self._provider_behavior_buttons):
            if not isinstance(instance, dict):
                continue
            for behavior_key, button in instance.items():
                button.setChecked(behavior_key == current_behavior_key)

        for entry in list(self._loadout_dropdown_instances):
            widget = entry.get("widget")
            row = entry.get("row")
            section_key = str(entry.get("section_key") or "").strip()
            if not isinstance(widget, MarshmallowDropdown):
                continue
            if section_key == "provider_behavior" and isinstance(row, QWidget):
                self._set_widget_visible_if_changed(row, loadouts_enabled)
            widget.set_options(options, current_name)
            self._set_widget_enabled_if_changed(widget, loadouts_enabled)

        formatting_card = self._card_widgets.get(self._loadout_editor_formatting_card_key)
        if formatting_card is not None:
            self._set_widget_visible_if_changed(formatting_card, loadouts_enabled)

        if self._selected_section_key in {"formatting", "provider_behavior"}:
            sidebar = self._sidebar_sections.get(self._selected_section_key)
            if sidebar is not None:
                sidebar.set_cards(self._sidebar_cards_for_section(self._selected_section_key))
            visible_cards = self._visible_card_keys_for_section(self._selected_section_key)
            if self._selected_card_key not in visible_cards:
                self._set_active_card(visible_cards[0] if visible_cards else None)

    def _restore_base_controlled_values(self) -> None:
        for full_key, field_def in self._iter_saved_loadout_controlled_fields():
            widget = self.field_widgets.get(full_key)
            if widget is None:
                continue
            self._set_widget_value(
                widget,
                copy.deepcopy(self._loadout_base_values_cache.get(full_key)),
            )

    def _apply_selected_loadout_to_widgets(self) -> None:
        behavior_key = self._current_editor_behavior_key()
        current_loadout = self._selected_loadout_for_behavior(behavior_key)
        if current_loadout is None:
            self._restore_base_controlled_values()
            return

        provider = self._provider_for_behavior_key(behavior_key)
        if provider is None:
            self._restore_base_controlled_values()
            return

        for field_key, (category_key, _field_def) in get_loadout_field_bindings(provider).items():
            full_key = f"{category_key}.{field_key}"
            widget = self.field_widgets.get(full_key)
            if widget is None:
                continue
            value = copy.deepcopy(current_loadout.settings.get(field_key))
            self._set_widget_value(widget, value)

    def _sync_loadout_controlled_editability(self) -> None:
        loadouts_enabled = self._is_loadouts_enabled_in_ui()
        current_behavior_key = self._current_editor_behavior_key()
        has_selected_loadout = self._selected_loadout_for_behavior(current_behavior_key) is not None

        if not loadouts_enabled:
            return
        if has_selected_loadout:
            return

        for full_key, _field_def in self._iter_saved_loadout_controlled_fields():
            row = self.setting_rows.get(full_key)
            widget = self.field_widgets.get(full_key)
            target = row or widget
            if target is None:
                continue

            category_key, _field_key = full_key.split(".", 1)
            if category_key == "formatting":
                self._set_widget_enabled_if_changed(target, False)
            elif category_key == current_behavior_key:
                self._set_widget_enabled_if_changed(target, False)

    def _refresh_loadout_editor_widgets(self) -> None:
        previous_suppress = self._suppress_dirty_tracking
        self._suppress_dirty_tracking = True
        try:
            self._sync_loadout_selector_instances()
            if self._is_loadouts_enabled_in_ui():
                self._apply_selected_loadout_to_widgets()
            else:
                self._restore_base_controlled_values()
            self._update_dependencies()
            self._sync_loadout_controlled_editability()
        finally:
            self._suppress_dirty_tracking = previous_suppress
        if not self._suppress_dirty_tracking:
            self._update_dirty_markers()

    def _capture_current_loadout_from_widgets(self, behavior_key: str | None = None) -> None:
        if not self._is_loadouts_enabled_in_ui():
            return

        resolved_key = str(behavior_key or self._current_editor_behavior_key()).strip()
        current_loadout = self._selected_loadout_for_behavior(resolved_key)
        provider = self._provider_for_behavior_key(resolved_key)
        if current_loadout is None or provider is None:
            return

        updated_settings = copy.deepcopy(current_loadout.settings)
        for field_key, (category_key, field_def) in get_loadout_field_bindings(provider).items():
            full_key = f"{category_key}.{field_key}"
            if full_key not in self.field_widgets:
                continue
            updated_settings[field_key] = copy.deepcopy(self._current_field_value(full_key, field_def))

        updated_loadout = LoadoutDefinition(
            name=current_loadout.name,
            provider=current_loadout.provider,
            settings=updated_settings,
            meta_comment=current_loadout.meta_comment,
        )

        provider_loadouts = list(self._loadout_editor_draft_by_provider.get(resolved_key, []))
        for index, loadout in enumerate(provider_loadouts):
            if loadout.name == current_loadout.name:
                provider_loadouts[index] = updated_loadout
                break
        self._loadout_editor_draft_by_provider[resolved_key] = provider_loadouts

    def _on_loadout_provider_requested(self, behavior_key: str) -> None:
        previous_key = self._current_editor_behavior_key()
        self._capture_current_loadout_from_widgets(previous_key)
        self._set_provider_behavior_page(behavior_key, user_selected=True)

    def _on_loadout_selected_from_dropdown(self, loadout_name: str) -> None:
        behavior_key = self._current_editor_behavior_key()
        self._capture_current_loadout_from_widgets(behavior_key)
        self._loadout_editor_selected_names[behavior_key] = str(loadout_name or "").strip() or None
        self._refresh_loadout_editor_widgets()

    def _on_loadout_add_requested(self, loadout_name: str) -> None:
        behavior_key = self._current_editor_behavior_key()
        provider = self._provider_for_behavior_key(behavior_key)
        normalized_name = str(loadout_name or "").strip()
        if provider is None or not normalized_name:
            return

        existing = self._draft_loadouts_for_behavior(behavior_key)
        if any(loadout.name.casefold() == normalized_name.casefold() for loadout in existing):
            QMessageBox.warning(
                self,
                "Loadouts",
                f"A loadout named '{normalized_name}' already exists for {provider.value}.",
            )
            return

        self._capture_current_loadout_from_widgets(behavior_key)
        selected_loadout = self._selected_loadout_for_behavior(behavior_key)
        if selected_loadout is not None:
            new_settings = copy.deepcopy(selected_loadout.settings)
        else:
            new_settings = build_visual_loadout_settings(self.config_manager, provider)
            for field_key, (category_key, _field_def) in get_loadout_field_bindings(provider).items():
                full_key = f"{category_key}.{field_key}"
                if full_key in self._loadout_base_values_cache:
                    new_settings[field_key] = copy.deepcopy(self._loadout_base_values_cache[full_key])

        updated = existing + [
            LoadoutDefinition(
                name=normalized_name,
                provider=provider,
                settings=new_settings,
            )
        ]
        self._loadout_editor_draft_by_provider[behavior_key] = updated
        self._loadout_editor_selected_names[behavior_key] = normalized_name
        self._refresh_loadout_editor_widgets()

    def _on_loadout_delete_requested(self, loadout_name: str) -> None:
        behavior_key = self._current_editor_behavior_key()
        self._capture_current_loadout_from_widgets(behavior_key)

        updated = [
            loadout
            for loadout in self._draft_loadouts_for_behavior(behavior_key)
            if loadout.name != loadout_name
        ]
        self._loadout_editor_draft_by_provider[behavior_key] = updated
        selected_name = self._selected_loadout_name_for_behavior(behavior_key)
        if selected_name == loadout_name:
            self._loadout_editor_selected_names[behavior_key] = updated[0].name if updated else None
        self._refresh_loadout_editor_widgets()

    def _loadout_editor_has_structural_changes(self) -> bool:
        return serialize_settings_loadouts(self._flatten_loadout_editor_draft()) != serialize_settings_loadouts(
            self._normalize_loadout_sequence(self.config_manager.get_loadouts())
        )

    def _loadout_base_values_dirty(self) -> bool:
        for full_key, _field_def in self._iter_saved_loadout_controlled_fields():
            category_key, field_key = full_key.split(".", 1)
            if self._loadout_base_values_cache.get(full_key) != self.config_manager.get_setting(category_key, field_key):
                return True
        return False

    def _build_standard_card(self, section_key: str, card_def, *, field_refs=None):
        card = QWidget()
        card.setStyleSheet(
            f"""
            QWidget {{
                background-color: {BrandColors.SIDEBAR_BG};
                border-radius: 8px;
            }}
            """
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(BrandColors.CARD_PADDING + 4, 18, BrandColors.CARD_PADDING + 4, BrandColors.CARD_PADDING)
        layout.setSpacing(6)

        title = QLabel(card_def.title)
        title.setStyleSheet(
            f"""
            color: {BrandColors.TEXT_PRIMARY};
            font-size: {BrandColors.FONT_SIZE_TITLE};
            font-weight: 700;
            background-color: transparent;
            """
        )
        layout.addWidget(title)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background-color: {BrandColors.INPUT_BORDER}; border: none;")
        layout.addWidget(divider)

        content_widget = QWidget()
        content_widget.setStyleSheet("background-color: transparent;")
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(6)
        self._card_content_layouts[card_def.key] = content_layout

        if getattr(card_def, "description", None):
            desc = QLabel(render_tooltip_text(card_def.description))
            desc.setWordWrap(True)
            desc.setTextFormat(Qt.RichText)
            desc.setStyleSheet(
                f"""
                color: {BrandColors.TEXT_SECONDARY};
                font-size: {BrandColors.FONT_SIZE_REGULAR};
                background-color: transparent;
                padding-top: 4px;
                padding-bottom: 6px;
                """
            )
            content_layout.addWidget(desc)

        refs = list((getattr(card_def, "field_refs", None) if field_refs is None else field_refs) or [])
        for category_key, field_key in refs:
            field = self._resolve_field_def(category_key, field_key)
            if field is None:
                continue
            entry = self._build_field_entry(field, category_key, section_key, card_def.key)
            if entry is not None:
                content_layout.addWidget(entry)

        layout.addWidget(content_widget)

        return card

    def _build_provider_behavior_card(self, section_key: str, card_def):
        card = QWidget()
        card.setStyleSheet(
            f"""
            QWidget {{
                background-color: {BrandColors.SIDEBAR_BG};
                border-radius: 8px;
            }}
            """
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(BrandColors.CARD_PADDING + 4, 18, BrandColors.CARD_PADDING + 4, BrandColors.CARD_PADDING)
        layout.setSpacing(10)

        title = QLabel(card_def.title)
        title.setStyleSheet(
            f"""
            color: {BrandColors.TEXT_PRIMARY};
            font-size: {BrandColors.FONT_SIZE_TITLE};
            font-weight: 700;
            background-color: transparent;
            """
        )
        layout.addWidget(title)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background-color: {BrandColors.INPUT_BORDER}; border: none;")
        layout.addWidget(divider)

        content_widget = QWidget()
        content_widget.setStyleSheet("background-color: transparent;")
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)

        content_layout.addWidget(self._build_loadout_selector_block(section_key=section_key))
        layout.addWidget(content_widget)
        return card

    def _build_loadout_editor_card(self, section_key: str, card_def):
        card = QWidget()
        card.setStyleSheet(
            f"""
            QWidget {{
                background-color: {BrandColors.SIDEBAR_BG};
                border-radius: 8px;
            }}
            """
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(BrandColors.CARD_PADDING + 4, 18, BrandColors.CARD_PADDING + 4, BrandColors.CARD_PADDING)
        layout.setSpacing(10)

        header = QWidget()
        header.setStyleSheet("background-color: transparent;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(10)

        icon_label = QLabel()
        icon_label.setStyleSheet("background-color: transparent;")
        icon_label.setFixedSize(18, 18)
        pixmap = IconUtils.get_pixmap(
            "backpack.svg",
            color=BrandColors.ACCENT,
            size=18,
            dpr=self.devicePixelRatioF(),
        )
        if not pixmap.isNull():
            icon_label.setPixmap(pixmap)
        header_layout.addWidget(icon_label, 0, Qt.AlignVCenter)

        title = QLabel(card_def.title)
        title.setStyleSheet(
            f"""
            color: {BrandColors.TEXT_PRIMARY};
            font-size: {BrandColors.FONT_SIZE_TITLE};
            font-weight: 700;
            background-color: transparent;
            """
        )
        header_layout.addWidget(title, 1, Qt.AlignVCenter)
        layout.addWidget(header)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background-color: {BrandColors.INPUT_BORDER}; border: none;")
        layout.addWidget(divider)

        layout.addWidget(self._build_loadout_selector_block(section_key=section_key))
        return card

    def _apply_behavior_field_value(self, behavior_key: str, field_key: str) -> None:
        category = self._category_defs_by_key.get(behavior_key)
        if category is None:
            return
        field = self._resolve_field_def(behavior_key, field_key)
        if field is None:
            return
        for item in self._iter_fields([field]):
            self._apply_field_value(category, item)

    def _apply_behavior_group_values(self, behavior_key: str, group: dict) -> None:
        for field_key in group.get("fields", []):
            self._apply_behavior_field_value(behavior_key, field_key)

    def _behavior_group_card_key(self, behavior_key: str, index: int) -> str:
        return f"provider_behavior::{behavior_key}::{int(index)}"

    def _build_next_behavior_group_field(
        self,
        group: dict,
        behavior_key: str,
        section_key: str,
        card_key: str,
        *,
        refresh_values: bool,
    ) -> bool:
        content_layout = self._card_content_layouts.get(card_key)
        if content_layout is None:
            return False

        field_keys = list(group.get("fields", []) or [])
        next_index = int(self._card_next_field_index.get(card_key, 0) or 0)
        if next_index >= len(field_keys):
            self._card_build_complete_keys.add(card_key)
            return False

        field_key = field_keys[next_index]
        field = self._resolve_field_def(behavior_key, field_key)
        if field is not None:
            entry = self._build_field_entry(field, behavior_key, section_key, card_key, provider_key=behavior_key)
            if entry is not None:
                content_layout.addWidget(entry)
            if refresh_values:
                self._apply_behavior_field_value(behavior_key, field_key)

        next_index += 1
        self._card_next_field_index[card_key] = next_index
        if next_index >= len(field_keys):
            self._card_build_complete_keys.add(card_key)
        return True

    def _ensure_provider_behavior_group_cards_built(
        self,
        behavior_key: str,
        *,
        refresh_dirty: bool = True,
        max_groups: int | None = None,
        background: bool = False,
    ) -> None:
        normalized_key = str(behavior_key or "").strip()
        if not normalized_key or normalized_key in self._provider_behavior_complete_keys:
            return

        section_layout = self._section_layouts.get("provider_behavior")
        if section_layout is None:
            return

        if self._provider_behavior_selector_card_key not in self._card_widgets:
            self._ensure_card_built(
                "provider_behavior",
                self._provider_behavior_selector_card_key,
                refresh_values=self._settings_values_loaded,
                background=background,
            )

        groups = PROVIDER_BEHAVIOR_GROUPS.get(normalized_key, [])
        provider_cards = self._provider_behavior_group_card_keys.setdefault(normalized_key, [])
        if not groups:
            self._provider_behavior_complete_keys.add(normalized_key)
            return

        def next_incomplete_group_index() -> int | None:
            for group_index in range(len(groups)):
                card_key = self._behavior_group_card_key(normalized_key, group_index)
                if card_key not in self._card_build_complete_keys:
                    return group_index
            return None

        if next_incomplete_group_index() is None:
            self._provider_behavior_complete_keys.add(normalized_key)
            return

        step_limit = None if max_groups is None else max(1, int(max_groups))

        built_count = 0
        previous_suppress = self._suppress_dirty_tracking
        self._suppress_dirty_tracking = True
        try:
            while True:
                index = next_incomplete_group_index()
                if index is None:
                    break
                if step_limit is not None and built_count >= step_limit:
                    break

                group = groups[index]
                card_key = self._behavior_group_card_key(normalized_key, index)
                if card_key not in self._card_widgets:
                    self._dynamic_card_titles[card_key] = str(group.get("title") or "Provider")
                    card_widget = self._build_behavior_group_card(
                        group,
                        normalized_key,
                        "provider_behavior",
                        card_key,
                        field_keys=[],
                    )
                    card_widget.setProperty("sidebarTitle", str(group.get("title") or "Provider"))
                    card_widget.setVisible(False)
                    insert_index = max(0, section_layout.count() - 1)
                    section_layout.insertWidget(insert_index, card_widget)
                    self._card_widgets[card_key] = card_widget
                    provider_cards.append(card_key)
                    self._card_next_field_index[card_key] = 0
                    if not list(group.get("fields", []) or []):
                        self._card_build_complete_keys.add(card_key)
                        built_count += 1
                        continue

                if background:
                    if self._build_next_behavior_group_field(
                        group,
                        normalized_key,
                        "provider_behavior",
                        card_key,
                        refresh_values=True,
                    ):
                        built_count += 1
                    else:
                        self._card_build_complete_keys.add(card_key)
                    break

                while card_key not in self._card_build_complete_keys:
                    if not self._build_next_behavior_group_field(
                        group,
                        normalized_key,
                        "provider_behavior",
                        card_key,
                        refresh_values=True,
                    ):
                        break
                    built_count += 1
        finally:
            self._suppress_dirty_tracking = previous_suppress

        if next_incomplete_group_index() is None:
            self._provider_behavior_complete_keys.add(normalized_key)

        if background:
            if built_count:
                self._sections_needing_activation_sync.add("provider_behavior")
            return

        if not built_count and not refresh_dirty:
            return

        previous_suppress = self._suppress_dirty_tracking
        self._suppress_dirty_tracking = True
        try:
            self._update_dependencies()
        finally:
            self._suppress_dirty_tracking = previous_suppress
        self._refresh_loadout_editor_widgets()
        if refresh_dirty:
            self._update_dirty_markers()

    def _queue_provider_behavior_preload(self, *, exclude: str | None = None) -> None:
        excluded_key = str(exclude or "").strip()
        pending: list[tuple[str, int]] = []
        for key, groups in PROVIDER_BEHAVIOR_GROUPS.items():
            if key == excluded_key:
                continue
            for index, _group in enumerate(groups):
                card_key = self._behavior_group_card_key(key, index)
                if card_key not in self._card_build_complete_keys:
                    pending.append((key, index))

        self._pending_provider_behavior_preload_keys = pending

        timer = getattr(self, "_provider_behavior_preload_timer", None)
        if not isinstance(timer, QTimer):
            return
        if not pending:
            timer.stop()
            return

        timer.start(120 if self.isVisible() else 220)

    def _preload_next_provider_behavior_group(self) -> None:
        if not self.isVisible():
            self._queue_provider_behavior_preload(exclude=self._provider_behavior_selected_key)
            return

        while self._pending_provider_behavior_preload_keys:
            next_item = self._pending_provider_behavior_preload_keys.pop(0)
            if isinstance(next_item, tuple):
                next_key, expected_index = next_item
            else:
                next_key, expected_index = str(next_item), 0
            next_key = str(next_key or "").strip()
            if not next_key:
                continue
            if next_key == self._provider_behavior_selected_key:
                continue
            if next_key in self._provider_behavior_complete_keys:
                continue
            card_key = self._behavior_group_card_key(next_key, int(expected_index or 0))
            if card_key in self._card_build_complete_keys:
                continue
            self._ensure_provider_behavior_group_cards_built(
                next_key,
                refresh_dirty=False,
                max_groups=1,
                background=True,
            )
            if card_key not in self._card_build_complete_keys:
                self._pending_provider_behavior_preload_keys.insert(0, (next_key, int(expected_index or 0)))
            break

        if self._pending_provider_behavior_preload_keys:
            timer = getattr(self, "_provider_behavior_preload_timer", None)
            if isinstance(timer, QTimer):
                timer.start(24)

    def _build_behavior_group_card(
        self,
        group: dict,
        behavior_key: str,
        section_key: str,
        card_key: str,
        *,
        field_keys=None,
    ):
        group_card = QWidget()
        group_card.setStyleSheet(
            f"""
            QWidget {{
                background-color: {BrandColors.SIDEBAR_BG};
                border-radius: 8px;
            }}
            """
        )
        layout = QVBoxLayout(group_card)
        layout.setContentsMargins(BrandColors.CARD_PADDING + 4, 18, BrandColors.CARD_PADDING + 4, BrandColors.CARD_PADDING)
        layout.setSpacing(6)

        header = QWidget()
        header.setStyleSheet("background-color: transparent;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(10)

        icon_label = QLabel()
        icon_label.setStyleSheet("background-color: transparent;")
        icon_label.setFixedSize(18, 18)
        pixmap = IconUtils.get_pixmap(
            str(group.get("icon") or "settings.svg"),
            color=BrandColors.ACCENT,
            size=18,
            dpr=self.devicePixelRatioF(),
        )
        if not pixmap.isNull():
            icon_label.setPixmap(pixmap)
        header_layout.addWidget(icon_label, 0, Qt.AlignVCenter)

        title = QLabel(str(group.get("title") or "Group"))
        title.setStyleSheet(
            f"""
            color: {BrandColors.TEXT_PRIMARY};
            font-size: {BrandColors.FONT_SIZE_TITLE};
            font-weight: 700;
            background-color: transparent;
            """
        )
        header_layout.addWidget(title, 1, Qt.AlignVCenter)
        layout.addWidget(header)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background-color: {BrandColors.INPUT_BORDER}; border: none;")
        layout.addWidget(divider)

        content_widget = QWidget()
        content_widget.setStyleSheet("background-color: transparent;")
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(6)
        self._card_content_layouts[card_key] = content_layout

        refs = list((group.get("fields", []) if field_keys is None else field_keys) or [])
        for field_key in refs:
            field = self._resolve_field_def(behavior_key, field_key)
            if field is None:
                continue
            entry = self._build_field_entry(field, behavior_key, section_key, card_key, provider_key=behavior_key)
            if entry is not None:
                content_layout.addWidget(entry)

        layout.addWidget(content_widget)

        return group_card

    def _build_field_entry(self, field, category_key: str, section_key: str, card_key: str, provider_key: str | None = None):
        docs_url = self._get_field_docs_url(field)
        full_key = f"{category_key}.{field.key}"

        if field.type == SettingType.DIVIDER:
            return Divider(field.label)

        if field.type == SettingType.DESCRIPTION:
            widget = Description(field.default)
            self.field_widgets[full_key] = widget
            return widget

        if field.type == SettingType.HINT:
            widget = HintCard(
                field.label,
                field.default,
                variant=getattr(field, "hint_variant", None) or "info",
            )
            self.field_widgets[full_key] = widget
            widget.setProperty("settingInfoTitle", field.label)
            widget.setProperty("settingInfoBody", str(field.default or ""))
            if docs_url:
                widget.setProperty("docsUrl", docs_url)
            self._field_locations[full_key] = {
                "section_key": section_key,
                "card_key": card_key,
                "provider_key": provider_key,
            }
            self._install_info_filters(widget)
            self._register_search_target_for_field(section_key, card_key, category_key, field, provider_key=provider_key)
            return widget

        if field.type == SettingType.REDIRECT:
            widget = self._create_field_widget(field, category_key)
            self.setting_rows[full_key] = widget
            self._display_rows[full_key] = widget
            self._field_display_key[full_key] = full_key
            self._field_locations[full_key] = {
                "section_key": section_key,
                "card_key": card_key,
                "provider_key": provider_key,
            }
            self._register_navigation_anchor(widget)
            self._install_info_filters(widget)
            self._register_search_target_for_field(section_key, card_key, category_key, field, provider_key=provider_key)
            return widget

        if field.type == SettingType.ROW:
            sub_widgets = []
            for sub in field.sub_fields or []:
                sub_widget = self._create_field_widget(sub, category_key)
                sub_widgets.append(sub_widget)
                sub_full_key = f"{category_key}.{sub.key}"
                self._field_display_key[sub_full_key] = full_key
                self._field_locations[sub_full_key] = {
                    "section_key": section_key,
                    "card_key": card_key,
                    "provider_key": provider_key,
                }
            widget = MultiColumnRow(sub_widgets, field.ratios)
            row = SettingRow(
                field.label,
                widget,
                field.tooltip,
                description=self._get_field_inline_description(field),
                docs_url=docs_url,
                docs_handler=self._open_docs_from_sender,
            )
            self.field_widgets[full_key] = widget
            self.setting_rows[full_key] = row
            self._display_rows[full_key] = row
            self._field_display_key[full_key] = full_key
            self._field_locations[full_key] = {
                "section_key": section_key,
                "card_key": card_key,
                "provider_key": provider_key,
            }
            self._register_navigation_anchor(row)
            self._install_info_filters(row)
            self._register_search_target_for_field(section_key, card_key, category_key, field, provider_key=provider_key)
            return row

        widget = self._create_field_widget(field, category_key)
        if widget is None:
            return None

        if field.type == SettingType.BOOLEAN:
            row = ToggleRow(
                field.label,
                widget,
                field.tooltip,
                description=self._get_field_inline_description(field, fallback_to_tooltip=True),
                docs_url=docs_url,
                docs_handler=self._open_docs_from_sender,
            )
        else:
            row = SettingRow(
                field.label,
                widget,
                field.tooltip,
                description=self._get_field_inline_description(field),
                docs_url=docs_url,
                docs_handler=self._open_docs_from_sender,
            )

        self.setting_rows[full_key] = row
        self._display_rows[full_key] = row
        self._field_display_key[full_key] = full_key
        self._field_locations[full_key] = {
            "section_key": section_key,
            "card_key": card_key,
            "provider_key": provider_key,
        }
        self._register_navigation_anchor(row)
        self._install_info_filters(row)
        self._register_search_target_for_field(section_key, card_key, category_key, field, provider_key=provider_key)
        return row

    def _resolve_field_def(self, category_key: str, field_key: str):
        category = self._category_defs_by_key.get(category_key)
        if category is None:
            return None
        for field in self._iter_fields(category.fields):
            if field.key == field_key:
                return field
        return None

    def _register_search_target_for_field(self, section_key: str, card_key: str, category_key: str, field, provider_key: str | None = None) -> None:
        target_key = (
            str(section_key or ""),
            str(card_key or ""),
            str(provider_key or ""),
            f"{category_key}.{field.key}",
        )
        if target_key in self._search_target_keys:
            return
        self._search_target_keys.add(target_key)

        section = self._section_defs_by_key.get(section_key)
        card = self._card_defs_by_key.get(card_key)
        card_title = str(card.title if card else self._dynamic_card_titles.get(card_key, "")).strip()
        provider_label = ""
        if provider_key:
            for provider, behavior_key in self.BEHAVIOR_CATEGORY_BY_PROVIDER.items():
                if behavior_key == provider_key:
                    provider_label = provider.value
                    break
        extra_labels = ""
        if field.type == SettingType.ROW and field.sub_fields:
            extra_labels = " ".join(sub.label for sub in field.sub_fields if sub.label)
        self.search_targets.append(
            {
                "section_key": section_key,
                "card_key": card_key,
                "provider_key": provider_key,
                "label_lower": str(field.label or "").lower(),
                "key_lower": str(field.key or "").lower(),
                "section_lower": str(section.label if section else "").lower(),
                "card_lower": card_title.lower(),
                "provider_lower": provider_label.lower(),
                "extra_lower": extra_labels.lower(),
                "full_key": f"{category_key}.{field.key}",
            }
        )

    def _install_info_filters(self, widget: QWidget) -> None:
        anchor = widget
        for candidate in [widget, *widget.findChildren(QWidget)]:
            candidate.installEventFilter(self)
            self._info_anchor_by_object_id[id(candidate)] = anchor

    def _on_sidebar_section_requested(self, section_key: str) -> None:
        self._set_active_section(section_key, scroll_to_top=True)

    def _on_sidebar_card_requested(self, section_key: str, card_key: str) -> None:
        self._set_active_section(section_key, scroll_to_top=False)
        self._set_active_card(card_key)
        card = self._card_widgets.get(card_key)
        if card:
            self._smooth_ensure_visible(card, y_margin=32, duration_ms=240)

    def _set_active_section(self, section_key: str, *, scroll_to_top: bool) -> None:
        self._selected_section_key = str(section_key or "").strip()
        if not self._selected_section_key:
            return

        built_new_section = self._ensure_section_built(
            self._selected_section_key,
            refresh_values=self._settings_values_loaded,
            background=False,
        )
        if built_new_section and self._settings_values_loaded:
            self._sections_needing_activation_sync.add(self._selected_section_key)

        if self._selected_section_key == "provider_behavior":
            provider_key = (
                self._provider_behavior_selected_key
                or self.BEHAVIOR_CATEGORY_BY_PROVIDER.get(self._get_selected_provider())
                or "deepseek_behavior"
            )
            self._ensure_provider_behavior_group_cards_built(provider_key)
            self._set_provider_behavior_page(provider_key, user_selected=False)

        if self._settings_values_loaded:
            self._sync_section_after_activation(self._selected_section_key)
        self._hide_info_bubble()
        self._sync_section_visibility()
        for key, sidebar in self._sidebar_sections.items():
            is_active = key == self._selected_section_key
            sidebar.set_active(is_active)
            sidebar.set_expanded(is_active)
            if is_active:
                sidebar.set_cards(self._sidebar_cards_for_section(key))

        visible_cards = self._visible_card_keys_for_section(self._selected_section_key)
        first_card = visible_cards[0] if visible_cards else None
        self._set_active_card(first_card)
        if scroll_to_top:
            self._smooth_scroll_to(0, duration_ms=220)

    def _visible_card_keys_for_section(self, section_key: str) -> list[str]:
        section = self._section_defs_by_key.get(section_key)
        if section is None:
            return []
        if section_key != "provider_behavior":
            return [
                card_key
                for card_key in section.card_keys
                if card_key in self._card_widgets and not self._card_widgets[card_key].isHidden()
            ]
        behavior_key = self._provider_behavior_selected_key or self.BEHAVIOR_CATEGORY_BY_PROVIDER.get(self._get_selected_provider()) or "deepseek_behavior"
        return [self._provider_behavior_selector_card_key] + list(self._provider_behavior_group_card_keys.get(behavior_key, []))

    def _set_active_card(self, card_key: str | None) -> None:
        self._selected_card_key = str(card_key or "").strip() or None
        for section_key, sidebar in self._sidebar_sections.items():
            if section_key == self._selected_section_key:
                sidebar.set_active_card(self._selected_card_key)
            else:
                sidebar.set_active_card(None)

    def _set_provider_behavior_page(self, behavior_key: str, *, user_selected: bool) -> None:
        key = str(behavior_key or "").strip()
        previous_key = self._provider_behavior_selected_key or self.BEHAVIOR_CATEGORY_BY_PROVIDER.get(self._get_selected_provider()) or "deepseek_behavior"
        if previous_key != key:
            self._capture_current_loadout_from_widgets(previous_key)
        self._provider_behavior_selected_key = key
        if user_selected:
            self._provider_behavior_user_selected = True
        for instance in list(self._provider_behavior_buttons):
            if not isinstance(instance, dict):
                continue
            for provider_key, button in instance.items():
                button.setChecked(provider_key == key)
        if self._selected_section_key == "provider_behavior":
            self._ensure_provider_behavior_group_cards_built(key)
        self._sync_loadout_selector_instances()
        if key not in self._provider_behavior_complete_keys:
            self._refresh_loadout_editor_widgets()
            return

        for provider_key, card_keys in self._provider_behavior_group_card_keys.items():
            is_active_provider = provider_key == key
            for card_key in card_keys:
                widget = self._card_widgets.get(card_key)
                if widget is not None:
                    self._set_widget_visible_if_changed(widget, is_active_provider)

        sidebar = self._sidebar_sections.get("provider_behavior")
        if sidebar is not None:
            sidebar.set_cards(self._sidebar_cards_for_section("provider_behavior"))

        visible_cards = self._visible_card_keys_for_section("provider_behavior")
        if self._selected_section_key == "provider_behavior":
            current_card = self._selected_card_key
            if current_card not in visible_cards:
                self._set_active_card(visible_cards[0] if visible_cards else None)
            else:
                self._set_active_card(current_card)
        self._queue_provider_behavior_preload(exclude=key)
        self._refresh_loadout_editor_widgets()

    def _sync_provider_behavior_default_page(self, *, force: bool = False) -> None:
        if self._provider_behavior_user_selected and not force:
            return
        provider = self._get_selected_provider()
        behavior_key = self.BEHAVIOR_CATEGORY_BY_PROVIDER.get(provider) or "deepseek_behavior"
        self._set_provider_behavior_page(behavior_key, user_selected=False)

    def _show_pending_info_bubble(self) -> None:
        anchor = self._pending_info_anchor
        if anchor is None or not anchor.isVisible():
            return
        self._info_hide_timer.stop()
        title = str(anchor.property("settingInfoTitle") or "").strip()
        body = str(anchor.property("settingInfoBody") or "").strip()
        docs_url = str(anchor.property("docsUrl") or "").strip() or None
        if not title and not body:
            return
        self._active_info_anchor = anchor
        self._info_bubble.set_anchor(anchor)
        self._info_bubble.show_for(anchor, title or "Setting", body, docs_url, preferred_global_pos=self._pending_info_pos)
        self._clear_pending_info_request()

    def _clear_pending_info_request(self) -> None:
        self._info_timer.stop()
        self._pending_info_anchor = None
        self._pending_info_pos = None

    def _hide_info_bubble(self, *_args) -> None:
        self._clear_pending_info_request()
        self._info_hide_timer.stop()
        self._active_info_anchor = None
        if hasattr(self, "_info_bubble") and self._info_bubble is not None:
            self._info_bubble.hide_now()

    def _find_info_anchor(self, widget: QWidget | None):
        current = widget
        while current is not None:
            if str(current.property("settingInfoTitle") or "").strip() or str(current.property("settingInfoBody") or "").strip():
                return current
            current = current.parentWidget()
        return None

    def _is_info_bubble_widget(self, widget: QWidget | None) -> bool:
        current = widget
        while current is not None:
            if current is self._info_bubble:
                return True
            current = current.parentWidget()
        return False

    def _cursor_is_over_info_bubble(self, global_pos: QPoint | None = None) -> bool:
        bubble = getattr(self, "_info_bubble", None)
        if bubble is None or not bubble.isVisible():
            return False
        return bubble.contains_global(global_pos or QCursor.pos())

    def _cursor_hits_anchor(self, anchor: QWidget | None, global_pos: QPoint | None = None) -> bool:
        if anchor is None:
            return False

        top_widget = QApplication.widgetAt(global_pos or QCursor.pos())
        current = top_widget
        while current is not None:
            if current is anchor:
                return True
            current = current.parentWidget()
        return False

    def eventFilter(self, obj, event):
        event_type = event.type()

        if obj is self.scroll_area.viewport() and event_type == QEvent.Resize:
            self._hide_info_bubble()

        is_settings_widget = isinstance(obj, QWidget) and (obj is self or self.isAncestorOf(obj))

        if event_type == QEvent.MouseButtonPress and is_settings_widget:
            self._last_focus_reason = Qt.MouseFocusReason
            self._set_active_navigation_highlight(None)

        if event_type == QEvent.FocusIn and is_settings_widget:
            reason = event.reason() if hasattr(event, "reason") else None
            self._last_focus_reason = reason
            anchor = self._resolve_navigation_anchor(obj)
            if anchor is not None:
                if self._should_show_navigation_highlight_for_focus_reason(reason):
                    self._set_active_navigation_highlight(
                        anchor,
                        auto_scroll=self._should_auto_scroll_for_focus_reason(reason),
                    )
                else:
                    self._set_active_navigation_highlight(None)

        if not isinstance(obj, QWidget) or event_type not in INFO_BUBBLE_TRIGGER_EVENTS:
            return super().eventFilter(obj, event)

        if id(obj) in self._info_bubble_widget_ids:
            if event_type in INFO_BUBBLE_HOVER_EVENTS:
                self._clear_pending_info_request()
                self._info_hide_timer.stop()
            elif event_type in INFO_BUBBLE_HIDE_EVENTS:
                self._info_hide_timer.start()
            return super().eventFilter(obj, event)

        anchor = self._info_anchor_by_object_id.get(id(obj))
        if anchor is not None:
            cursor_pos = QCursor.pos()
            cursor_over_bubble = self._cursor_is_over_info_bubble(cursor_pos)
            cursor_hits_anchor = self._cursor_hits_anchor(anchor, cursor_pos)
            if event_type in INFO_BUBBLE_HOVER_EVENTS:
                if cursor_over_bubble or not cursor_hits_anchor:
                    # Only react when this anchor is the topmost thing under the
                    # cursor. Qt can still emit hover-ish events for widgets hidden
                    # beneath our floating info bubble, and those should not be able
                    # to replace the bubble that is already visible.
                    self._clear_pending_info_request()
                    return super().eventFilter(obj, event)
                self._info_hide_timer.stop()
                self._pending_info_anchor = anchor
                self._pending_info_pos = cursor_pos
                self._info_timer.start()
            elif event_type in INFO_BUBBLE_HIDE_EVENTS:
                if cursor_over_bubble:
                    self._info_hide_timer.stop()
                    return super().eventFilter(obj, event)
                self._info_hide_timer.start()
            else:
                self._hide_info_bubble()

        return super().eventFilter(obj, event)

    def _current_field_value(self, full_key: str, field_def):
        widget = self.field_widgets.get(full_key)
        if widget is None or field_def is None:
            return None

        value = None
        if field_def.type == SettingType.BOOLEAN:
            value = widget.isChecked()
        elif field_def.type in [SettingType.STRING, SettingType.PASSWORD]:
            value = widget.text()
        elif field_def.type == SettingType.DIRECTORY:
            value = widget.text().strip()
            if getattr(field_def, "nullable", False) and not value:
                value = None
        elif field_def.type == SettingType.INTEGER:
            text_value = widget.text()
            value = int(text_value) if text_value else 0
        elif field_def.type == SettingType.TEXTAREA:
            value = widget.toPlainText()
        elif full_key == "runtime.parallelization_mode" and isinstance(widget, StyledComboBox):
            value = self._runtime_parallel_mode_key()
        elif field_def.type == SettingType.DROPDOWN:
            value = widget.currentText()
        elif field_def.type == SettingType.SWITCHER and isinstance(widget, SegmentedSwitcher):
            value = widget.current_value()
        elif field_def.type == SettingType.INPUT_PAIR:
            value = []
            for pair in widget.get_pairs():
                if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                    continue
                left = str(pair[0] or "")
                right = str(pair[1] or "")
                if not left.strip() and not right.strip():
                    continue
                value.append([left, right])
        elif field_def.type == SettingType.INPUT_LIST:
            value = widget.get_items()
        elif field_def.type == SettingType.MULTI_SELECT_DROPDOWN:
            value = widget.selected_keys()
        elif field_def.type == SettingType.PROVIDER_LANE_SELECTOR and isinstance(widget, RuntimeProviderLaneDropdown):
            value = self._runtime_lane_value_from_widget(widget)

        is_enabled = self._is_dependency_met(getattr(field_def, "depends", None)) if getattr(field_def, "depends", None) else True
        if (not is_enabled) and (full_key in self._dep_override_cache):
            value = self._dep_override_cache[full_key]
        return value

    def _update_dirty_markers(self) -> None:
        dirty_display_keys = set()
        loadouts_enabled = self._is_loadouts_enabled_in_ui()

        for full_key, field_def in (self.field_defs or {}).items():
            if getattr(field_def, "transient", False):
                continue
            if field_def.type in {
                SettingType.BUTTON,
                SettingType.DESCRIPTION,
                SettingType.DIVIDER,
                SettingType.HINT,
                SettingType.REDIRECT,
                SettingType.ROW,
            }:
                continue
            if full_key not in self.field_widgets:
                continue
            if loadouts_enabled and self._is_loadout_controlled_full_key(full_key):
                continue

            category_key, field_key = full_key.split(".", 1)
            current_value = self._current_field_value(full_key, field_def)
            saved_value = self.config_manager.get_setting(category_key, field_key)
            if full_key == "system_settings.config_storage_location":
                saved_value = infer_preset_from_config_dir(Path(self.config_manager.config_dir).resolve())[0]
            elif full_key == "system_settings.config_storage_custom_path":
                saved_value = infer_preset_from_config_dir(Path(self.config_manager.config_dir).resolve())[1]
            if current_value != saved_value:
                dirty_display_keys.add(self._field_display_key.get(full_key, full_key))

        any_dirty = False
        for display_key, row in (self._display_rows or {}).items():
            is_dirty = display_key in dirty_display_keys
            if hasattr(row, "set_dirty"):
                row.set_dirty(is_dirty)
            any_dirty = any_dirty or is_dirty

        if loadouts_enabled:
            any_dirty = any_dirty or self._loadout_base_values_dirty() or self._loadout_editor_has_structural_changes()

        any_dirty = self._update_runtime_parallelization_dirty_markers() or any_dirty

        self.unsaved_changes = any_dirty

    def _begin_auto_scroll(self, duration_ms: int) -> None:
        self.is_auto_scrolling = True
        if hasattr(self, "_auto_scroll_reset_timer") and isinstance(self._auto_scroll_reset_timer, QTimer):
            self._auto_scroll_reset_timer.start(max(0, int(duration_ms)) + 60)

    def _end_auto_scroll(self) -> None:
        self.is_auto_scrolling = False
        if hasattr(self, "_auto_scroll_reset_timer") and isinstance(self._auto_scroll_reset_timer, QTimer):
            self._auto_scroll_reset_timer.stop()

    def _smooth_scroll_to(self, value: int, *, duration_ms: int = 280) -> bool:
        self._begin_auto_scroll(duration_ms)
        started = self.scroll_area.smooth_scroll_to(int(value), duration_ms=duration_ms)
        if not started:
            self._end_auto_scroll()
        return started

    def _smooth_ensure_visible(self, widget: QWidget, *, y_margin: int = 50, duration_ms: int = 280) -> bool:
        self._begin_auto_scroll(duration_ms)
        started = self.scroll_area.smooth_ensure_widget_visible(widget, y_margin=y_margin, duration_ms=duration_ms)
        if not started:
            self._end_auto_scroll()
        return started

    def _load_values(self):
        self._settings_values_loaded = False
        override_cache = getattr(self, "_dep_override_cache", None)
        if override_cache is not None:
            override_cache.clear()
        if hasattr(self, "_last_custom_template"):
            delattr(self, "_last_custom_template")
        self._persistent_profile_entries = {}
        self._persistent_profile_options_loaded = False
        self._provider_behavior_user_selected = False
        self._load_loadout_editor_state_from_config()
        previous_suppress = self._suppress_dirty_tracking
        self._suppress_dirty_tracking = True
        try:
            for category in SCHEMA:
                self._apply_category_values(category)

            self._refresh_loaded_state(refresh_profiles=False)
            current_behavior_key = (
                self.BEHAVIOR_CATEGORY_BY_PROVIDER.get(self._get_selected_provider())
                or "deepseek_behavior"
            )
            self._ensure_provider_behavior_group_cards_built(current_behavior_key, refresh_dirty=False)
            self._queue_provider_behavior_preload(exclude=current_behavior_key)
            self._queue_section_preload(exclude=self._selected_section_key)
            delete_btn = self.field_widgets.get("system_settings.delete_persistent_profile_btn")
            if isinstance(delete_btn, QPushButton):
                delete_btn.setEnabled(False)
            self._sync_profile_clear_button_label()
        finally:
            self._suppress_dirty_tracking = previous_suppress
            self._settings_values_loaded = True
            self.unsaved_changes = False

    def refresh_from_config(self, force: bool = False) -> bool:
        if self.unsaved_changes and not force:
            return False

        try:
            self.config_manager.load_settings()
        except Exception as exc:
            Logger.warning(f"Failed to reload settings: {exc}")

        self._load_values()
        self._sync_application_settings_info()
        return True

    def select_category_by_key(self, category_key: str) -> bool:
        resolved_key = str(category_key or "").strip()
        if not resolved_key:
            return False

        section_key = resolved_key
        if resolved_key not in self._section_defs_by_key:
            for full_key, location in (self._field_locations or {}).items():
                if full_key.startswith(f"{resolved_key}."):
                    section_key = str(location.get("section_key") or "")
                    break

        if section_key not in self._section_defs_by_key:
            return False

        self._set_active_section(section_key, scroll_to_top=True)
        if resolved_key in PROVIDER_BEHAVIOR_GROUPS:
            self._set_provider_behavior_page(resolved_key, user_selected=False)
        return True

    def focus_setting(self, category_key: str, field_key: str) -> bool:
        category_key = str(category_key or "").strip()
        field_key = str(field_key or "").strip()
        if not category_key or not field_key:
            return False

        full_key = f"{category_key}.{field_key}"
        location = self._field_locations.get(full_key)
        if not location:
            return False

        section_key = str(location.get("section_key") or "")
        card_key = str(location.get("card_key") or "")
        provider_key = str(location.get("provider_key") or "").strip() or None
        self._set_active_section(section_key, scroll_to_top=False)
        self._set_active_card(card_key)
        if provider_key:
            self._set_provider_behavior_page(provider_key, user_selected=False)
        target = self.setting_rows.get(full_key) or self.field_widgets.get(full_key)
        if not target:
            return False
        self._set_active_navigation_highlight(target, auto_scroll=True, pulse=True)
        return True

    def _on_setting_changed(self):
        if self._suppress_dirty_tracking:
            return
        sender = self.sender()
        full_key = str(sender.property("fullKey") or "").strip() if sender is not None else ""
        if full_key == "system_settings.ignore_provider_locks":
            QTimer.singleShot(0, self.refresh_dynamic_dropdown_options)
            QTimer.singleShot(0, self._sync_runtime_parallelization_widgets_from_config)
        elif full_key == "providers_credentials.provider":
            QTimer.singleShot(0, self._sync_runtime_parallelization_current_provider)
        elif full_key.endswith(".show_deprecated_models"):
            category_key = full_key.split(".", 1)[0]
            model_field = self.field_defs.get(f"{category_key}.model")
            if model_field:
                QTimer.singleShot(0, lambda c=category_key, f=model_field: self._refresh_dropdown_options_for_field(c, f))
        if full_key and self._is_loadout_controlled_full_key(full_key):
            field_def = self.field_defs.get(full_key)
            if field_def is not None:
                if self._is_loadouts_enabled_in_ui():
                    behavior_key = self._current_editor_behavior_key()
                    if full_key.split(".", 1)[0] in PROVIDER_BEHAVIOR_GROUPS:
                        behavior_key = full_key.split(".", 1)[0]
                    self._capture_current_loadout_from_widgets(behavior_key)
                else:
                    self._loadout_base_values_cache[full_key] = copy.deepcopy(
                        self._current_field_value(full_key, field_def)
                    )
        if isinstance(sender, Tumbler) and not animations_disabled():
            # Let the short switch animation finish before dependency/dirty updates
            # touch the surrounding Settings tree
            self.update_timer.start(190)
        else:
            QTimer.singleShot(0, self._update_dirty_markers)
            self.update_timer.start()

    def _on_input_pair_alternative_action(
        self,
        category_key: str,
        field_key: str,
        widget: InputPairsWidget,
        action_name: str,
    ) -> None:
        action_name = str(action_name or "").strip()
        if not action_name:
            return

        if (category_key == "network_settings") and (field_key == "api_keys") and (action_name == "generate_api_key"):
            self._generate_api_key(widget)
            return

        Logger.warning(f"Unhandled input pair alternative action: {category_key}.{field_key} -> {action_name}")

    def _generate_api_key(self, widget: InputPairsWidget) -> None:
        existing: set[str] = set()
        for pair in (widget.get_pairs() or []):
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                key_val = str(pair[1] or "").strip()
                if key_val:
                    existing.add(key_val)

        for _ in range(25):
            name, key_val = generate_api_key(prefix="intenserp")
            if key_val in existing:
                continue

            widget.upsert_pair(name, key_val, emit_change=True)
            return

        QMessageBox.warning(
            self,
            "Generate Key",
            "Failed to generate a unique key. Please try again.",
        )

    def _get_widget_value(self, widget):
        if isinstance(widget, Tumbler):
            return widget.isChecked()
        if isinstance(widget, (StyledLineEdit, QLineEdit, DirectoryEntry)):
            return widget.text()
        if isinstance(widget, StyledComboBox):
            return widget.currentText()
        if isinstance(widget, SegmentedSwitcher):
            return widget.current_value()
        if isinstance(widget, StyledTextEdit):
            return widget.toPlainText()
        if isinstance(widget, InputPairsWidget):
            return widget.get_pairs()
        if isinstance(widget, InputListWidget):
            return widget.get_items()
        if isinstance(widget, MarshmallowMultiSelectDropdown):
            return widget.selected_keys()
        return None

    def _set_widget_value(self, widget, value):
        widget.blockSignals(True)
        try:
            if isinstance(widget, Tumbler):
                widget.setChecked(bool(value))
            elif isinstance(widget, (StyledLineEdit, QLineEdit, DirectoryEntry)):
                widget.setText("" if value is None else str(value))
            elif isinstance(widget, StyledComboBox):
                widget.setCurrentText("" if value is None else str(value))
            elif isinstance(widget, SegmentedSwitcher):
                widget.set_value("" if value is None else str(value), emit=False, animate=False)
            elif isinstance(widget, StyledTextEdit):
                widget.setPlainText("" if value is None else str(value))
            elif isinstance(widget, InputPairsWidget):
                widget.set_pairs(value or [])
            elif isinstance(widget, InputListWidget):
                widget.set_items(value or [])
            elif isinstance(widget, MarshmallowMultiSelectDropdown):
                widget.set_selected_keys(value or [])
        finally:
            widget.blockSignals(False)

    def _set_widget_enabled_if_changed(self, widget: QWidget | None, enabled: bool) -> None:
        if widget is None:
            return
        enabled = bool(enabled)
        try:
            if widget.isEnabled() == enabled:
                return
        except RuntimeError:
            return
        widget.setEnabled(enabled)

    def _set_widget_visible_if_changed(self, widget: QWidget | None, visible: bool) -> None:
        if widget is None:
            return
        visible = bool(visible)
        try:
            requested_visible = widget.property("_settings_requested_visible")
            explicitly_hidden = widget.testAttribute(Qt.WA_WState_Hidden)
            if requested_visible == visible and explicitly_hidden == (not visible):
                return
            widget.setProperty("_settings_requested_visible", visible)
            widget.setVisible(visible)
        except RuntimeError:
            return

    def _sync_section_visibility(self) -> None:
        selected_section_key = str(self._selected_section_key or "").strip()
        for key, section_widget in self._section_widgets.items():
            self._set_widget_visible_if_changed(section_widget, key == selected_section_key)

    def _is_dependency_met(self, expr: str | None) -> bool:
        if not expr:
            return True

        # Top-level OR: any alternative being fully met satisfies the expression.
        alternatives = [alt.strip() for alt in str(expr).split("||")]
        if len(alternatives) > 1:
            return any(self._is_dependency_met(alt) for alt in alternatives if alt)

        parts = [part.strip() for part in alternatives[0].split("&&")]
        for part in parts:
            if not part:
                continue

            if "==" in part:
                left, right = part.split("==", 1)
                dep_key = left.strip()
                expected = right.strip()
                widget = self.field_widgets.get(dep_key)
                if not widget:
                    return False

                value = self._get_widget_value(widget)
                if isinstance(value, bool):
                    expected_bool = expected.lower() in {"1", "true", "yes", "on"}
                    if value != expected_bool:
                        return False
                else:
                    if str(value or "").strip() != expected:
                        return False
                continue

            if "!=" in part:
                left, right = part.split("!=", 1)
                dep_key = left.strip()
                expected = right.strip()
                widget = self.field_widgets.get(dep_key)
                if not widget:
                    return False

                value = self._get_widget_value(widget)
                if isinstance(value, bool):
                    expected_bool = expected.lower() in {"1", "true", "yes", "on"}
                    if value == expected_bool:
                        return False
                else:
                    if str(value or "").strip() == expected:
                        return False
                continue

            # Backwards-compatible: treat the token as a widget key and check truthiness.
            widget = self.field_widgets.get(part)
            if not widget:
                return False

            value = self._get_widget_value(widget)
            if isinstance(value, bool):
                if not value:
                    return False
            else:
                if not str(value or "").strip():
                    return False

        return True

    def _update_dependencies(self):
        for dependent_key, field_def in (self.field_defs or {}).items():
            depends_expr = getattr(field_def, "depends", None) if field_def else None
            if not depends_expr:
                depends_expr = None

            widget = self.field_widgets.get(dependent_key)
            if not widget:
                continue

            is_met = self._is_dependency_met(depends_expr) if depends_expr else True
            forced_value = getattr(field_def, "force_when_dep_unmet", None) if field_def else None

            desired_mode = None
            should_override = False
            override_value = None

            if not is_met:
                if forced_value is not None:
                    should_override = True
                    override_value = forced_value
                    if isinstance(widget, Tumbler):
                        desired_mode = "forced"
                elif isinstance(widget, Tumbler):
                    # Disabled + not counted: show as OFF and treat as unmet.
                    should_override = True
                    override_value = False
                    desired_mode = "ignored"

            if is_met:
                if dependent_key in self._dep_override_cache:
                    cached_value = self._dep_override_cache.pop(dependent_key)
                    self._set_widget_value(widget, cached_value)
                if isinstance(widget, Tumbler):
                    widget.set_dependency_mode(None)
            else:
                if should_override:
                    if dependent_key not in self._dep_override_cache:
                        self._dep_override_cache[dependent_key] = self._get_widget_value(widget)
                    self._set_widget_value(widget, override_value)
                if isinstance(widget, Tumbler):
                    widget.set_dependency_mode(desired_mode)

            # If there's a SettingRow for this field, enable/disable the whole row
            row = self.setting_rows.get(dependent_key)
            if row:
                self._set_widget_enabled_if_changed(row, is_met)
            else:
                self._set_widget_enabled_if_changed(widget, is_met)
            if not is_met and isinstance(widget, (StyledLineEdit, DirectoryEntry)):
                widget.set_error(False) # Clear error if disabled

            visible_expr = getattr(field_def, "visible_depends", None) if field_def else None
            if visible_expr is not None:
                should_show = self._is_dependency_met(visible_expr)
                if row:
                    self._set_widget_visible_if_changed(row, should_show)
                else:
                    self._set_widget_visible_if_changed(widget, should_show)

        self._apply_forced_overrides()
        self._sync_runtime_parallelization_enabled_state()
        if not self._suppress_dirty_tracking:
            self._update_dirty_markers()

    def _apply_forced_overrides(self) -> None:
        preset_widget = self.field_widgets.get("formatting.formatting_preset")
        template_widget = self.field_widgets.get("formatting.formatting_template")
        template_row = self.setting_rows.get("formatting.formatting_template")
        if preset_widget is not None and template_widget is not None:
            is_custom = str(preset_widget.currentText() or "") == "Custom"
            if template_row is not None:
                self._set_widget_enabled_if_changed(template_row, is_custom)
            else:
                self._set_widget_enabled_if_changed(template_widget, is_custom)

    def _is_loadouts_enabled_in_ui(self) -> bool:
        widget = self.field_widgets.get("experimental.enable_loadouts")
        if isinstance(widget, Tumbler):
            return bool(widget.isChecked())
        return bool(self.config_manager.get_setting("experimental", "enable_loadouts"))

    def _is_loadout_controlled_full_key(self, full_key: str) -> bool:
        normalized = str(full_key or "").strip()
        if not normalized or "." not in normalized:
            return False
        category_key, _field_key = normalized.split(".", 1)
        return (category_key == "formatting") or (category_key in PROVIDER_BEHAVIOR_GROUPS)

    def _on_loadouts_toggle_changed(self) -> None:
        if self._is_loadouts_enabled_in_ui():
            for full_key, field_def in self._iter_saved_loadout_controlled_fields():
                self._loadout_base_values_cache[full_key] = copy.deepcopy(
                    self._current_field_value(full_key, field_def)
                )
        self._refresh_loadout_editor_widgets()
        self._clear_search_results()

    def _on_search_text_changed(self, text):
        self.search_timer.stop()
        self.search_clear_action.setVisible(bool(text.strip()))
        if text.strip():
            self.search_nav.show()
            self.search_timer.start()
        else:
            self._clear_search_results()

    def _clear_search(self) -> None:
        self.search_input.clear()

    def _clear_search_results(self) -> None:
        self._search_matches = []
        self._search_match_index = -1
        self.search_nav.hide()
        self.search_status_label.setText("0 / 0")
        self.search_prev_btn.setEnabled(False)
        self.search_next_btn.setEnabled(False)
        self._clear_flash()
        self._sync_active_navigation_highlight(self.focusWidget())

    def _update_search_nav_ui(self) -> None:
        total = len(self._search_matches)
        current = self._search_match_index + 1 if total > 0 and self._search_match_index >= 0 else 0
        self.search_status_label.setText(f"{current} / {total}")
        can_navigate = total > 1
        self.search_prev_btn.setEnabled(can_navigate)
        self.search_next_btn.setEnabled(can_navigate)

    def _activate_search_target(self, target: dict) -> None:
        section_key = str(target.get("section_key") or "")
        card_key = str(target.get("card_key") or "")
        provider_key = str(target.get("provider_key") or "").strip() or None
        if section_key:
            self._set_active_section(section_key, scroll_to_top=False)
        if provider_key:
            self._set_provider_behavior_page(provider_key, user_selected=False)
        if card_key:
            self._set_active_card(card_key)
        full_key = str(target.get("full_key") or "")
        widget = self.setting_rows.get(full_key) or self.field_widgets.get(full_key)
        if widget is None or widget.isHidden():
            return
        self._set_active_navigation_highlight(widget, auto_scroll=True, pulse=True)

    def _goto_search_match(self, index: int) -> None:
        if not self._search_matches:
            return
        total = len(self._search_matches)
        self._search_match_index = index % total
        self._update_search_nav_ui()
        self._activate_search_target(self._search_matches[self._search_match_index])

    def _goto_previous_search_match(self) -> None:
        if not self._search_matches:
            return
        self._goto_search_match(self._search_match_index - 1)

    def _goto_next_search_match(self) -> None:
        if not self._search_matches:
            return
        self._goto_search_match(self._search_match_index + 1)

    def _score_match(self, query: str, target: dict) -> float:
        candidates = [
            target.get("label_lower", ""),
            target.get("key_lower", ""),
            target.get("section_lower", ""),
            target.get("card_lower", ""),
            target.get("provider_lower", ""),
            target.get("extra_lower", ""),
        ]
        combined = " ".join(cand for cand in candidates if cand).strip()
        direct_score = self._direct_match_score(query, candidates, combined)
        if direct_score > 0.0:
            return direct_score
        return self._fuzzy_match_score(query, candidates)

    def _normalize_search_text(self, text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())

    def _direct_match_score(self, query: str, candidates: list[str], combined: str) -> float:
        normalized_query = self._normalize_search_text(query)
        query_terms = [term for term in re.split(r"\s+", str(query or "").strip().lower()) if term]
        best = 0.0

        for index, cand in enumerate(candidates):
            if not cand:
                continue

            weight = 1.0
            if index == 0:
                weight = 1.0
            elif index == 1:
                weight = 0.96
            elif index == 3:
                weight = 0.93
            elif index == 4:
                weight = 0.88
            else:
                weight = 0.84

            normalized_cand = self._normalize_search_text(cand)
            if query == cand:
                best = max(best, 1.0 * weight)
                continue
            if normalized_query and normalized_query == normalized_cand:
                best = max(best, 0.995 * weight)
                continue
            if cand.startswith(query):
                best = max(best, 0.975 * weight)
                continue
            if normalized_query and normalized_cand.startswith(normalized_query):
                best = max(best, 0.965 * weight)
                continue
            if query and query in cand:
                idx = cand.find(query)
                best = max(best, (0.93 + (1 - idx / max(len(cand), 1)) * 0.03) * weight)
                continue
            if normalized_query and normalized_query in normalized_cand:
                idx = normalized_cand.find(normalized_query)
                best = max(best, (0.91 + (1 - idx / max(len(normalized_cand), 1)) * 0.03) * weight)

        if query_terms and combined:
            all_terms_present = all(term in combined for term in query_terms)
            if all_terms_present:
                best = max(best, 0.82)

        return best

    def _fuzzy_match_score(self, query: str, candidates: list[str]) -> float:
        best = 0.0
        normalized_query = self._normalize_search_text(query)
        for cand in candidates:
            if not cand:
                continue

            ratio = SequenceMatcher(None, query, cand).ratio()
            best = max(best, ratio * 0.75)

            normalized_cand = self._normalize_search_text(cand)
            if normalized_query and normalized_cand:
                normalized_ratio = SequenceMatcher(None, normalized_query, normalized_cand).ratio()
                best = max(best, normalized_ratio * 0.78)

        return best

    def _perform_search(self):
        query = self.search_input.text().strip().lower()
        if not query:
            return

        direct_matches = []
        fuzzy_matches = []

        for target in self.search_targets:
            score = self._score_match(query, target)
            if score >= 0.80:
                direct_matches.append((score, target))
            elif score >= 0.68:
                fuzzy_matches.append((score, target))

        if direct_matches:
            direct_matches.sort(key=lambda item: item[0], reverse=True)
            chosen_matches = direct_matches[:24]
        else:
            fuzzy_matches.sort(key=lambda item: item[0], reverse=True)
            chosen_matches = fuzzy_matches[:8]

        self._search_matches = [target for _score, target in chosen_matches]
        if not self._search_matches:
            self._search_match_index = -1
            self._update_search_nav_ui()
            self._sync_active_navigation_highlight(self.focusWidget())
            return

        self._search_match_index = 0
        self._update_search_nav_ui()
        self._activate_search_target(self._search_matches[0])

    def _flash_widget(self, widget):
        overlay = getattr(self, "_highlight_overlay", None)
        if overlay and isinstance(overlay, _SearchHighlightOverlay):
            overlay.pulse_widget(widget)

    def _clear_flash(self):
        overlay = getattr(self, "_highlight_overlay", None)
        if overlay and isinstance(overlay, _SearchHighlightOverlay):
            overlay.clear_flash()

    def _on_category_clicked(self, item):
        return

    def _on_scroll(self, value):
        if self.is_auto_scrolling:
            return
        section = self._section_defs_by_key.get(self._selected_section_key or "")
        if section is None:
            return

        visible_cards = self._visible_card_keys_for_section(section.key)
        if not visible_cards:
            return

        vbar = self.scroll_area.verticalScrollBar()
        if value >= max(0, vbar.maximum() - 2):
            self._set_active_card(visible_cards[-1])
            return

        scroll_pos = value
        active_card = None
        for card_key in visible_cards:
            widget = self._card_widgets.get(card_key)
            if widget is None or not widget.isVisible():
                continue
            widget_y = widget.mapTo(self.scroll_content, QPoint(0, 0)).y()
            if widget_y <= scroll_pos + 60:
                active_card = card_key
        if active_card:
            self._set_active_card(active_card)

    def _sync_config_storage_from_active_dir(self):
        preset_widget = self.field_widgets.get("system_settings.config_storage_location")
        custom_widget = self.field_widgets.get("system_settings.config_storage_custom_path")
        if not preset_widget or not custom_widget:
            return

        active_dir = Path(getattr(self.config_manager, "config_dir", "config_data")).resolve()
        preset, custom_path = infer_preset_from_config_dir(active_dir)

        preset_widget.blockSignals(True)
        options = [preset_widget.itemText(i) for i in range(preset_widget.count())]
        preset_to_apply = preset if preset in options else "Custom"
        preset_widget.setCurrentText(preset_to_apply)
        preset_widget.blockSignals(False)

        if preset_to_apply == "Custom":
            custom_widget.blockSignals(True)
            custom_widget.setText(custom_path)
            custom_widget.blockSignals(False)

        self._on_config_storage_location_changed(preset_to_apply)

    def _is_behavior_category(self, category_key: str) -> bool:
        return bool(category_key) and str(category_key).endswith("_behavior")

    def _get_selected_category_key(self) -> str | None:
        return str(self._selected_section_key or "") or None

    def _should_use_paged_settings_view(self) -> bool:
        return True

    def _sync_paged_settings_view(self, *_args, scroll_to_top: bool = False) -> None:
        if scroll_to_top:
            self._smooth_scroll_to(0, duration_ms=220)

    def _should_show_only_active_provider_behavior(self) -> bool:
        return False

    def _get_selected_provider(self) -> DriverProvider:
        widget = self.field_widgets.get("providers_credentials.provider")
        if isinstance(widget, StyledComboBox):
            provider_setting = widget.currentText()
        else:
            provider_setting = self.config_manager.get_setting("providers_credentials", "provider")
        return DriverProvider.from_setting(provider_setting)

    def _set_category_visible(self, category_key: str, visible: bool) -> None:
        return

    def _sync_behavior_category_visibility(self, *_args) -> None:
        self._sync_provider_behavior_default_page()

    def _on_config_storage_location_changed(self, text: str):
        is_custom = text == "Custom"
        custom_key = "system_settings.config_storage_custom_path"
        row = self.setting_rows.get(custom_key)
        widget = self.field_widgets.get(custom_key)

        if row:
            self._set_widget_enabled_if_changed(row, is_custom)
        elif widget:
            self._set_widget_enabled_if_changed(widget, is_custom)

        if not is_custom and isinstance(widget, (StyledLineEdit, DirectoryEntry)):
            widget.set_error(False)

    def _on_preset_changed(self, text):
        template_widget = self.field_widgets.get("formatting.formatting_template")
        if not template_widget:
            return

        previous_block = template_widget.blockSignals(True)
        try:
            if text == "Custom":
                self._set_widget_enabled_if_changed(template_widget, True)
                # We need to store the custom value temporarily if we switch away from Custom.
                
                if hasattr(self, "_last_custom_template"):
                    # Ignoring lint because we know it exists here
                    template_widget.setPlainText(self._last_custom_template)
                    
            else:
                # If the widget is enabled, it means we are on Custom (or just started).
                if template_widget.isEnabled():
                    self._last_custom_template = template_widget.toPlainText()
                
                self._set_widget_enabled_if_changed(template_widget, False)
                template = FORMATTING_PRESET_TEMPLATES.get(text)
                if template is not None:
                    template_widget.setPlainText(template)
        finally:
            template_widget.blockSignals(previous_block)

        row = self.setting_rows.get("formatting.formatting_template")
        is_custom = text == "Custom"
        if row is not None:
            self._set_widget_enabled_if_changed(row, is_custom)
        else:
            self._set_widget_enabled_if_changed(template_widget, is_custom)

    def _sync_application_settings_info(self):
        version_widget = self.field_widgets.get("application_settings.current_version_info")
        if isinstance(version_widget, QLabel):
            version_widget.setText(f"Current version: {read_local_version()}")

    def _set_update_status(self, text: str):
        status_widget = self.field_widgets.get("application_settings.update_status_info")
        if isinstance(status_widget, QLabel):
            status_widget.setText(text)

    def _check_for_updates(self):
        if getattr(self, "_update_check_in_progress", False):
            return

        self._update_check_in_progress = True
        self._set_update_status("Status: Checking...")

        btn_key = "application_settings.check_for_updates_btn"
        btn = self.field_widgets.get(btn_key)
        original_text = btn.text() if isinstance(btn, QPushButton) else "Check"

        if isinstance(btn, QPushButton):
            btn.setEnabled(False)
            btn.setText("Checking...")

        def worker():
            result = check_for_updates()
            self.update_check_finished.emit(result, original_text)

        threading.Thread(target=worker, daemon=True).start()

    def _handle_update_check_result(self, result, original_button_text: str):
        self._update_check_in_progress = False

        btn = self.field_widgets.get("application_settings.check_for_updates_btn")
        if isinstance(btn, QPushButton):
            btn.setEnabled(True)
            btn.setText(original_button_text or "Check")

        self._sync_application_settings_info()

        if result.error:
            self._set_update_status("Status: Failed to check for updates.")
            QMessageBox.warning(
                self,
                "Check For Updates",
                "Failed to check for updates.\n\n"
                f"{result.error}",
            )
            return

        if result.update_available:
            sev = getattr(result, "remote_severity", None)
            sev_suffix = f", severity: {sev}" if sev is not None else ""
            self._set_update_status(f"Status: Update available ({result.remote_version}{sev_suffix}).")
            dialog = UpdateAvailableDialog(
                UpdateAvailableInfo(
                    local_version=str(result.local_version or "unknown"),
                    remote_version=str(result.remote_version or "unknown"),
                    remote_auto_updateable=getattr(result, "remote_auto_updateable", None),
                    remote_severity=getattr(result, "remote_severity", None),
                    remote_summary=getattr(result, "remote_summary", None),
                ),
                parent=self,
            )
            dialog.exec()
            return

        self._set_update_status(f"Status: Up to date ({result.local_version}).")
        QMessageBox.information(
            self,
            "No Updates Found",
            "You're up to date.\n\n"
            f"Current: {result.local_version}\n"
            f"Latest: {result.remote_version}",
        )

    def _reset_formatting(self):
        preset_widget = self.field_widgets.get("formatting.formatting_preset")
        if preset_widget:
            preset_widget.setCurrentText("Classic - Name")

    def _reset_injection(self):
        position_widget = self.field_widgets.get("formatting.injection_position")
        content_widget = self.field_widgets.get("formatting.injection_content")
        
        if position_widget:
            position_widget.setCurrentText("Before")
        
        if content_widget:
            content_widget.setPlainText("[Important Instructions]")

    def _get_profiles_root_dir(self) -> Path:
        base_dir = Path(getattr(self.config_manager, "config_dir", "config_data")).resolve()
        return (base_dir / "playwright_profiles").resolve()

    def _format_provider_label(self, provider_key: str) -> str:
        for provider in DriverProvider:
            if provider.key == provider_key:
                if provider is DriverProvider.GLM_CHAT:
                    return "GLM"
                return provider.value

        raw = str(provider_key or "").strip()
        if not raw:
            return "Unknown"
        return raw.replace("_", " ").title()

    def _build_persistent_profile_entries(self) -> list[tuple[str, str, Path]]:
        legacy: list[tuple[tuple[str], tuple[str, str, Path]]] = []
        accounts: list[tuple[tuple[str, str, int], tuple[str, str, Path]]] = []

        profiles_root = self._get_profiles_root_dir()

        # Legacy profiles: [config_dir]/playwright_profiles/<provider_key>/
        try:
            if profiles_root.exists() and profiles_root.is_dir():
                for child in profiles_root.iterdir():
                    if not child.is_dir():
                        continue
                    if child.name in {"ece", "accounts"}:
                        continue
                    provider_name = self._format_provider_label(child.name)
                    label = f"[Legacy] {provider_name}"
                    token = str(child.resolve())
                    legacy.append(((provider_name.lower(),), (token, label, child)))
        except Exception:
            pass

        # Account profiles: [config_dir]/playwright_profiles/accounts/<provider_key>/<hash>[_slot]/
        config_dir = Path(getattr(self.config_manager, "config_dir", "config_data")).resolve()
        account_roots = [profiles_root / "accounts", profiles_root / "ece"]
        account_manager = None

        pre_roots = [root for root in account_roots if root.exists() and root.is_dir()]
        if pre_roots:
            try:
                from ece.manager import EceManager

                account_manager = EceManager(config_dir)
            except Exception as exc:
                Logger.debug(f"Accounts: unable to read credentials for profile labels: {exc}")
                account_manager = None

            # EceManager may migrate/rename directories; re-evaluate roots after init
            roots = [root for root in account_roots if root.exists() and root.is_dir()]
            for root in roots:
                try:
                    provider_dirs = [p for p in root.iterdir() if p.is_dir()]
                except Exception:
                    provider_dirs = []

                for provider_dir in provider_dirs:
                    provider_key = provider_dir.name
                    provider_name = self._format_provider_label(provider_key)

                    hash_to_email: dict[str, str] = {}
                    if account_manager is not None:
                        try:
                            pairs = account_manager.get_provider_pairs(provider_key)
                            for pair in pairs:
                                email = (pair.email or "").strip()
                                if not email:
                                    continue
                                ident = account_manager.get_profile_dir(provider_key, email=email, slot=0).name
                                hash_to_email[ident] = email
                        except Exception:
                            hash_to_email = {}

                    try:
                        ident_dirs = [p for p in provider_dir.iterdir() if p.is_dir()]
                    except Exception:
                        ident_dirs = []

                    for ident_dir in ident_dirs:
                        ident_name = ident_dir.name
                        base_ident = ident_name
                        slot = 0
                        if "_" in ident_name:
                            maybe_base, maybe_slot = ident_name.rsplit("_", 1)
                            if maybe_slot.isdigit():
                                base_ident = maybe_base
                                try:
                                    slot = int(maybe_slot)
                                except Exception:
                                    slot = 0

                        email = None
                        if base_ident != "manual":
                            email = hash_to_email.get(base_ident)

                        if base_ident == "manual":
                            label = f"[Account] {provider_name} - manual"
                        elif email:
                            label = f"[Account] {provider_name} - {email}"
                        else:
                            label = f"[Account] {provider_name} - {base_ident}"

                        if slot > 0:
                            label = f"{label} (slot {slot})"

                        token = str(ident_dir.resolve())
                        sort_ident = (email or base_ident or "").lower()
                        accounts.append(((provider_name.lower(), sort_ident, slot), (token, label, ident_dir)))

        legacy_sorted = [item for _k, item in sorted(legacy, key=lambda t: t[0])]
        accounts_sorted = [item for _k, item in sorted(accounts, key=lambda t: t[0])]
        return legacy_sorted + accounts_sorted

    def _refresh_persistent_profile_options(self):
        select_widget = self.field_widgets.get("system_settings.persistent_profile_to_delete")
        delete_btn = self.field_widgets.get("system_settings.delete_persistent_profile_btn")

        if not isinstance(select_widget, StyledComboBox):
            return

        old_token = select_widget.currentData(Qt.UserRole)
        entries = self._build_persistent_profile_entries()
        self._persistent_profile_entries = {token: (label, path) for token, label, path in entries}
        self._persistent_profile_options_loaded = True

        select_widget.blockSignals(True)
        try:
            select_widget.clear()

            if not entries:
                select_widget.addItem("(No saved profiles found)", "")
                select_widget.setEnabled(False)
                if isinstance(delete_btn, QPushButton):
                    delete_btn.setEnabled(False)
                return

            select_widget.setEnabled(True)
            if isinstance(delete_btn, QPushButton):
                delete_btn.setEnabled(True)

            for token, label, _path in entries:
                select_widget.addItem(label, token)

            if old_token and str(old_token) in self._persistent_profile_entries:
                idx = select_widget.findData(old_token, Qt.UserRole)
                if idx >= 0:
                    select_widget.setCurrentIndex(idx)
        finally:
            select_widget.blockSignals(False)

    def _get_selected_persistent_profile(self) -> tuple[str, str, Path] | None:
        select_widget = self.field_widgets.get("system_settings.persistent_profile_to_delete")
        if not isinstance(select_widget, StyledComboBox):
            return None

        token = select_widget.currentData(Qt.UserRole)
        if token is None:
            token = ""

        entry = self._persistent_profile_entries.get(str(token))
        if not entry:
            return None

        label, path = entry
        return (str(token), label, path)

    def _delete_selected_persistent_profile(self):
        selected = self._get_selected_persistent_profile()
        if not selected:
            QMessageBox.information(self, "Delete Profile", "No saved browser profile is selected.")
            return

        _token, label, profile_dir = selected
        base_dir = Path(getattr(self.config_manager, "config_dir", "config_data")).resolve()

        try:
            profile_dir.resolve().relative_to(base_dir)
        except Exception:
            QMessageBox.warning(
                self,
                "Delete Profile",
                "Refusing to delete profile: resolved path is outside the config directory.",
            )
            return

        if not profile_dir.exists():
            QMessageBox.information(self, "Delete Profile", "That profile folder no longer exists.")
            self._refresh_persistent_profile_options()
            return

        reply = QMessageBox.question(
            self,
            "Delete Profile",
            "This will permanently delete the selected saved browser profile:\n\n"
            f"{label}\n\n"
            "This removes cookies/local storage and will log you out.\n\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if reply != QMessageBox.Yes:
            return

        try:
            shutil.rmtree(profile_dir)
            Logger.success(f"Deleted persistent profile: {label}")
            QMessageBox.information(self, "Delete Profile", "Profile deleted successfully.")
        except Exception as e:
            Logger.error(f"Error deleting persistent profile: {e}")
            QMessageBox.warning(self, "Delete Profile", f"Failed to delete profile:\n\n{e}")
        finally:
            self._refresh_persistent_profile_options()

    def _profile_clear_provider_options(self) -> list[DriverProvider]:
        return list(DriverProvider)

    def _selected_profile_clear_providers(self) -> list[DriverProvider]:
        widget = self.field_widgets.get("system_settings.clear_persistent_profile_providers")
        if not isinstance(widget, MarshmallowMultiSelectDropdown):
            return self._profile_clear_provider_options()

        selected: list[DriverProvider] = []
        seen: set[DriverProvider] = set()
        for key in widget.selected_keys():
            provider = DriverProvider.from_setting(key)
            if provider is None or provider in seen:
                continue
            selected.append(provider)
            seen.add(provider)
        return selected

    def _sync_profile_clear_button_label(self) -> None:
        button = self.field_widgets.get("system_settings.clear_persistent_profiles_btn")
        if not isinstance(button, QPushButton):
            return

        providers = self._selected_profile_clear_providers()
        total_count = len(self._profile_clear_provider_options())
        selected_count = len(providers)

        if selected_count <= 0:
            button.setText("Select Providers")
            button.setEnabled(False)
        elif selected_count == total_count:
            button.setText("Clear All")
            button.setEnabled(True)
        elif selected_count == 1:
            button.setText(f"Clear {providers[0].value}")
            button.setEnabled(True)
        else:
            button.setText(f"Clear {selected_count} Providers")
            button.setEnabled(True)

    def set_profile_clear_provider_selection(self, providers: list[str] | tuple[str, ...] | set[str]) -> bool:
        self._ensure_card_built(
            "provider_login",
            "saved_sessions",
            refresh_values=self._settings_values_loaded,
        )
        self._maybe_refresh_persistent_profile_options("provider_login", force=True)
        widget = self.field_widgets.get("system_settings.clear_persistent_profile_providers")
        if not isinstance(widget, MarshmallowMultiSelectDropdown):
            return False

        selected_values: list[str] = []
        for value in providers or []:
            provider = DriverProvider.from_setting(str(value or ""))
            if provider is not None:
                selected_values.append(provider.value)

        widget.set_selected_keys(selected_values)
        self._sync_profile_clear_button_label()
        return True

    def _persistent_profile_dirs_for_providers(self, providers: list[DriverProvider]) -> list[Path]:
        profiles_root = self._get_profiles_root_dir()
        candidates: list[Path] = []
        seen: set[str] = set()

        for provider in providers:
            for candidate in (
                profiles_root / "accounts" / provider.key,
                profiles_root / "ece" / provider.key,
                profiles_root / provider.key,
            ):
                try:
                    resolved = str(candidate.resolve())
                except Exception:
                    resolved = str(candidate)
                if resolved in seen:
                    continue
                seen.add(resolved)
                candidates.append(candidate)

        return [path for path in candidates if path.exists()]

    def _clear_selected_persistent_profiles(self):
        providers = self._selected_profile_clear_providers()
        if not providers:
            QMessageBox.information(self, "Clear Profiles", "Select at least one provider to clear.")
            return
        self._clear_persistent_profiles_for_providers(providers)

    def _clear_all_persistent_profiles(self):
        self._clear_persistent_profiles_for_providers(self._profile_clear_provider_options())

    def _clear_persistent_profiles_for_providers(self, providers: list[DriverProvider]) -> None:
        profiles_root = self._get_profiles_root_dir()
        base_dir = Path(getattr(self.config_manager, "config_dir", "config_data")).resolve()

        try:
            profiles_root.resolve().relative_to(base_dir)
        except Exception:
            QMessageBox.warning(
                self,
                "Clear Profiles",
                "Refusing to clear profiles: resolved path is outside the config directory.",
            )
            return

        if not profiles_root.exists():
            QMessageBox.information(self, "Clear Profiles", "No saved browser profiles were found.")
            self._refresh_persistent_profile_options()
            return

        profile_dirs = self._persistent_profile_dirs_for_providers(providers)
        if not profile_dirs:
            QMessageBox.information(self, "Clear Profiles", "No saved profiles were found for the selected providers.")
            self._refresh_persistent_profile_options()
            return

        total_count = len(self._profile_clear_provider_options())
        provider_names = ", ".join(provider.value for provider in providers)
        title = "Clear All Profiles" if len(providers) == total_count else "Clear Provider Profiles"
        scope_text = (
            "ALL saved browser profiles"
            if len(providers) == total_count
            else f"saved browser profiles for: {provider_names}"
        )

        reply = QMessageBox.question(
            self,
            title,
            f"This will delete {scope_text} used for Persistent Sessions.\n\n"
            "This removes cookies/local storage and will log you out of the selected providers and saved accounts.\n\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if reply != QMessageBox.Yes:
            return

        deleted_count = 0
        try:
            for profile_dir in profile_dirs:
                try:
                    profile_dir.resolve().relative_to(base_dir)
                except Exception:
                    raise RuntimeError(f"Refusing to delete profile path outside the config directory: {profile_dir}")
                shutil.rmtree(profile_dir)
                deleted_count += 1

            Logger.success(f"Cleared {deleted_count} persistent profile folder(s).")
            QMessageBox.information(self, "Clear Profiles", "Selected profiles cleared successfully.")
        except Exception as e:
            Logger.error(f"Error clearing persistent profiles: {e}")
            QMessageBox.warning(self, "Clear Profiles", f"Failed to clear profiles:\n\n{e}")
        finally:
            self._refresh_persistent_profile_options()
            self._sync_profile_clear_button_label()

    def save_settings(self):
        validation_errors = []
        loadouts_enabled = self._is_loadouts_enabled_in_ui()
        loadout_data_changed = self._loadout_editor_has_structural_changes()

        if loadouts_enabled:
            self._capture_current_loadout_from_widgets(self._current_editor_behavior_key())
            loadout_data_changed = self._loadout_editor_has_structural_changes()

        # Snapshot old values for fields with "affects" so we can detect changes later
        _affects_snapshot = {}
        for _cat in SCHEMA:
            for _field in self._iter_fields(_cat.fields):
                _affects_list = getattr(_field, "affects", None)
                if _affects_list:
                    _key = f"{_cat.key}.{_field.key}"
                    _affects_snapshot[_key] = (
                        self.config_manager.get_setting(_cat.key, _field.key),
                        _affects_list,
                    )

        active_config_dir = Path(getattr(self.config_manager, "config_dir", "config_data")).resolve()
        storage_preset_widget = self.field_widgets.get("system_settings.config_storage_location")
        storage_custom_widget = self.field_widgets.get("system_settings.config_storage_custom_path")

        prev_preset = self.config_manager.get_setting("system_settings", "config_storage_location")
        prev_custom_path = self.config_manager.get_setting("system_settings", "config_storage_custom_path")
        requested_preset = storage_preset_widget.currentText() if storage_preset_widget else (prev_preset or "Relative")
        requested_custom_path = storage_custom_widget.text() if storage_custom_widget else (prev_custom_path or "")

        target_config_dir = None
        try:
            target_config_dir = resolve_config_dir(requested_preset, requested_custom_path).resolve()
        except Exception as e:
            if isinstance(storage_custom_widget, (StyledLineEdit, DirectoryEntry)):
                storage_custom_widget.set_error(True)
            validation_errors.append(f"Config Storage Location: {e}")
        else:
            if isinstance(storage_custom_widget, (StyledLineEdit, DirectoryEntry)):
                storage_custom_widget.set_error(False)
        
        for category in SCHEMA:
            for field in self._iter_fields(category.fields):
                if getattr(field, "transient", False):
                    continue
                key = f"{category.key}.{field.key}"
                if loadouts_enabled and self._is_loadout_controlled_full_key(key):
                    self.config_manager.set_setting(
                        category.key,
                        field.key,
                        copy.deepcopy(
                            self._loadout_base_values_cache.get(
                                key,
                                self.config_manager.get_setting(category.key, field.key),
                            )
                        ),
                    )
                    continue
                widget = self.field_widgets.get(key)
                
                if widget:
                    value = None
                    if field.type == SettingType.BOOLEAN:
                        value = widget.isChecked()
                    elif field.type in [SettingType.STRING, SettingType.PASSWORD]:
                        value = widget.text()
                    elif field.type == SettingType.DIRECTORY:
                        value = widget.text().strip()
                        if getattr(field, "nullable", False) and not value:
                            value = None
                    elif field.type == SettingType.INTEGER:
                        text_val = widget.text()
                        value = int(text_val) if text_val else 0
                    elif field.type == SettingType.TEXTAREA:
                        value = widget.toPlainText()
                    elif key == "runtime.parallelization_mode" and isinstance(widget, StyledComboBox):
                        value = self._runtime_parallel_mode_key()
                    elif field.type == SettingType.DROPDOWN:
                        value = widget.currentText()
                    elif field.type == SettingType.SWITCHER and isinstance(widget, SegmentedSwitcher):
                        value = widget.current_value()
                    elif field.type == SettingType.INPUT_PAIR:
                        value = widget.get_pairs()
                    elif field.type == SettingType.INPUT_LIST:
                        value = widget.get_items()
                    elif field.type == SettingType.MULTI_SELECT_DROPDOWN:
                        value = widget.selected_keys()
                    elif field.type == SettingType.PROVIDER_LANE_SELECTOR and isinstance(widget, RuntimeProviderLaneDropdown):
                        value = self._runtime_lane_value_from_widget(widget)
                    elif field.type in [SettingType.BUTTON, SettingType.DIVIDER, SettingType.DESCRIPTION, SettingType.HINT, SettingType.ROW, SettingType.REDIRECT]:
                        continue # These don't have values to save
                        
                    # Check dependencies
                    is_enabled = self._is_dependency_met(field.depends) if field.depends else True

                    if (not is_enabled) and (key in self._dep_override_cache):
                        value = self._dep_override_cache[key]
                        
                    if is_enabled:
                        # Check required
                        if field.required and not value:
                            if isinstance(widget, (StyledLineEdit, DirectoryEntry)):
                                widget.set_error(True)
                            validation_errors.append(f"{field.label}: This field is required.")
                        
                        # Run validator if exists
                        elif field.validator:
                            try:
                                field.validator(value)
                                if field.type == SettingType.INPUT_LIST and field.key == "ip_whitelist":
                                    value = normalize_ip_list(value)
                                if isinstance(widget, (StyledLineEdit, DirectoryEntry)):
                                    widget.set_error(False)
                            except ValueError as e:
                                if isinstance(widget, (StyledLineEdit, DirectoryEntry)):
                                    widget.set_error(True)
                                validation_errors.append(f"{field.label}: {str(e)}")
                    else:
                        # If disabled, ensure no error state
                        if isinstance(widget, (StyledLineEdit, DirectoryEntry)):
                            widget.set_error(False)
                    
                    if not validation_errors:
                        self.config_manager.set_setting(category.key, field.key, value)

        if validation_errors:
            error_msg = "\n".join(validation_errors)
            QMessageBox.warning(self, "Validation Error", f"Please fix the following errors:\n\n{error_msg}")
            return

        self._apply_runtime_parallelization_to_config()
        self.config_manager.set_loadouts(self._flatten_loadout_editor_draft())

        perform_migration = False
        if target_config_dir and target_config_dir != active_config_dir:
            reply = QMessageBox.question(
                self,
                "Move Config Storage",
                "You're about to change where configuration data is stored.\n\n"
                f"From:\n{active_config_dir}\n\n"
                f"To:\n{target_config_dir}\n\n"
                "This will save all settings, replace the destination directory contents, "
                "and restart the application.\n\n"
                "Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )

            if reply != QMessageBox.Yes:
                rollback_preset = prev_preset or infer_preset_from_config_dir(active_config_dir)[0]
                rollback_custom = prev_custom_path or infer_preset_from_config_dir(active_config_dir)[1]
                if storage_preset_widget:
                    storage_preset_widget.blockSignals(True)
                    storage_preset_widget.setCurrentText(rollback_preset)
                    storage_preset_widget.blockSignals(False)
                if storage_custom_widget:
                    storage_custom_widget.blockSignals(True)
                    storage_custom_widget.setText(rollback_custom)
                    storage_custom_widget.blockSignals(False)

                self._on_config_storage_location_changed(rollback_preset)
                self.config_manager.set_setting("system_settings", "config_storage_location", rollback_preset)
                self.config_manager.set_setting("system_settings", "config_storage_custom_path", rollback_custom)
                target_config_dir = active_config_dir
            else:
                perform_migration = True

        self.config_manager.save_settings()
        self.unsaved_changes = False

        # Determine which UI components are affected by the changes
        _affected = set()
        for _key, (_old_val, _affects_list) in _affects_snapshot.items():
            _cat, _fld = _key.split(".", 1)
            _new_val = self.config_manager.get_setting(_cat, _fld)
            if _new_val != _old_val:
                _affected.update(_affects_list)
        if loadout_data_changed:
            _affected.add("chevron_dropdown")

        self.settings_saved.emit(_affected)

        if not perform_migration:
            self.close()
            return

        try:
            migrate_config_dir(active_config_dir, target_config_dir)
            write_pointer_file(target_config_dir)
            QMessageBox.information(
                self,
                "Config Storage",
                "Configuration migrated successfully.\n\nRestarting now...",
            )
            self.restart_requested.emit()
            self.close()
        except Exception as e:
            Logger.error(f"Config migration failed: {e}")

            rollback_preset = prev_preset or infer_preset_from_config_dir(active_config_dir)[0]
            rollback_custom = prev_custom_path or infer_preset_from_config_dir(active_config_dir)[1]
            self.config_manager.set_setting("system_settings", "config_storage_location", rollback_preset)
            self.config_manager.set_setting("system_settings", "config_storage_custom_path", rollback_custom)
            self.config_manager.save_settings()

            self._sync_config_storage_from_active_dir()
            QMessageBox.warning(
                self,
                "Config Migration Failed",
                "Failed to migrate configuration to the new location.\n\n"
                f"Error:\n{e}",
            )
            return

    def closeEvent(self, event):
        dialog = getattr(self, "_credential_manager_dialog", None)
        if dialog and dialog.isVisible():
            QMessageBox.information(
                self,
                "Credential Manager",
                "Close the Credential Manager window before closing Settings.",
            )
            event.ignore()
            return

        if self.unsaved_changes:
            reply = QMessageBox.question(
                self, "Unsaved Changes",
                "You have unsaved changes. Are you sure you want to discard them?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                self.unsaved_changes = False
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()

    def _open_credential_manager(self) -> None:
        existing = getattr(self, "_credential_manager_dialog", None)
        if existing and existing.isVisible():
            existing.activateWindow()
            existing.raise_()
            return

        dialog = CredentialManagerDialog(self.config_manager, parent=self)
        self._credential_manager_dialog = dialog

        def _clear_ref(*_args) -> None:
            if getattr(self, "_credential_manager_dialog", None) is dialog:
                self._credential_manager_dialog = None

        dialog.finished.connect(_clear_ref)
        dialog.open()
