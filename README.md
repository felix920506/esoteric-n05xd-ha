# Esoteric RS-232C for Home Assistant

A Home Assistant custom integration, installed through [HACS](https://hacs.xyz), for
controlling Esoteric audio equipment over its RS-232C port. It connects through a
network serial server (`socket://` or RFC 2217) or a local serial port.

It is built from the official *Esoteric RS-232C command table, Rev 1.5 (2024/9/30)*.

> [!WARNING]
> **Hardware testing status**
>
> - **N-05XD**: the only model the maintainer owns. Status requests, volume, STOP and
>   input stepping are confirmed on a real unit (see
>   [N-05XD hardware results](#n-05xd-hardware-results)). Power on/off and playback
>   keys other than STOP haven't been sent to a real unit yet.
> - **All other models** (Grandioso C1X / C1X solo / E1, K-01XD / K-03XD / K-05XD,
>   F-01 / F-02): written from the command table alone and **never tested on real
>   hardware**. They may not work, or only partly. Bug reports with
>   [diagnostics](#reporting-problems) are very welcome.

## Installation

1. In HACS, open the menu, choose **Custom repositories**, and add this repository's
   URL with type **Integration**.
2. Install **Esoteric RS-232C** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Esoteric RS-232C**.

## Configuration

| Field      | Description |
|------------|-------------|
| Name       | Optional. Defaults to "Esoteric &lt;model&gt;". |
| Connection | Where the unit's RS-232C port is. See [Connection strings](#connection-strings). |
| Model      | Your unit. This sets which entities and commands are offered. Pick **Other / generic** for a model that isn't listed. |
| Baud rate  | Esoteric units use **9600**, so leave this at 9600. It is only sent for `rfc2217://` and local ports. |

Under **Configure** you can change the polling interval (default 10 s) and the
response timeout (default 1 s). **Reconfigure** changes the connection, model or baud
rate.

### Connection strings

| Connection string | Use it for |
|-------------------|------------|
| `socket://192.168.1.50:4001` | A network serial server in raw TCP mode, sometimes called "TCP server" or "Real COM off". The serial settings (**9600 8N1, no flow control**) have to be set in the server's own settings page. Also accepts `tcp://host:port` or plain `host:port`. |
| `rfc2217://192.168.1.50:4001` | A serial server that supports **RFC 2217** (Telnet COM Port Control). This integration sends 9600 8N1 to the server on every connect, so the server needs no setup. Use it when the server ignores its own baud setting and expects the client to send one. |
| `/dev/ttyUSB0`, `/dev/serial/by-id/...`, `COM3`, `serial:///dev/ttyS1` | A serial port on the Home Assistant host itself, such as a USB-RS232 adapter. Opened at the configured baud rate, 8N1. |

#### About "the client has to send the baud rate"

Raw TCP (`socket://`) has no way to carry a baud rate. pyserial's `socket://`
handler accepts a baud rate argument but does nothing with it: it logs "ignored port
configuration change" and sends nothing to the server. So
`miniterm socket://ip:port 115200` puts the same bytes on the wire as
`miniterm socket://ip:port`.

There are two standard ways for a client to set the baud rate on a serial server:

- **RFC 2217.** Use `rfc2217://host:port`. Many servers (Moxa, Lantronix, ser2net,
  ESP-Link, USR, and others) have an RFC 2217 mode, sometimes on a separate port.
- **The server's own settings page.** Then use `socket://`.

Some low-cost servers use their own in-band "set baud" packets, such as USR's
"Similar RFC2217". These aren't supported yet. If your server needs one, open an
issue with the make and model. The transport layer has a backend registry
(`transport.register_backend`), so new kinds of servers can be added.

#### RS-232 wiring

The unit's port is DTE-style: pin 2 = TXD, pin 3 = RXD, pin 5 = GND. Depending on
your serial server, you may need a null-modem (crossover) cable.

### Shared serial ports (several clients)

Some serial servers let more than one TCP client connect to the same port, and send
everything the unit transmits to all of them. This integration is built for that:

- Every status line the unit sends is applied to Home Assistant, even when another
  client asked for it. Other clients' traffic keeps the entities up to date.
- A request counts as answered as soon as the matching status line arrives, whoever
  asked for it.
- ACK/NAK bytes say nothing about which client they belong to. A stray ACK/NAK is
  ignored when nothing of ours is waiting. If one arrives while one of our commands
  is waiting, it is accepted as our answer.
- Echoed client commands (`@?INPUT`, `@KEY 01`, `@POWER ON`) are ignored, for
  servers that echo them back.
- A request is only treated as unsupported after three NAKs in a row while the unit
  is answering other requests, so another client's NAK doesn't disable it.

Many serial servers allow only **one** client per port instead. The STE 8-port
server used for testing accepts a second connection and then resets it straight
away. The config flow reports this as "port busy". At runtime, the integration
keeps retrying with backoff until the port frees up.

Clients on a shared port can still interleave commands. The spec asks for at least
20 ms between commands. This integration keeps to that for its own commands, but it
can't control what other clients send.

## Supported models and entities

| Model | Command groups | Media player | Volume | Input select |
|-------|----------------|--------------|--------|--------------|
| N-05XD | Common, Network/DAC | power, play/pause/stop/next/prev, repeat, shuffle | 0–100.0 in 0.5 steps (`@VOLUME`) | by stepping INPUT+ through NET, Bluetooth, LINE1, LINE2, XLR, COAX1, COAX2, OPT1, OPT2, USB |
| K-01XD / K-03XD / K-05XD | Common, CD | power, transport, repeat, shuffle | – | – |
| Grandioso C1X / C1X solo | Common, AMP | power | 0–99.9 (KEY up/down, `@VOLUME`) | direct (XLR1-3, ESLA1-3, RCA1-2) |
| Grandioso E1 | Common, Phono EQ | power | – | direct (XLR1-3, RCA, OPT) |
| F-01 / F-02 | Common, AMP | power | 0–99.9 (assumed) | by stepping INPUT |
| Other / generic | Common | power | – | – |

Each unit gets these entities:

- **Media player**: power, playback state, track number and elapsed time (when the
  unit reports Track Elapsed), volume, source, repeat and shuffle, depending on the
  model.
- **Sensors**: one per status request the model supports, such as Input, Analog or
  Digital output, Sample rate, Upconversion, MQA, Codec, Media (with total tracks
  and time), Play mode, Phono output, EQ curve, Gain and Cartridge gain. Each sensor
  has a `raw` attribute with the last line received.
- **Number**: volume in the unit's own steps, for models with volume control.
- **Buttons**: every `@KEY` code the command table lists for the model's groups,
  such as Dimmer, Tray, Display, Mute toggle, Next input, Menu, and the phono EQ
  keys. Rarely used keys (digits, cursor, setup) start out disabled.

### Things the protocol does not provide

- **No power query.** Power state is inferred. If the unit answers any status
  request, it is considered on. If it NAKs or ignores the first few, it is
  considered in standby. While in standby, only a few requests are sent per poll.
  This still has to be checked against real hardware.
- **No mute state.** Mute is a toggle button only.
- **No input list query.** Models without direct input select (N-05XD, F-01/02)
  change source by pressing INPUT+ until the unit reports the input you asked for.
  The N-05XD's input list is built in. For F-01/02, the list fills in as inputs are
  seen (from the remote or the Next input button) and is kept across restarts.
- **Requests can be state-dependent.** A request that gets three NAKs in a row is
  polled much less often (once every 30 polls), not dropped for good.
- **Repeat and shuffle are toggle keys.** Setting them presses the key until the
  unit reports the requested mode.

## N-05XD hardware results

These were tested remotely on a real N-05XD through an STE 8-port serial server
(`socket://`, 9600 8N1 set on the server). Nobody could see the unit. It was
stopped on the NET input, and its menu settings were unknown. Replies took
60–90 ms over the network.

### Matches the command table

| Command | Result |
|---------|--------|
| `?INPUT` `?AOUT` `?PSTS` `?PMODE` `?REPEAT` `?MQA` `?VOLUME` | Answered. |
| `?MEDIA` `?OUTPUT` `?EQ` `?GAIN` `?CGAIN` | NAK. These are documented only for CD players and phono EQs. |
| `VOLUME 55.0` / `VOLUME 55.5` | ACK, and the unit reads back the new value. |
| `KEY 28` (STOP) | ACK. When already stopped, the reported track resets to 0. |
| `KEY 20` (INPUT+) | ACK. Steps through NET, Bluetooth, LINE1, LINE2, XLR, COAX1, COAX2, OPT1, OPT2, USB. |
| `INPUT <name>` (direct select) | NAK. Direct select is documented only for C1X, C1X solo and E1, so the N-05XD changes source by stepping INPUT+. |

### Differences from the command table (to confirm on site)

The unit didn't match the command table in the cases below. These may depend on
the source, the playback state or menu settings (for example, repeat and shuffle
make no sense on AirPlay), so **the integration keeps the documented behavior**.
Each one is tagged in the code with its `DOC-MISMATCH-xx` code (`grep -rn
DOC-MISMATCH`), so the code can be updated once it's confirmed on the unit.

| Code | Command | Command table says | N-05XD did (stopped, NET) | Integration behavior now |
|------|---------|--------------------|---------------------------|--------------------------|
| `DOC-MISMATCH-01` | `?DOUT` | Common request (digital output) | NAK | Polled as documented. After 3 NAKs in a row the sensor shows unavailable, and the request is retried every 30 polls. |
| `DOC-MISMATCH-02` | `?UPCONV` | Network/DAC request | NAK | Same as above. |
| `DOC-MISMATCH-03` | `KEY 29` (REPEAT) | Network/DAC key | NAK | Repeat can be set from the media player. A NAK shows as an error. |
| `DOC-MISMATCH-04` | `KEY 2A` (SHUFFLE) | Network/DAC key | NAK | Shuffle can be set from the media player. A NAK shows as an error. |
| `DOC-MISMATCH-05` | `?CODEC`, `?FS` | Answer carries a codec / sampling frequency | `@CODEC` with no value, `@FS NON` | An empty value shows as unknown, and `NON` is shown as-is. |

The CD-group REPEAT/SHUFFLE codes (`KEY 47`, `KEY 1F`) were also NAKed. That's
expected, since they aren't documented for the N-05XD.

On site, try each `DOC-MISMATCH` item:
1. While playing from a local source (USB or NET library), not AirPlay or
   Bluetooth.
2. With the RS-232C-related menu settings checked.

`esoteric.send_command` (below) is the easiest way to do this.

Not tested yet: POWER ON/OFF and how the unit behaves in standby, PLAY, PAUSE,
next/previous, Dimmer, Menu and the cursor keys.

## Raw commands: `esoteric.send_command`

This action is for testing and for commands the integration doesn't cover. Leave
out the leading `@` and the trailing CR.

```yaml
action: esoteric.send_command
data:
  config_entry_id: <your entry>
  command: "?INPUT"        # or "KEY 5A", "VOLUME 30.0", "POWER ON"
response_variable: reply   # {"result": "response", "response": "@INPUT USB"}
```

`result` is `ack`, `nak`, or `response`. A `response` result carries the status line
the unit sent back.

## Reporting problems

Please include:

1. **Diagnostics**: Settings → Devices & services → Esoteric → ⋮ → *Download
   diagnostics*. This has the last line received for every request, which requests
   were found unsupported, and counters for ACK, NAK, timeouts and reconnects.
2. **Debug logs**, with every byte sent and received:

   ```yaml
   logger:
     logs:
       custom_components.esoteric: debug
   ```

## Development

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
```

The tests run against a simulated unit (`tests/fake_device.py`). It is a shared-port
TCP server with optional RFC 2217 negotiation. The local serial backend is tested
through a pseudo-terminal. No real hardware is needed.

Code layout:

| File | Purpose |
|------|---------|
| `transport.py` | Pluggable byte transports: TCP, RFC 2217, local serial, plus `register_backend()`. Has no Home Assistant imports. |
| `protocol.py` | Framing, ACK/NAK handling, request matching, and the reconnect loop. Has no Home Assistant imports. |
| `models.py` | Per-model capabilities and key codes, taken from the command table. |
| `coordinator.py` | Polling, push updates, standby detection, and higher-level actions. |

## Disclaimer

This is an unofficial project, not affiliated with or endorsed by Esoteric Company or
TEAC Corporation.
