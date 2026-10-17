from __future__ import annotations


DOCS_BASE_URL = "https://lemomation.github.io/irn/"


def build_docs_url(path: str, anchor: str | None = None) -> str:
    """
    Build an absolute docs URL from a docs-site path and optional anchor.
    """
    normalized_path = str(path or "").strip().lstrip("/")
    if normalized_path and not normalized_path.endswith("/"):
        normalized_path = f"{normalized_path}/"

    normalized_anchor = str(anchor or "").strip()
    if normalized_anchor.startswith("#"):
        normalized_anchor = normalized_anchor[1:]

    url = f"{DOCS_BASE_URL}{normalized_path}"
    if normalized_anchor:
        url = f"{url}#{normalized_anchor}"
    return url
