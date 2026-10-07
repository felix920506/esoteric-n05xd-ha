# Esoteric RS-232C for Home Assistant

A Home Assistant custom integration, installed through [HACS](https://hacs.xyz), for
controlling Esoteric audio equipment over its RS-232C port. It connects through a
network serial server (`socket://` or RFC 2217) or a local serial port.

It is built from the official *Esoteric RS-232C command table, Rev 1.5 (2024/9/30)*.

> [!WARNING]
> **Hardware testing status**
>
> - **N-05XD**: the only model the maintainer owns. It has been tested on a real unit,
>   including power, standby, volume, playback, input, dimmer and menu keys (see
>   [N-05XD hardware results](#n-05xd-hardware-results)). A few commands behave
>   differently from the command table; those are tagged `DOC-MISMATCH-xx` until
>   confirmed.
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

Tested on a real N-05XD through an STE 8-port serial server (`socket://`, 9600 8N1
set on the server). The first round, on 2026-10-05, was remote with the unit
stopped on NET. The second, on 2026-10-07, was on site with a camera on the front
panel and included AirPlay playback. Replies took 30–150 ms over the network.

### Matches the command table

| Command | Result |
|---------|--------|
| `?INPUT` `?AOUT` `?PSTS` `?PMODE` `?REPEAT` `?FS` `?CODEC` `?MQA` `?VOLUME` | Answered. During AirPlay: `@FS 44.1kHz`, `@CODEC AAC`. |
| `?MEDIA` `?OUTPUT` `?EQ` `?GAIN` `?CGAIN` | NAK. These are documented only for CD players and phono EQs. |
| `?DOUT` `?UPCONV` | NAK. Listed for this model's command groups, but the N-05XD has no digital output and no upconversion setting (`DOC-MISMATCH-01`, `-02`). |
| `POWER OFF` | ACK in about 30 ms. |
| `VOLUME 54.0` | ACK. The display shows "Volume 54.0", and the readback matches. |
| `KEY 26` / `27` / `28` / `24` / `25` (play, pause, stop, previous, next) | ACK. Confirmed audibly on AirPlay. |
| `KEY 20` (INPUT+) | ACK. Steps through NET, Bluetooth, LINE1, LINE2, XLR, COAX1, COAX2, OPT1, OPT2, USB. |
| `KEY 5A` (Dimmer) | ACK. Cycles display brightness, shown as "DIMMER n". |
| `KEY 21` (Menu), `KEY 22` / `23` (< >) | ACK. See the notes below. |
| `INPUT <name>` (direct select) | NAK. Direct select is documented only for C1X, C1X solo and E1, so the N-05XD changes source by stepping INPUT+. |

### Not covered by the command table

- **Standby.** The unit still answers every request. It reports `@INPUT OFF` and
  `@AOUT OFF`, and default values for the rest (`PMODE CONTINUE`, `REPEAT OFF`).
  Commands are still ACKed. The integration treats `INPUT OFF` as standby. For
  other models it still assumes standby when the unit NAKs or ignores requests.
- **Power-on time.** After `POWER ON`, the display shows "Initialize" for about
  30 s. Requests are answered straight away.
- **Menu keys.** MENU (`KEY 21`) steps through setup items (`CLK`, `VOLDP`, …)
  instead of opening and closing the menu. **< and > change the shown setting
  immediately.** The menu closes on its own after a few seconds. The menu and
  cursor buttons are disabled by default in Home Assistant for this reason.
- **Volume display.** The menu setting `VOLDP> STEP` matches the volume being
  reported in steps.

### Differences from the command table

These may depend on the source, the playback state or menu settings (for
example, repeat and shuffle make no sense on AirPlay). So **the integration keeps
the documented behavior** unless the table below says otherwise. Each one is
tagged in the code with its `DOC-MISMATCH-xx` code (`grep -rn DOC-MISMATCH`).

| Code | Command | Command table says | N-05XD did | Integration behavior now | Status |
|------|---------|--------------------|------------|--------------------------|--------|
| `DOC-MISMATCH-01` | `?DOUT` | Common request (digital output) | NAK, both idle and during AirPlay | Not polled on the N-05XD, and no sensor. | **Resolved.** The N-05XD has no digital output. |
| `DOC-MISMATCH-02` | `?UPCONV` | Network/DAC request | NAK, both idle and during AirPlay | Not polled on the N-05XD, and no sensor. | **Resolved.** The N-05XD's menu has no upconversion setting. |
| `DOC-MISMATCH-03` | `KEY 29` (REPEAT) | Network/DAC key | NAK, both idle and during AirPlay | Repeat can be set from the media player. A NAK shows as an error. | Open. Test on a source that supports repeat. |
| `DOC-MISMATCH-04` | `KEY 2A` (SHUFFLE) | Network/DAC key | NAK, both idle and during AirPlay | Same as above. | Open, as above. |
| `DOC-MISMATCH-05` | `?CODEC`, `?FS` | Reply carries a codec / sampling frequency | `@CODEC` with no value and `@FS NON`, but only while idle | An empty value shows as unknown. | **Resolved.** Normal values while playing. |
| `DOC-MISMATCH-06` | `POWER ON` | ACK (`0x06`) | Replies `0x83` instead whenever it actually powers on. | If no ACK arrives, the integration reads INPUT back and treats the command as successful when the unit reports itself on. | Handled |
| `DOC-MISMATCH-07` | `?PSTS` | Play status | Stays `PLAY 0 0 00 TE` while AirPlay is paused (so does the display), with no track number or time | Shown as reported. | Open. Expected to be AirPlay-specific. |
| `DOC-MISMATCH-08` | `POWER ON` | ACK means the command was received | Within about 6–8 s of `POWER OFF`, it's ACKed but ignored, and the unit stays in standby | After POWER ON, the integration reads INPUT back and re-sends POWER ON for up to 15 s while the unit still reports `OFF`. Measured on the unit: 8.8 s from turn-off to on. | Handled |

The CD-group REPEAT/SHUFFLE codes (`KEY 47`, `KEY 1F`) were also NAKed. That's
expected, since they aren't documented for the N-05XD.

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
