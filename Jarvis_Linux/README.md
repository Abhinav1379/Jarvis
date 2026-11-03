# Jarvis for Linux

A local command assistant for an ordinary Linux desktop session. Text mode uses
only Python's standard library (Python 3.9+). Voice input and output are optional
and can be enabled independently.

## Start

```bash
cd Jarvis_Linux
python3 jarvis_main.py --no-voice
python3 jarvis_main.py --check
python3 jarvis_main.py -c "system status"
```

For desktop integration on Debian/Ubuntu/Kali, install the tools you need:

```bash
sudo apt install xdg-utils libglib2.0-bin libnotify-bin
```

Run Jarvis as your normal user inside GNOME, KDE Plasma, Xfce, or another XDG
desktop session. Both X11 and Wayland are detected. Actual availability depends
on the installed desktop tools and services. `--check` shows display, session
bus, available tools, voice dependencies and state-file errors. It also probes
internet connectivity. Network commands use existing tools such as NetworkManager's
`nmcli`; missing tools return a clear failure.

Jarvis refuses root startup by default, privilege-elevating app launches,
arbitrary shell commands, and system power commands. It never installs packages
or obtains administrator privileges for you. App blocklists and Wi-Fi restrictions
also apply when commands run inside a routine.

## Configuration

Copy `config.example.json` to `~/.config/jarvis/config.json` and edit it, or pass
`--config /path/to/config.json`. A project-local `jarvis_config.json` takes
precedence over the default XDG location. `JARVIS_CONFIG` can select another file.

Settings precedence is defaults, configuration file, `JARVIS_*` environment
variables, then runtime changes. See `.env.example`. Runtime changes such as
`set mic device index to 0` need `save settings` to survive a restart. Environment
overrides are not saved to the configuration file.

Invalid setting types, voice backends, numeric limits and list/map contents fall
back to defaults with warnings when loaded. Invalid runtime changes fail without
replacing the current value. Microphone index `-1` means the system default; `0`
selects the first device. Settings saves use atomic replacement and private file
permissions. Unreadable or malformed JSON is preserved: repair or move the file
and restart before saving settings. Excessively nested JSON also falls back
safely, and an environment file with invalid UTF-8 is skipped.

## Existing commands

```text
system status
cpu usage
memory
disk space
battery
temperature
top processes
monitoring on
monitoring off
list apps
rescan apps
open firefox
open downloads
find report.pdf
search for python tutorials
notify me build finished
lock screen
voice status
help
```

System status collects each reading once, includes exposed temperatures, and
identifies unavailable readings. Individual temperature/process commands remain
available. Monitoring checks battery, RAM, sustained CPU load and disk space;
alerts reset after recovery instead of repeating every check. External power
suppresses low-charge warnings unless the battery still reports discharging.
Battery time estimates use matched energy/power or charge/current readings;
unavailable readings leave the estimate unknown.

