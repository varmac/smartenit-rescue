from pathlib import Path

RUNTIME_COMMAND = "smartenit-rescue run --config path/to/config.toml"


def test_clean_release_smoke_checks_do_not_inherit_pythonpath() -> None:
    text = Path("CONTRIBUTING.md").read_text(encoding="utf-8")

    assert "uv venv --seed --python .venv/bin/python .smoke-env" in text
    assert (
        "env -u PYTHONPATH uv pip install --python .smoke-env/bin/python "
        "--no-cache dist/*.whl" in text
    )
    assert "env -u PYTHONPATH .smoke-env/bin/smartenit-rescue profiles list" in text
    assert (
        "env -u PYTHONPATH .smoke-env/bin/python -c "
        '"import smartenit_rescue.profiles as profiles; '
        "assert any(profile.profile_id == 'smartenit.4040c' for profile in "
        'profiles.iter_builtin_profiles())"' in text
    )
    assert "python3 -m venv .smoke-env" not in text
    assert "python3 -m pip install --no-input --no-cache-dir" not in text
    assert "env -u PYTHONPATH python3 -c " not in text


def test_ci_installed_wheel_smoke_uses_uv_python() -> None:
    text = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "uv venv --seed --python .venv/bin/python .smoke-env" in text
    assert (
        "env -u PYTHONPATH uv pip install --python .smoke-env/bin/python "
        '--no-cache "${WHEEL[0]}"' in text
    )
    assert "env -u PYTHONPATH .smoke-env/bin/python -c " in text
    assert "env -u PYTHONPATH .smoke-env/bin/smartenit-rescue profiles list" in text
    assert "python3 -m venv .smoke-env" not in text
    assert (
        "env -u PYTHONPATH python3 -m pip install --no-input --no-cache-dir" not in text
    )
    assert "env -u PYTHONPATH python3 -c" not in text
    assert (
        'env -u PYTHONPATH python -c "import smartenit_rescue.profiles as profiles; '
        "assert any(profile.profile_id == 'smartenit.4040c' for profile in "
        'profiles.iter_builtin_profiles())"' not in text
    )


def test_readme_links_runtime_guides_and_describes_synthetic_scope() -> None:
    text = Path("README.md").read_text(encoding="utf-8")
    prose = " ".join(text.split())

    assert RUNTIME_COMMAND in text
    assert "[Home Assistant first-run guide](docs/home-assistant.md)" in text
    assert "[operations guide](docs/operations.md)" in text
    assert "owners of Smartenit controllers who can no longer use" in prose
    assert "original iOS or Android apps" in prose
    assert "replacement Zigbee coordinator" in prose
    assert "planned, not implemented" in prose
    assert "reserved synthetic 4040C-compatible switch" in prose
    assert "no physical device or gateway has verified public support" in prose
    assert "The foundation milestone ships no device command" not in text


def test_runtime_guides_cover_lifecycle_and_deployment_contracts() -> None:
    home_assistant = Path("docs/home-assistant.md").read_text(encoding="utf-8")
    operations = Path("docs/operations.md").read_text(encoding="utf-8")
    home_assistant_prose = " ".join(home_assistant.split())

    assert (
        "uv run smartenit-rescue run --config operator-state/config.toml"
        in home_assistant
    )
    assert (
        "install -m 0600 config.example.toml operator-state/config.toml"
        in home_assistant
    )
    assert "Home Assistant restart" in home_assistant
    assert "Broker restart" in home_assistant
    assert "Abrupt bridge stop" in home_assistant
    assert "Clean up the synthetic entity" in home_assistant
    assert "last will" in home_assistant_prose
    assert "QoS 1" in home_assistant

    assert RUNTIME_COMMAND in operations
    for deployment in ("Terminal", "systemd", "launchd", "Docker Compose"):
        assert f"## {deployment}" in operations
    assert "stderr" in operations
    assert "SIGINT" in operations
    assert "SIGTERM" in operations
    assert "secret files" in operations
    assert "useradd --system" in operations
    assert "groupadd --system" in operations
    assert (
        "homeassistant/device/smartenit_rescue_0200000000000001/config"
        in home_assistant
    )
    assert "mosquitto_pub -r -n -t" in home_assistant
    assert "while Home Assistant remains connected" in home_assistant
    discovery_cleanup = home_assistant.index(
        "homeassistant/device/smartenit_rescue_0200000000000001/config"
    )
    removal_confirmation = home_assistant.index(
        "Confirm that Home Assistant removes", discovery_cleanup
    )
    availability_cleanup = home_assistant.index(
        "smartenit-rescue/v1/0200000000000001/availability",
        removal_confirmation,
    )
    assert discovery_cleanup < removal_confirmation < availability_cleanup
    assert "unrelated discovery or availability prefix" in home_assistant_prose


def test_runtime_documentation_does_not_restore_old_delivery_claims() -> None:
    paths = (
        Path("README.md"),
        Path("docs/architecture.md"),
        Path("docs/home-assistant.md"),
        Path("docs/operations.md"),
    )
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    prose = " ".join(text.split())

    assert (
        "uses unique request identifiers and at-most-once adapter submission"
        not in prose
    )
    assert "submit each physical command at most once" not in prose
    assert "QoS 1 permits redelivery" in prose
    assert "makes no exactly-once guarantee for physical execution" in prose
    assert "does not retry a received command" in prose


def test_harmony_g2_documentation_states_command_only_and_session_limits() -> None:
    paths = (
        Path("README.md"),
        Path("docs/architecture.md"),
        Path("docs/home-assistant.md"),
        Path("docs/operations.md"),
        Path("docs/harmony-g2.md"),
    )
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    prose = " ".join(text.split())

    for concept in (
        "externally provisioned local session",
        "Turn on",
        "Turn off",
        "accepted",
        "not physical state",
        "no automatic retry",
        "cold bootstrap is not supported",
    ):
        assert concept in prose
    assert "docs/harmony-g2.md" in Path("README.md").read_text(encoding="utf-8")
    assert "Harmony G2" in text
    assert "4040C" in text
    assert "experimental" in text
    assert "controllers are heaters" not in prose.lower()
