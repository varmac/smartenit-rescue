from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Condition, Event, Lock, Thread
from time import monotonic
from typing import Protocol
from uuid import uuid4

import pytest
from paho.mqtt import client as mqtt
from paho.mqtt.client import ConnectFlags, MQTTMessage
from paho.mqtt.enums import (
    CallbackAPIVersion,
    MQTTErrorCode,
    MQTTProtocolVersion,
)
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode
from paho.mqtt.subscribeoptions import SubscribeOptions

_HOST_ENV = "SMARTENIT_RESCUE_TEST_MQTT_HOST"
_PORT_ENV = "SMARTENIT_RESCUE_TEST_MQTT_PORT"
_USERNAME_ENV = "SMARTENIT_RESCUE_TEST_MQTT_USERNAME"
_PASSPHRASE_ENV = "SMARTENIT_RESCUE_TEST_MQTT_" + "PASSWORD"
_DEVICE_ID = "0200000000000001"
_TIMEOUT_SECONDS = 10.0
_PROCESS_POLL_SECONDS = 0.05
_STDERR_TAIL_CHARS = 1000


@dataclass(frozen=True, slots=True)
class _Credentials:
    username: str
    passphrase: str


@dataclass(frozen=True, slots=True)
class _ReceivedMessage:
    topic: str
    payload: bytes
    qos: int
    retain: bool


class _ProcessMonitor:
    def __init__(
        self,
        process: subprocess.Popen[str],
        redactions: Sequence[str],
    ) -> None:
        if process.stderr is None:
            raise ValueError("process monitor requires a stderr pipe")
        self._process = process
        self._stderr = process.stderr
        values_by_length: dict[int, set[str]] = {}
        for value in redactions:
            if value:
                values_by_length.setdefault(len(value), set()).add(value)
        self._redactions = tuple(
            value
            for length in sorted(values_by_length, reverse=True)
            for value in values_by_length[length]
        )
        self._redaction_pattern = (
            re.compile("|".join(re.escape(value) for value in self._redactions))
            if self._redactions
            else None
        )
        self._redaction_overlap = max(
            (len(value) - 1 for value in self._redactions),
            default=0,
        )
        self._pending_stderr = ""
        self._tail = ""
        self._tail_lock = Lock()
        self._stderr_thread = Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    @classmethod
    def launch(
        cls,
        arguments: Sequence[str],
        *,
        redactions: Sequence[str] = (),
    ) -> _ProcessMonitor:
        process = subprocess.Popen(
            list(arguments),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        return cls(process, redactions)

    def raise_if_exited(self, description: str) -> None:
        returncode = self._process.poll()
        if returncode is None:
            return
        self._finish_stderr()
        raise AssertionError(
            f"bridge exited with code {returncode} while waiting for {description}; "
            f"stderr tail: {self.stderr_tail()}"
        )

    def send_signal(
        self,
        requested_signal: signal.Signals,
        *,
        description: str,
    ) -> None:
        self.raise_if_exited(description)
        try:
            self._process.send_signal(requested_signal)
        except ProcessLookupError:
            self.raise_if_exited(description)
            raise

    def wait(self, timeout: float) -> tuple[int, str]:
        returncode = self._process.wait(timeout=timeout)
        self._finish_stderr()
        return returncode, self.stderr_tail()

    def terminate(self) -> None:
        if self._process.poll() is None:
            self._process.send_signal(signal.SIGTERM)
            try:
                self._process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3.0)
        self._finish_stderr()

    def stderr_tail(self) -> str:
        with self._tail_lock:
            tail = self._tail
        sanitized = " ".join(tail.splitlines()).strip()
        return sanitized[-_STDERR_TAIL_CHARS:] or "<empty>"

    def _drain_stderr(self) -> None:
        while chunk := self._stderr.read(4096):
            with self._tail_lock:
                self._consume_stderr(chunk, final=False)
        with self._tail_lock:
            self._consume_stderr("", final=True)

    def _consume_stderr(self, chunk: str, *, final: bool) -> None:
        combined = self._pending_stderr + chunk
        if self._redaction_pattern is None:
            self._pending_stderr = ""
            self._append_tail(combined)
            return

        holdback = 0 if final else self._redaction_overlap
        safe_end = max(0, len(combined) - holdback)
        cursor = 0
        parts: list[str] = []
        for match in self._redaction_pattern.finditer(combined):
            if match.start() >= safe_end:
                break
            parts.append(combined[cursor : match.start()])
            parts.append("<redacted>")
            cursor = match.end()
        if cursor < safe_end:
            parts.append(combined[cursor:safe_end])
            cursor = safe_end

        self._pending_stderr = combined[max(cursor, safe_end) :]
        self._append_tail("".join(parts))

    def _append_tail(self, text: str) -> None:
        self._tail = (self._tail + text)[-_STDERR_TAIL_CHARS:]

    def _finish_stderr(self) -> None:
        self._stderr_thread.join(timeout=1.0)


