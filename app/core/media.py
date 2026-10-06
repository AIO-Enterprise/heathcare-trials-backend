"""
Media URL helpers — resolve /outputs/ paths across deployment origins.

On AWS (single origin via CloudFront/nginx), BACKEND_PUBLIC_URL is unset
and these functions return relative paths (current behaviour).

On Azure (separate Static Web App + Container App origins), set
BACKEND_PUBLIC_URL to the Container App domain so media URLs are absolute.
"""

from app.core.config import settings


def media_url(path: str) -> str:
    """Prefix an /outputs/... path with BACKEND_PUBLIC_URL if configured."""
    base = (settings.BACKEND_PUBLIC_URL or "").rstrip("/")
    return f"{base}{path}" if base else path


def media_to_disk(url: str) -> str:
    """Strip BACKEND_PUBLIC_URL prefix + leading /outputs/ to get a disk-relative path.

    Works with both absolute (https://.../outputs/...) and relative (/outputs/...) URLs.
    """
    base = (settings.BACKEND_PUBLIC_URL or "").rstrip("/")
    path = url
    if base and path.startswith(base):
        path = path[len(base) :]
    return path.lstrip("/").removeprefix("outputs/")
