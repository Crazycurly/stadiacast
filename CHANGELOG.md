# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- A built-in credential: the receiver now starts with no certificate configured and serves a
  Google Trust Services chain that the controller accepts, issued for a domain dedicated to the
  project and revoked before publication. `--cert`/`--key` and a pair under `certs/` take
  precedence, so forks and hosted deployments keep serving their own; `tools/pack_credential.py`
  builds the bundle.
- Headset audio: Opus frames reach the controller's headset jack as SRTP on the media 5-tuple,
  gated by `SetAudioMode(mode=2)` and paced on a 20 ms grid with their own SSRC and sequence
  numbers. `--audio-source` picks what ffmpeg encodes: `tone`, `file:PATH`, `system` for what
  the computer plays (the `audio` extra: macOS through PortAudio and a virtual output such as
  BlackHole, Windows through the WASAPI loopback, Linux through the PulseAudio monitor,
  untested) or `device:NAME` for one capture device (`--audio-devices` lists them).
  `--audio-bitrate`, `--audio-application {lowdelay,audio}` and `--audio-volume` set the
  encoder and the gain, `--audio-follow-volume {on,off}` whether the Windows volume slider and
  mute reach the headset, and `/_audio` starts, stops and adjusts the stream at run time. A
  front end with its own encoder starts the pacer alone (`ControllerSession.audio_start(None)`)
  and feeds it Opus frames (`audio_feed()`).
- Standalone executables for Windows, macOS (Apple silicon) and Linux attached to every release:
  `stadiacast` and `stadiacast-provision` as single files built with PyInstaller by the Release
  workflow (`make exe` builds them locally; the receiver re-launches itself for its DNS and
  SNTP helpers when frozen).
- A virtual gamepad on Windows: `--gamepad vigem` turns each controller into an Xbox 360 pad
  through the ViGEmBus driver (the `gamepad` extra), with the stick deadzones of XInput
  (`--gamepad-deadzone`), the game's rumble forwarded to the controller, and one pad per
  controller; `/_status` lists them under `gamepads`.
- The battery level, headset detection and the controller's hardware description, in
  `/_status` and as the `controller_battery`, `controller_audio_config` and
  `controller_hardware` events.

### Changed
- `docs/protocol.md` and `docs/setup.md` record that the firmware does not check certificate
  revocation (confirmed on a real controller).
- `tools/start_receiver.sh` no longer insists on a pair under `certs/`: without one, the
  receiver serves the built-in credential.
- A busy port 80 or 8008 no longer stops the receiver (`port_busy`); 443 is the only port
  the controller needs.
- What the user has to install or configure raises `stadiacast.errors.SetupError`, a
  `RuntimeError` whose message says what to install: for the headset audio a missing `audio`
  extra, PortAudio library, loopback or capture device, `pactl` or ffmpeg; for
  `--gamepad vigem` a computer without Windows, the `gamepad` extra or the ViGEmBus driver.
- `/_status` lists a controller whose virtual pad could not be created under `gamepads`, as
  `{"error": "..."}`.
- A pad that Windows never plugs in is reported, in the log and under `gamepads`, as the
  missing Xbox 360 controller driver that desktop Windows 10 and 11 have and Windows Server
  lacks, instead of the driver's raw error.
- `POST /_audio` must name an action: `{"do": "state"}` is gone, `GET /_audio` shows the stream.
- The headset stream's state (`/_audio`, `playback` in `/_status`) reports
  `follow_system_volume`, and no longer reports the unused `first_rtp_ts`, `last_rtp_ts` and
  `wire_bytes` stats.

### Fixed
- `--gamepad vigem`: in a game the input arrives on the game session, and it now reaches the
  virtual pad; it used to be counted as undecodable and never acked.
- `--gamepad vigem`: a pad that could not be created is tried again after the controller
  reboots or reconnects, instead of staying missing until the receiver restarts.
- The command line no longer prints every `RuntimeError` as one line, which hid the traceback
  of a bug that raised one. Only what the user can fix (a missing file, bad JSON, a port in use,
  a `SetupError`) is one line, now including the errors from reading `--rpc-response`,
  `--rpc-fields` and `--offer-json`.
- `--audio-devices` prints its list on standard output instead of standard error.
- A `/_audio` volume change before READY no longer stops `--audio-source` from starting at
  READY; the gain is kept for the next start.
- `GET /_audio` never changes the stream: only a `POST` runs an action, and other methods get
  405.
- `/_audio` answers 400 only for what the caller can fix (a missing or malformed field, a bad
  source spelling, a `SetupError`); a bug is a 500 with its traceback in `handler_error`, no
  longer a 400 carrying its message.
- The headset audio source is resolved in a worker thread, so listing the sound devices,
  setting up COM and running `pactl` no longer stall every controller's link.
- Scaling and level metering of captured system audio no longer run on the event loop shared
  by every controller.
- Any failure of the `--audio-source` autostart is logged; `audio_start_failed` now carries the
  `spec`, like `audio_source_invalid`.
- A capture device that fails to start (busy or unplugged) is tried again instead of leaving the
  headset stream silent for good.
- A WASAPI loopback whose audio library fails to load or initialise ends the capture with the
  reason (`audio_capture_ended`) instead of stalling the stream silently.
- The headset stream's notes no longer grow without bound while ffmpeg keeps failing.

## [0.1.0] - 2026-10-29

First public version of Stadiacast: a LAN-side stand-in for the Stadia cloud endpoints that
keeps the Stadia Controller's Wi-Fi mode working with your own PC. Everything below was
confirmed against real controllers running the last Wi-Fi firmware, `ota.282115`.

### Added
- The receiver (`stadiacast`): the HTTPS boot endpoints (`generate_204`, `stadiaconfig`,
  Omaha, Clearcut), the cloudcast discovery RPCs, the GMsg push channel, protobuf WebRTC
  signaling, STUN/ICE, DTLS-SRTP, WireHeader2 framing and the display RPC state machine up to
  READY, with every button, stick and trigger decoded into a JSON Lines event log.
- The game session: `StartGame`, the proxied `InitializationRequest`, a second ICE/DTLS
  endpoint, input on its `0x66` channel and rumble (`POST /_rumble`) kept alive across the
  controller's haptics watchdog.
- Several controllers at once: one session per controller (own UDP ports, DTLS certificates,
  GMsg queue and display link), labelled `c0`, `c1`, ... in the log and in `/_status`;
  `?controller=` selects the target of `/_rumble` and `/_start_game`.
- `--preset live`: the flag set proven end to end, including the Clearcut TLS block that keeps
  the controller from powering off after its first analytics upload.
- Helper processes spawned by the receiver: a redirecting DNS sink and an SNTP server.
- `stadiacast-provision`: Wi-Fi provisioning over Bluetooth LE for a controller in setup mode.
- `stadiacast-show-input`: a per-controller summary of the input recorded in a run.
- `tools/start_receiver.sh` / `tools/stop_receiver.sh`: rig settings in `.env`, a fresh run
  directory per start, an optional packet capture.
- The protobuf schemas with their provenance (`src/stadiacast/schemas/`) and a small proto2
  compiler that keeps the hand-written `.proto` files and their descriptors in sync.
- A fake controller (`tests/fake_controller.py`) that reproduces the firmware's network
  behaviour, and a test suite that drives the whole path over loopback without hardware.
- Documentation: setup guide, protocol notes, architecture overview, contributing guide,
  security policy, code of conduct.
- MIT License, with a `NOTICE` for the three protobuf schemas reproduced from the firmware.

[Unreleased]: https://github.com/jkfran/stadiacast/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jkfran/stadiacast/releases/tag/v0.1.0
