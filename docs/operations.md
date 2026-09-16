# Operations

Smartenit Rescue is one foreground process. It never forks, daemonizes, writes a PID
file, rotates logs, restarts itself, or retries a received device command. Terminal,
systemd, launchd, and Docker deployments all run:

```sh
smartenit-rescue run --config path/to/config.toml
```

Use `config.example.toml` as a starting point for the simulator. The experimental
Harmony G2 shape and its limitations are documented separately in
[`harmony-g2.md`](harmony-g2.md).

## Configuration and secrets

The configuration file contains broker locations and secret-file paths, not secret
values. Create the username and password files outside the repository, make them
readable only by the account running the bridge, and set restrictive permissions such
as mode `0600`. Both files must contain a nonblank value and both settings must be
present or absent together. Never place credentials in the command line, environment,
Compose file, or logs.

The example disables TLS only for an explicitly trusted local proof. Enable TLS when
traffic crosses an untrusted network and configure the broker account with the
minimum publish and subscribe permissions described in the Home Assistant guide.

Harmony G2 additionally requires an externally provisioned local session and a
certificate-pin file. Its private state directory must be owned by the process account
with mode `0700`, and the rotating session file must have mode `0600`. The
configuration may stay read-only, but the session path must be separately writable;
renewal atomically replaces that file. Cold bootstrap is not supported. Enroll the
certificate pin only after independent verification on the trusted LAN.

Warnings and expected startup failures are written to stderr. Keep the process in the
foreground and let the surrounding service manager capture and rotate that stream.
`SIGINT` and `SIGTERM` request the same idempotent graceful shutdown: stop accepting
events, publish bridge `offline` when connected, close the backend once, disconnect,
and exit.

## Terminal

Install the package, prepare the config and secret files, then run the foreground
command directly:

```sh
smartenit-rescue run --config path/to/config.toml
```

Press Control-C for graceful shutdown. Configuration and connection failures return a
nonzero status with a bounded stderr diagnostic.

For a G2 run, create a dedicated local state directory and install the externally
provisioned session before starting:

```sh
install -d -m 0700 path/to/private-state
install -m 0600 path/to/provisioned-session.json \
  path/to/private-state/g2-session.json
```

Point `backend.session_file` at that writable destination, not at the source copy.

## systemd

The template at `deploy/systemd/smartenit-rescue.service` uses `Type=exec`, a dedicated
`smartenit-rescue` user and group, `Restart=on-failure`, and a 30-second stop timeout.
Install the executable at `/usr/local/bin/smartenit-rescue`, the edited configuration
at `/etc/smartenit-rescue/config.toml`, and the unit at
`/etc/systemd/system/smartenit-rescue.service`.

The unit uses `StateDirectory=smartenit-rescue` and `StateDirectoryMode=0700`, which
provide `/var/lib/smartenit-rescue` as writable owner-only state while
`ProtectSystem=strict` keeps the remaining filesystem read-only. For G2, install the
initial session as `/var/lib/smartenit-rescue/g2-session.json`, owned by the service
account with mode `0600`, and reference that path from the read-only configuration.

Create the required system account before starting the unit, or have configuration
management create an equivalent locked account. On a distribution with
`groupadd`/`useradd`, run these commands as root:

```sh
groupadd --system smartenit-rescue
useradd --system --gid smartenit-rescue --no-create-home --home-dir /nonexistent \
  --shell /usr/sbin/nologin smartenit-rescue
```

Install the executable and read-only operator files with explicit ownership and modes.
Replace the example's credential-file placeholders with these absolute paths; omit both
credential settings and the last two install commands if the local broker is
intentionally unauthenticated:

```sh
install -o root -g smartenit-rescue -m 0750 \
  path/to/smartenit-rescue /usr/local/bin/smartenit-rescue
install -d -o root -g smartenit-rescue -m 0750 /etc/smartenit-rescue
install -o root -g smartenit-rescue -m 0640 \
  path/to/config.toml /etc/smartenit-rescue/config.toml
install -o root -g smartenit-rescue -m 0640 \
  path/to/mqtt-username /etc/smartenit-rescue/mqtt-username
install -o root -g smartenit-rescue -m 0640 \
  path/to/mqtt-password /etc/smartenit-rescue/mqtt-password
```

For G2, prepare the initial rotating session before startup:

```sh
install -d -o smartenit-rescue -g smartenit-rescue -m 0700 \
  /var/lib/smartenit-rescue
install -o smartenit-rescue -g smartenit-rescue -m 0600 \
  path/to/provisioned-session.json /var/lib/smartenit-rescue/g2-session.json
```

Set `backend.session_file` to that session destination. Then install the unit as
root-owned mode `0644`, validate it, and start it:

```sh
install -o root -g root -m 0644 \
  path/to/smartenit-rescue.service /etc/systemd/system/smartenit-rescue.service
systemd-analyze verify /etc/systemd/system/smartenit-rescue.service
systemctl daemon-reload
systemctl enable --now smartenit-rescue.service
journalctl -u smartenit-rescue.service
```

`systemctl stop smartenit-rescue.service` sends `SIGTERM` and allows up to 30 seconds
for foreground shutdown. The service manager, not the application, performs failure
restarts.

## launchd

The template at `deploy/launchd/com.smartenit.rescue.plist` deliberately contains
`<SERVICE_USER>` and `<SERVICE_GROUP>` placeholders so a system-domain job cannot
silently run as root. Before loading it, create a dedicated non-admin, non-login local
service account and matching group using your MDM, configuration-management system, or
directory-service tooling. The commands below use `smartenit-rescue` for both names;
verify that the account and group exist before continuing:

```sh
id smartenit-rescue
dscl . -read /Groups/smartenit-rescue
```

