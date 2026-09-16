from __future__ import annotations

import plistlib
import re
import tomllib
from pathlib import Path
from typing import cast

from smartenit_rescue.config import load_config

ROOT = Path(__file__).parents[1]
FOREGROUND_ARGUMENTS = ["run", "--config"]


def read_artifact(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_example_config_is_valid_synthetic_runtime_configuration() -> None:
    with (ROOT / "config.example.toml").open("rb") as config_file:
        config = tomllib.load(config_file)

    assert config == {
        "runtime": {
            "client_id": "smartenit-rescue",
            "topic_prefix": "smartenit-rescue/v1",
            "discovery_prefix": "homeassistant",
            "home_assistant_status_topic": "homeassistant/status",
        },
        "mqtt": {
            "host": "localhost",
            "port": 1883,
            "keepalive_seconds": 60,
            "tls": False,
            "username_file": "<mqtt-username-file>",
            "password_file": "<mqtt-password-file>",
        },
        "backend": {
            "type": "simulator",
            "devices": [
                {
                    "device_id": "0200000000000001",
                    "name": "Synthetic Load",
                    "profile_id": "smartenit.4040c",
                    "initial_on": False,
                }
            ],
        },
    }
    loaded = load_config(ROOT / "config.example.toml")
    assert str(loaded.backend.devices[0].device_id) == "0200000000000001"


def test_systemd_service_runs_foreground_as_dedicated_user() -> None:
    service = read_artifact("deploy/systemd/smartenit-rescue.service")

    assert "Type=exec" in service
    assert "User=smartenit-rescue" in service
    assert "Restart=on-failure" in service
    assert "TimeoutStopSec=30" in service
    assert (
        "ExecStart=/usr/local/bin/smartenit-rescue run --config "
        "/etc/smartenit-rescue/config.toml" in service
    )
    assert "Type=forking" not in service
    assert "PIDFile=" not in service
    assert "--daemon" not in service
    assert "StateDirectory=smartenit-rescue" in service
    assert "StateDirectoryMode=0700" in service


def test_launchd_service_runs_the_same_foreground_command() -> None:
    with (ROOT / "deploy/launchd/com.smartenit.rescue.plist").open("rb") as plist_file:
        launchd = cast(dict[str, object], plistlib.load(plist_file))

    arguments = cast(list[str], launchd["ProgramArguments"])
    assert arguments[1:3] == FOREGROUND_ARGUMENTS
    assert arguments[0] == "/PATH/TO/smartenit-rescue"
    assert arguments[3] == "/PATH/TO/config.toml"
    assert launchd["KeepAlive"] == {"SuccessfulExit": False}
    assert launchd["UserName"] == "<SERVICE_USER>"
    assert launchd["GroupName"] == "<SERVICE_GROUP>"
    assert launchd["WorkingDirectory"] == "<PRIVATE_STATE_DIR>"
    assert "--daemon" not in arguments
    assert not any("pid" in argument.lower() for argument in arguments)


def test_container_builds_and_installs_a_wheel_then_drops_privileges() -> None:
    dockerfile = read_artifact("Dockerfile")

    assert len(re.findall(r"^FROM python:3\.13-slim", dockerfile, re.MULTILINE)) == 2
    assert "pip wheel --no-cache-dir --wheel-dir /wheels ." in dockerfile
    assert "pip install --no-cache-dir /wheels/smartenit_rescue-*.whl" in dockerfile
    assert "USER rescue" in dockerfile
    assert (
        'ENTRYPOINT ["smartenit-rescue", "run", "--config", '
        '"/etc/smartenit-rescue/config.toml"]' in dockerfile
    )
    assert "--daemon" not in dockerfile


def test_compose_runs_only_the_bridge_with_read_only_operator_files() -> None:
    compose = read_artifact("compose.example.yaml")
    uncommented = "\n".join(
        line.partition("#")[0].rstrip() for line in compose.splitlines()
    )
    services = re.search(r"(?m)^services:\n(?P<body>(?:^ .*(?:\n|$))*)", uncommented)

    assert services is not None
    assert re.findall(r"(?m)^  ([a-z][a-z0-9_-]*):$", services.group("body")) == [
        "bridge"
    ]
    assert (
        "    entrypoint:\n"
        "      - smartenit-rescue\n"
        "      - run\n"
        "      - --config\n"
        "      - /etc/smartenit-rescue/config.toml" in uncommented
    )
    assert (
        "${SMARTENIT_RESCUE_PRIVATE_DIR:?Set an absolute private directory outside "
        "this checkout}/config.toml:/etc/smartenit-rescue/config.toml:ro" in compose
    )
    assert "./operator-state/config.toml:" not in compose
    assert "./config.example.toml:/etc/smartenit-rescue/config.toml:ro" not in compose
    assert "./operator-state:/var/lib/smartenit-rescue:rw" in compose
    assert "mqtt_username" in compose
    assert "mqtt_password" in compose
    assert "./secrets/" not in compose
    assert compose.count("read_only: true") >= 1
    assert "--daemon" not in compose


def test_operations_keep_config_read_only_and_private_state_owner_only() -> None:
    operations = read_artifact("docs/operations.md")

    assert "externally provisioned local session" in operations
    assert "mode `0700`" in operations
    assert "mode `0600`" in operations
    assert "StateDirectory=smartenit-rescue" in operations
    assert "<PRIVATE_STATE_DIR>" in operations
    assert "operator-state" in operations
    assert "SMARTENIT_RESCUE_PRIVATE_DIR" in operations
    assert "outside the checkout" in operations
    assert "10001" in operations


def test_docker_context_excludes_operator_secrets_and_local_state() -> None:
    ignored = set(read_artifact(".dockerignore").splitlines())

    assert {".git", ".scratch", ".venv", "secrets"} <= ignored
