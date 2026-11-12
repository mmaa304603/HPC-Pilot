"""Configure JWT and a persistent localhost Slurm REST service; run with sudo."""

from __future__ import annotations

import argparse
import configparser
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from hpc_pilot.api import config
from hpc_pilot.api.slurm import SlurmAPIError, SlurmClient, user_token


def run(command: list[str], *, check: bool = True, timeout: int = 30):
    environment = dict(os.environ)
    environment.pop("SLURM_JWT", None)
    return subprocess.run(
        command, env=environment, check=check, capture_output=True, text=True, timeout=timeout
    )


def slurm_settings(text: str) -> dict[str, str]:
    """Read a single-file Slurm configuration without discarding comments on write."""
    settings = {}
    for line in text.splitlines():
        active = line.split("#", 1)[0].strip()
        if re.match(r"(?i)^include(?:\s|=)", active):
            raise ValueError(
                "Automatic setup requires a single-file Slurm configuration; "
                "coordinate JWT settings in included files manually."
            )
        if "=" in active:
            key, value = active.split("=", 1)
            name = key.strip().lower()
            if name in {
                "authtype",
                "authtypes",
                "authalttypes",
                "authaltparameters",
                "statesavelocation",
                "slurmuser",
                "accountingstoragetype",
            }:
                if name in settings:
                    raise ValueError(f"Duplicate Slurm setting: {key}")
                settings[name] = value.strip()
    return settings


def jwt_configuration(text: str, key_path: Path) -> str:
    settings = slurm_settings(text)
    types = [v.strip() for v in settings.get("authalttypes", "").split(",") if v.strip()]
    if "auth/jwt" not in types:
        types.append("auth/jwt")
    parameters = [v.strip() for v in settings.get("authaltparameters", "").split(",") if v.strip()]
    if any(v.split("=", 1)[0] in ("jwks", "disable_token_creation") for v in parameters):
        raise ValueError(
            "Existing external JWT or disabled token issuance requires site-specific setup."
        )
    if not any(v.startswith("jwt_key=") for v in parameters):
        parameters.append(f"jwt_key={key_path}")
    for name, value in (
        ("AuthAltTypes", ",".join(types)),
        ("AuthAltParameters", ",".join(parameters)),
    ):
        pattern = rf"(?im)^[ \t]*{name}[ \t]*=[^\n]*"
        match = re.search(pattern, text)
        comment = ""
        if match and "#" in match.group():
            comment = "  #" + match.group().split("#", 1)[1]
        replacement = f"{name}={value}{comment}"
        if match:
            text = text[: match.start()] + replacement + text[match.end() :]
        else:
            text = text.rstrip() + "\n" + replacement + "\n"
    return text


def scheduler_options(plugins: str, version: str = "auto") -> list[str]:
    """Use versioned content plugins before 23.11, and slurmctld afterward."""
    if version != "auto" and not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        raise ValueError("Invalid configured Slurm API version.")
    if "openapi/slurmctld" in plugins:
        return ["-s", "slurmctld"] + ([] if version == "auto" else ["-d", version])
    versions = set(re.findall(r"openapi/(v\d+\.\d+\.\d+)\b", plugins))
    if not versions or (version != "auto" and version not in versions):
        raise ValueError("The installed slurmrestd has no matching scheduler API plugin.")
    selected = (
        max(versions, key=lambda v: tuple(map(int, v[1:].split("."))))
        if version == "auto"
        else version
    )
    return ["-s", selected]


def service_override(binary: str, conf: Path, account: str, options: list[str], port: int) -> str:
    def quote(value: str) -> str:
        if any(c in value for c in "\r\n\x00"):
            raise ValueError("Service settings must not contain control characters.")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

    command = [binary, "-f", str(conf), "-a", "rest_auth/jwt", *options, f"127.0.0.1:{port}"]
    return (
        "# Managed by hpc_pilot.api.setup_slurm\n[Service]\n"
        f"User={account}\nGroup={pwd.getpwnam(account).pw_gid}\n"
        "EnvironmentFile=\nEnvironment=SLURM_JWT=daemon\n"
        "UnsetEnvironment=SLURMRESTD_LISTEN SLURMRESTD_AUTH_TYPES "
        "SLURMRESTD_OPENAPI_PLUGINS SLURMRESTD_DATA_PARSER_PLUGINS\n"
        "ExecStart=\nExecStart=" + " ".join(map(quote, command)) + "\n"
    )