Replace the two account placeholders with those exact names. Also replace both
`/PATH/TO` values with absolute paths to the executable and configuration file;
launchd does not expand shell syntax. Do not substitute `root`, `wheel`, or a normal
login account.

Install the executable as root-owned and group-executable. Store configuration and
credential files outside the repository as root-owned, group-readable files. These
example modes let only root and the dedicated service group read operator data:

```sh
sudo install -o root -g smartenit-rescue -m 0750 \
  path/to/smartenit-rescue /usr/local/bin/smartenit-rescue
sudo install -d -o root -g smartenit-rescue -m 0750 /etc/smartenit-rescue
sudo install -o root -g smartenit-rescue -m 0640 \
  path/to/config.toml /etc/smartenit-rescue/config.toml
sudo install -o root -g smartenit-rescue -m 0640 \
  path/to/mqtt-username /etc/smartenit-rescue/mqtt-username
sudo install -o root -g smartenit-rescue -m 0640 \
  path/to/mqtt-password /etc/smartenit-rescue/mqtt-password
```

Point the configuration's credential-file settings at those absolute secret paths.
For G2, replace `<PRIVATE_STATE_DIR>` in the plist with a dedicated absolute directory,
create it for the service account with mode `0700`, and put the rotating session there
with mode `0600`:

```sh
sudo install -d -o smartenit-rescue -g smartenit-rescue -m 0700 \
  /var/lib/smartenit-rescue
sudo install -o smartenit-rescue -g smartenit-rescue -m 0600 \
  path/to/provisioned-session.json /var/lib/smartenit-rescue/g2-session.json
```

Set `backend.session_file` to that destination. Do not place the rotating file under
the root-owned read-only configuration directory.
Install the edited plist as a root-owned LaunchDaemon, then validate and load that
installed copy:

```sh
sudo install -o root -g wheel -m 0644 \
  deploy/launchd/com.smartenit.rescue.plist \
  /Library/LaunchDaemons/com.smartenit.rescue.plist
plutil -lint /Library/LaunchDaemons/com.smartenit.rescue.plist
sudo launchctl bootstrap system \
  /Library/LaunchDaemons/com.smartenit.rescue.plist
```

Use `launchctl bootout system/com.smartenit.rescue` for a normal stop. launchd sends a
termination signal to the foreground process; the application does not daemonize or
manage a PID file. launchd captures stderr through its normal service logging.

## Docker Compose

`Dockerfile` builds a wheel in a Python 3.13 slim build stage, installs it into a clean
Python 3.13 slim runtime image, and runs as the unprivileged `rescue` user.
`compose.example.yaml` starts only the bridge; it does not include a broker or Home
Assistant.

The Compose example takes its read-only configuration and broker credential files
from one private directory **outside the checkout**. Set the required environment
variable to an absolute path (this example uses the operator's config directory),
then copy and edit the config. Do not overwrite an existing config:

```sh
export SMARTENIT_RESCUE_PRIVATE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/smartenit-rescue"
install -d -m 0700 "$SMARTENIT_RESCUE_PRIVATE_DIR"
if [ ! -e "$SMARTENIT_RESCUE_PRIVATE_DIR/config.toml" ]; then
  install -m 0644 config.example.toml "$SMARTENIT_RESCUE_PRIVATE_DIR/config.toml"
fi
```

The parent directory's mode `0700` keeps these files private on the host. The
config file itself must be readable by the unprivileged container user; its
bind mount is read-only. Create both Compose secret source files in that same
directory with your local editor, without placing credential values in shell
commands. Docker Compose file-backed secrets retain their host file ownership
and permissions, so use mode `0644` for each file inside the private directory
to let container UID `10001` read it. If the local broker is intentionally
unauthenticated, remove both credential-file settings from the config and create
empty source files so Compose can still start. Do not overwrite existing
credentials:

```sh
for credential in mqtt_username mqtt_password; do
  if [ ! -e "$SMARTENIT_RESCUE_PRIVATE_DIR/$credential" ]; then
    install -m 0644 /dev/null "$SMARTENIT_RESCUE_PRIVATE_DIR/$credential"
  fi
done
```

For an authenticated broker, put the two nonblank values in their respective
files with a local editor. Set both credential-file paths in the private config
to `/run/secrets/mqtt_username` and `/run/secrets/mqtt_password`. Edit `mqtt.host`
so it resolves to the external broker from inside the container; `localhost`
inside the container is the container, not the Docker host.

Prepare the separately writable, Git-ignored `operator-state` directory for
container UID and GID `10001`:

```sh
install -d -m 0700 operator-state
sudo chown 10001:10001 operator-state
```

The external private directory and `operator-state` are excluded from the build
context; `operator-state` is also excluded from source control. The repo
ignores any local `secrets/` directory as a second safeguard, but Compose does
not read from it.

For G2, install the externally provisioned session in the same writable
`operator-state` directory with owner-only modes:

```sh
sudo install -o 10001 -g 10001 -m 0600 \
  path/to/provisioned-session.json operator-state/g2-session.json
```

Set `backend.session_file` to `/var/lib/smartenit-rescue/g2-session.json`. The Compose
file mounts the configuration read-only from outside `operator-state` and mounts
`operator-state` separately as writable private state. Keep
`SMARTENIT_RESCUE_PRIVATE_DIR` exported in the shell running Compose. Then start
the attached foreground service:

```sh
docker compose -f compose.example.yaml up --build
```

The configuration is mounted read-only, the container filesystem is read-only, and
the exec-form entrypoint preserves direct signal delivery. Inspect stderr with
`docker compose -f compose.example.yaml logs bridge`. Stop gracefully with a 30-second
window:

```sh
docker compose -f compose.example.yaml stop -t 30 bridge
```

Compose restart policy handles failures. The bridge itself never restarts or replays a
command.