class _MessageLog:
    def __init__(self) -> None:
        self._condition = Condition()
        self._messages: list[_ReceivedMessage] = []

    def append(self, message: _ReceivedMessage) -> None:
        with self._condition:
            self._messages.append(message)
            self._condition.notify_all()

    def mark(self) -> int:
        with self._condition:
            return len(self._messages)

    def since(self, index: int) -> tuple[_ReceivedMessage, ...]:
        with self._condition:
            return tuple(self._messages[index:])

    def wait_for(
        self,
        predicate: Callable[[_ReceivedMessage], bool],
        *,
        after: int,
        description: str,
        abort_check: Callable[[], None] | None = None,
    ) -> tuple[int, _ReceivedMessage]:
        deadline = monotonic() + _TIMEOUT_SECONDS
        with self._condition:
            while True:
                for index in range(after, len(self._messages)):
                    message = self._messages[index]
                    if predicate(message):
                        return index, message
                if abort_check is not None:
                    abort_check()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise AssertionError(f"timed out waiting for {description}")
                wait_seconds = remaining
                if abort_check is not None:
                    wait_seconds = min(wait_seconds, _PROCESS_POLL_SECONDS)
                self._condition.wait(wait_seconds)


class _Observer:
    def __init__(self, client_id: str, credentials: _Credentials | None) -> None:
        self.messages = _MessageLog()
        self._connected = Event()
        self._subscribed = Event()
        self._connect_reason: ReasonCode | None = None
        self._subscribe_reasons: list[ReasonCode] | None = None
        self._loop_started = False
        self._client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=MQTTProtocolVersion.MQTTv5,
        )
        self._client.on_connect = self._on_connect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_message = self._on_message
        if credentials is not None:
            self._client.username_pw_set(
                credentials.username,
                credentials.passphrase,
            )

    @property
    def started(self) -> bool:
        return self._loop_started

    def start(self, host: str, port: int, topics: Sequence[str]) -> None:
        result = self._client.connect(
            host,
            port,
            keepalive=10,
            clean_start=True,
        )
        assert result is MQTTErrorCode.MQTT_ERR_SUCCESS
        result = self._client.loop_start()
        assert result is MQTTErrorCode.MQTT_ERR_SUCCESS
        self._loop_started = True
        assert self._connected.wait(_TIMEOUT_SECONDS), "observer did not connect"
        assert self._connect_reason is not None
        assert not self._connect_reason.is_failure, (
            f"observer connection failed: {self._connect_reason}"
        )

        result, _message_id = self._client.subscribe(
            [
                (
                    topic,
                    SubscribeOptions(qos=1, retainAsPublished=True),
                )
                for topic in topics
            ]
        )
        assert result is MQTTErrorCode.MQTT_ERR_SUCCESS
        assert self._subscribed.wait(_TIMEOUT_SECONDS), (
            "observer subscription was not acknowledged"
        )
        assert self._subscribe_reasons is not None
        assert all(not reason.is_failure for reason in self._subscribe_reasons), (
            f"observer subscription failed: {self._subscribe_reasons}"
        )

    def publish(
        self,
        topic: str,
        payload: bytes,
        *,
        retain: bool = False,
    ) -> None:
        result = self._client.publish(topic, payload, qos=1, retain=retain)
        assert result.rc is MQTTErrorCode.MQTT_ERR_SUCCESS
        result.wait_for_publish(timeout=_TIMEOUT_SECONDS)
        assert result.is_published(), f"publish was not acknowledged: {topic}"

    def close(self) -> None:
        if not self._loop_started:
            return
        self._loop_started = False
        self._client.disconnect()
        self._client.loop_stop()

    def _on_connect(
        self,
        _client: mqtt.Client,
        _userdata: object,
        _flags: ConnectFlags,
        reason_code: ReasonCode,
        _properties: Properties | None,
    ) -> None:
        self._connect_reason = reason_code
        self._connected.set()

    def _on_subscribe(
        self,
        _client: mqtt.Client,
        _userdata: object,
        _message_id: int,
        reason_codes: list[ReasonCode],
        _properties: Properties | None,
    ) -> None:
        self._subscribe_reasons = reason_codes
        self._subscribed.set()

    def _on_message(
        self,
        _client: mqtt.Client,
        _userdata: object,
        message: MQTTMessage,
    ) -> None:
        self.messages.append(
            _ReceivedMessage(
                topic=message.topic,
                payload=message.payload,
                qos=message.qos,
                retain=message.retain,
            )
        )


