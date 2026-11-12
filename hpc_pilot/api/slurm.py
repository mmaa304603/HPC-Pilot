"""Small read-only slurmrestd client using Python's standard library."""

from __future__ import annotations

import json
import math
import os
import pwd
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from hpc_pilot.api.config import expand_path

_VERSION = re.compile(r"v\d+\.\d+\.\d+")


class SlurmAPIError(RuntimeError):
    """An HTTP, transport, or Slurm application error."""


def user_token(username: str, slurm_conf: str | None = None) -> str:
    """Issue a local user JWT; setup may run this as the invoking sudo user."""
    current = pwd.getpwuid(os.geteuid()).pw_name
    command = ["scontrol", "token"]
    if username != current:
        if os.geteuid() != 0:
            raise SlurmAPIError(
                "Automatic JWT issuance requires the current Linux user; "
                "provide SLURM_JWT for another account."
            )
        command = ["runuser", "-u", username, "--", *command]
    environment = dict(os.environ)
    environment.pop("SLURM_JWT", None)
    if slurm_conf is not None:
        environment["SLURM_CONF"] = str(expand_path(slurm_conf))
    try:
        result = subprocess.run(
            command, env=environment, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SlurmAPIError(
            "Unable to issue a local user JWT; check scontrol and the controller."
        ) from exc
    match = re.fullmatch(r"SLURM_JWT=(\S+)", result.stdout.strip())
    if result.returncode or not match or match[1] == "daemon":
        raise SlurmAPIError(
            "Slurm token issuance failed. Run setup_slurm on the controller "
            "and verify that local user token creation is allowed."
        )
    return match[1]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        # Do not forward authentication headers to a redirect destination.
        return None


@dataclass
class SlurmClient:
    """Connect to a configured Slurm REST service with JWT authentication.

    ``base_url`` is the service root, e.g. ``http://127.0.0.1:6820``.
    ``api_version`` is explicit because supported versions vary by server.
    """

    base_url: str
    token: str = field(repr=False)
    username: str
    api_version: str | None = None
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
        self.base_url = self.base_url.rstrip("/")
        if not self.token or not self.username:
            raise ValueError("A Slurm JWT and username are required.")
        if self.token == "daemon":
            raise ValueError(
                "SLURM_JWT=daemon is for the REST server only; "
                "obtain a user JWT with scontrol token."
            )
        if any(c in self.token + self.username for c in "\r\n"):
            raise ValueError("Credentials must not contain newlines.")
        if self.api_version is not None and not _VERSION.fullmatch(self.api_version):
            raise ValueError("API version must have the form v0.0.XX.")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Timeout must be a positive, finite number.")

    def _get(self, path: str) -> dict[str, Any]:
        request = Request(
            self.base_url + path,
            headers={
                "Accept": "application/json",
                "X-SLURM-USER-NAME": self.username,
                "X-SLURM-USER-TOKEN": self.token,
            },
            method="GET",
        )
        opener = build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                data = json.load(response)
        except HTTPError as exc:
            try:
                detail = self._http_error_detail(exc)
            finally:
                exc.close()
            hint = {
                401: "Check or refresh SLURM_JWT.",
                403: "Check the user's Slurm permissions.",
                404: "Check the service URL and supported API version.",
                500: (
                    "Refresh the user JWT, verify SLURM_USER, "
                    "and check slurmrestd/controller logs."
                ),
            }.get(exc.code, "Check slurmrestd or its proxy.")
            message = f"HTTP {exc.code}: {hint}"
            if detail:
                message += f" Slurm response: {detail}"
            raise SlurmAPIError(message) from None
        except (URLError, OSError) as exc:
            raise SlurmAPIError(f"Unable to reach slurmrestd: {exc}") from None
        except (ValueError, UnicodeError):
            raise SlurmAPIError("slurmrestd returned invalid JSON.") from None
        if not isinstance(data, dict):
            raise SlurmAPIError("Expected a JSON object from slurmrestd.")
        if data.get("errors"):
            detail = json.dumps(data["errors"]).replace(self.token, "[redacted]")
            raise SlurmAPIError(f"Slurm reported errors: {detail}")
        return cast(dict[str, Any], data)

    def _http_error_detail(self, error: HTTPError) -> str:
        """Extract a bounded server diagnostic without exposing the user's token."""
        try:
            body = error.read(8192).decode("utf-8", errors="replace").strip()
        except OSError:
            return ""
        try:
            data = json.loads(body)
        except ValueError:
            # Plain authentication failures are useful; proxy HTML pages are not.
            detail = body if error.headers.get_content_type() == "text/plain" else ""
        else:
            detail = ""
            if isinstance(data, dict):
                for key in ("errors", "error", "message", "detail"):
                    if data.get(key):
                        value = data[key]
                        detail = value if isinstance(value, str) else json.dumps(value)
                        break
        return " ".join(detail.replace(self.token, "[redacted]").split())[:2000]

    def versions(self) -> list[str]:
        """Read available scheduler API versions from the server's OpenAPI schema."""
        schema = self._get("/openapi.json")
        paths = schema.get("paths")
        if not isinstance(paths, dict):
            raise SlurmAPIError("OpenAPI response has no paths object.")
        versions = {
            match.group(1)
            for path in paths
            if (match := re.match(r"^/slurm/(v\d+\.\d+\.\d+)/", path))
        }
        return sorted(versions, key=lambda v: tuple(int(n) for n in v[1:].split(".")))

    def query(self, resource: str, identifier: str | None = None) -> dict[str, Any]:
        """Query one of the supported read-only operations."""
        if not self.api_version:
            raise ValueError("Set --api-version or SLURM_API_VERSION; use 'slurm versions' first.")
        resources = {"ping": "ping", "nodes": "node", "jobs": "job", "partitions": "partition"}
        if resource not in resources or (resource == "ping" and identifier is not None):
            raise ValueError("Unsupported Slurm query.")
        if identifier is not None:
            if not identifier or identifier in (".", ".."):
                raise ValueError("Resource identifier must not be empty, '.' or '..'.")
            endpoint = f"{resources[resource]}/{quote(identifier, safe='')}"
        else:
            endpoint = f"{resource}/"
        return self._get(f"/slurm/{self.api_version}/{endpoint}")
