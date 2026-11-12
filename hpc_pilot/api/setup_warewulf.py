"""Enable the local Warewulf API; run with sudo on the controller."""

from __future__ import annotations

import argparse
import configparser
import os
import pwd
import re
import secrets
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from hpc_pilot.api import config
from hpc_pilot.api.config import atomic_write
from hpc_pilot.api.warewulf import WarewulfAPIError, WarewulfClient, read_credentials


def configuration_texts(
    config_text: str, auth_text: str, username: str, password_hash: str
) -> tuple[str, str, int]:
    """Preserve existing settings/users and enable localhost-only API access."""
    import yaml

    config = yaml.safe_load(config_text) or {}
    auth = yaml.safe_load(auth_text) or {}
    if not isinstance(config, dict) or not isinstance(auth, dict):
        raise ValueError("Warewulf configuration and authentication files must be YAML mappings.")
    api = config.get("api") or {}
    users = auth.get("users") or []
    if not isinstance(api, dict) or not isinstance(users, list):
        raise ValueError("Expected an api mapping and a users list.")
    if any(not isinstance(user, dict) or not user.get("name") for user in users):
        raise ValueError("Every existing API user must be a mapping with a name.")
    api.update({"enabled": True, "allowed subnets": ["127.0.0.0/8", "::1/128"]})
    # v4.6.4 serves the management API on the existing provisioning HTTP listener.
    # Refuse a newer TLS-only API configuration rather than silently changing it.
    if api.get("tls"):
        raise ValueError("Existing TLS-only API configuration requires site-specific setup.")
    if "api" not in config:
        new_config = config_text.rstrip() + "\n" + yaml.safe_dump({"api": api}, sort_keys=False)
    else:
        config["api"] = api
        new_config = yaml.safe_dump(config, sort_keys=False)
    entry = next((user for user in users if user["name"] == username), None)
    if entry is None:
        entry = {"name": username}
        users.append(entry)
    entry["password hash"] = password_hash
    auth["users"] = users
    port = int((config.get("warewulf") or {}).get("port", 9873))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid warewulf:port.")
    return new_config, yaml.safe_dump(auth, sort_keys=False), port


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Override the shared API settings file")
    parser.add_argument("--api-user", help="Override warewulf.user from the settings file")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("Run with sudo: sudo /usr/bin/python3 -m hpc_pilot.api.setup_warewulf")
    try:
        settings, selected_path, client_user = config.setup_configuration(args.config)
        api_user = args.api_user or settings["warewulf"]["user"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", api_user):
            parser.error(
                "API username may contain only letters, digits, underscore, dot, and dash."
            )
        status = _setup(api_user, client_user, api_config_path=selected_path)
        print(f"Client settings: {selected_path}")
        return status
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        WarewulfAPIError,
        configparser.Error,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1


def _setup(
    api_user: str,
    client_user: str,
    *,
    config_path: Path = Path("/etc/warewulf/warewulf.conf"),
    client_dir: Path | None = None,
    api_config_path: Path | None = None,
) -> int:
    import crypt

    import yaml

    version = subprocess.run(["wwctl", "version"], check=True, capture_output=True, text=True)
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version.stdout)
    if not match or tuple(int(n) for n in match.groups()) < (4, 6, 1):
        raise ValueError("Warewulf 4.6.1+ with REST support is required.")
    owner = pwd.getpwnam(client_user)
    auth_path = config_path.with_name("auth.conf")
    client_dir = client_dir or config.api_directory(Path(owner.pw_dir))
    credentials_path = client_dir / "credentials" / "warewulf.env"
    if any(path.is_symlink() for path in (config_path, auth_path, credentials_path.parent, credentials_path)):
        raise ValueError("Configuration and credential paths must not be symlinks.")
    config_text = config_path.read_text()
    auth_text = auth_path.read_text() if auth_path.exists() else "users: []\n"
    if credentials_path.exists():
        existing = read_credentials(credentials_path)
        if existing.get("WAREWULF_API_USER") != api_user or not existing.get(
            "WAREWULF_API_PASSWORD"
        ):
            raise ValueError(
                f"Existing credentials do not match {api_user}; refusing to overwrite."
            )
        password = existing["WAREWULF_API_PASSWORD"]
    else:
        auth: Any = yaml.safe_load(auth_text) or {}
        if any(user.get("name") == api_user for user in auth.get("users", [])):
            raise ValueError(
                "API user already exists without saved credentials; use --api-user NAME."
            )
        password = secrets.token_urlsafe(32)
    password_hash = crypt.crypt(password, crypt.mksalt(crypt.METHOD_BLOWFISH, rounds=4096))
    if not password_hash or not password_hash.startswith("$2"):
        raise RuntimeError("System crypt does not support bcrypt password hashing.")
    new_config, new_auth, port = configuration_texts(
        config_text, auth_text, api_user, password_hash
    )
    url = f"http://127.0.0.1:{port}"
    env_text = "".join(
        f"export {key}={shlex.quote(value)}\n"
        for key, value in (
            ("WAREWULF_API_USER", api_user),
            ("WAREWULF_API_PASSWORD", password),
        )
    )
    config.ensure_directory(credentials_path.parent, owner.pw_uid, owner.pw_gid)
    atomic_write(credentials_path, env_text, 0o600, owner.pw_uid, owner.pw_gid)
    if api_config_path is not None:
        config.update_config(
            api_config_path,
            "warewulf",
            {
                "port": str(port),
                "user": api_user,
            },
            uid=owner.pw_uid,
            gid=owner.pw_gid,
        )
    print("Enabling the localhost management API and restarting warewulfd...")
    config_stat = config_path.stat()
    atomic_write(
        config_path,
        new_config,
        config_stat.st_mode & 0o777,
        config_stat.st_uid,
        config_stat.st_gid,
    )
    atomic_write(auth_path, new_auth, 0o600, 0, 0)
    subprocess.run(["systemctl", "restart", "warewulfd"], check=True)
    client = WarewulfClient(url, api_user, password)
    # The systemd service is Type=exec; allow time for HTTP initialization.
    import time

    for attempt in range(20):
        try:
            client.query("nodes")
            break
        except WarewulfAPIError:
            if attempt == 19:
                raise
            time.sleep(0.5)
    for resource in ("nodes", "profiles", "images", "overlays"):
        client.query(resource)
        print(f"Verified GET {resource}: OK")
    print(f"Credentials saved with mode 0600 for {client_user}: {credentials_path}")
    print("Then: python -m hpc_pilot.api warewulf nodes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
