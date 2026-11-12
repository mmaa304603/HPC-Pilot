# HPC Pilot service APIs

This component connects to Slurm and Warewulf through their existing APIs.
It provides terminal commands, client settings, and administrator setup helpers.
No LLM is needed.

## Quick start

Run from the repository root on the controller, using Python 3.11+.
Warewulf setup currently needs Python 3.11 or 3.12 with PyYAML and bcrypt-capable
`crypt`. Run setup through `sudo` from your regular Linux account; run queries
as that regular user.

Create the client settings file if you want to customize the defaults:

```bash
python -m hpc_pilot.api config init
```

Settings live at `~/.hpc-pilot/api/config.ini` for both source checkouts and
installed packages. Review that file, then set up the services you need:

```bash
sudo /usr/bin/python3 -m hpc_pilot.api.setup_slurm
sudo /usr/bin/python3 -m hpc_pilot.api.setup_warewulf
```

Use the same supported Python interpreter for setup if `/usr/bin/python3` is not
appropriate for your installation. Each helper also creates the client settings
file when it is absent. Existing settings for the other service are preserved.

After successful setup:

```bash
python -m hpc_pilot.api slurm versions
python -m hpc_pilot.api slurm ping
python -m hpc_pilot.api slurm nodes
python -m hpc_pilot.api warewulf nodes
```

Authentication is automatic after setup: Slurm obtains a user JWT through
`scontrol token`, and Warewulf loads the saved API password. Each service's
client module handles its own authentication.

## Configuration

The public [config.example.ini](config.example.ini) contains the complete defaults.
It is the version-controlled template; the runtime `config.ini` contains your
site's values. Setup helpers and terminal queries select the same file.

```text
hpc_pilot/api/
├── config.py                         # Configuration loader
├── config.example.ini                # Public defaults, committed to Git
└── config.ini                        # Optional development override, Git-ignored

~/.hpc-pilot/
├── config.yaml                       # Existing main application settings
└── api/
    ├── config.ini                    # Default API settings
    └── credentials/
        └── warewulf.env              # Private generated credentials, mode 0600
```

Rules for API files:

- Use `.ini` for settings and `<service>.env` for private credentials.
- Keep ports, API usernames, timeouts, and the Slurm API version in `config.ini`;
  keep passwords and JWTs out of both the INI file and its public template.
- Store persistent credentials under `api/credentials/`. Slurm JWTs are issued
  on demand and held in memory, with no persistent JWT file.
- Source checkouts and installed packages use the same runtime location.
  Development overrides require `--config` or `HPC_PILOT_API_CONFIG`.
- `HPC_PILOT_HOME` relocates the complete runtime tree; it defaults to
  `~/.hpc-pilot`. Setup requires `sudo` from a regular account and uses
  `SUDO_USER` to choose the owner of settings and credentials.

The main client settings are:

```ini
[slurm]
port = 6820
api_version = auto
user =
timeout = 10

[warewulf]
port = 9873
user = hpc-pilot
timeout = 10
```

- A blank Slurm `user` uses the current Linux account. `api_version = auto`
  discovers the newest scheduler API exposed by the server; Slurm setup records
  the version it verified.
- Both services connect to `http://127.0.0.1:<port>`. Ports must be integers
  from 1 to 65535. `timeout` is the HTTP request timeout in seconds and must
  be positive and finite.
- `warewulf.user` names a separate API account. Its default, `hpc-pilot`, is a
  service identity, independent of anyone's Linux login. Customize it in your
  configuration before setup if needed.
- Slurm setup uses `/etc/slurm/slurm.conf` and the `slurmrestd` daemon account.
  These are fixed conventions for this deployment. The daemon account is
  separate from the Linux user making requests.

Choose another settings file with `HPC_PILOT_API_CONFIG` or `--config`. For a
development override, create a private file beside the source:

```bash
python -m hpc_pilot.api --config hpc_pilot/api/config.ini config init
python -m hpc_pilot.api --config hpc_pilot/api/config.ini slurm nodes
sudo /usr/bin/python3 -m hpc_pilot.api.setup_slurm --config hpc_pilot/api/config.ini
sudo /usr/bin/python3 -m hpc_pilot.api.setup_warewulf --config hpc_pilot/api/config.ini
```

Explicit settings files must exist when used for queries or setup. File selection is:

```text
--config → HPC_PILOT_API_CONFIG → <HPC_PILOT_HOME>/api/config.ini
```

