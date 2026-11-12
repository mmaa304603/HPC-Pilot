"""Read-only Warewulf REST client using HTTP Basic authentication."""

from __future__ import annotations

import base64
import json
import math
import os
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class WarewulfAPIError(RuntimeError):
    """A Warewulf HTTP or response error."""


def read_credentials(path: Path) -> dict[str, str]:
    """Read the private Warewulf credentials without executing shell code."""
    if path.is_symlink():
        raise ValueError(f"Credentials must not be a symlink: {path}")
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError(f"Credentials must be a private file (chmod 600): {path}")
    if os.geteuid() != 0 and metadata.st_uid not in (os.geteuid(), 0):
        raise ValueError(f"Credentials file belongs to a different user: {path}")
    result = {}
    for line in path.read_text().splitlines():
        words = shlex.split(line)
        if len(words) == 2 and words[0] == "export" and "=" in words[1]:
            key, value = words[1].split("=", 1)
            result[key] = value
    return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


@dataclass
class WarewulfClient:
    """Query a configured Warewulf 4.6.1+ management API."""

    base_url: str
    username: str
    password: str = field(repr=False)
    timeout: float = 10.0

    def __post_init__(self) -> None:
        url = urlsplit(self.base_url)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            raise ValueError("URL must be an HTTP(S) service root without credentials or query.")
        if not self.username or not self.password or ":" in self.username:
            raise ValueError("A Warewulf API username (without ':') and password are required.")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Timeout must be a positive, finite number.")
        self.base_url = self.base_url.rstrip("/")

    def query(self, resource: str, identifier: str | None = None) -> Any:
        """List nodes, profiles, images, or overlays, or inspect one by name."""
        if resource not in ("nodes", "profiles", "images", "overlays"):
            raise ValueError("Unsupported Warewulf query.")
        if identifier is None:
            path = f"/api/{resource}" + ("" if resource == "images" else "/")
        else:
            if not identifier or identifier in (".", ".."):
                raise ValueError("Invalid resource identifier.")
            path = f"/api/{resource}/{quote(identifier, safe='')}"
        credentials = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        request = Request(
            self.base_url + path,
            headers={"Accept": "application/json", "Authorization": f"Basic {credentials}"},
            method="GET",
        )
        opener = build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            exc.close()
            hint = {
                401: "Check WAREWULF_API_USER and WAREWULF_API_PASSWORD.",
                403: "Check api:allowed subnets; connect from localhost or an SSH tunnel.",
                404: "Check that the API is enabled and the resource exists.",
            }.get(exc.code, "Check warewulfd logs.")
            raise WarewulfAPIError(f"HTTP {exc.code}: {hint}") from None
        except (URLError, OSError) as exc:
            raise WarewulfAPIError(f"Unable to reach warewulfd: {exc}") from None
        except (ValueError, UnicodeError):
            raise WarewulfAPIError("Warewulf returned invalid JSON.") from None
