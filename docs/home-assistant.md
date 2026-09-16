# Home Assistant

Start here to prove the bridge-to-Home Assistant path without sending a command to a
real controller. The reserved synthetic 4040C-compatible device verifies MQTT state
and discovery. The later Harmony G2 section describes a separate, experimental
command-only presentation; it does not establish physical-device compatibility.

## Run the synthetic proof

You need a reachable MQTT v5 broker, Home Assistant with the standard MQTT
integration, and [uv](https://docs.astral.sh/uv/getting-started/installation/) on the
machine running this bridge. Home Assistant and the bridge must connect to the same
broker. The broker and Home Assistant are external services; this repository does
not install either one. Home Assistant's [MQTT setup guide](https://www.home-assistant.io/integrations/mqtt/)
explains broker selection, adding the integration, and additional broker logins.

1. In Home Assistant, open **Settings > Devices & services > Add Integration** and
   choose **MQTT**. Connect it to your broker. Keep MQTT discovery enabled with its
   default `homeassistant` prefix for this example. If Home Assistant runs in a
   container or on another host, its `localhost` is not the bridge host; use an
   address each service can reach.
2. From a fresh checkout of this repository, prepare an ignored private config and
   install the bridge dependencies. If `operator-state/config.toml` already exists,
   do not run the copy command below; use a separate checkout or private test
   directory instead.

   ```sh
   install -d -m 0700 operator-state
   install -m 0600 config.example.toml operator-state/config.toml
   uv sync
   ```

3. Edit `operator-state/config.toml`. Set `mqtt.host` and `mqtt.port` to the broker
   address and port as seen from the bridge process. Leave the simulator backend,
   reserved device ID, and `initial_on = false` unchanged. For an authenticated
   broker, create `operator-state/mqtt-username` and
   `operator-state/mqtt-password` with your local editor, each containing its own
   nonblank value. In the private config, set `username_file` to the quoted relative
   path `mqtt-username` and `password_file` to the quoted relative path
   `mqtt-password`.

   The paths resolve relative to `operator-state/config.toml`. Restrict both files
   with `chmod 600 operator-state/mqtt-username operator-state/mqtt-password`.
   For an intentionally unauthenticated local broker, remove **both** settings
   instead. Never put secret values in TOML, shell arguments, or logs.
4. Start the foreground bridge and leave this terminal open:

   ```sh
   uv run smartenit-rescue run --config operator-state/config.toml
   ```

5. In Home Assistant, open the MQTT integration's devices and find **Synthetic
   Load**. It should have a switch initially showing **Off** and a confidence
   diagnostic. Turn the synthetic switch **On**, then **Off**. The switch should
   follow each observed state. This exercises only the in-memory simulator.

Press Control-C to stop the bridge gracefully. For a continuously running service,
see the [operations guide](operations.md). Its Docker example reads the config
and credential files from a private directory outside the checkout, while
`operator-state` holds only writable runtime state. The credential-file paths
inside the container must be `/run/secrets/mqtt_username` and
`/run/secrets/mqtt_password`.

## Broker boundary

Configure broker authentication and ACLs before exposing it outside a trusted local
environment. The bridge needs permission to subscribe to its exact command topics and
`homeassistant/status`, and to publish its discovery, state, confidence, and
availability topics. Use TLS whenever broker traffic crosses an untrusted network.
Keep the username and password in separate operator-owned files, never in TOML,
command-line arguments, or logs. The reserved synthetic device ID is
`0200000000000001`. Availability depends on both the bridge and the synthetic
device being online. Broker configuration, accounts, certificates, and ACLs remain
outside Smartenit Rescue.

## Exercise explicit desired state

Turn the synthetic switch on, then off. Home Assistant sends literal, non-retained
`ON` and `OFF` commands, and the displayed switch state follows the authoritative
state topic rather than changing optimistically.

State and confidence are retained at QoS 1. Home Assistant may process a discovery
document before it finishes subscribing to the new entities' state topics; retaining
the latest observation ensures that later subscription still receives it. Retained
bridge and device availability gate that observation, so stored state is not treated
as usable while either availability topic is offline.

Commands use MQTT QoS 1, which can deliver duplicates. Repeated `ON` and `OFF` values
are safe because they request idempotent desired state. The bridge does not retry a
received command, and MQTT does not provide an exactly-once physical-execution
guarantee. A retained command is rejected.

## If the synthetic device does not appear

- Check that Home Assistant and the bridge use the same broker and that discovery is
  enabled with the `homeassistant` prefix. Restart the bridge after correcting it.
- Check the bridge terminal for a connection or authentication error. For an
  authenticated broker, confirm that both credential files exist, are nonblank, and
  are readable by the bridge user.
- If the device appears as unavailable, verify broker connectivity and leave the
  bridge running. A retained state alone does not make an offline device available.
- If you see **Turn on** and **Turn off** buttons instead of a switch, you are using
  the G2 command-only backend, not the synthetic configuration in this recipe.

## Harmony G2 command-only entities

Complete the private setup in the [Harmony G2 guide](harmony-g2.md) before starting
this backend. It requires an externally provisioned local session, an independently
verified certificate pin, and exact controller/component mappings; cold bootstrap is
not supported.

Each configured On/Off load appears with **Turn on** and **Turn off** buttons,
Availability, and a **Last command** diagnostic. It intentionally has no stateful
switch, state topic, or confidence entity. The G2 acknowledgement can establish an
`accepted` command outcome, but it is not physical state. Availability means the
authenticated local binding was recently validated; it does not mean the attached
load is On or Off.

The last-command diagnostic records `accepted`, `rejected`, or `indeterminate` with
the requested action and time. It is retained only for visibility. Reconnect, restart,
and Home Assistant birth republish discovery and availability without executing that
history. There is no automatic retry after a timeout or disconnect; inspect the load
locally before deciding whether to send a fresh explicit action.

## Restart and availability checks

Run each check independently and confirm that no switch action occurs by itself:

1. **Home Assistant restart:** leave the bridge running and restart Home Assistant.
   Its `homeassistant/status` birth message causes discovery, availability, state, and
   confidence to be republished. The switch should return without a command replay.
2. **Broker restart:** restart only the broker. The bridge reconnects with a clean
   session and republishes its snapshot. Offline commands are not queued for the
   bridge, and reconnect does not create a command.
3. **Abrupt bridge stop:** run the bridge outside a restarting service manager, then
   terminate that process abruptly. The broker publishes the retained bridge last
   will, and Home Assistant should mark the device unavailable. Start the bridge again
   and confirm availability and state are republished without changing the switch.
4. **Graceful stop:** send `SIGINT` or `SIGTERM`. The bridge publishes retained
   `offline`, closes its backend once, disconnects, and exits without a command.

## Verified synthetic workflow

The reserved simulator workflow was verified with Home Assistant's standard MQTT
integration. The synthetic switch and confidence diagnostic appeared, and explicit
On then Off actions produced matching authoritative state. The entities recovered
after a Home Assistant process restart and after a separate broker restart.

An abrupt bridge stop made the entities unavailable through the retained last will.
Starting the bridge again restored availability, the latest Off state, and observed
confidence. Neither restart produced a command replay. This verifies only the
synthetic MQTT workflow; it is not evidence of physical-device or gateway support.

## Clean up the synthetic entity

Stop the bridge while Home Assistant remains connected to the broker. Using the same
broker connection and authentication options as the proof, publish an empty retained
payload to the exact discovery topic. The commands below assume an unauthenticated
broker on `localhost:1883`. For another broker, configure the MQTT client for its
host, port, and authentication without putting a password on the command line:

```sh
mosquitto_pub -r -n -t "homeassistant/device/smartenit_rescue_0200000000000001/config"
```

Confirm that Home Assistant removes the synthetic device and its entities before
continuing. Then clear the exact retained availability, state, and confidence topics
created by this example:

```sh
mosquitto_pub -r -n -t "smartenit-rescue/v1/0200000000000001/availability"
mosquitto_pub -r -n -t "smartenit-rescue/v1/bridge/availability"
mosquitto_pub -r -n -t "smartenit-rescue/v1/0200000000000001/1/on_off/state"
mosquitto_pub -r -n -t "smartenit-rescue/v1/0200000000000001/1/on_off/confidence"
```

Discovery is non-retained in normal bridge operation, but its exact tombstone above
also removes a retained config left by an earlier or manual publisher. Do not publish
an empty retained payload to a wildcard, an unrelated discovery or availability
prefix, or an unrelated state or confidence topic. Finally, remove the example
configuration and its operator-owned secret files.