def _environment() -> tuple[str, int, _Credentials | None]:
    host = os.environ.get(_HOST_ENV)
    if not host:
        pytest.skip(f"set {_HOST_ENV} to run the real-broker integration test")

    raw_port = os.environ.get(_PORT_ENV, "1883")
    try:
        port = int(raw_port)
    except ValueError:
        pytest.fail(f"{_PORT_ENV} must be an integer")
    if not 1 <= port <= 65535:
        pytest.fail(f"{_PORT_ENV} must be between 1 and 65535")

    username = os.environ.get(_USERNAME_ENV)
    passphrase = os.environ.get(_PASSPHRASE_ENV)
    if (username is None) != (passphrase is None):
        pytest.fail(f"{_USERNAME_ENV} and {_PASSPHRASE_ENV} must be set together")
    if username is None or passphrase is None:
        return host, port, None
    if not username.strip() or not passphrase.strip():
        pytest.fail(f"{_USERNAME_ENV} and {_PASSPHRASE_ENV} must be nonblank")
    return host, port, _Credentials(username, passphrase)


def _write_config(
    directory: Path,
    *,
    host: str,
    port: int,
    client_id: str,
    topic_prefix: str,
    discovery_prefix: str,
    status_topic: str,
    credentials: _Credentials | None,
) -> Path:
    credential_lines = ""
    if credentials is not None:
        username_path = directory / "mqtt-username"
        passphrase_path = directory / "mqtt-password"
        username_path.write_text(credentials.username, encoding="utf-8")
        passphrase_path.write_text(credentials.passphrase, encoding="utf-8")
        credential_lines = (
            f"username_file = {json.dumps(str(username_path))}\n"
            f"password_file = {json.dumps(str(passphrase_path))}\n"
        )

    config_path = directory / "runtime.toml"
    config_path.write_text(
        "[runtime]\n"
        f"client_id = {json.dumps(client_id)}\n"
        f"topic_prefix = {json.dumps(topic_prefix)}\n"
        f"discovery_prefix = {json.dumps(discovery_prefix)}\n"
        f"home_assistant_status_topic = {json.dumps(status_topic)}\n"
        "\n"
        "[mqtt]\n"
        f"host = {json.dumps(host)}\n"
        f"port = {port}\n"
        "keepalive_seconds = 10\n"
        "tls = false\n"
        f"{credential_lines}"
        "\n"
        "[backend]\n"
        'type = "simulator"\n'
        "\n"
        "[[backend.devices]]\n"
        f'device_id = "{_DEVICE_ID}"\n'
        'name = "Synthetic Integration Device"\n'
        'profile_id = "smartenit.4040c"\n'
        "initial_on = false\n",
        encoding="utf-8",
    )
    return config_path