When `HPC_PILOT_HOME` is unset, the default is `~/.hpc-pilot/api/config.ini`.
There is no automatic checkout-local configuration. Relative command-line paths
resolve from the current working directory. `~` expands to the invoking user's home
when setup runs under `sudo`.

Connection precedence is **command-line options > environment variables > INI
settings > built-in defaults**. Environment overrides are `SLURM_REST_PORT`,
`SLURM_USER`, `SLURM_API_VERSION`, `WAREWULF_REST_PORT`, and `WAREWULF_API_USER`.
Use port variables for local connections. Old `SLURM_REST_URL` and
`WAREWULF_REST_URL` exports produce an error asking you to replace them.

Connection flags such as `--port`, `--user`, and `--timeout` go before the
resource, but ordinary usage needs only the service and operation. Use
`python -m hpc_pilot.api slurm --help`
or `python -m hpc_pilot.api warewulf --help` to inspect them.

```bash
python -m hpc_pilot.api slurm --port 6820 --timeout 20 ping
python -m hpc_pilot.api warewulf --port 9873 nodes
```

Only the settings in `config.example.ini` are accepted. Unknown keys or sections
produce an error, including the former `url`, `ca_file`, `auto_token`,
`credentials_file`, and `[setup]` settings. Use `port` and remove obsolete keys
when updating an older INI. Configuration migration and automatic format
conversion are no longer part of the client.

## Authentication

Local HTTP still uses the authentication required by each service. The CLI
calls helpers in the service modules before sending a request:

| Service module | Helper | Authentication |
| --- | --- | --- |
| [slurm.py](slurm.py) | `user_token()` | Obtains a JWT through `scontrol token` for the current Linux/Slurm user. |
| [warewulf.py](warewulf.py) | `read_credentials()` | Reads the saved Warewulf API username and password without executing shell code. |

Slurm tokens are issued for each CLI invocation and held in memory. An explicit
`SLURM_JWT` overrides automatic issuance; unset it when it expires to resume
automatic issuance. `SLURM_JWT=daemon` belongs only in the REST server process.
During setup, the helper obtains the verification token as the invoking sudo user.

Warewulf setup saves generated credentials with mode `0600` at
`<HPC_PILOT_HOME>/api/credentials/warewulf.env`. The CLI loads its password
automatically unless `WAREWULF_API_PASSWORD` is supplied. The API username
comes from `--user`, `WAREWULF_API_USER`, or `warewulf.user`. This API account
is separate from Linux login accounts; it is defined in `/etc/warewulf/auth.conf`.
The server's password hash cannot replace the client password.

Authentication helpers live alongside their service clients. There is no shared
`auth.py` module, authentication switch, or configurable credential-file path.

## Slurm setup and usage

`setup_slurm` is intended for the local active controller with systemd. It:

1. Installs a matching REST package through DNF/RPM or APT when it is missing,
   and checks the REST daemon's version against the installed Slurm client.
2. Preserves an existing private JWT signing key or creates a new key owned by
   `SlurmUser`. Existing authentication methods and unrelated settings remain.
3. Adds JWT as an alternative authentication method. When a local accounting
   daemon is configured, it updates its JWT settings to use the same key.
4. Creates a dedicated REST service account and a managed systemd drop-in at
   `/etc/systemd/system/slurmrestd.service.d/50-hpc-pilot.conf`. It binds to
   `127.0.0.1` at the configured port, selects the installed scheduler API plugin,
   and enables the persistent service.
5. Restarts `slurmctld` and local `slurmdbd` only when their configuration changes.
   It restarts the REST service, obtains a JWT as the invoking user, verifies
   version discovery and an authenticated controller ping, and saves client settings.

Run setup during an appropriate maintenance window: a first-time JWT change
restarts the controller. Setup writes configuration directly. If setup fails, applied configuration and
service changes remain in place. Inspect the reported error and service state,
correct the problem, and rerun setup; restart affected services manually if a
previous attempt wrote their configuration but failed to restart them.

The helper handles a single-file controller configuration and local accounting.
Included configurations, external JWKS, disabled user token issuance, or remote
accounting require coordinated site-specific setup; it reports these cases
rather than replacing that policy. The local listener always uses
`http://127.0.0.1:<port>` with the port from `[slurm]`.

Stop an earlier foreground REST daemon before switching to the persistent
service so both do not compete for the same port. Successful setup needs no
second terminal. Check service state with:

```bash
systemctl status slurmrestd
ss -ltn '( sport = :6820 )'
```

Read-only terminal operations:

