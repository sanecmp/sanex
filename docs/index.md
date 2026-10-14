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
the indicator selects that user's unique non-closing graphical session. If there
are several, use `loginctl list-sessions` and pass the intended session explicitly
with `--session SESSION-ID`.

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
