from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QFont, QFontMetrics, QIntValidator, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from config.manager import ConfigManager
from config.loadouts import get_behavior_category_for_provider
from config.validators import validate_email, validate_port
from drivers.providers import DriverProvider, is_provider_locked, provider_options
from ece.manager import EceManager
from ece.models import CredentialPair
from ui.core.brand import BrandColors
from ui.core.icons import IconType, IconUtils
from ui.niche.hotswap_dialog import PROVIDER_ICON_MAP
from ui.widgets.components import (
    MultiColumnRow,
    SettingRow,
    StyledButton,
    StyledComboBox,
    StyledLineEdit,
    StyledTextEdit,
    Tumbler,
    ToggleRow,
)
from ui.widgets.smooth_scroll_area import SmoothScrollArea
from ui.widgets.step_progress import StepProgressBar
from utils.api_key_generator import generate_api_key
from utils.resource_path import resolve_resource_path


@dataclass
class QuickSetupState:
    provider: str = "DeepSeek"

    auto_login: bool = False
    email: str = ""
    password: str = ""
    select_least_used: bool = False
    reload_on_failure: bool = False

    port: int = 7777
    available_on_lan: bool = False
    show_ip: bool = True
    use_api_keys: bool = False
    api_key_name: str = ""
    api_key_value: str = ""

    persistent_sessions: bool = True

    enable_reasoning: bool = False
    send_reasoning: bool = False
    enable_search: bool = False
    send_as_text_file: bool = False
    use_perplexity_spaces: bool = False
    paste_system_instructions_into_space: bool = False
    enable_url_context: bool = False
    use_system_prompt_field: bool = False
    humanize_mouse_movements: bool = True
    huggingchat_inference_provider: str = "auto"
    huggingchat_thinking_effort: str = "auto"
    huggingchat_text_file_message: str = "Please read the attached file and respond to it."
    huggingchat_auto_disable_ratelimited_accounts: bool = False


class ProviderChoiceCard(QFrame):
    clicked = Signal(str)

    def __init__(self, provider_name: str, description: str, icon_file: str | None, parent=None) -> None:
        super().__init__(parent)
        self._provider_name = str(provider_name or "").strip()
        self._checked = False

        self.setObjectName("providerChoiceCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(16)

        icon_label = QLabel()
        icon_label.setStyleSheet("background-color: transparent;")
        icon_label.setFixedSize(26, 26)
        if icon_file:
            icon = IconUtils.get_icon(icon_file, color=BrandColors.TEXT_PRIMARY, size=22, widget=icon_label)
            if not icon.isNull():
                icon_label.setPixmap(icon.pixmap(22, 22))
        icon_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(icon_label, 0, Qt.AlignVCenter)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)

        title = QLabel(self._provider_name)
        title.setStyleSheet(
            f"""
            font-size: {BrandColors.FONT_SIZE_XLARGE};
            font-weight: 700;
            color: {BrandColors.TEXT_PRIMARY};
            background-color: transparent;
            """
        )
        title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        text_layout.addWidget(title, 0)

        desc = QLabel(str(description or "").strip())
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"""
            font-size: {BrandColors.FONT_SIZE_SMALL};
            font-weight: 400;
            color: {BrandColors.TEXT_SECONDARY};
            background-color: transparent;
            """
        )
        desc.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        text_layout.addWidget(desc, 0)

        layout.addLayout(text_layout, 1)

        self._apply_style()

    def _apply_style(self) -> None:
        checked = "true" if self._checked else "false"
        self.setProperty("checked", checked)
        self.setStyleSheet(
            f"""
            QFrame#providerChoiceCard {{
                background-color: {BrandColors.SIDEBAR_BG};
                border: 1px solid {BrandColors.INPUT_BORDER};
                border-radius: 8px;
            }}
            QFrame#providerChoiceCard:hover {{
                background-color: {BrandColors.ITEM_HOVER};
                border: 1px solid {BrandColors.ACCENT};
            }}
            QFrame#providerChoiceCard[checked="true"] {{
                background-color: {BrandColors.CATEGORY_ACTIVE_BG};
                border: 1px solid {BrandColors.CATEGORY_ACTIVE_BORDER};
            }}
            """
        )
        self.update()

    def setChecked(self, checked: bool) -> None:
        desired = bool(checked)
        if desired == self._checked:
            return
        self._checked = desired
        self._apply_style()

    def isChecked(self) -> bool:
        return bool(self._checked)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._provider_name)
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        key = event.key()
        if key in {Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space}:
            self.clicked.emit(self._provider_name)
            event.accept()
            return
        super().keyPressEvent(event)


class AutoSizingStackedWidget(QStackedWidget):
    """
    QStackedWidget that sizes itself to the current page instead of the largest page.

    This prevents a tall page (like SillyTavern instructions) from forcing scrollbars
    on all earlier steps.
    """

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt naming)
        current = self.currentWidget()
        if current is None:
            return super().sizeHint()
        return current.sizeHint()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt naming)
        current = self.currentWidget()
        if current is None:
            return super().minimumSizeHint()
        return current.minimumSizeHint()


class StaticCodeBlock(QPlainTextEdit):
    """
    Read-only code block that does not scroll on mouse wheel.

    This avoids the odd “scroll inside the code block” feel when hovering.
    """

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        event.ignore()