```bash
python -m hpc_pilot.api slurm versions
python -m hpc_pilot.api slurm ping
python -m hpc_pilot.api slurm nodes
python -m hpc_pilot.api slurm jobs
python -m hpc_pilot.api slurm partitions
```

Append a node name, job ID, or partition name to inspect one resource.
`versions` inspects the OpenAPI document; `ping` makes an authenticated request
to the scheduler. The client has a fixed query allowlist and exposes no arbitrary
HTTP endpoint execution.

References: [JWT setup guide](https://slurm.schedmd.com/jwt.html),
[REST setup and troubleshooting](https://slurm.schedmd.com/rest_quickstart.html),
[daemon options](https://slurm.schedmd.com/slurmrestd.html),
[client/OpenAPI guide](https://slurm.schedmd.com/rest_clients.html).

## Warewulf setup and usage

`setup_warewulf` enables the existing daemon's localhost management API, preserves
existing users, and creates the API account specified in the private settings.
It reads the existing listener port from `/etc/warewulf/warewulf.conf` and saves
that port in the client INI. Editing the client port alone does not change the
Warewulf provisioning listener.

It generates a password and saves credentials with mode `0600` for the invoking
user at the fixed runtime credential path described above. Re-running setup
reuses the saved password.

The helper saves credentials and client settings before updating the server,
restarts `warewulfd`, and verifies nodes, profiles, images, and overlays.
Restarting briefly interrupts provisioning HTTP requests. Applied changes remain
in place on failure. Saved credentials remain
available so a retry reuses the same password.
It requires Warewulf 4.6.1+, PyYAML, and bcrypt support in Python's `crypt` module.
Use a supported system Python with `crypt`; Python 3.13+ removes that module.

```bash
python -m hpc_pilot.api warewulf nodes
python -m hpc_pilot.api warewulf profiles
python -m hpc_pilot.api warewulf images
python -m hpc_pilot.api warewulf overlays
```

Append an identifier to inspect one resource. Interactive API documentation is
served at `http://127.0.0.1:9873/api/docs`. Warewulf API credentials grant
administrative access to provisioning, so the helper restricts access to localhost.

References: [API and authentication](https://warewulf.org/docs/v4.6.x/server/api.html),
[server/API configuration](https://warewulf.org/docs/v4.6.x/server/configuration.html#api).

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Connection refused | Run the appropriate setup helper and inspect the service status and listener. Issuing a JWT does not start the REST service. |
| Slurm token issuance fails | Check controller connectivity and JWT configuration. Local automatic issuance uses the current Linux account. |
| Slurm HTTP 401/500 | Remove an expired exported `SLURM_JWT` to use automatic issuance; check the configured user and daemon logs. |
| Warewulf HTTP 401 | Verify the configured API account and private credentials file, or rerun setup. |
| Warewulf HTTP 403 | Connect from localhost or an SSH tunnel; inspect the API subnet restrictions. |
| HTTP 404 | Check the service port, API version, and resource. |
| Unsupported settings or an old URL variable | Compare the INI with `config.example.ini`; replace URL exports with the corresponding `*_REST_PORT` variables. |
| Setup fails | Applied changes remain in place. Inspect the error and service state, fix the cause, and rerun; restart affected services if needed. |

## Layout and verification

| File | Responsibility |
| --- | --- |
| [config.example.ini](config.example.ini) | Public defaults for the seven client settings. |
| [config.py](config.py) | Settings selection, validation, runtime paths, and safe writes. |
| [slurm.py](slurm.py) | Slurm requests, version discovery, errors, and local JWT issuance. |
| [warewulf.py](warewulf.py) | Warewulf requests, errors, and private credential reading. |
| [setup_slurm.py](setup_slurm.py), [setup_warewulf.py](setup_warewulf.py) | Administrator setup, configuration writes, and verification. |
| [cli.py](cli.py), [__main__.py](__main__.py) | Argument parsing, configuration overrides, dispatch, and JSON output. |
| [tests/](tests/) | HTTP, configuration, and setup checks with temporary files and mocked services. |

Run the API tests:

```bash
python -m unittest discover -s hpc_pilot/api/tests -v
```

Tests do not modify a live controller or require credentials or root access.
They use temporary localhost HTTP servers; Warewulf setup tests require PyYAML
and bcrypt-capable `crypt`. After installation, `hpc-pilot-api` is equivalent to
`python -m hpc_pilot.api`. Exit codes are `0` for success, `1` for service/config
errors, and `2` for CLI usage errors.

Agent integration should route operations through HPC Pilot's central dispatcher
for its permission checks, approvals, and audit logging. Terminal commands use
the services' native authorization.