def _wait_for_message(
    observer: _Observer,
    topic: str,
    payload: bytes,
    *,
    after: int,
    retain: bool | None = None,
    monitor: _ProcessMonitor | None = None,
) -> _ReceivedMessage:
    def matches(message: _ReceivedMessage) -> bool:
        return (
            message.topic == topic
            and message.payload == payload
            and (retain is None or message.retain is retain)
        )

    description = f"{topic}={payload!r}"
    abort_check = None
    if monitor is not None:
        abort_check = lambda: monitor.raise_if_exited(description)
    _index, message = observer.messages.wait_for(
        matches,
        after=after,
        description=description,
        abort_check=abort_check,
    )
    return message


def _wait_for_topic(
    observer: _Observer,
    topic: str,
    *,
    after: int,
    monitor: _ProcessMonitor | None = None,
) -> _ReceivedMessage:
    abort_check = None
    if monitor is not None:
        abort_check = lambda: monitor.raise_if_exited(topic)
    _index, message = observer.messages.wait_for(
        lambda received: received.topic == topic,
        after=after,
        description=topic,
        abort_check=abort_check,
    )
    return message


def _force_bridge_reconnect(
    host: str,
    port: int,
    bridge_client_id: str,
    credentials: _Credentials | None,
) -> None:
    takeover_connected = Event()
    takeover_reason: list[ReasonCode] = []
    client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=bridge_client_id,
        protocol=MQTTProtocolVersion.MQTTv5,
        reconnect_on_failure=False,
    )
    if credentials is not None:
        client.username_pw_set(credentials.username, credentials.passphrase)

    def on_connect(
        _client: mqtt.Client,
        _userdata: object,
        _flags: ConnectFlags,
        reason_code: ReasonCode,
        _properties: Properties | None,
    ) -> None:
        takeover_reason.append(reason_code)
        takeover_connected.set()

    client.on_connect = on_connect
    loop_started = False
    try:
        result = client.connect(
            host,
            port,
            keepalive=10,
            clean_start=True,
        )
        assert result is MQTTErrorCode.MQTT_ERR_SUCCESS
        result = client.loop_start()
        assert result is MQTTErrorCode.MQTT_ERR_SUCCESS
        loop_started = True
        assert takeover_connected.wait(_TIMEOUT_SECONDS), (
            "client-ID takeover did not connect"
        )
        assert takeover_reason and not takeover_reason[-1].is_failure, (
            f"client-ID takeover failed: {takeover_reason[-1]}"
        )
    finally:
        client.disconnect()
        if loop_started:
            client.loop_stop()


def _assert_no_topic(
    messages: Sequence[_ReceivedMessage],
    topic: str,
    *,
    description: str,
) -> None:
    assert not [message for message in messages if message.topic == topic], description


class _RetainedPublisher(Protocol):
    def publish(
        self,
        topic: str,
        payload: bytes,
        *,
        retain: bool = False,
    ) -> None: ...


class _ProcessTerminator(Protocol):
    def terminate(self) -> None: ...


class _CleanupObserver(_RetainedPublisher, Protocol):
    @property
    def started(self) -> bool: ...

    def close(self) -> None: ...


def _clear_retained_topics(
    publisher: _RetainedPublisher,
    topics: Sequence[str],
    *,
    primary_error: BaseException | None,
) -> None:
    failures: list[str] = []
    for topic in topics:
        try:
            publisher.publish(topic, b"", retain=True)
        except (AssertionError, OSError, RuntimeError, ValueError) as error:
            failures.append(f"{topic}: {error}")
    if not failures:
        return

    detail = "failed to clear retained integration-test topics: " + "; ".join(failures)
    if primary_error is not None:
        primary_error.add_note(detail)
        return
    raise AssertionError(detail)


