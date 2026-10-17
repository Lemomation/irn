from __future__ import annotations

from pathlib import Path

from utils.logger import Logger
from utils.resource_path import resolve_resource_path


NEWS_DOCS_URL = "https://lemomation.github.io/irn/news/"
VIEWED_NEWS_FILENAME = "viewednews.txt"


def _normalize_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    try:
        return candidate.resolve()
    except Exception:
        return candidate.absolute()


def get_latest_news_path() -> Path:
    return resolve_resource_path(".github", "state", "latestnews.txt")


def get_viewed_news_path(config_dir: str | Path) -> Path:
    return _normalize_path(config_dir) / VIEWED_NEWS_FILENAME


def _read_version_file(path: Path, *, label: str, missing_ok: bool = False) -> int | None:
    if not path.exists():
        if not missing_ok:
            Logger.warning(f"{label} file not found: {path}")
        return None

    try:
        raw_value = path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        Logger.warning(f"Failed to read {label} file '{path}': {exc}")
        return None

    if not raw_value:
        Logger.warning(f"{label} file is empty: {path}")
        return None

    try:
        return int(raw_value)
    except ValueError:
        Logger.warning(f"{label} file contains an invalid version '{raw_value}': {path}")
        return None


def get_latest_news_version() -> int | None:
    return _read_version_file(
        get_latest_news_path(),
        label="Latest news version",
    )


def get_viewed_news_version(config_dir: str | Path) -> int | None:
    return _read_version_file(
        get_viewed_news_path(config_dir),
        label="Viewed news version",
        missing_ok=True,
    )


def has_unviewed_news(config_dir: str | Path) -> bool:
    latest_version = get_latest_news_version()
    if latest_version is None:
        return False

    viewed_version = get_viewed_news_version(config_dir)
    if viewed_version is None:
        return True

    return viewed_version != latest_version


def mark_latest_news_viewed(config_dir: str | Path) -> bool:
    latest_version = get_latest_news_version()
    if latest_version is None:
        return False

    viewed_path = get_viewed_news_path(config_dir)
    try:
        viewed_path.parent.mkdir(parents=True, exist_ok=True)
        viewed_path.write_text(str(latest_version), encoding="utf-8")
        return True
    except Exception as exc:
        Logger.warning(f"Failed to write viewed news version '{viewed_path}': {exc}")
        return False
