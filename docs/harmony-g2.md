# Harmony G2 local backend

The Harmony G2 backend is an experimental, LAN-only path for controlling exactly
configured Smartenit loads through an existing gateway. A load may be a heater, lamp,
pump, fan, or another appropriate device. The backend does not turn Smartenit Rescue
into an automation platform; Home Assistant or another MQTT consumer still owns the
user interface and automations.

```text
Home Assistant buttons
        |
        v
MQTT broker -> Smartenit Rescue -> pinned HTTPS -> Harmony G2 -> controller -> load
                    |                    |
                    |                    `-- exact EUI-64 and component binding
                    `-- accepted outcome, availability, and last-command diagnostic
```

The backend has no vendor-cloud fallback. It talks only to the configured private IPv4
literal, checks the enrolled certificate fingerprint before sending a credential, and
permits only the local routes needed for renewal, inventory, and explicit On or Off.
Redirects, hostnames, public addresses, unknown routes, and unknown response shapes
fail closed.

## Prerequisites and limitations

### Can I use the G2 backend today?

Before attempting setup, confirm that you have **all** of the following:

- A reachable G2 on the local network and a local session that already exists and can
  be renewed without the cloud.
- An independently verified certificate fingerprint for that G2.
- The exact EUI-64 and G2 component ID for each controller you intend to expose.
- A private writable directory for the rotating session file.

An email and password for the former Smartenit app are not enough. This software
cannot log in to create the first local G2 session, extract one from a gateway, or
recover one from the cloud. Without a usable existing session, the G2 backend cannot
control a device today. The planned replacement-coordinator route is separate and
does not require the G2, but it is not implemented in this release.

If the retained local refresh path stops working, a separately obtained local session
or a different Zigbee coordinator is required.

The current public proof covers the 4040C On/Off path through generated protocol
fixtures. Its Harmony G2 support label remains `experimental` until the sanitized
real-hardware checklist below passes. Metering readback and other controller models are
not implied by this support.

## Non-runnable configuration shape

The following TOML is deliberately non-runnable. Every angle-bracket value is a
sanitization marker that the operator must replace in a private configuration file.

```toml
[runtime]
client_id = "<mqtt-client-id>"
topic_prefix = "<mqtt-topic-prefix>"
discovery_prefix = "homeassistant"
home_assistant_status_topic = "homeassistant/status"

[mqtt]
host = "<mqtt-host>"
port = 1883
keepalive_seconds = 60
tls = false
username_file = "<mqtt-username-file>"
password_file = "<mqtt-password-file>"

[backend]
type = "harmony_g2"
host = "<private-ip-address>"
certificate_sha256_file = "<certificate-pin-file>"
session_file = "<writable-session-file>"

[[backend.devices]]
device_id = "<canonical-eui64>"
name = "<load-name>"
profile_id = "smartenit.4040c"
component_id = "<exact-g2-component-id>"
```

The display name describes the attached load. Matching never uses that name, a device
ID suffix, inventory order, or a guessed component. Startup succeeds only when every
configured EUI-64/component pair has one exact inventory match with an `OnOff`
processor.

## Local session file

The session file has exactly three keys. This example also uses markers and is not a
usable credential:

```json
{
  "access_token": "<local-access-token>",
  "expires_at": "<rfc3339-utc-expiry>",
  "refresh_token": "<local-refresh-token>"
}
```

On POSIX systems, the immediate parent directory must be a real directory with mode
`0700`; the session must be a real regular file with mode `0600`. Symbolic links and
group- or world-accessible modes are rejected. Renewal writes a same-directory
temporary file, flushes it, atomically replaces the session, and flushes the directory.
The state path must therefore remain writable for the lifetime of the service. A
read-only container secret cannot be used as the rotating session file.

Tokens, local component IDs, the gateway address, pin, paths, and raw HTTP bodies are
excluded from normal representations and bounded errors. Keep the session and private
configuration out of the repository, support requests, screenshots, and logs.

## Certificate enrollment

Obtain the G2 certificate fingerprint while connected directly to the trusted local
network. Verify the SHA-256 value independently—for example, by comparing a second
capture from a separately trusted local machine or a local administrative display.
Do not accept a fingerprint learned only through the same untrusted path it is meant
to protect.

Store exactly 64 hexadecimal SHA-256 characters, optionally shown as colon-separated
byte pairs, in the configured pin file. Re-enroll deliberately after a verified
gateway certificate change. A mismatch prevents every HTTP request, including the
Authorization header and session-renewal body.

## Home Assistant behavior

A command-only On/Off capability creates **Turn on** and **Turn off** buttons, device
availability, and a **Last command** diagnostic. It does not create a switch or an
On/Off state topic because the known G2 acknowledgement is not physical state. An
`accepted` diagnostic means only that the G2 explicitly acknowledged the request.
Confirm the physical load through appropriate local evidence when safety matters.

The diagnostic is retained for visibility, but it is not a queued instruction.
Startup, reconnect, Home Assistant birth, and service restart publish discovery and
availability without executing it. There is no automatic retry after an ambiguous
response. MQTT QoS 1 can redeliver a newly published command, so consumers should use
explicit On and Off actions and avoid treating delivery as exactly once.

If a G2 request fails, availability for every affected configured load goes offline.
The bridge rechecks the local session and exact inventory at most once per minute and
restores availability when that check succeeds. Recovery does not replay a command or
turn a load on or off.

## Bounded startup and command errors

- `externally provisioned local session is required`: the configured session file is
  absent; supply a valid private session before starting.
- `certificate pin file is invalid`: the pin is missing, malformed, non-ASCII, too
  large, or not a regular file.
- `Harmony G2 backend initialization failed`: permissions, renewal, inventory, profile,
  or exact binding validation failed. Inspect only private operator-side records; do not
  copy raw sessions or inventory into an issue.
- `rejected`: validation or an explicit refusal established that the command was not
  safely accepted.
- `indeterminate`: request bytes may have been sent, but the response did not establish
  the outcome. Inspect the load locally and issue a fresh explicit action if needed.

## Sanitized hardware-acceptance checklist

Run this only with direct supervision and an attached load that can be operated safely.
Keep the notes private until every identifier and operational detail has been removed.

1. Block internet access for both the bridge process and G2 while preserving their LAN
   path; confirm that no external DNS lookup or connection occurs.
2. Start with a due local session and verify that renewal atomically rotates the private
   file before inventory access.
3. Verify every configured EUI-64/component binding exactly, without friendly-name or
   suffix matching.
4. Confirm Home Assistant shows Turn on, Turn off, Availability, and Last command, with
   no stateful switch.
5. Establish an Off baseline on one low-risk load, send On briefly, verify the physical
   result locally, then send Off and verify the final physical result.
6. Interrupt one response after submission and confirm one `indeterminate` outcome with
   no automatic retry.
7. Restart the service and reconnect MQTT; confirm that neither action replays a prior
   command.
8. Finish with every test load explicitly Off and verify that state physically.

Only after that complete result may a specific model/capability row advance from
`experimental` to `hardware_verified`. Other controllers require their own evidence,
profile, binding, command, and safety verification.
