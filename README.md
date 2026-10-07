# sanex

sanex monitors configured local accounts and applies limits received from sanea. The production service targets Debian-based Linux with GNOME, systemd/logind, and AccountsService.

Its Python distribution is `sanecmp-sanex`; the import package and installed
commands remain `sanex`, `sanex-indicator` and `sanex-window-agent`. It depends on
`sanecmp-sanelib`. Installation and self-updates use the distribution name.

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
./install.sh --destdir /tmp/sanex-root /path/to/sanex.whl
```

## System installation

A system Python 3.12+, curl and a root-owned system uv are required. Python and
uv, including their containing directories, must not be writable by the child.
The default Python is `/usr/bin/python3`; `--python` and `--uv` accept other
protected paths. The installer does not download a managed Python.

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
[sanea network setup](https://github.com/sanecmp/sanea#home-network-access).

After registration, `sanex.path` starts `sanex.service` automatically. The status indicator starts automatically when a user next signs in to GNOME; installation does not modify already running sessions. Use a local wheel path instead of the indexed requirement to install an unpublished build.
The private technical log is written to `/var/log/sanex/sanex.log` and rotated automatically.

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
