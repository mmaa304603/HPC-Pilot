"""Local API settings, runtime paths, and safe configuration writes."""

from __future__ import annotations

import configparser
import io
import os
import pwd
import tempfile
from pathlib import Path


SLURM_CONF = Path("/etc/slurm/slurm.conf")
SLURM_SERVICE_USER = "slurmrestd"


def local_url(port: str | int) -> str:
    """Construct a loopback endpoint from a validated TCP port."""
    if not str(port).isascii() or not str(port).isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("Service port must be an integer between 1 and 65535.")
    return f"http://127.0.0.1:{int(port)}"


def client_home() -> Path:
    """Resolve the caller's home, including when a setup command runs under sudo."""
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        return Path(pwd.getpwnam(os.environ["SUDO_USER"]).pw_dir)
    return Path.home()


def client_directory(home: Path | None = None) -> Path:
    """Main HPC Pilot runtime directory, shared with the rest of the application."""
    return expand_path(
        os.environ.get("HPC_PILOT_HOME", (home or client_home()) / ".hpc-pilot"), home
    )


def api_directory(home: Path | None = None) -> Path:
    return client_directory(home) / "api"


def default_credentials_path(service: str, home: Path | None = None) -> Path:
    if not service or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in service):
        raise ValueError("Invalid service name for a credentials file.")
    return api_directory(home) / "credentials" / f"{service}.env"


def expand_path(value: str | Path, home: Path | None = None) -> Path:
    text = str(value)
    if text == "~" or text.startswith("~/"):
        return (home or client_home()) / text[2:]
    return Path(text).expanduser()


def config_path(value: str | Path | None = None, *, home: Path | None = None) -> Path:
    selected = value or os.environ.get("HPC_PILOT_API_CONFIG")
    if selected:
        return expand_path(selected, home).absolute()
    return api_directory(home) / "config.ini"


def _defaults() -> configparser.ConfigParser:
    result = configparser.ConfigParser(interpolation=None)
    result.read_string(Path(__file__).with_name("config.example.ini").read_text())
    return result


def load_config(
    path: str | Path | None = None, *, home: Path | None = None
) -> configparser.ConfigParser:
    """An explicitly selected file must exist; an absent default uses public defaults."""
    result = _defaults()
    selected = config_path(path, home=home)
    if selected.exists():
        supplied = configparser.ConfigParser(interpolation=None)
        with selected.open() as stream:
            supplied.read_file(stream)
        if supplied.defaults():
            raise ValueError("Use explicit service sections instead of [DEFAULT].")
        for section in supplied.sections():
            if section not in result or supplied[section].keys() - result[section].keys():
                raise ValueError(f"Unsupported settings in [{section}]; see config.example.ini.")
        result.read_dict(supplied)
    elif path is not None or os.environ.get("HPC_PILOT_API_CONFIG"):
        raise ValueError(f"API configuration does not exist: {selected}")
    for section in result.sections():
        local_url(result[section]["port"])
    return result


def ensure_directory(path: Path, uid: int, gid: int) -> None:
    """Create each missing runtime directory privately and with the intended owner."""
    missing = []
    current = path
    while not current.exists():
        if current.is_symlink():
            raise ValueError(f"Runtime directory must not be a symlink: {current}")
        missing.append(current)
        current = current.parent
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError(f"Runtime directory must not contain symlinks: {path}")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        os.chown(directory, uid, gid)


def atomic_write(path: Path, text: str, mode: int, uid: int, gid: int) -> None:
    """Replace a file without leaving partial data or publicly readable credentials."""
    if path.is_symlink():
        raise ValueError(f"Refusing to replace a symlink: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), uid, gid)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def update_config(
    path: Path,
    service: str | None = None,
    values: dict[str, str] | None = None,
    *,
    uid: int | None = None,
    gid: int | None = None,
) -> None:
    """Create defaults or update one section, preserving other configured values."""
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("API configuration and its directory must not be symlinks.")
    ensure_directory(path.parent, uid, gid)
    if service is None:
        if path.exists():
            return
        text = Path(__file__).with_name("config.example.ini").read_text()
    else:
        # A new file starts from defaults, never another selected site's settings.
        settings = load_config(path) if path.exists() else _defaults()
        if (values or {}).keys() - settings[service].keys():
            raise ValueError(f"Unsupported settings in [{service}]; see config.example.ini.")
        settings[service].update(values or {})
        local_url(settings[service]["port"])
        stream = io.StringIO()
        settings.write(stream)
        text = stream.getvalue()
    atomic_write(path, text, 0o600, uid, gid)


def invoking_user() -> str:
    user = os.environ.get("SUDO_USER")
    if not user or user == "root":
        raise ValueError("Run setup with sudo from a regular account (SUDO_USER is required).")
    pwd.getpwnam(user)
    return user


def setup_configuration(
    value: str | Path | None = None,
) -> tuple[configparser.ConfigParser, Path, str]:
    """Select setup settings and runtime paths for the intended regular user."""
    username = invoking_user()
    owner_home = Path(pwd.getpwnam(username).pw_dir)
    selected = config_path(value, home=owner_home)
    settings = load_config(value, home=owner_home)
    return settings, selected, username
