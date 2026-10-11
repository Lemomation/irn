from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Optional, Tuple

import requests

from utils.version_file import VersionFileInfo, parse_version_file

DEFAULT_REMOTE_VERSION_URL = (
    "https://raw.githubusercontent.com/Lemomation/irn/refs/heads/v2-rewrite/version.json"
)

_SEMVER_RE = re.compile(
    r"^\s*v?"
    r"(?P<major>0|[1-9]\d*)"
    r"(?:\.(?P<minor>0|[1-9]\d*))?"
    r"(?:\.(?P<patch>0|[1-9]\d*))?"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"\s*$"
)
_REV_SUFFIX_RE = re.compile(r"^rev(?P<number>0|[1-9]\d*)$", re.IGNORECASE)
_PROJECT_RELEASE_ORDER = {
    "": 0,
    "update": 1,
    "major": 1,
    "patch": 2,
    "rev": 3,
}


@dataclass(frozen=True)
class UpdateCheckResult:
    local_version: str
    remote_version: Optional[str]
    update_available: bool
    error: Optional[str] = None
    remote_auto_updateable: Optional[bool] = None
    remote_severity: Optional[int] = None
    remote_summary: Optional[str] = None


def _parse_semver(version: str) -> Tuple[Tuple[int, int, int], Optional[Tuple[str, ...]]]:
    match = _SEMVER_RE.match(version or "")
    if not match:
        raise ValueError(f"Unsupported version format: {version!r}")

    major = int(match.group("major"))
    minor = int(match.group("minor") or 0)
    patch = int(match.group("patch") or 0)

    prerelease = match.group("prerelease")
    prerelease_parts = tuple(prerelease.split(".")) if prerelease else None
    return (major, minor, patch), prerelease_parts


def _parse_project_release_stage(
    prerelease: Optional[Tuple[str, ...]],
) -> Optional[Tuple[int, int]]:
    """
    Recognize IntenseRP's release suffixes so same-core hotfix tags sort in the
    order they are actually published instead of plain alphabetical order.
    """
    if prerelease is None:
        return _PROJECT_RELEASE_ORDER[""], 0

    if len(prerelease) != 1:
        return None

    token = prerelease[0].strip().lower()
    if token in {"update", "major", "patch"}:
        return _PROJECT_RELEASE_ORDER[token], 0

    rev_match = _REV_SUFFIX_RE.fullmatch(token)
    if rev_match is not None:
        return _PROJECT_RELEASE_ORDER["rev"], int(rev_match.group("number"))

    return None


def compare_versions(a: str, b: str) -> int:
    """
    Compare two SemVer-like version strings.

    Returns:
        -1 if a < b
         0 if a == b
         1 if a > b
    """
    a_core, a_pre = _parse_semver(a)
    b_core, b_pre = _parse_semver(b)

    if a_core != b_core:
        return -1 if a_core < b_core else 1

    a_stage = _parse_project_release_stage(a_pre)
    b_stage = _parse_project_release_stage(b_pre)

    if a_stage is not None or b_stage is not None:
        if a_stage is None:
            return -1
        if b_stage is None:
            return 1
        if a_stage != b_stage:
            return -1 if a_stage < b_stage else 1
        return 0

    if a_pre is None and b_pre is None:
        return 0
    if a_pre is None:
        return 1
    if b_pre is None:
        return -1

    for a_id, b_id in zip(a_pre, b_pre):
        a_is_num = a_id.isdigit()
        b_is_num = b_id.isdigit()

        if a_is_num and b_is_num:
            a_num = int(a_id)
            b_num = int(b_id)
            if a_num != b_num:
                return -1 if a_num < b_num else 1
            continue

        if a_is_num != b_is_num:
            # Numeric identifiers have lower precedence than non-numeric identifiers.
            return -1 if a_is_num else 1

        if a_id != b_id:
            return -1 if a_id < b_id else 1

    if len(a_pre) == len(b_pre):
        return 0
    return -1 if len(a_pre) < len(b_pre) else 1


def get_version_file_path(base_dir: Optional[Path] = None) -> Path:
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent
    return (base_dir / "version.json").resolve()


def read_local_version(version_file: Optional[Path] = None) -> str:
    return read_local_version_info(version_file).version


def read_local_version_info(version_file: Optional[Path] = None) -> VersionFileInfo:
    if version_file is None:
        version_file = get_version_file_path()
    try:
        raw = version_file.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        raw = ""
    return parse_version_file(raw, default_version="unknown", default_auto_updateable=True, default_severity=2)


def fetch_remote_version(url: str = DEFAULT_REMOTE_VERSION_URL, timeout_s: float = 5.0) -> str:
    return fetch_remote_version_info(url, timeout_s=timeout_s).version


def fetch_remote_version_info(url: str = DEFAULT_REMOTE_VERSION_URL, timeout_s: float = 5.0) -> VersionFileInfo:
    response = requests.get(
        url,
        timeout=timeout_s,
        headers={"User-Agent": "IntenseRP-Next-UpdateChecker"},
    )
    response.raise_for_status()
    return parse_version_file(
        response.text,
        default_version="unknown",
        default_auto_updateable=True,
        default_severity=2,
        strict=True,
    )


def check_for_updates(
    remote_url: str = DEFAULT_REMOTE_VERSION_URL,
    timeout_s: float = 5.0,
    version_file: Optional[Path] = None,
) -> UpdateCheckResult:
    local_info = read_local_version_info(version_file)
    try:
        remote_info = fetch_remote_version_info(remote_url, timeout_s=timeout_s)
    except Exception as exc:
        return UpdateCheckResult(
            local_version=local_info.version,
            remote_version=None,
            update_available=False,
            error=str(exc),
        )

    try:
        update_available = compare_versions(remote_info.version, local_info.version) > 0
    except Exception as exc:
        return UpdateCheckResult(
            local_version=local_info.version,
            remote_version=remote_info.version,
            update_available=False,
            error=str(exc),
            remote_auto_updateable=remote_info.auto_updateable,
            remote_severity=remote_info.severity,
            remote_summary=remote_info.summary,
        )

    return UpdateCheckResult(
        local_version=local_info.version,
        remote_version=remote_info.version,
        update_available=update_available,
        error=None,
        remote_auto_updateable=remote_info.auto_updateable,
        remote_severity=remote_info.severity,
        remote_summary=remote_info.summary,
    )