App discovery respects user overrides (including hidden entries), desktop
visibility, installed `TryExec` programs and working directories. These keys
follow the [freedesktop desktop-entry specification](https://specifications.freedesktop.org/desktop-entry/latest/recognized-keys.html).
Launch prefers `gtk-launch` and `gio`, with an argument-list fallback. File and
HTTP/HTTPS opening try `xdg-open`, then `gio open`; failures and timeouts are
reported. Invalid Exec commands, missing URL hosts and invalid ports are rejected
before a desktop helper runs. Headless desktop actions fail clearly. GUI apps
run detached; helper commands have time limits and never use shell execution.

File search streams directory entries, respects exclusions and checks its time
budget inside each directory. `files.max_depth = 0` searches only the selected
roots' immediate contents. Indexed lookup falls back to a directory walk if
unavailable. Limited results are marked, and ambiguous or incomplete searches
require selecting a result with `open 1`. Filesystem calls themselves can still
block on an unavailable mount. Wi-Fi status reports unknown service/radio state
instead of claiming the radio is on, and tries the existing `iwgetid` fallback
when NetworkManager is unavailable.

## Voice setup

For speech output, install a system engine:

```bash
sudo apt install espeak-ng
python3 jarvis_main.py --voice-out --no-voice-in
```

Automatic output order: Piper (with a configured model and an installed player),
espeak-ng, espeak, speech-dispatcher's `spd-say`, then optional `pyttsx3`.
Piper needs `voice.piper_model`, `voice.piper_binary`, and one of `pw-play`,
`paplay` or `aplay`. Jarvis does not download Piper models.

For microphone input, use a virtual environment and install optional dependencies:

```bash
sudo apt install python3-venv python3-dev portaudio19-dev
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-voice.txt
python jarvis_main.py --voice
```

The default Google recognizer sends microphone audio to an online speech
service. For offline input, choose Vosk (`pip install vosk` and set
`voice.vosk_model_path` to an extracted model directory) or faster-whisper
(`pip install faster-whisper`, set `voice.stt_backend` to `whisper` and choose
`voice.whisper_model`). Whisper can download the selected model on first use;
pre-download it if the machine must stay offline.

Say `Jarvis, check battery`, or say `Jarvis` and give one command within eight
seconds. `voice.require_wake_word` controls the gate. Missing dependencies or a
microphone failure leave text commands usable. Recognizer network requests have
a timeout; service failures are reported once per outage and appear in
`voice status`. Changing an active input backend/model/device stops input;
enable it again to apply the change. Changing an active output backend/Piper
model likewise requires re-enabling output. A listener still stopping cannot be
started a second time.

Repeated wake words refresh the eight-second window. Changing the wake word
also updates typed command prefixes immediately. Speech that overlaps recording
or transcription is discarded, and stopping speech cancels queued output and
pending Piper playback.

## Persistent reminders

```text
remind me in 10 minutes to stretch
set a timer for 5 minutes
remind me at 2026-11-20 09:00 to check the build
list reminders
cancel reminder 1
cancel reminders
```

Times use the machine's local timezone. New reminders must be within 365 days;
up to 100 can be pending. `list reminders` numbers them by due date; cancellation
numbers refer to the current list. Dated reminders are one-time reminders.
Use a number for a single cancellation: `cancel reminder` alone preserves all
reminders and asks for the number. `cancel reminders` or `cancel all reminders`
cancels all.

Reminders are saved before success is reported and restored at startup. Keep
Jarvis running to receive reminders on time; reminders due while it is stopped
are reported as missed in the background on its next start. Timers recheck the
clock at least once a minute and do not deliver before the saved due time.
This also applies to `-c` commands:
creating a reminder and exiting saves it for a later run. To keep just the
background services running, use `python3 jarvis_main.py --daemon --no-voice`.

Delivery prints text and attempts a desktop notification and optional speech.
Disabled/unavailable notifications do not prevent terminal delivery. A failed
completion save keeps the reminder and retries in a minute. Records are removed
before delivery to avoid replaying completed reminders after restart; a crash
between that save and delivery can lose that notification. This is not an alarm
service that runs while Jarvis is closed.

## Named routines

```text
save routine morning check: system status; battery; open firefox
list routines
show routine morning check
run routine morning check
delete routine morning check
```

Saving a routine does not execute it. Names are case-insensitive and contain
1–60 letters, digits, spaces, underscores or hyphens. Each routine holds 1–20
existing commands; up to 100 routines can be saved. Duplicate names require an
explicit delete before replacement.

Each step goes through the normal command router. Shell/privileged/unsupported
commands, exit/stop commands and nested routine commands are refused. Validation
happens both when saving and before running, including routines edited on disk.
App safety is checked again at launch. Results show each attempted step. The
routine stops at the first failure or app-choice prompt; selecting that app
later resolves only the choice, without continuing the remaining steps. Changes
made by earlier successful steps are not rolled back.

Use semicolons to separate routine steps, including when the first step is a
notification or reminder. Saved routine names can contain connector words such
as `and` and `then`; an exact saved name is resolved before command chaining.
For an ordinary reminder or notification, connector words and semicolons remain
part of the message instead of executing that message immediately.

## State and testing

Reminder and routine files live under `$XDG_STATE_HOME/jarvis`, or
`~/.local/state/jarvis`. They use atomic replacement and private file permissions.
One Jarvis instance owns each state file at a time. Another instance can still
use ordinary commands but cannot change reminders/routines while the first
instance owns them. State errors never silently replace unreadable JSON: close
Jarvis, repair or move the affected file, and restart. `.lock` files can remain
after exit; the operating system releases their locks automatically. Shutdown
retains ownership until an in-progress atomic state write has finished.

```bash
python3 -B -m unittest discover -s . -v
```

The suite uses fake platforms, recognizers, timers, subprocesses and temporary
state/desktop-entry files. It launches no real apps, uses no microphone or speech
service, and changes no desktop settings. It covers full reminder/routine stores,
concurrent delivery and cancellation, shutdown during writes and malformed state.
Run desktop and audio smoke checks on your target Linux machine before replacing
the actual repository copy.

The additional `test_stress.py` cases exercise a 3,000-file tree, a 10,000-entry
app index, 2,000 concurrent routine runs, unusual selection input and reminder
payload preservation. They use the same fake desktop boundary and work with
standard-library unittest discovery above. With pytest installed, run:

```bash
python3 -B -m pytest test_jarvis.py test_stress.py -q -p no:cacheprovider
```

See [TUNING_REPORT.md](TUNING_REPORT.md) for measured results and fixes. This
verification ran on Windows with simulated Linux services; native Linux desktop,
audio, `/proc`/`sys` and POSIX integration still require a Linux machine.