def write_configuration(path: Path, text: str) -> bool:
    """Write changed content atomically, preserving existing file permissions."""
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError(f"Refusing to modify a symlink: {path}")
    if path.exists() and path.read_text() == text:
        return False
    metadata = path.stat() if path.exists() else None
    config.atomic_write(
        path,
        text,
        stat.S_IMODE(metadata.st_mode) if metadata else 0o644,
        metadata.st_uid if metadata else 0,
        metadata.st_gid if metadata else 0,
    )
    return True


def ensure_daemon() -> str:
    """Install the REST package matching the installed Slurm package when needed."""
    binary = shutil.which("slurmrestd")
    if binary is None:
        if shutil.which("dnf") and shutil.which("rpm"):
            version = run(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", "slurm"]).stdout.strip()
            run(["dnf", "install", "-y", f"slurm-slurmrestd-{version}"], timeout=300)
        elif shutil.which("apt-get") and shutil.which("dpkg-query"):
            version = run(["dpkg-query", "-W", "-f=${Version}", "slurm-wlm"]).stdout.strip()
            run(["apt-get", "install", "-y", f"slurmrestd={version}"], timeout=300)
        else:
            raise ValueError("Install slurmrestd from the same build as Slurm, then rerun setup.")
        binary = shutil.which("slurmrestd")
    if binary is None:
        raise ValueError("slurmrestd was not found after installation.")
    controller = re.search(r"\d+\.\d+\.\d+", run(["scontrol", "--version"]).stdout)
    daemon = re.search(r"\d+\.\d+\.\d+", run([binary, "-V"]).stdout)
    if not controller or not daemon or controller.group() != daemon.group():
        raise ValueError("slurmrestd and the installed Slurm client versions must match.")
    return binary


def verify(username: str, url: str, version: str, slurm_conf: Path) -> str:
    deadline = time.monotonic() + 30
    while True:
        try:
            client = SlurmClient(url, user_token(username, str(slurm_conf)), username, timeout=3)
            versions = client.versions()
            if not versions or (version != "auto" and version not in versions):
                raise SlurmAPIError("Configured scheduler API version is unavailable.")
            client.api_version = versions[-1] if version == "auto" else version
            response = client.query("ping")
            if any(ping.get("pinged") == "DOWN" for ping in response.get("pings", [])):
                raise SlurmAPIError("The REST service reports the controller is DOWN.")
            return client.api_version
        except SlurmAPIError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def setup(
    settings: configparser.ConfigParser,
    path: Path,
    client_user: str,
    *,
    override_path: Path | None = None,
) -> None:
    owner = pwd.getpwnam(client_user)
    conf = config.SLURM_CONF
    original = conf.read_text()
    current = slurm_settings(original)
    url = config.local_url(settings["slurm"]["port"])
    port = int(settings["slurm"]["port"])
    version = settings["slurm"].get("api_version", "auto").strip() or "auto"
    slurm_user = pwd.getpwnam(current.get("slurmuser", "slurm"))
    if "statesavelocation" not in current:
        raise ValueError("Set StateSaveLocation explicitly before running automatic setup.")
    parameters = [p.strip() for p in current.get("authaltparameters", "").split(",")]
    key = next((p.split("=", 1)[1] for p in parameters if p.startswith("jwt_key=")), None)
    key_path = Path(key) if key else Path(current["statesavelocation"]) / "jwt_hs256.key"
    if not key_path.is_absolute():
        raise ValueError("JWT key path must be absolute.")
    new_conf = jwt_configuration(original, key_path)
    db_conf = conf.with_name("slurmdbd.conf")
    database_text = None
    if current.get("accountingstoragetype") == "accounting_storage/slurmdbd":
        if not db_conf.exists():
            raise ValueError("Remote accounting requires coordinated slurmdbd JWT setup first.")
        db_settings = slurm_settings(db_conf.read_text())
        if db_settings.get("slurmuser", "slurm") != slurm_user.pw_name:
            raise ValueError(
                "Different controller/database service accounts need site-specific key access."
            )
        if any(
            p.startswith("jwt_key=") and p != f"jwt_key={key_path}"
            for p in db_settings.get("authaltparameters", "").split(",")
        ):
            raise ValueError(
                "Controller and slurmdbd use different JWT keys; reconcile them first."
            )
        database_text = jwt_configuration(db_conf.read_text(), key_path)
    if key_path.is_symlink():
        raise ValueError("JWT key must not be a symlink.")
    if key_path.exists():
        metadata = key_path.stat()
        if (
            metadata.st_size < 32
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_uid not in (0, slurm_user.pw_uid)
        ):
            raise ValueError(
                "Existing JWT key needs at least 32 bytes and private root/Slurm ownership."
            )
    service_user = config.SLURM_SERVICE_USER
    if service_user in ("root", slurm_user.pw_name, client_user):
        raise ValueError(
            "Use a dedicated REST service account, separate from root, Slurm, and the client."
        )
    if run(["systemctl", "is-active", "slurmctld"], check=False).returncode != 0:
        raise ValueError("Run Slurm setup on the active local controller with systemd.")
    binary = ensure_daemon()
    try:
        account = pwd.getpwnam(service_user)
    except KeyError:
        run(
            [
                "useradd",
                "--system",
                "--user-group",
                "--no-create-home",
                "--shell",
                shutil.which("nologin") or "/usr/sbin/nologin",
                service_user,
            ]
        )
        account = pwd.getpwnam(service_user)
    if account.pw_uid in (0, slurm_user.pw_uid) or account.pw_gid in (0, slurm_user.pw_gid):
        raise ValueError("REST service account must not use the root or Slurm UID/primary group.")
    # Some Slurm releases enforce daemon identity checks even for plugin listing.
    # Create and validate the service account before probing, and run the probes
    # with the same identity and configuration as the eventual systemd service.
    probe = ["runuser", "-u", service_user, "--", binary, "-f", str(conf)]
    authentication = run([*probe, "-a", "list"])
    if "rest_auth/jwt" not in authentication.stdout + authentication.stderr:
        raise ValueError("The installed slurmrestd has no JWT authentication plugin.")
    plugins = run([*probe, "-s", "list"])
    options = scheduler_options(plugins.stdout + plugins.stderr, version)
    override = override_path or Path("/etc/systemd/system/slurmrestd.service.d/50-hpc-pilot.conf")
    if override.parent.is_symlink():
        raise ValueError("REST service override directory must not be a symlink.")
    override.parent.mkdir(parents=True, exist_ok=True)
    database_changed = False
    if not key_path.exists():
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(fd, "wb") as stream:
            os.fchown(stream.fileno(), slurm_user.pw_uid, slurm_user.pw_gid)
            stream.write(secrets.token_bytes(32))
    controller_changed = write_configuration(conf, new_conf)
    if database_text is not None:
        database_changed = write_configuration(db_conf, database_text)
    write_configuration(override, service_override(binary, conf, service_user, options, port))
    if database_changed:
        run(["systemctl", "restart", "slurmdbd"])
    if controller_changed:
        print("Applying JWT settings: restarting slurmctld...")
        run(["systemctl", "restart", "slurmctld"])
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "slurmrestd"])
    run(["systemctl", "restart", "slurmrestd"])
    selected = verify(client_user, url, version, conf)
    config.update_config(
        path,
        "slurm",
        {
            "port": str(port),
            "api_version": selected,
            "user": "",
        },
        uid=owner.pw_uid,
        gid=owner.pw_gid,
    )
    print(f"Verified authenticated Slurm ping ({selected}); persistent localhost service enabled.")
    print(f"Client settings: {path}")
    print("Run: python -m hpc_pilot.api slurm nodes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Override the shared API settings file")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("Run with sudo: sudo /usr/bin/python3 -m hpc_pilot.api.setup_slurm")
    try:
        settings, path, username = config.setup_configuration(args.config)
        setup(settings, path, username)
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        SlurmAPIError,
        configparser.Error,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Slurm setup failed: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr.strip()[:2000], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