def _cleanup_test_resources(
    monitor: _ProcessTerminator | None,
    observer: _CleanupObserver,
    topics: Sequence[str],
    *,
    primary_error: BaseException | None,
) -> None:
    failures: list[str] = []
    if monitor is not None:
        try:
            monitor.terminate()
        except (OSError, subprocess.TimeoutExpired) as error:
            failures.append(f"process termination: {error.__class__.__name__}")

    if observer.started:
        try:
            _clear_retained_topics(
                observer,
                topics,
                primary_error=None,
            )
        except AssertionError as error:
            failures.append(str(error))

    try:
        observer.close()
    except (AssertionError, OSError, RuntimeError, ValueError) as error:
        failures.append(f"observer close: {error.__class__.__name__}")

    if not failures:
        return
    detail = "integration-test cleanup failures: " + "; ".join(failures)
    if primary_error is not None:
        primary_error.add_note(detail)
        return
    raise AssertionError(detail)


def test_process_monitor_reports_early_exit_with_bounded_redacted_stderr() -> None:
    redacted_value = "do-not-print-this-value"
    monitor = _ProcessMonitor.launch(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stderr.write('discarded-prefix-' + 'x' * 1200 + "
                f"'{redacted_value}-tail'); "
                "raise SystemExit(7)"
            ),
        ],
        redactions=(redacted_value,),
    )
    messages = _MessageLog()
    try:
        with pytest.raises(AssertionError) as error:
            messages.wait_for(
                lambda _message: False,
                after=0,
                description="synthetic broker message",
                abort_check=lambda: monitor.raise_if_exited("synthetic broker message"),
            )
    finally:
        monitor.terminate()

    diagnostic = str(error.value)
    assert "code 7" in diagnostic
    assert "synthetic broker message" in diagnostic
    assert "<redacted>-tail" in diagnostic
    assert redacted_value not in diagnostic
    assert "discarded-prefix" not in diagnostic


@pytest.mark.parametrize(
    ("redactions", "emitted", "expected"),
    [
        (
            ("review-user", "review-user-private-suffix"),
            "review-user-private-suffix",
            "<redacted>",
        ),
        (
            ("line-one\nline-two",),
            "before line-one\nline-two after",
            "before <redacted> after",
        ),
    ],
)
def test_process_monitor_redacts_overlapping_and_multiline_values(
    redactions: tuple[str, ...],
    emitted: str,
    expected: str,
) -> None:
    monitor = _ProcessMonitor.launch(
        [
            sys.executable,
            "-c",
            f"import sys; sys.stderr.write({emitted!r})",
        ],
        redactions=redactions,
    )
    try:
        returncode, diagnostic = monitor.wait(_TIMEOUT_SECONDS)
    finally:
        monitor.terminate()

    assert returncode == 0
    assert diagnostic == expected


def test_process_monitor_redacts_before_long_output_tail_truncation() -> None:
    redacted_value = "boundary-value-unique-tail"
    monitor = _ProcessMonitor.launch(
        [
            sys.executable,
            "-c",
            f"import sys; sys.stderr.write({redacted_value!r} * 100)",
        ],
        redactions=(redacted_value,),
    )
    try:
        returncode, diagnostic = monitor.wait(_TIMEOUT_SECONDS)
    finally:
        monitor.terminate()

    assert returncode == 0
    assert "unique-tail" not in diagnostic
    assert diagnostic
    assert set(diagnostic.split("<redacted>")) == {""}


