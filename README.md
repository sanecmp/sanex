# sanex

sanex monitors configured local accounts and applies limits received from sanea. The production service targets Debian-based Linux with GNOME, systemd/logind, and AccountsService.

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
ma tools
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

A real installation changes `/opt`, `/var/log`, `/etc/systemd/system`, `/etc/xdg/autostart`, and `/etc/logrotate.d`, so it must run as root:

```bash
sudo ./install.sh sanex
sudo /opt/sanex/bin/sanex register YOUR-CODE
```

After registration, `sanex.path` starts `sanex.service` automatically. The status indicator starts automatically when a user next signs in to GNOME; installation does not modify already running sessions. Use a local wheel path instead of `sanex` to install an unpublished build.
The private technical log is written to `/var/log/sanex/sanex.log` and rotated automatically.