class WelcomeWindow(QDialog):
    settings_applied = Signal()

    STEP_TITLES = [
        "Provider",
        "Account",
        "Server",
        "Features",
        "SillyTavern",
    ]

    def __init__(self, config_manager: ConfigManager, parent=None) -> None:
        # Intentionally modeless and top-level (so it behaves like its own window)
        super().__init__(parent=None)
        self._config = config_manager
        self._state = self._build_initial_state(config_manager)

        self.setWindowTitle("Welcome")
        self.setObjectName("welcomeWindow")
        # Keep the intro page compact as Quick Setup can expand this later
        self.resize(640, 450)
        self.setMinimumSize(560, 400)
        self.setStyleSheet(
            f"""
            QDialog#welcomeWindow {{
                background-color: {BrandColors.WINDOW_BG};
                color: {BrandColors.TEXT_PRIMARY};
            }}
            QLabel {{
                background-color: transparent;
            }}
            QStackedWidget {{
                background-color: transparent;
            }}
            """
        )

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self._root_stack = QStackedWidget()
        root_layout.addWidget(self._root_stack)

        self._welcome_page = self._build_welcome_page()
        self._wizard_page = self._build_wizard_page()

        self._root_stack.addWidget(self._welcome_page)
        self._root_stack.addWidget(self._wizard_page)
        self._root_stack.setCurrentWidget(self._welcome_page)

        self._sync_provider_dependent_ui()
        self._sync_api_key_ui()
        self._sync_sillytavern_instructions()

    @staticmethod
    def _build_initial_state(config: ConfigManager) -> QuickSetupState:
        state = QuickSetupState()

        raw_provider = config.get_setting("providers_credentials", "provider") or "DeepSeek"
        provider_enum = DriverProvider.from_setting(str(raw_provider))
        if provider_enum is None or is_provider_locked(provider_enum, config):
            provider_enum = DriverProvider.DEEPSEEK
        state.provider = provider_enum.value

        state.auto_login = bool(config.get_setting("providers_credentials", "auto_login"))
        is_first_run = bool(getattr(config, "is_first_run", False))

        if is_first_run and provider_enum in {
            DriverProvider.DEEPSEEK,
            DriverProvider.GLM_CHAT,
        }:
            # Quick Setup is meant to guide first-time users. Auto Login is usually what they want here
            state.auto_login = True
        state.select_least_used = bool(config.get_setting("providers_credentials", "select_least_used"))
        state.reload_on_failure = bool(config.get_setting("providers_credentials", "reload_on_failure"))

        try:
            state.port = int(config.get_setting("network_settings", "port") or 7777)
        except Exception:
            state.port = 7777
        state.available_on_lan = bool(config.get_setting("network_settings", "available_on_lan"))
        state.show_ip = bool(config.get_setting("network_settings", "show_ip"))
        state.use_api_keys = bool(config.get_setting("network_settings", "use_api_keys"))

        state.persistent_sessions = bool(config.get_setting("system_settings", "persistent_sessions"))

        provider = DriverProvider.from_setting(state.provider)
        WelcomeWindow._populate_provider_feature_state(config, state, provider)

        # API keys (best-effort read the first one for display)
        pairs = config.get_setting("network_settings", "api_keys") or []
        if isinstance(pairs, list) and pairs:
            first = pairs[0]
            if isinstance(first, dict):
                state.api_key_name = str(first.get("name", "") or "")
                state.api_key_value = str(first.get("key", "") or "")
            elif isinstance(first, (list, tuple)) and len(first) >= 2:
                state.api_key_name = str(first[0] or "")
                state.api_key_value = str(first[1] or "")

        return state

    @staticmethod
    def _populate_provider_feature_state(
        config: ConfigManager,
        state: QuickSetupState,
        provider: DriverProvider,
    ) -> None:
        behavior_key = WelcomeWindow._behavior_category_for_provider(provider)
        is_first_run = bool(getattr(config, "is_first_run", False))

        state.enable_reasoning = bool(config.get_setting(behavior_key, "enable_deepthink"))
        state.send_reasoning = bool(config.get_setting(behavior_key, "send_deepthink"))
        state.enable_search = bool(config.get_setting(behavior_key, "enable_search"))
        state.send_as_text_file = bool(config.get_setting(behavior_key, "send_as_text_file"))
        state.use_perplexity_spaces = False
        state.paste_system_instructions_into_space = False
        state.enable_url_context = False
        state.use_system_prompt_field = False
        state.humanize_mouse_movements = True
        state.huggingchat_inference_provider = "auto"
        state.huggingchat_thinking_effort = "auto"
        state.huggingchat_text_file_message = "Please read the attached file and respond to it."
        state.huggingchat_auto_disable_ratelimited_accounts = False

        if provider == DriverProvider.PERPLEXITY:
            state.use_perplexity_spaces = bool(config.get_setting("perplexity_behavior", "use_spaces"))
            state.paste_system_instructions_into_space = bool(
                config.get_setting("perplexity_behavior", "paste_system_instructions_into_space")
            )
            if is_first_run:
                state.use_perplexity_spaces = True
                state.paste_system_instructions_into_space = True
        elif provider == DriverProvider.AI_STUDIO:
            state.enable_url_context = bool(config.get_setting("aistudio_behavior", "enable_url_context"))
            state.use_system_prompt_field = bool(
                config.get_setting("aistudio_behavior", "use_system_prompt_field")
            )
            raw_humanize = config.get_setting("aistudio_behavior", "humanize_mouse_movements")
            state.humanize_mouse_movements = True if raw_humanize is None else bool(raw_humanize)
        elif provider == DriverProvider.HUGGINGCHAT:
            state.use_system_prompt_field = bool(
                config.get_setting("huggingchat_behavior", "use_system_prompt_field")
            )
            state.huggingchat_inference_provider = str(
                config.get_setting("huggingchat_behavior", "inference_provider") or "auto"
            ).strip() or "auto"
            state.huggingchat_thinking_effort = str(
                config.get_setting("huggingchat_behavior", "thinking_effort") or "auto"
            ).strip() or "auto"
            state.huggingchat_text_file_message = str(
                config.get_setting("huggingchat_behavior", "text_file_message")
                or "Please read the attached file and respond to it."
            )
            state.huggingchat_auto_disable_ratelimited_accounts = bool(
                config.get_setting(
                    "huggingchat_behavior",
                    "auto_disable_ratelimited_accounts",
                )
            )

    def _build_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("welcomeCard")
        card.setStyleSheet(
            f"""
            QFrame#welcomeCard {{
                background-color: {BrandColors.SIDEBAR_BG};
                border: 1px solid {BrandColors.INPUT_BORDER};
                border-radius: 12px;
            }}
            """
        )
        return card

    def _build_welcome_page(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(22, 22, 22, 18)
        page_layout.setSpacing(14)

        card = self._build_card()
        page_layout.addWidget(card, 1)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(26, 26, 26, 22)
        layout.setSpacing(14)

        welcome = QLabel("WELCOME!")
        welcome.setAlignment(Qt.AlignCenter)
        welcome.setStyleSheet(
            f"""
            font-size: 30px;
            font-weight: 800;
            color: {BrandColors.TEXT_PRIMARY};
            background-color: transparent;
            """
        )
        layout.addWidget(welcome, 0)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(12)
        title_row.addStretch(1)

        logo_label = QLabel()
        logo_label.setStyleSheet("background-color: transparent;")
        logo_path = resolve_resource_path("ui", "assets", "brand", "newlogo-nobg.png")
        pixmap = QPixmap(str(logo_path))
        if not pixmap.isNull():
            scaled = pixmap.scaled(88, 88, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(2.0)
            logo_label.setPixmap(scaled)
        logo_label.setFixedSize(44, 44)
        title_row.addWidget(logo_label, 0, Qt.AlignVCenter)

        app_name = QLabel()
        app_name.setTextFormat(Qt.RichText)
        app_name.setText(
            f'<span style="font-size: 28px; font-weight: 700; color: {BrandColors.TEXT_PRIMARY};">IntenseRP </span>'
            f'<span style="font-size: 28px; font-weight: 700; color: {BrandColors.ACCENT};">Next</span>'
        )
        app_name.setStyleSheet("background-color: transparent;")
        title_row.addWidget(app_name, 0, Qt.AlignVCenter)

        title_row.addStretch(1)
        layout.addLayout(title_row, 0)

        desc = QLabel(
            "IntenseRP Next is a local OpenAI-compatible API + desktop app that drives provider web UIs "
            "(DeepSeek / GLM Chat / Moonshot / QwenLM / Perplexity / HuggingChat / Google AI Studio / Xiaomi MiMo) in a real browser, "
            "so clients like SillyTavern can talk to a local browser session."
        )
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignCenter)
        desc.setStyleSheet(
            f"""
            font-size: {BrandColors.FONT_SIZE_REGULAR};
            color: {BrandColors.TEXT_SECONDARY};
            background-color: transparent;
            padding: 2px 6px;
            """
        )
        layout.addWidget(desc, 0)

        prompt = QLabel("Do you want a quick setup, or would you like to do everything manually?")
        prompt.setWordWrap(True)
        prompt.setAlignment(Qt.AlignCenter)
        prompt.setStyleSheet(
            f"""
            font-size: {BrandColors.FONT_SIZE_LARGE};
            color: {BrandColors.TEXT_PRIMARY};
            font-weight: 600;
            background-color: transparent;
            padding-top: 6px;
            """
        )
        layout.addWidget(prompt, 0)

        layout.addStretch(1)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 8, 0, 0)
        button_row.setSpacing(10)

        quick = QPushButton("Quick Setup")
        quick.setCursor(Qt.PointingHandCursor)
        quick.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {BrandColors.ACCENT};
                color: {BrandColors.TEXT_PRIMARY};
                border: none;
                padding: 12px 20px;
                border-radius: 6px;
                font-weight: 700;
                font-size: {BrandColors.FONT_SIZE_REGULAR};
            }}
            QPushButton:hover {{
                background-color: #4a80e0;
            }}
            QPushButton:pressed {{
                background-color: #3c6ac3;
            }}
            """
        )
        IconUtils.apply_icon(quick, IconType.START, BrandColors.TEXT_PRIMARY, size=16, y_offset=2)
        quick.setIconSize(QSize(16, 16))
        quick.clicked.connect(self._enter_quick_setup)
        button_row.addWidget(quick, 1)

        manual = QPushButton("Manual")
        manual.setCursor(Qt.PointingHandCursor)
        manual.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {BrandColors.WINDOW_BG};
                color: {BrandColors.TEXT_PRIMARY};
                border: 1px solid {BrandColors.INPUT_BORDER};
                padding: 12px 20px;
                border-radius: 6px;
                font-weight: 700;
                font-size: {BrandColors.FONT_SIZE_REGULAR};
            }}
            QPushButton:hover {{
                background-color: {BrandColors.ITEM_HOVER};
                border: 1px solid {BrandColors.ACCENT};
            }}
            """
        )
        IconUtils.apply_icon(manual, IconType.CANCEL, BrandColors.TEXT_PRIMARY, size=16, y_offset=2)
        manual.setIconSize(QSize(16, 16))
        manual.clicked.connect(self._dismiss_welcome)
        button_row.addWidget(manual, 1)

        layout.addLayout(button_row, 0)

        return page

    def _build_wizard_page(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(22, 22, 22, 18)
        page_layout.setSpacing(12)

        card = self._build_card()
        page_layout.addWidget(card, 1)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 22, 22, 18)
        layout.setSpacing(10)

        self._stepper = StepProgressBar(len(self.STEP_TITLES))
        layout.addWidget(self._stepper, 0)

        self._step_title = QLabel("")
        self._step_title.setAlignment(Qt.AlignCenter)
        self._step_title.setStyleSheet(
            f"""
            font-size: {BrandColors.FONT_SIZE_XLARGE};
            font-weight: 800;
            color: {BrandColors.TEXT_PRIMARY};
            background-color: transparent;
            padding-bottom: 2px;
            """
        )
        layout.addWidget(self._step_title, 0)

        self._wizard_stack = AutoSizingStackedWidget()
        self._wizard_stack.setStyleSheet("background-color: transparent;")

        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            f"""
            QScrollArea {{
                background-color: transparent;
                border: none;
            }}
            QScrollBar:vertical {{
                border: none;
                background: transparent;
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

        scroll.setWidget(self._wizard_stack)

        self._wizard_scroll = scroll
        layout.addWidget(scroll, 1)

        self._wizard_stack.addWidget(self._build_step_provider())
        self._wizard_stack.addWidget(self._build_step_account())
        self._wizard_stack.addWidget(self._build_step_server())
        self._wizard_stack.addWidget(self._build_step_features())
        self._wizard_stack.addWidget(self._build_step_sillytavern())
        def _on_wizard_page_changed(_idx: int) -> None:
            try:
                self._wizard_stack.updateGeometry()
                self._wizard_stack.adjustSize()
            except Exception:
                pass

        self._wizard_stack.currentChanged.connect(_on_wizard_page_changed)

        nav = QHBoxLayout()
        nav.setContentsMargins(0, 6, 0, 0)
        nav.setSpacing(10)

        cancel = QPushButton("Cancel")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {BrandColors.WINDOW_BG};
                color: {BrandColors.TEXT_PRIMARY};
                border: 1px solid {BrandColors.INPUT_BORDER};
                padding: 10px 20px;
                border-radius: 6px;
                font-size: {BrandColors.FONT_SIZE_REGULAR};
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {BrandColors.ITEM_HOVER};
                border: 1px solid {BrandColors.ACCENT};
            }}
            """
        )
        IconUtils.apply_icon(cancel, IconType.CANCEL, BrandColors.TEXT_PRIMARY, size=16, y_offset=2)
        cancel.setIconSize(QSize(16, 16))
        cancel.clicked.connect(self.close)
        nav.addWidget(cancel, 0)

        nav.addStretch(1)

        self._back_button = QPushButton("Back")
        self._back_button.setCursor(Qt.PointingHandCursor)
        self._back_button.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {BrandColors.WINDOW_BG};
                color: {BrandColors.TEXT_PRIMARY};
                border: 1px solid {BrandColors.INPUT_BORDER};
                padding: 10px 20px;
                border-radius: 6px;
                font-size: {BrandColors.FONT_SIZE_REGULAR};
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {BrandColors.ITEM_HOVER};
                border: 1px solid {BrandColors.ACCENT};
            }}
            QPushButton:disabled {{
                background-color: {BrandColors.WINDOW_BG};
                color: {BrandColors.TEXT_DISABLED};
                border: 1px solid {BrandColors.INPUT_BORDER};
            }}
            """
        )
        self._back_button.clicked.connect(self._go_back)
        nav.addWidget(self._back_button, 0)

        self._next_button = QPushButton("Next")
        self._next_button.setCursor(Qt.PointingHandCursor)
        self._next_button.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {BrandColors.ACCENT};
                color: {BrandColors.TEXT_PRIMARY};
                border: none;
                padding: 10px 20px;
                border-radius: 6px;
                font-size: {BrandColors.FONT_SIZE_REGULAR};
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: #4a80e0;
            }}
            QPushButton:pressed {{
                background-color: #3c6ac3;
            }}
            """
        )
        self._next_button.clicked.connect(self._go_next)
        nav.addWidget(self._next_button, 0)

        layout.addLayout(nav, 0)

        self._set_step(0)
        return page

    def _build_step_container(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet("background-color: transparent;")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 4, 8, 6)
        layout.setSpacing(12)
        frame._content_layout = layout  # type: ignore[attr-defined]
        return frame

    def _build_step_provider(self) -> QWidget:
        frame = self._build_step_container()
        layout = frame._content_layout  # type: ignore[attr-defined]

        intro = QLabel("Pick the provider web app you want to drive.")
        intro.setWordWrap(True)
        intro.setAlignment(Qt.AlignCenter)
        intro.setStyleSheet(
            f"color: {BrandColors.TEXT_SECONDARY}; font-size: {BrandColors.FONT_SIZE_REGULAR};"
        )
        layout.addWidget(intro, 0)

        self._provider_cards = {}
        for provider in provider_options(include_locked=False, config_manager=self._config):
            desc = ""
            if provider == "DeepSeek":
                desc = "Default choice. Simple login flow."
            elif provider == "GLM Chat":
                desc = "May require CAPTCHA during login."
            elif provider == "Moonshot":
                desc = "Google popup login. Auto Login can fill it, but Persistent Sessions are still recommended."
            elif provider == "QwenLM":
                desc = "Email/password login, very smooth experience."
            elif provider == "Perplexity":
                desc = "Email-code login. Persistent Sessions are strongly recommended."
            elif provider == "HuggingChat":
                desc = "Hugging Face login. Small monthly credits, but good model variety."
            elif provider == "Google AI Studio":
                desc = "Google login with Gemini models. Humanized mouse movement is recommended."
            elif provider == "Xiaomi MiMo":
                desc = "Xiaomi account login. May need a VPN or Browser Proxy URL in geoblocked regions."

            icon_file = PROVIDER_ICON_MAP.get(provider)
            card = ProviderChoiceCard(provider, desc, icon_file, parent=frame)
            card.clicked.connect(self._set_provider)
            layout.addWidget(card, 0)
            self._provider_cards[provider] = card

        layout.addStretch(1)
        return frame

    def _build_step_account(self) -> QWidget:
        frame = self._build_step_container()
        layout = frame._content_layout  # type: ignore[attr-defined]

        self._account_info = QLabel("")
        self._account_info.setWordWrap(True)
        self._account_info.setAlignment(Qt.AlignCenter)
        self._account_info.setStyleSheet(
            f"color: {BrandColors.TEXT_SECONDARY}; font-size: {BrandColors.FONT_SIZE_REGULAR};"
        )
        layout.addWidget(self._account_info, 0)

        self._auto_login = Tumbler()
        self._auto_login.setChecked(bool(self._state.auto_login))
        self._auto_login.stateChanged.connect(lambda *_: self._on_auto_login_changed())
        self._auto_login_row = ToggleRow(
            "Enable Auto Login",
            self._auto_login,
            description="Fill credentials automatically for supported browser providers.",
        )
        layout.addWidget(self._auto_login_row, 0)

        self._email_input = StyledLineEdit()
        self._email_input.setPlaceholderText("Email")
        self._email_input.textChanged.connect(lambda *_: self._clear_account_errors())
        self._email_row = SettingRow("Email", self._email_input)
        layout.addWidget(self._email_row, 0)

        self._password_input = StyledLineEdit()
        self._password_input.setPlaceholderText("Password")
        from PySide6.QtWidgets import QLineEdit

        self._password_input.setEchoMode(QLineEdit.Password)
        self._password_input.textChanged.connect(lambda *_: self._clear_account_errors())
        self._password_row = SettingRow("Password", self._password_input)
        layout.addWidget(self._password_row, 0)

        self._select_least_used = Tumbler()
        self._select_least_used.setChecked(bool(self._state.select_least_used))
        self._select_least_used_row = ToggleRow(
            "Select Least Used",
            self._select_least_used,
            description="Prefer the least recently used saved account.",
        )
        layout.addWidget(self._select_least_used_row, 0)

        self._reload_on_failure = Tumbler()
        self._reload_on_failure.setChecked(bool(self._state.reload_on_failure))
        self._reload_on_failure_row = ToggleRow(
            "Reload on Failure",
            self._reload_on_failure,
            description="Restart and rotate account/profile on empty or rate-limited responses.",
        )
        layout.addWidget(self._reload_on_failure_row, 0)

        self._account_error = QLabel("")
        self._account_error.setWordWrap(True)
        self._account_error.setAlignment(Qt.AlignCenter)
        self._account_error.setVisible(False)
        self._account_error.setStyleSheet(
            f"color: {BrandColors.DANGER}; font-size: {BrandColors.FONT_SIZE_SMALL}; font-weight: 800;"
        )
        layout.addWidget(self._account_error, 0)

        layout.addStretch(1)
        return frame

    def _build_step_server(self) -> QWidget:
        frame = self._build_step_container()
        layout = frame._content_layout  # type: ignore[attr-defined]

        intro = QLabel("Configure the local API server endpoint.")
        intro.setWordWrap(True)
        intro.setAlignment(Qt.AlignCenter)
        intro.setStyleSheet(
            f"color: {BrandColors.TEXT_SECONDARY}; font-size: {BrandColors.FONT_SIZE_REGULAR};"
        )
        layout.addWidget(intro, 0)

        self._port_input = StyledLineEdit()
        self._port_input.setValidator(QIntValidator())
        self._port_input.setText(str(int(self._state.port)))
        self._port_input.textChanged.connect(lambda *_: self._clear_server_errors())
        self._port_input.textChanged.connect(lambda *_: self._sync_sillytavern_instructions())
        layout.addWidget(SettingRow("Port", self._port_input, description="Default is 7777."), 0)

        self._available_on_lan = Tumbler()
        self._available_on_lan.setChecked(bool(self._state.available_on_lan))
        self._available_on_lan.stateChanged.connect(lambda *_: self._on_server_setting_changed())
        layout.addWidget(
            ToggleRow(
                "Available on LAN",
                self._available_on_lan,
                description="Allow other devices on your network to connect.",
            ),
            0,
        )

        self._show_ip = Tumbler()
        self._show_ip.setChecked(bool(self._state.show_ip))
        self._show_ip.stateChanged.connect(lambda *_: self._on_server_setting_changed())
        layout.addWidget(
            ToggleRow(
                "Show IP",
                self._show_ip,
                description="Print server address(es) when starting.",
            ),
            0,
        )

        self._use_api_keys = Tumbler()
        self._use_api_keys.setChecked(bool(self._state.use_api_keys))
        self._use_api_keys.stateChanged.connect(lambda *_: self._on_use_api_keys_changed())
        layout.addWidget(
            ToggleRow(
                "Require API Key",
                self._use_api_keys,
                description="Recommended when using LAN access.",
            ),
            0,
        )

        self._api_key_container = QWidget()
        api_key_layout = QVBoxLayout(self._api_key_container)
        api_key_layout.setContentsMargins(0, 0, 0, 0)
        api_key_layout.setSpacing(10)

        self._api_key_name = StyledLineEdit()
        self._api_key_name.setPlaceholderText("Key name (optional)")
        self._api_key_name.textChanged.connect(lambda *_: self._clear_server_errors())
        api_key_layout.addWidget(SettingRow("API Key Name", self._api_key_name), 0)

        self._api_key_value = StyledLineEdit()
        self._api_key_value.setReadOnly(True)
        self._api_key_value.setPlaceholderText("Click Generate to create a key")
        self._api_key_value.textChanged.connect(lambda *_: self._sync_sillytavern_instructions())

        regenerate = StyledButton("Generate")
        icon = IconUtils.get_icon(
            "dices.svg",
            color=BrandColors.TEXT_PRIMARY,
            size=14,
            widget=regenerate,
            include_disabled=True,
        )
        if not icon.isNull():
            regenerate.setIcon(icon)
            regenerate.setIconSize(QSize(14, 14))
        regenerate.clicked.connect(self._regenerate_api_key)

        api_key_row = MultiColumnRow([self._api_key_value, regenerate], ratios=[80, 20], spacing=10)
        api_key_layout.addWidget(SettingRow("API Key", api_key_row), 0)

        self._server_error = QLabel("")
        self._server_error.setWordWrap(True)
        self._server_error.setAlignment(Qt.AlignCenter)
        self._server_error.setVisible(False)
        self._server_error.setStyleSheet(
            f"color: {BrandColors.DANGER}; font-size: {BrandColors.FONT_SIZE_SMALL}; font-weight: 800;"
        )
        api_key_layout.addWidget(self._server_error, 0)

        layout.addWidget(self._api_key_container, 0)
        layout.addStretch(1)

        return frame

    def _build_step_features(self) -> QWidget:
        frame = self._build_step_container()
        layout = frame._content_layout  # type: ignore[attr-defined]

        intro = QLabel("Helpful defaults. You can change everything later in Settings.")
        intro.setWordWrap(True)
        intro.setAlignment(Qt.AlignCenter)
        intro.setStyleSheet(
            f"color: {BrandColors.TEXT_SECONDARY}; font-size: {BrandColors.FONT_SIZE_REGULAR};"
        )
        layout.addWidget(intro, 0)

        self._persistent_sessions = Tumbler()
        self._persistent_sessions.setChecked(bool(self._state.persistent_sessions))
        layout.addWidget(
            ToggleRow(
                "Keep Provider Sessions Signed In",
                self._persistent_sessions,
                description="Recommended. Reuse a browser profile so provider logins survive restarts.",
            ),
            0,
        )

        self._perplexity_spaces = Tumbler()
        self._perplexity_spaces.setChecked(bool(self._state.use_perplexity_spaces))
        self._perplexity_spaces.stateChanged.connect(lambda *_: self._on_perplexity_spaces_changed())
        self._perplexity_spaces_row = ToggleRow(
            "Use a Perplexity Space",
            self._perplexity_spaces,
            description=(
                "Recommended for Perplexity. Uses an IntenseRP-managed Space instead of a plain chat. (also acts as a bit of a jailbreak against Perplexity's weirdness around system prompts and instructions)"
            ),
        )
        layout.addWidget(self._perplexity_spaces_row, 0)

        self._perplexity_space_instructions = Tumbler()
        self._perplexity_space_instructions.setChecked(
            bool(self._state.paste_system_instructions_into_space)
        )
        self._perplexity_space_instructions_row = ToggleRow(
            "Put System Prompt in Space Instructions",
            self._perplexity_space_instructions,
            description=(
                "Moves leading system messages into the Space instructions box when they fit."
            ),
        )
        layout.addWidget(self._perplexity_space_instructions_row, 0)

        self._use_system_prompt_field = Tumbler()
        self._use_system_prompt_field.setChecked(bool(self._state.use_system_prompt_field))
        self._use_system_prompt_field_row = ToggleRow(
            "Use AI Studio System Instructions",
            self._use_system_prompt_field,
            description=(
                "Moves leading system messages into AI Studio's native System Instructions field."
            ),
        )
        layout.addWidget(self._use_system_prompt_field_row, 0)

        self._humanize_mouse_movements = Tumbler()
        self._humanize_mouse_movements.setChecked(bool(self._state.humanize_mouse_movements))
        self._humanize_mouse_movements_row = ToggleRow(
            "Humanize AI Studio Mouse Movements",
            self._humanize_mouse_movements,
            description=(
                "Recommended for Google AI Studio. Adds visible pointer movement and small pauses around UI actions."
            ),
        )
        layout.addWidget(self._humanize_mouse_movements_row, 0)

        self._huggingchat_inference_provider = StyledLineEdit()
        self._huggingchat_inference_provider.setPlaceholderText("auto")
        self._huggingchat_inference_provider.setText(self._state.huggingchat_inference_provider)
        self._huggingchat_inference_provider_row = SettingRow(
            "HuggingChat Inference Provider",
            self._huggingchat_inference_provider,
            description="Usually leave this as auto. Advanced users can enter values like together or featherless-ai.",
        )
        layout.addWidget(self._huggingchat_inference_provider_row, 0)

        self._huggingchat_auto_disable = Tumbler()
        self._huggingchat_auto_disable.setChecked(
            bool(self._state.huggingchat_auto_disable_ratelimited_accounts)
        )
        self._huggingchat_auto_disable_row = ToggleRow(
            "Auto-Disable Spent HChat Accounts",
            self._huggingchat_auto_disable,
            description="When HuggingChat shows Upgrade Required, disable that saved account so rotation can skip it.",
        )
        layout.addWidget(self._huggingchat_auto_disable_row, 0)

        self._enable_reasoning = Tumbler()
        self._enable_reasoning.setChecked(bool(self._state.enable_reasoning))
        self._enable_reasoning.stateChanged.connect(lambda *_: self._on_reasoning_toggles_changed())
        self._enable_reasoning_row = ToggleRow(
            "Enable Reasoning Mode",
            self._enable_reasoning,
            description="Turns on the provider's reasoning toggle (DeepThink / Thinking).",
        )
        layout.addWidget(self._enable_reasoning_row, 0)

        self._huggingchat_thinking_effort = StyledComboBox()
        self._huggingchat_thinking_effort.addItems(["auto", "default", "low", "medium", "high"])
        if self._state.huggingchat_thinking_effort not in {
            self._huggingchat_thinking_effort.itemText(i)
            for i in range(self._huggingchat_thinking_effort.count())
        }:
            self._huggingchat_thinking_effort.addItem(self._state.huggingchat_thinking_effort)
        self._huggingchat_thinking_effort.setCurrentText(self._state.huggingchat_thinking_effort)
        self._huggingchat_thinking_effort_row = SettingRow(
            "HuggingChat Thinking Effort",
            self._huggingchat_thinking_effort,
            description="Used when HuggingChat Thinking is enabled and the selected model exposes the control.",
        )
        layout.addWidget(self._huggingchat_thinking_effort_row, 0)

        self._send_reasoning = Tumbler()
        self._send_reasoning.setChecked(bool(self._state.send_reasoning))
        self._send_reasoning_row = ToggleRow(
            "Send Reasoning",
            self._send_reasoning,
            description="Include reasoning content in responses.",
        )
        layout.addWidget(self._send_reasoning_row, 0)

        self._enable_search = Tumbler()
        self._enable_search.setChecked(bool(self._state.enable_search))
        self._enable_search_row = ToggleRow(
            "Enable Search",
            self._enable_search,
            description="Let the provider use web search. Extra source metadata is filtered from API responses.",
        )
        layout.addWidget(self._enable_search_row, 0)

        self._enable_url_context = Tumbler()
        self._enable_url_context.setChecked(bool(self._state.enable_url_context))
        self._enable_url_context_row = ToggleRow(
            "Let AI Studio Read URLs",
            self._enable_url_context,
            description="Enables AI Studio's URL Context tool for prompts that include links.",
        )
        layout.addWidget(self._enable_url_context_row, 0)

        self._send_as_text_file = Tumbler()
        self._send_as_text_file.setChecked(bool(self._state.send_as_text_file))
        self._send_as_text_file.stateChanged.connect(lambda *_: self._on_send_as_text_file_changed())
        self._send_as_text_file_row = ToggleRow(
            "Upload Prompt as Text File",
            self._send_as_text_file,
            description="Useful for very long prompts. Normal pasting is simpler for everyday use.",
        )
        layout.addWidget(self._send_as_text_file_row, 0)

        self._huggingchat_text_file_message = StyledTextEdit()
        self._huggingchat_text_file_message.setPlainText(self._state.huggingchat_text_file_message)
        self._huggingchat_text_file_message_row = SettingRow(
            "HChat Text File Message",
            self._huggingchat_text_file_message,
            description="Companion text pasted after the prompt file uploads. HuggingChat needs some message text with files.",
        )
        layout.addWidget(self._huggingchat_text_file_message_row, 0)

        layout.addStretch(1)
        return frame

    def _make_code_block(self, text: str, *, min_lines: int = 1) -> QPlainTextEdit:
        editor = StaticCodeBlock()
        editor.setReadOnly(True)
        editor.setFocusPolicy(Qt.ClickFocus)
        editor.setPlainText(text.rstrip("\n"))
        editor.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        editor.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        editor.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        font = QFont("Consolas", 10)
        editor.setFont(font)

        editor.setStyleSheet(
            f"""
            QPlainTextEdit {{
                background-color: {BrandColors.INPUT_BG};
                color: {BrandColors.TEXT_PRIMARY};
                border: 2px solid {BrandColors.INPUT_BORDER};
                border-radius: 6px;
                padding: 8px 10px;
            }}
            QScrollBar:vertical {{
                border: none;
                background: transparent;
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

        metrics = QFontMetrics(font)
        line_height = max(14, int(metrics.lineSpacing()))
        lines = max(int(min_lines), int(text.count("\n") + 1))
        desired = 18 + (lines * line_height)
        editor.setFixedHeight(int(desired))
        return editor

    def _build_step_sillytavern(self) -> QWidget:
        frame = self._build_step_container()
        layout = frame._content_layout  # type: ignore[attr-defined]

        title = QLabel("Copy these into SillyTavern")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"font-size: {BrandColors.FONT_SIZE_LARGE}; font-weight: 700; color: {BrandColors.TEXT_PRIMARY};"
        )
        layout.addWidget(title, 0)

        self._st_overview = QLabel(
            "In SillyTavern: API Type = Chat Completion, Source = Custom (OpenAI-compatible)."
        )
        self._st_overview.setWordWrap(True)
        self._st_overview.setAlignment(Qt.AlignCenter)
        self._st_overview.setStyleSheet(
            f"color: {BrandColors.TEXT_SECONDARY}; font-size: {BrandColors.FONT_SIZE_REGULAR};"
        )
        layout.addWidget(self._st_overview, 0)

        self._st_endpoint_block = self._make_code_block("", min_lines=2)
        layout.addWidget(SettingRow("Custom Endpoint", self._st_endpoint_block), 0)

        self._st_model_block = self._make_code_block("", min_lines=4)
        layout.addWidget(SettingRow("Model", self._st_model_block), 0)

        self._st_key_block = self._make_code_block("", min_lines=2)
        layout.addWidget(SettingRow("API Key", self._st_key_block), 0)

        hint = QLabel(
            "Tip: For best name handling, set SillyTavern -> Character Names Behavior = Completion Object."
        )
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet(
            f"color: {BrandColors.TEXT_SECONDARY}; font-size: {BrandColors.FONT_SIZE_SMALL};"
        )
        layout.addWidget(hint, 0)

        layout.addStretch(1)
        return frame

    @staticmethod
    def _behavior_category_for_provider(provider: DriverProvider) -> str:
        return get_behavior_category_for_provider(provider) or "deepseek_behavior"

    def _resize_to_at_least(self, width: int, height: int) -> None:
        try:
            self.resize(max(int(self.width()), int(width)), max(int(self.height()), int(height)))
        except Exception:
            pass

    def _enter_quick_setup(self) -> None:
        # the wizard needs a bit more breathing room than the intro
        self.setMinimumSize(560, 520)
        self._resize_to_at_least(640, 640)
        self._root_stack.setCurrentWidget(self._wizard_page)
        self._set_provider(self._state.provider)
        self._set_step(0)

    def _mark_welcome_dismissed(self) -> None:
        marker = getattr(self._config, "mark_welcome_dismissed", None)
        if callable(marker):
            marker()

    def _dismiss_welcome(self) -> None:
        self._mark_welcome_dismissed()
        self.close()

    def _set_provider(self, provider: str) -> None:
        provider_value = str(provider or "").strip() or "DeepSeek"
        provider_enum = DriverProvider.from_setting(provider_value)
        if provider_enum is None or is_provider_locked(provider_enum, self._config):
            provider_enum = DriverProvider.DEEPSEEK
        provider_label = provider_enum.value
        self._state.provider = provider_label
        self._load_provider_feature_state(provider_enum)
        self._sync_feature_widgets_from_state()

        for name, card in (self._provider_cards or {}).items():
            try:
                card.setChecked(name == provider_label)
            except Exception:
                pass

        self._sync_provider_dependent_ui()
        self._sync_sillytavern_instructions()

    def _provider_supports_auto_login(self) -> bool:
        provider = DriverProvider.from_setting(self._state.provider)
        return provider in {
            DriverProvider.DEEPSEEK,
            DriverProvider.GLM_CHAT,
            DriverProvider.MOONSHOT,
            DriverProvider.QWEN_LM,
            DriverProvider.PERPLEXITY,
            DriverProvider.HUGGINGCHAT,
            DriverProvider.AI_STUDIO,
            DriverProvider.MIMO,
        }

    def _sync_provider_dependent_ui(self) -> None:
        provider = DriverProvider.from_setting(self._state.provider)

        if provider == DriverProvider.GLM_CHAT:
            self._account_info.setText(
                "GLM Chat supports Auto Login, but you may still need to solve a CAPTCHA in the browser."
            )
        elif provider == DriverProvider.MOONSHOT:
            self._account_info.setText(
                "Moonshot can attempt Auto Login through the Google popup now. "
                "If Google asks for extra confirmation or leaves the popup hanging, just finish it manually."
            )
        elif provider == DriverProvider.QWEN_LM:
            self._account_info.setText(
                "QwenLM supports Auto Login. If you keep it off, the browser will wait for manual login."
            )
        elif provider == DriverProvider.PERPLEXITY:
            self._account_info.setText(
                "Perplexity uses email-code login. Auto Login can enter your email, but you still need "
                "to type the 6-digit code in the browser. Persistent Sessions help a lot here."
            )
        elif provider == DriverProvider.HUGGINGCHAT:
            self._account_info.setText(
                "HuggingChat supports username/email + password Auto Login. Credits are small, so "
                "saving a few accounts and disabling spent ones in Credential Manager is useful."
            )
        elif provider == DriverProvider.AI_STUDIO:
            self._account_info.setText(
                "Google AI Studio can attempt Auto Login through the Google sign-in flow, but Persistent "
                "Sessions are strongly recommended because Google may still ask for manual confirmation."
            )
        elif provider == DriverProvider.MIMO:
            self._account_info.setText(
                "Xiaomi MiMo supports email/password Auto Login. If the page refuses the connection, "
                "your region is probably geoblocked; use a system VPN or Browser Proxy URL."
            )
        else:
            self._account_info.setText(
                "DeepSeek supports Auto Login. If you keep it off, the browser will wait for manual login."
            )

        supports_auto_login = self._provider_supports_auto_login()
        show_identity_fields = supports_auto_login

        self._auto_login_row.setVisible(supports_auto_login)
        self._select_least_used_row.setVisible(supports_auto_login)
        self._reload_on_failure_row.setVisible(supports_auto_login)

        self._email_row.setVisible(show_identity_fields)
        self._password_row.setVisible(show_identity_fields)

        self._email_row.label.setText("Email")
        self._password_row.label.setText("Password")
        self._email_input.setPlaceholderText("Email")
        self._password_input.setPlaceholderText("Password")
        if provider == DriverProvider.PERPLEXITY:
            self._password_row.label.setText("Password")
            self._password_input.setPlaceholderText("Unused for Perplexity email-code login")
            self._password_row.setVisible(False)
        elif provider == DriverProvider.HUGGINGCHAT:
            self._email_row.label.setText("Username or Email")
            self._email_input.setPlaceholderText("HF username or email")

        if not show_identity_fields:
            self._auto_login.blockSignals(True)
            self._auto_login.setChecked(False)
            self._auto_login.blockSignals(False)

        self._on_auto_login_changed()
        self._sync_provider_feature_visibility(provider)
        self._sync_feature_labels(provider)
        self._on_perplexity_spaces_changed()
        self._on_reasoning_toggles_changed()

    @staticmethod
    def _set_tumbler_checked(widget: QWidget, checked: bool) -> None:
        try:
            widget.blockSignals(True)
            widget.setChecked(bool(checked))
        finally:
            widget.blockSignals(False)

    def _load_provider_feature_state(self, provider: DriverProvider) -> None:
        self._populate_provider_feature_state(self._config, self._state, provider)

    def _sync_feature_widgets_from_state(self) -> None:
        if not hasattr(self, "_enable_reasoning"):
            return

        self._set_tumbler_checked(self._enable_reasoning, self._state.enable_reasoning)
        self._set_tumbler_checked(self._send_reasoning, self._state.send_reasoning)
        self._set_tumbler_checked(self._enable_search, self._state.enable_search)
        self._set_tumbler_checked(self._send_as_text_file, self._state.send_as_text_file)
        self._set_tumbler_checked(self._perplexity_spaces, self._state.use_perplexity_spaces)
        self._set_tumbler_checked(
            self._perplexity_space_instructions,
            self._state.paste_system_instructions_into_space,
        )
        self._set_tumbler_checked(self._enable_url_context, self._state.enable_url_context)
        self._set_tumbler_checked(
            self._use_system_prompt_field,
            self._state.use_system_prompt_field,
        )
        self._set_tumbler_checked(
            self._humanize_mouse_movements,
            self._state.humanize_mouse_movements,
        )
        self._huggingchat_inference_provider.setText(
            self._state.huggingchat_inference_provider
        )
        if self._huggingchat_thinking_effort.findText(
            self._state.huggingchat_thinking_effort
        ) < 0:
            self._huggingchat_thinking_effort.addItem(
                self._state.huggingchat_thinking_effort
            )
        self._huggingchat_thinking_effort.setCurrentText(
            self._state.huggingchat_thinking_effort
        )
        self._huggingchat_text_file_message.setPlainText(
            self._state.huggingchat_text_file_message
        )
        self._set_tumbler_checked(
            self._huggingchat_auto_disable,
            self._state.huggingchat_auto_disable_ratelimited_accounts,
        )

    def _sync_provider_feature_visibility(self, provider: DriverProvider) -> None:
        is_perplexity = provider == DriverProvider.PERPLEXITY
        is_ai_studio = provider == DriverProvider.AI_STUDIO
        is_huggingchat = provider == DriverProvider.HUGGINGCHAT
        is_mimo = provider == DriverProvider.MIMO

        self._perplexity_spaces_row.setVisible(is_perplexity)
        self._perplexity_space_instructions_row.setVisible(is_perplexity)
        self._enable_url_context_row.setVisible(is_ai_studio)
        self._use_system_prompt_field_row.setVisible(is_ai_studio or is_huggingchat)
        self._humanize_mouse_movements_row.setVisible(is_ai_studio)
        self._huggingchat_inference_provider_row.setVisible(is_huggingchat)
        self._huggingchat_auto_disable_row.setVisible(is_huggingchat)
        self._huggingchat_thinking_effort_row.setVisible(is_huggingchat)
        self._huggingchat_text_file_message_row.setVisible(is_huggingchat)
        self._enable_reasoning_row.setVisible(not is_mimo)
        self._enable_search_row.setVisible(not is_mimo)

        # Perplexity doesn't expose usable thinking traces in the stream yet, so
        # showing "Send Thinking" in first-run setup is mostly useless and just weird
        self._send_reasoning_row.setVisible(not is_perplexity)
        if is_perplexity:
            self._set_tumbler_checked(self._send_reasoning, False)
        if is_mimo:
            self._set_tumbler_checked(self._enable_reasoning, True)
            self._set_tumbler_checked(self._enable_search, False)
        self._sync_huggingchat_parameter_state()

    def _set_toggle_row_text(
        self,
        row: ToggleRow,
        label: str,
        description: str | None = None,
    ) -> None:
        row._label_text = str(label or "")  # type: ignore[attr-defined]
        row._description_text = str(description or "")  # type: ignore[attr-defined]
        row._update_label_style(row.isEnabled())  # type: ignore[attr-defined]
        if row.desc_label is not None:
            row.desc_label.setText(str(description or ""))
        for widget in (row, row.label, row.control):
            widget.setProperty("settingInfoTitle", str(label or ""))
            widget.setProperty("settingInfoBody", str(description or ""))

    def _sync_feature_labels(self, provider: DriverProvider) -> None:
        if provider == DriverProvider.AI_STUDIO:
            self._set_toggle_row_text(
                self._use_system_prompt_field_row,
                "Use AI Studio System Instructions",
                "Moves leading system messages into AI Studio's native System Instructions field.",
            )
            self._set_toggle_row_text(
                self._enable_reasoning_row,
                "Use AI Studio Thinking Level",
                "Uses the configured Thinking Level for supported AI Studio models.",
            )
            self._set_toggle_row_text(
                self._send_reasoning_row,
                "Send Thinking",
                "Include AI Studio thinking summaries in API responses.",
            )
            self._set_toggle_row_text(
                self._enable_search_row,
                "Enable Google Search",
                "Let AI Studio use Google Search grounding. Source metadata is filtered from API responses.",
            )
        elif provider == DriverProvider.PERPLEXITY:
            self._set_toggle_row_text(
                self._enable_reasoning_row,
                "Enable Perplexity Thinking",
                "Uses Perplexity's Thinking control when your account and selected model expose it.",
            )
            self._set_toggle_row_text(
                self._send_reasoning_row,
                "Send Thinking",
                "Perplexity does not currently expose usable thinking text to forward.",
            )
            self._set_toggle_row_text(
                self._enable_search_row,
                "Enable Web Search",
                "Let Perplexity search the web. Source metadata is filtered from API responses.",
            )
        elif provider == DriverProvider.HUGGINGCHAT:
            self._set_toggle_row_text(
                self._use_system_prompt_field_row,
                "Use HuggingChat System Prompt Field",
                "Moves leading system messages into HuggingChat's custom system prompt field.",
            )
            self._set_toggle_row_text(
                self._enable_reasoning_row,
                "Enable HuggingChat Thinking",
                "Uses HuggingChat's thinking effort selector when the selected model exposes it.",
            )
            self._set_toggle_row_text(
                self._send_reasoning_row,
                "Send Thinking",
                "Forward <think> text from HuggingChat responses to the API client.",
            )
            self._set_toggle_row_text(
                self._enable_search_row,
                "Enable Exa Search",
                "Let HuggingChat use the Exa MCP web search server. Tool payloads are filtered from API responses.",
            )
        elif provider == DriverProvider.MIMO:
            self._set_toggle_row_text(
                self._send_reasoning_row,
                "Send Thinking",
                "Forward <think> text from MiMo responses to the API client. MiMo keeps thinking on server-side either way.",
            )
        elif provider in {DriverProvider.MOONSHOT, DriverProvider.QWEN_LM}:
            self._set_toggle_row_text(
                self._enable_reasoning_row,
                "Enable Thinking",
                "Turns on the provider's Thinking mode before sending.",
            )
            self._set_toggle_row_text(
                self._send_reasoning_row,
                "Send Thinking",
                "Include thinking content in API responses.",
            )
            self._set_toggle_row_text(
                self._enable_search_row,
                "Enable Search",
                "Let the provider use web search. Extra source metadata is filtered from API responses.",
            )
        else:
            self._set_toggle_row_text(
                self._enable_reasoning_row,
                "Enable DeepThink",
                "Turns on the provider's DeepThink mode before sending.",
            )
            self._set_toggle_row_text(
                self._send_reasoning_row,
                "Send DeepThink",
                "Include DeepThink content in API responses.",
            )
            self._set_toggle_row_text(
                self._enable_search_row,
                "Enable Search",
                "Let the provider use web search. Extra source metadata is filtered from API responses.",
            )

    def _on_auto_login_changed(self) -> None:
        enabled = bool(self._auto_login.isChecked())

        self._email_input.setEnabled(enabled)
        self._password_input.setEnabled(enabled)

        # These only matter when account selection is active.
        self._select_least_used_row.setEnabled(enabled)
        self._reload_on_failure_row.setEnabled(enabled)

        if not enabled:
            self._clear_account_errors()

    def _on_reasoning_toggles_changed(self) -> None:
        provider = DriverProvider.from_setting(self._state.provider)
        if provider == DriverProvider.MIMO:
            self._send_reasoning_row.setEnabled(True)
            self._sync_huggingchat_parameter_state()
            return

        enabled = bool(self._enable_reasoning.isChecked())
        self._send_reasoning_row.setEnabled(enabled)
        if not enabled:
            self._send_reasoning.setChecked(False)
        self._sync_huggingchat_parameter_state()

    def _on_send_as_text_file_changed(self) -> None:
        self._sync_huggingchat_parameter_state()

    def _sync_huggingchat_parameter_state(self) -> None:
        if not hasattr(self, "_huggingchat_thinking_effort_row"):
            return
        is_huggingchat = DriverProvider.from_setting(self._state.provider) == DriverProvider.HUGGINGCHAT
        self._huggingchat_thinking_effort_row.setEnabled(
            bool(is_huggingchat and self._enable_reasoning.isChecked())
        )
        self._huggingchat_text_file_message_row.setEnabled(
            bool(is_huggingchat and self._send_as_text_file.isChecked())
        )

    def _on_perplexity_spaces_changed(self) -> None:
        if not hasattr(self, "_perplexity_spaces"):
            return
        enabled = bool(self._perplexity_spaces.isChecked())
        self._perplexity_space_instructions_row.setEnabled(enabled)
        if not enabled:
            self._perplexity_space_instructions.setChecked(False)

    def _on_server_setting_changed(self) -> None:
        self._sync_sillytavern_instructions()

    def _on_use_api_keys_changed(self) -> None:
        self._sync_api_key_ui()
        self._sync_sillytavern_instructions()

    def _sync_api_key_ui(self) -> None:
        enabled = bool(self._use_api_keys.isChecked())
        self._api_key_container.setVisible(enabled)
        if enabled and not self._api_key_value.text().strip():
            self._regenerate_api_key()

    def _regenerate_api_key(self) -> None:
        name, key_val = generate_api_key(prefix="intenserp")
        if not self._api_key_name.text().strip():
            self._api_key_name.setText(name)
        self._api_key_value.setText(key_val)

    def _clear_account_errors(self) -> None:
        self._email_input.set_error(False)
        self._password_input.set_error(False)
        self._account_error.setVisible(False)
        self._account_error.setText("")

    def _clear_server_errors(self) -> None:
        self._port_input.set_error(False)
        self._server_error.setVisible(False)
        self._server_error.setText("")

    def _current_step_index(self) -> int:
        return int(self._wizard_stack.currentIndex())

    def _set_step(self, step_index: int) -> None:
        idx = int(step_index)
        idx = max(0, min(idx, len(self.STEP_TITLES) - 1))

        self._wizard_stack.setCurrentIndex(idx)
        self._stepper.set_current_step(idx)
        self._step_title.setText(self.STEP_TITLES[idx])
        self._back_button.setEnabled(idx > 0)

        is_last = idx == (len(self.STEP_TITLES) - 1)
        self._next_button.setText("Finish" if is_last else "Next")
        IconUtils.apply_icon(
            self._next_button,
            IconType.CONFIRM if is_last else IconType.START,
            BrandColors.TEXT_PRIMARY,
            size=14,
        )
        self._next_button.setIconSize(QSize(14, 14))

        try:
            self._wizard_scroll.verticalScrollBar().setValue(0)
        except Exception:
            pass

    def _go_back(self) -> None:
        self._set_step(self._current_step_index() - 1)

    def _go_next(self) -> None:
        idx = self._current_step_index()
        if not self._validate_step(idx):
            return

        if idx >= len(self.STEP_TITLES) - 1:
            self._finish()
            return

        self._set_step(idx + 1)

    def _validate_step(self, idx: int) -> bool:
        if idx == 1:
            return self._validate_account_step()
        if idx == 2:
            return self._validate_server_step()
        if idx == 3:
            self._on_reasoning_toggles_changed()
        return True

    def _validate_account_step(self) -> bool:
        self._clear_account_errors()

        if not self._provider_supports_auto_login():
            return True

        if not self._auto_login.isChecked():
            return True

        email = self._email_input.text().strip()
        password = self._password_input.text()
        provider = DriverProvider.from_setting(self._state.provider)
        requires_password = provider is not DriverProvider.PERPLEXITY

        ok = True
        if provider == DriverProvider.HUGGINGCHAT:
            if not email:
                self._email_input.set_error(True)
                self._account_error.setText("Username or email is empty.")
                self._account_error.setVisible(True)
                ok = False
            elif any(ch.isspace() for ch in email):
                self._email_input.set_error(True)
                self._account_error.setText("Username or email cannot contain whitespace.")
                self._account_error.setVisible(True)
                ok = False
            elif "@" in email:
                try:
                    validate_email(email)
                except ValueError as exc:
                    self._email_input.set_error(True)
                    self._account_error.setText(str(exc))
                    self._account_error.setVisible(True)
                    ok = False
        else:
            try:
                validate_email(email)
            except ValueError as exc:
                self._email_input.set_error(True)
                self._account_error.setText(str(exc))
                self._account_error.setVisible(True)
                ok = False

        if requires_password and not password.strip():
            self._password_input.set_error(True)
            self._account_error.setText("Password is empty.")
            self._account_error.setVisible(True)
            ok = False

        return ok

    def _validate_server_step(self) -> bool:
        self._clear_server_errors()

        port_raw = self._port_input.text().strip()
        try:
            port = int(port_raw)
            validate_port(port)
        except Exception as exc:
            self._port_input.set_error(True)
            self._server_error.setText(str(exc) or "Invalid port.")
            self._server_error.setVisible(True)
            return False

        if self._use_api_keys.isChecked():
            if not self._api_key_value.text().strip():
                self._server_error.setText("API key is empty. Click Generate.")
                self._server_error.setVisible(True)
                return False

        return True

    def _sync_sillytavern_instructions(self) -> None:
        port = 7777
        try:
            port = int(self._port_input.text().strip() or 7777)
        except Exception:
            port = 7777

        endpoint = f"http://127.0.0.1:{port}/v1"

        provider = DriverProvider.from_setting(self._state.provider)
        if provider == DriverProvider.GLM_CHAT:
            model_text = "glm-auto\nglm-chat\nglm-reasoner"
        elif provider == DriverProvider.MOONSHOT:
            model_text = "moonshot-auto\nmoonshot-chat\nmoonshot-reasoner"
        elif provider == DriverProvider.QWEN_LM:
            model_text = "qwen-auto\nqwen-chat\nqwen-reasoner"
        elif provider == DriverProvider.PERPLEXITY:
            model_text = "perplexity-auto\nperplexity-chat\nperplexity-reasoner"
        elif provider == DriverProvider.HUGGINGCHAT:
            model_text = "huggingchat-auto\nhuggingchat-chat\nhuggingchat-reasoner"
        elif provider == DriverProvider.AI_STUDIO:
            model_text = "aistudio-auto\naistudio-chat\naistudio-reasoner"
        elif provider == DriverProvider.MIMO:
            model_text = "mimo-auto\nmimo-chat\nmimo-reasoner"
        else:
            model_text = (
                "deepseek-auto\n"
                "deepseek-chat\n"
                "deepseek-reasoner"
            )

        key_text = "(leave blank)"
        if self._use_api_keys.isChecked():
            token = self._api_key_value.text().strip()
            key_text = token if token else "(missing)"

        self._st_endpoint_block.setPlainText(endpoint + "\n")
        self._st_model_block.setPlainText(model_text + "\n")
        self._st_key_block.setPlainText(key_text + "\n")

    def _finish(self) -> None:
        # Collect current state from widgets
        self._state.auto_login = bool(self._auto_login.isChecked()) if self._provider_supports_auto_login() else False
        self._state.email = self._email_input.text().strip()
        self._state.password = self._password_input.text()
        self._state.select_least_used = bool(self._select_least_used.isChecked())
        self._state.reload_on_failure = bool(self._reload_on_failure.isChecked())

        try:
            self._state.port = int(self._port_input.text().strip() or 7777)
        except Exception:
            self._state.port = 7777
        self._state.available_on_lan = bool(self._available_on_lan.isChecked())
        self._state.show_ip = bool(self._show_ip.isChecked())
        self._state.use_api_keys = bool(self._use_api_keys.isChecked())
        self._state.api_key_name = self._api_key_name.text().strip()
        self._state.api_key_value = self._api_key_value.text().strip()

        self._state.persistent_sessions = bool(self._persistent_sessions.isChecked())
        self._state.enable_reasoning = bool(self._enable_reasoning.isChecked())
        self._state.send_reasoning = bool(self._send_reasoning.isChecked())
        self._state.enable_search = bool(self._enable_search.isChecked())
        self._state.send_as_text_file = bool(self._send_as_text_file.isChecked())
        self._state.use_perplexity_spaces = bool(self._perplexity_spaces.isChecked())
        self._state.paste_system_instructions_into_space = bool(
            self._perplexity_space_instructions.isChecked()
        )
        self._state.enable_url_context = bool(self._enable_url_context.isChecked())
        self._state.use_system_prompt_field = bool(self._use_system_prompt_field.isChecked())
        self._state.humanize_mouse_movements = bool(self._humanize_mouse_movements.isChecked())
        self._state.huggingchat_inference_provider = (
            self._huggingchat_inference_provider.text().strip() or "auto"
        )
        self._state.huggingchat_thinking_effort = (
            self._huggingchat_thinking_effort.currentText().strip() or "auto"
        )
        self._state.huggingchat_text_file_message = (
            self._huggingchat_text_file_message.toPlainText().strip()
            or "Please read the attached file and respond to it."
        )
        self._state.huggingchat_auto_disable_ratelimited_accounts = bool(
            self._huggingchat_auto_disable.isChecked()
        )

        ok, error = self._apply_state()
        if not ok:
            QMessageBox.warning(self, "Quick Setup", error or "Failed to apply settings.")
            return

        self._mark_welcome_dismissed()
        self.settings_applied.emit()
        self.accept()

    def _apply_state(self) -> tuple[bool, str]:
        cfg = self._config

        provider = str(self._state.provider or "DeepSeek")
        cfg.set_setting("providers_credentials", "provider", provider)
        cfg.set_setting("providers_credentials", "auto_login", bool(self._state.auto_login))
        cfg.set_setting("providers_credentials", "select_least_used", bool(self._state.select_least_used))
        cfg.set_setting("providers_credentials", "reload_on_failure", bool(self._state.reload_on_failure))

        cfg.set_setting("system_settings", "persistent_sessions", bool(self._state.persistent_sessions))

        cfg.set_setting("network_settings", "port", int(self._state.port))
        cfg.set_setting("network_settings", "available_on_lan", bool(self._state.available_on_lan))
        cfg.set_setting("network_settings", "show_ip", bool(self._state.show_ip))
        cfg.set_setting("network_settings", "use_api_keys", bool(self._state.use_api_keys))

        api_pairs = []
        if self._state.use_api_keys:
            name = self._state.api_key_name.strip() or "Quick Setup"
            key_val = self._state.api_key_value.strip()
            if not key_val:
                return False, "API key is enabled but empty."
            api_pairs = [[name, key_val]]
        cfg.set_setting("network_settings", "api_keys", api_pairs)

        provider_enum = DriverProvider.from_setting(provider)
        behavior_key = self._behavior_category_for_provider(provider_enum)
        if provider_enum != DriverProvider.MIMO:
            cfg.set_setting(behavior_key, "enable_deepthink", bool(self._state.enable_reasoning))
        cfg.set_setting(behavior_key, "send_deepthink", bool(self._state.send_reasoning))
        if provider_enum != DriverProvider.MIMO:
            cfg.set_setting(behavior_key, "enable_search", bool(self._state.enable_search))
        cfg.set_setting(behavior_key, "send_as_text_file", bool(self._state.send_as_text_file))

        if provider_enum == DriverProvider.PERPLEXITY:
            cfg.set_setting(
                "perplexity_behavior",
                "use_spaces",
                bool(self._state.use_perplexity_spaces),
            )
            cfg.set_setting(
                "perplexity_behavior",
                "paste_system_instructions_into_space",
                bool(
                    self._state.use_perplexity_spaces
                    and self._state.paste_system_instructions_into_space
                ),
            )
        elif provider_enum == DriverProvider.AI_STUDIO:
            cfg.set_setting(
                "aistudio_behavior",
                "enable_url_context",
                bool(self._state.enable_url_context),
            )
            cfg.set_setting(
                "aistudio_behavior",
                "use_system_prompt_field",
                bool(self._state.use_system_prompt_field),
            )
            cfg.set_setting(
                "aistudio_behavior",
                "humanize_mouse_movements",
                bool(self._state.humanize_mouse_movements),
            )
        elif provider_enum == DriverProvider.HUGGINGCHAT:
            cfg.set_setting(
                "huggingchat_behavior",
                "use_system_prompt_field",
                bool(self._state.use_system_prompt_field),
            )
            cfg.set_setting(
                "huggingchat_behavior",
                "inference_provider",
                self._state.huggingchat_inference_provider.strip() or "auto",
            )
            cfg.set_setting(
                "huggingchat_behavior",
                "thinking_effort",
                self._state.huggingchat_thinking_effort.strip() or "auto",
            )
            cfg.set_setting(
                "huggingchat_behavior",
                "text_file_message",
                self._state.huggingchat_text_file_message.strip()
                or "Please read the attached file and respond to it.",
            )
            cfg.set_setting(
                "huggingchat_behavior",
                "auto_disable_ratelimited_accounts",
                bool(self._state.huggingchat_auto_disable_ratelimited_accounts),
            )

        # Credential Manager (first-run helper)
        supports_auto_login = provider_enum in {
            DriverProvider.DEEPSEEK,
            DriverProvider.GLM_CHAT,
            DriverProvider.MOONSHOT,
            DriverProvider.QWEN_LM,
            DriverProvider.PERPLEXITY,
            DriverProvider.HUGGINGCHAT,
            DriverProvider.AI_STUDIO,
            DriverProvider.MIMO,
        }
        should_write_identity = bool(supports_auto_login and self._state.auto_login)
        if should_write_identity:
            email = self._state.email.strip()
            password = self._state.password
            requires_password = provider_enum is not DriverProvider.PERPLEXITY

            if provider_enum == DriverProvider.HUGGINGCHAT:
                if not email:
                    return False, "Username or email is empty."
                if any(ch.isspace() for ch in email):
                    return False, "Username or email cannot contain whitespace."
                if "@" in email:
                    try:
                        validate_email(email)
                    except ValueError as exc:
                        return False, str(exc)
            else:
                try:
                    validate_email(email)
                except ValueError as exc:
                    return False, str(exc)
            if requires_password and not password.strip():
                return False, "Password is empty."

            try:
                ece = EceManager(getattr(cfg, "config_dir", "config_data"))
                existing = ece.get_provider_pairs(provider_enum)
                email_norm = email.lower()
                kept = [p for p in existing if str(p.email or "").strip().lower() != email_norm]
                kept.append(CredentialPair(email=email, password=password if requires_password else ""))
                ok, errors = ece.set_provider_pairs(provider_enum, kept)
                if not ok:
                    return False, "\n".join(errors or ["Failed to save credentials."])
            except Exception as exc:
                return False, f"Failed to save credentials: {exc}"

        try:
            cfg.save_settings()
        except Exception as exc:
            return False, f"Failed to save settings: {exc}"

        return True, ""