def test_retained_cleanup_attempts_every_topic_and_reports_all_failures() -> None:
    attempts: list[str] = []

    class FailingPublisher:
        def publish(
            self,
            topic: str,
            payload: bytes,
            *,
            retain: bool = False,
        ) -> None:
            attempts.append(topic)
            if topic == "first/topic":
                raise AssertionError("first failure")
            if topic == "second/topic":
                raise RuntimeError("second failure")

    with pytest.raises(AssertionError) as error:
        _clear_retained_topics(
            FailingPublisher(),
            ("first/topic", "second/topic", "third/topic"),
            primary_error=None,
        )

    assert attempts == ["first/topic", "second/topic", "third/topic"]
    assert "first/topic: first failure" in str(error.value)
    assert "second/topic: second failure" in str(error.value)


def test_retained_cleanup_preserves_and_notes_primary_failure() -> None:
    attempts: list[str] = []

    class FailingPublisher:
        def publish(
            self,
            topic: str,
            payload: bytes,
            *,
            retain: bool = False,
        ) -> None:
            attempts.append(topic)
            raise AssertionError(f"cannot clear {topic}")

    primary_error = ValueError("primary failure")
    _clear_retained_topics(
        FailingPublisher(),
        ("first/topic", "second/topic"),
        primary_error=primary_error,
    )

    assert attempts == ["first/topic", "second/topic"]
    assert str(primary_error) == "primary failure"
    assert primary_error.__notes__ == [
        (
            "failed to clear retained integration-test topics: "
            "first/topic: cannot clear first/topic; "
            "second/topic: cannot clear second/topic"
        )
    ]


@pytest.mark.parametrize("has_primary_error", [True, False])
def test_resource_cleanup_preserves_primary_and_finishes_after_termination_failure(
    has_primary_error: bool,
) -> None:
    events: list[str] = []

    class FailingTerminator:
        def terminate(self) -> None:
            events.append("terminate")
            raise subprocess.TimeoutExpired(cmd="bridge", timeout=3.0)

    class RecordingObserver:
        started = True

        def publish(
            self,
            topic: str,
            payload: bytes,
            *,
            retain: bool = False,
        ) -> None:
            events.append(f"clear:{topic}")

        def close(self) -> None:
            events.append("close")

    primary_error = ValueError("primary failure") if has_primary_error else None
    if primary_error is None:
        with pytest.raises(AssertionError, match="process termination"):
            _cleanup_test_resources(
                FailingTerminator(),
                RecordingObserver(),
                ("first/topic", "second/topic"),
                primary_error=None,
            )
    else:
        _cleanup_test_resources(
            FailingTerminator(),
            RecordingObserver(),
            ("first/topic", "second/topic"),
            primary_error=primary_error,
        )
        assert str(primary_error) == "primary failure"
        assert primary_error.__notes__ is not None
        assert len(primary_error.__notes__) == 1
        assert "process termination" in primary_error.__notes__[0]

    assert events == [
        "terminate",
        "clear:first/topic",
        "clear:second/topic",
        "close",
    ]


