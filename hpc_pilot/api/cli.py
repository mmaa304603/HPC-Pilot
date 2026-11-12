"""Terminal commands backed by shared INI settings and service credentials."""

from __future__ import annotations

import argparse
import configparser
import json
import os
import pwd
import sys
from typing import Any

from hpc_pilot.api import config
from hpc_pilot.api.slurm import SlurmAPIError, SlurmClient, user_token
from hpc_pilot.api.warewulf import WarewulfAPIError, WarewulfClient, read_credentials


def _connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", help="Local service port on 127.0.0.1")
    parser.add_argument("--user", help="Override the configured service user")
    parser.add_argument("--timeout", type=float, help="Override the request timeout")


def _value(args: argparse.Namespace, section: Any, name: str, env: str = "") -> str:
    value = getattr(args, name, None)
    if value is not None:
        return str(value)
    return os.environ.get(env) or section.get(name, "").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hpc-pilot-api", description="Query HPC service APIs directly (no LLM required)."
    )
    parser.add_argument(
        "--config",
        help="Settings file (default: <HPC_PILOT_HOME>/api/config.ini)",
    )
    services = parser.add_subparsers(dest="service", required=True)
    settings_command = services.add_parser("config", help="Manage local API settings")
    settings_operations = settings_command.add_subparsers(dest="operation", required=True)
    settings_operations.add_parser(
        "init", help="Create the default settings file without overwriting an existing file"
    )
    slurm = services.add_parser("slurm", help="Read-only Slurm REST queries")
    _connection_options(slurm)
    slurm.add_argument("--api-version", help="Override the configured API version")
    commands = slurm.add_subparsers(dest="operation", required=True)
    commands.add_parser("versions", help="List scheduler API versions")
    commands.add_parser("ping", help="Query controller availability")
    for resource in ("nodes", "jobs", "partitions"):
        commands.add_parser(resource, help=f"List or inspect {resource}").add_argument(
            "identifier", nargs="?"
        )
    warewulf = services.add_parser("warewulf", help="Read-only Warewulf REST queries")
    _connection_options(warewulf)
    commands = warewulf.add_subparsers(dest="operation", required=True)
    for resource in ("nodes", "profiles", "images", "overlays"):
        commands.add_parser(resource, help=f"List or inspect {resource}").add_argument(
            "identifier", nargs="?"
        )
    args = parser.parse_args(argv)
    secret = ""
    try:
        path = config.config_path(args.config)
        if args.service == "config":
            config.update_config(path)
            print(f"API settings: {path}")
            return 0
        settings = config.load_config(args.config)
        section = settings[args.service]
        prefix = "SLURM" if args.service == "slurm" else "WAREWULF"
        port = _value(args, section, "port", f"{prefix}_REST_PORT")
        if os.environ.get(f"{prefix}_REST_URL"):
            raise ValueError(f"Replace {prefix}_REST_URL with {prefix}_REST_PORT.")
        url = config.local_url(port)
        timeout = float(_value(args, section, "timeout"))
        identifier = getattr(args, "identifier", None)
        if args.service == "warewulf":
            credentials_file = config.default_credentials_path("warewulf")
            secret = os.environ.get("WAREWULF_API_PASSWORD", "")
            if not secret and credentials_file.exists():
                secret = read_credentials(credentials_file).get("WAREWULF_API_PASSWORD", "")
            user = _value(args, section, "user", "WAREWULF_API_USER")
            if not user or not secret:
                parser.error("Run setup_warewulf first, or provide Warewulf API credentials.")
            client = WarewulfClient(url, user, secret, timeout)
            result = client.query(args.operation, identifier)
        else:
            user = _value(args, section, "user", "SLURM_USER") or pwd.getpwuid(os.geteuid()).pw_name
            secret = os.environ.get("SLURM_JWT", "")
            if not secret:
                secret = user_token(user, str(config.SLURM_CONF))
            version = _value(args, section, "api_version", "SLURM_API_VERSION")
            slurm_client = SlurmClient(
                url, secret, user, None if version in ("", "auto") else version, timeout
            )
            if args.operation == "versions":
                result = {"versions": slurm_client.versions()}
            else:
                if not slurm_client.api_version:
                    versions = slurm_client.versions()
                    if not versions:
                        raise SlurmAPIError("The server exposes no scheduler API versions.")
                    slurm_client.api_version = versions[-1]
                result = slurm_client.query(args.operation, identifier)
        print(json.dumps(result, indent=2))
        return 0
    except (SlurmAPIError, WarewulfAPIError, ValueError, OSError, configparser.Error) as exc:
        message = str(exc).replace(secret, "[redacted]") if secret else str(exc)
        print(f"Error: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
