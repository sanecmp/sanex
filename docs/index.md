# sanex guide

sanex monitors configured local accounts and applies limits received from sanea. The production service targets Debian-based Linux with GNOME, systemd/logind, and AccountsService.

Its Python distribution is `sanecmp-sanex`; the import package and installed
commands remain `sanex`, `sanex-indicator` and `sanex-window-agent`. It depends on
`sanecmp-sanelib`. Installation and self-updates use the distribution name.

## Requirements

Python 3.12+, Debian-based Linux x86-64, GNOME, systemd/logind and AccountsService.
System installation needs curl and protected system Python and uv; local development uses makeapp (`makeapp>=2.3,<3`).

## Installation

Run the installer described under [System installation](#system-installation), then register using the code shown by sanea.
Use the development mode below to work without changing system services or files.

## Development mode

Install Python 3.12+, uv and makeapp (`makeapp>=2.3,<3`). Run package commands from this directory.
For an isolated client:

```bash
ma tools
ma up --tool
sanex develop --state-dir /tmp/sanex-dev register YOUR-CODE
sanex develop --state-dir /tmp/sanex-dev service
```

Use the short registration code shown by sanea in place of `YOUR-CODE`. Development mode runs under the current account and does not change system files.
Its technical log is written to `sanex.log` inside the selected state directory.

## Tests and build

```bash
ma tests
uv build
```

Local tests run through makeapp on Python 3.12. CI continues to run pytest directly.
Unit tests simulate external resource boundaries. CLI commands are installed with
`ma up --tool` and run directly, including from another directory.

A safe staged installation can be inspected without invoking systemd:

```bash
./install.sh --destdir /tmp/sanex-root --uv "$(command -v uv)" /path/to/sanex.whl
```

## System installation

A system Python 3.12+, curl and a root-owned system uv are required. Python and
uv, including their containing directories, must not be writable by the child.
The default Python is `/usr/bin/python3`; `--python` and `--uv` accept other
protected paths. The installer does not download a managed Python.

The standard uv installation puts its executables in the current user's
`~/.local/bin`. That copy is not suitable for the root service or its self-updates,
and `install.sh` does not search user directories. Install a separate root-owned
copy in `/usr/local/bin`, without changing shell profiles:

```bash
curl -LsSf https://astral.sh/uv/install.sh \
    | sudo env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
```

These options are documented in the
[official uv installer reference](https://docs.astral.sh/uv/reference/installer/).
Running the uv installer with only `sudo sh` uses root's user directory instead
of the system PATH and does not solve this prerequisite.

A real installation changes `/opt`, `/var/log`, `/etc/systemd/system`, `/etc/xdg/autostart`, and `/etc/logrotate.d`, so it must run as root:

```bash
curl -fsSL https://raw.githubusercontent.com/sanecmp/sanex/main/install.sh \
    | sudo sh -s -- 'sanecmp-sanex==0.1.0'
sudo /opt/sanex/bin/sanex register YOUR-CODE
```

PyPI is the default source. To install from GitHub instead, pass `--from-github`
after `sudo sh -s --`; this mode requires system Git and installs both sanex
and sanelib from the `main` branches of their official repositories.
Do not supply `PACKAGE` together with `--from-github`. Third-party dependencies
still come from PyPI, or the HTTPS index selected with `--index-url`.

Replace `YOUR-CODE` with the current code shown in sanea's **Computers** section.
Registration stays open for 30 seconds. Use the absolute sanex path; installation
does not add that command to sudo's PATH. Sanea and sanex must be on a reachable
home network, with HTTPS TCP 8443 and discovery UDP 62117 allowed on the server.
When sanea runs on another computer, its installer adds local IP addresses to
`SANEA_ALLOWED_HOSTS`; see the
[sanea network setup](https://github.com/sanecmp/sanea/blob/main/docs/index.md#home-network-access).

Registration safely removes a protected, empty PKI directory left by an earlier
installation. A directory containing any registration material is not removed
or overwritten.

After registration, `sanex.path` starts `sanex.service` automatically. The status indicator starts automatically when a user next signs in to GNOME; installation does not modify already running sessions. Use a local wheel path instead of the indexed requirement to install an unpublished build.
The private technical log is written to `/var/log/sanex/sanex.log` and rotated automatically.
Startup records include the installed sanex version and configuration ID; stop
records distinguish requested stops, cancellation and failures. Missing sanea
discovery replies and synchronization errors are logged when their state changes,
with a separate recovery message after synchronization resumes.
Before registration, `sanex.service` remains inactive and the indicator reports
that the service is unavailable; this is expected until registration creates
the configuration file.

The indicator remains visible without time limits and shows a warning if the
service is unavailable. GNOME needs an enabled AppIndicator/StatusNotifier
extension to display it. The indicator waits for the extension to start and
registers again if it restarts. Reading status does not enable account data
collection; accounts without configured limits also receive an unlimited status.
To diagnose startup, run `/opt/sanex/bin/sanex-indicator`
as the logged-in user, without sudo. When the process is outside a logind session,
the indicator selects that user's unique ready graphical session (`online` or
`active`). If there are several, use `loginctl list-sessions` and pass the
intended session explicitly with `--session SESSION-ID`.

Graphical sessions in logind's `opening` state are kept in session accounting
without starting window observers or authorizing the indicator. Observers start
when the session becomes `online` or `active`; this does not create another
session run. Observers stop at `closing`, while accounting keeps the session
until it disappears from logind.

uv's managed application environment is `/opt/sanex/bundle/sanecmp-sanex`.
Configuration and counters stay in separate protected directories under
`/opt/sanex`, outside that environment. Self-updates install the exact
`sanecmp-sanex` version requested by sanea; they never install the unrelated
unprefixed PyPI project.

## Updating sanex

```bash
curl -fsSL https://raw.githubusercontent.com/sanecmp/sanex/main/install.sh \
    | sudo sh
```

You can also update sanex through sanea's web interface. In **Computers**, enter
the version you want in **Update sanex** and start the update.

## Uninstalling sanex

Back up any local data you want to keep, then run:

```bash
curl -fsSL https://raw.githubusercontent.com/sanecmp/sanex/main/uninstall.sh \
    | sudo sh
```

From a checkout, run `sudo ./uninstall.sh` instead. The script takes no arguments
and asks you to type `sanex` in the terminal before making any changes. Any other
answer, or no terminal, cancels removal.

Removal stops and disables `sanex.path` and `sanex.service`, stops the installed
indicators without ending user sessions, and deletes the application, local
registration, private keys, counters, events and technical logs. It also removes
the systemd units, indicator autostart, icons and logrotate rule. No requests are
sent to sanea and none of its data is changed. Python and uv are not required
for removal and are not changed.

## Application identification

Application names and IDs are recorded separately from raw process names,
executables and window-owner PIDs. Recognition supports Steam's client helpers
and game IDs, Flatpak instance descriptors (including AT-SPI proxies), AppImage
runtime hints, desktop launcher hints, and absolute GJS script paths or resource
URIs. Unknown applications keep their raw process identity; no GJS application is ignored
solely because it uses the interpreter.

An application ID identifies the application, not its launch. Separate process
trees remain separate launches. Steam games with available SteamAppId or
SteamGameId hints are not merged into the Steam client. A Proton compatibility
ID alone does not identify a game: installers also use it. A game's window title
provides its display name when available; otherwise it falls back to its Steam ID.
If the window appears later, its title replaces the fallback for that same launch,
including when its start has already been sent to sanea. The discovered name is
kept when the window disappears; later title changes do not create more updates.
No Steam manifests, installation files, library scans or network lookups are used.

AppImages use the original absolute `APPIMAGE` path as their application ID,
not the changing `/tmp/.mount_*` executable path. The executable must be inside
the runtime's `APPDIR`; programs launched outside it do not inherit this identity.
The image itself is not read or executed for identification. Renaming or moving
the original image changes its ID; remounting it does not. Old records lacking
these hints remain unchanged rather than being merged by executable name alone.

AT-SPI remains the primary window source. EWMH additionally discovers ordinary
X11 and Xwayland windows, including games without accessible windows. It uses
the session user's local DISPLAY and Xauthority hints from their systemd user
manager; connection setup runs under that user's EUID. Native Wayland windows
remain outside EWMH. Windows belonging to an already accessible PID are not
added again. Failed sources preserve their known windows rather than ending runs.
If the AT-SPI bus disconnects, sanex releases it and reconnects on the next
observation, authenticating again under the session owner's EUID. Disconnect
completion is also retrieved during shutdown. X11 connection warnings include
the failure category without exposing the session environment or credentials.

After a Steam client is observed, sanex also checks same-user processes for
SteamAppId or SteamGameId. Window and process observations share one launch and
one snapshot's metadata cache. Without a detected client, there is no additional
process scan. Scans stop when the client exits; already discovered games are
checked individually until exit. A client that starts hidden must first become
observable, directly or as a recognized game's ancestor.
Process-only observations account for game lifetime; they do not invent a window
or kill a process to enforce a window rule. Normal window close actions use AT-SPI
or EWMH, according to the window's source.

A Flatpak proxy identifies its sandbox application, not the individual games
behind a shared proxy. Missing metadata falls back to raw process data; past
records without identifying metadata cannot be reconstructed.

On a new system boot, saved session and reported application runs are completed
with their last saved durations before their active state is cleared. End times
use the last saved observation; if it is unavailable, start plus recorded duration
is used as an estimate. Shutdown time is not added to usage. An interrupted
recovery is retried without appending duplicate endings. Restarting only the
service within the same boot preserves active runs.

### Implementation and cost

`applications/common.py` defines the provider contract and shared bounded reads.
`applications/resolver.py` receives providers and does not import their modules
or know their environment keys. `service/applications.py` selects and wires the
providers; adding an application handler does not require changing the base.

Only observed window owners and up to 32 same-UID ancestors are inspected.
Process identities and optional metadata, including misses, are shared within
one snapshot. Metadata is reread in the next snapshot so exec and PID reuse do
not leave a persistent recognition cache. Each optional file is limited to
64 KiB; arbitrary environment values and complete command lines are not stored
in events or logs. Desktop-name lookups use a 256-entry cache with a five-minute
refresh, including misses, and never scan application directories. User home
directories need not be exposed to the service for naming; absent desktop names
fall back to IDs. Recognition starts no subprocesses and makes no network calls.