def test_packaged_runtime_against_real_broker(
    tmp_path: Path,
) -> None:
    host, port, credentials = _environment()
    run_id = uuid4().hex
    bridge_client_id = f"smartenit-rescue-integration-{run_id}"
    topic_prefix = f"smartenit-rescue/integration/{run_id}"
    discovery_prefix = f"homeassistant-integration-{run_id}"
    status_topic = f"{topic_prefix}/home-assistant/status"
    discovery_topic = f"{discovery_prefix}/device/smartenit_rescue_{_DEVICE_ID}/config"
    bridge_availability_topic = f"{topic_prefix}/bridge/availability"
    device_availability_topic = f"{topic_prefix}/{_DEVICE_ID}/availability"
    command_topic = f"{topic_prefix}/{_DEVICE_ID}/1/on_off/command"
    state_topic = f"{topic_prefix}/{_DEVICE_ID}/1/on_off/state"
    confidence_topic = f"{topic_prefix}/{_DEVICE_ID}/1/on_off/confidence"
    cleanup_topics = (
        discovery_topic,
        bridge_availability_topic,
        device_availability_topic,
        command_topic,
        state_topic,
        confidence_topic,
        status_topic,
    )

    config_path = _write_config(
        tmp_path,
        host=host,
        port=port,
        client_id=bridge_client_id,
        topic_prefix=topic_prefix,
        discovery_prefix=discovery_prefix,
        status_topic=status_topic,
        credentials=credentials,
    )
    executable = Path(sys.executable).with_name("smartenit-rescue")
    assert executable.is_file(), "packaged smartenit-rescue CLI is not installed"

    observer = _Observer(
        f"smartenit-rescue-observer-{run_id}",
        credentials,
    )
    monitor: _ProcessMonitor | None = None
    try:
        observer.start(
            host,
            port,
            (f"{topic_prefix}/#", discovery_topic),
        )

        retained_seed_mark = observer.messages.mark()
        observer.publish(command_topic, b"ON", retain=True)
        _wait_for_message(
            observer,
            command_topic,
            b"ON",
            after=retained_seed_mark,
        )

        bridge_start_mark = observer.messages.mark()
        redactions: tuple[str, ...] = ()
        if credentials is not None:
            redactions = (credentials.username, credentials.passphrase)
        monitor = _ProcessMonitor.launch(
            [str(executable), "run", "--config", str(config_path)],
            redactions=redactions,
        )

        discovery = _wait_for_topic(
            observer,
            discovery_topic,
            after=bridge_start_mark,
            monitor=monitor,
        )
        assert discovery.qos == 1 and not discovery.retain
        document = json.loads(discovery.payload)
        switch_id = f"smartenit_rescue_{_DEVICE_ID}_1_on_off"
        assert document["components"][switch_id]["command_topic"] == command_topic
        assert document["components"][switch_id]["state_topic"] == state_topic
        assert document["components"][switch_id]["qos"] == 1
        assert document["components"][switch_id]["optimistic"] is False

        bridge_online = _wait_for_message(
            observer,
            bridge_availability_topic,
            b"online",
            after=bridge_start_mark,
            monitor=monitor,
        )
        device_online = _wait_for_message(
            observer,
            device_availability_topic,
            b"online",
            after=bridge_start_mark,
            monitor=monitor,
        )
        initial_state = _wait_for_message(
            observer,
            state_topic,
            b"OFF",
            after=bridge_start_mark,
            monitor=monitor,
        )
        initial_confidence = _wait_for_message(
            observer,
            confidence_topic,
            b"observed",
            after=bridge_start_mark,
            monitor=monitor,
        )
        assert bridge_online.qos == 1 and bridge_online.retain
        assert device_online.qos == 1 and device_online.retain
        assert initial_state.qos == 1 and initial_state.retain
        assert initial_confidence.qos == 1 and initial_confidence.retain

        retained_barrier_mark = observer.messages.mark()
        observer.publish(status_topic, b"online")
        _wait_for_message(
            observer,
            state_topic,
            b"OFF",
            after=retained_barrier_mark,
            monitor=monitor,
        )
        assert not [
            message
            for message in observer.messages.since(bridge_start_mark)
            if message.topic == state_topic and message.payload == b"ON"
        ], "retained startup command changed simulator state"

        on_mark = observer.messages.mark()
        observer.publish(command_topic, b"ON")
        on_command = _wait_for_message(
            observer,
            command_topic,
            b"ON",
            after=on_mark,
            monitor=monitor,
        )
        on_state = _wait_for_message(
            observer,
            state_topic,
            b"ON",
            after=on_mark,
            monitor=monitor,
        )
        on_confidence = _wait_for_message(
            observer,
            confidence_topic,
            b"observed",
            after=on_mark,
            monitor=monitor,
        )
        assert on_command.qos == 1 and not on_command.retain
        assert on_state.qos == 1 and on_state.retain
        assert on_confidence.qos == 1 and on_confidence.retain

        off_mark = observer.messages.mark()
        observer.publish(command_topic, b"OFF")
        off_command = _wait_for_message(
            observer,
            command_topic,
            b"OFF",
            after=off_mark,
            monitor=monitor,
        )
        off_state = _wait_for_message(
            observer,
            state_topic,
            b"OFF",
            after=off_mark,
            monitor=monitor,
        )
        off_confidence = _wait_for_message(
            observer,
            confidence_topic,
            b"observed",
            after=off_mark,
            monitor=monitor,
        )
        assert off_command.qos == 1 and not off_command.retain
        assert off_state.qos == 1 and off_state.retain
        assert off_confidence.qos == 1 and off_confidence.retain

        reconnect_mark = observer.messages.mark()
        _force_bridge_reconnect(
            host,
            port,
            bridge_client_id,
            credentials,
        )
        _wait_for_message(
            observer,
            discovery_topic,
            discovery.payload,
            after=reconnect_mark,
            monitor=monitor,
        )
        _wait_for_message(
            observer,
            bridge_availability_topic,
            b"online",
            after=reconnect_mark,
            monitor=monitor,
        )
        _wait_for_message(
            observer,
            state_topic,
            b"OFF",
            after=reconnect_mark,
            monitor=monitor,
        )

        reconnect_barrier_mark = observer.messages.mark()
        observer.publish(status_topic, b"online")
        _wait_for_message(
            observer,
            state_topic,
            b"OFF",
            after=reconnect_barrier_mark,
            monitor=monitor,
        )
        reconnect_messages = observer.messages.since(reconnect_mark)
        assert not [
            message
            for message in reconnect_messages
            if message.topic == state_topic and message.payload == b"ON"
        ], "retained command was applied during reconnect"
        _assert_no_topic(
            reconnect_messages,
            command_topic,
            description="bridge replayed a command during reconnect",
        )

        termination_mark = observer.messages.mark()
        monitor.send_signal(
            signal.SIGTERM,
            description="requesting graceful SIGTERM",
        )
        returncode, stderr = monitor.wait(_TIMEOUT_SECONDS)
        assert returncode == 0, stderr
        assert "ignored retained command" in stderr
        offline = _wait_for_message(
            observer,
            bridge_availability_topic,
            b"offline",
            after=termination_mark,
        )
        assert offline.qos == 1 and offline.retain
        _assert_no_topic(
            observer.messages.since(termination_mark),
            command_topic,
            description="bridge published a command during shutdown",
        )

        observer.publish(command_topic, b"", retain=True)
        retained_probe = _Observer(
            f"smartenit-rescue-retained-probe-{run_id}",
            credentials,
        )
        try:
            retained_probe.start(
                host,
                port,
                (
                    bridge_availability_topic,
                    device_availability_topic,
                    state_topic,
                    confidence_topic,
                    command_topic,
                    status_topic,
                ),
            )
            retained_offline = _wait_for_message(
                retained_probe,
                bridge_availability_topic,
                b"offline",
                after=0,
                retain=True,
            )
            retained_device_online = _wait_for_message(
                retained_probe,
                device_availability_topic,
                b"online",
                after=0,
                retain=True,
            )
            retained_state = _wait_for_message(
                retained_probe,
                state_topic,
                b"OFF",
                after=0,
                retain=True,
            )
            retained_confidence = _wait_for_message(
                retained_probe,
                confidence_topic,
                b"observed",
                after=0,
                retain=True,
            )
            retained_probe.publish(status_topic, b"probe-complete")
            _wait_for_message(
                retained_probe,
                status_topic,
                b"probe-complete",
                after=0,
                retain=False,
            )
            assert retained_offline.qos == 1
            assert retained_device_online.qos == 1
            assert retained_state.qos == 1
            assert retained_confidence.qos == 1
            _assert_no_topic(
                retained_probe.messages.since(0),
                command_topic,
                description="late subscriber received a retained command",
            )
        finally:
            retained_probe.close()
    finally:
        primary_error = sys.exception()
        _cleanup_test_resources(
            monitor,
            observer,
            cleanup_topics,
            primary_error=primary_error,
        )
