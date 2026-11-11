# iDevice Toolkit

A 3uTools-style desktop app for Linux to flash, back up and manage iPhones and iPads.
It is a PyQt6 front end for [libimobiledevice](https://libimobiledevice.org) and
[idevicerestore](https://github.com/libimobiledevice/idevicerestore).

## Features

| Page | What it does |
|---|---|
| **Device** | Auto-detects Normal / Recovery / DFU mode. Model, iOS, serial, IMEI, ECID, activation, baseband, MACs. Battery health, cycle count, capacity and temperature. Storage usage. Pair / Trust. |
| **Flash** | Firmware list with signing status (ipsw.me), resumable IPSW download with SHA1 check, restore with *Retain data* or *Erase*, step-by-step progress, error hints, SHSH blobs. Identifies the model in Recovery/DFU mode from the USB serial (CPID/BDID). |
| **Backup** | Back up, restore, encrypt or change the password, unpack and delete backups (`idevicebackup2`). |
| **Apps** | List, install and uninstall apps (needs `ideviceinstaller`). |
| **Files** | Browse the media partition and app documents, download, upload, rename, delete, export all photos. |
| **Toolbox** | Screenshot, crash and panic logs, reboot, shut down, sleep, enter or exit Recovery, timed DFU guide, rename, virtual location, developer mode, unpair, process list. |
| **Logs** | Console of every command run, plus a live device syslog with a filter. |

## Install (.deb)

Download the `.deb` from [Releases](../../releases) and install it:

```sh
sudo apt install ./idevice-toolkit_*_amd64.deb
```

Then launch **iDevice Toolkit** from the app menu, or run `idevice-toolkit`.

The package bundles **current git builds** of idevicerestore, libirecovery, libtatsu and
libimobiledevice in `/opt/idevice`. Distro packages can lag behind Apple's firmware
changes. For example, Ubuntu 26.04's idevicerestore fails on recent iPhones with
`Couldn't get Yonkers firmware data`. The bundled tools are kept **separate** from the
system libimobiledevice, so the desktop (KDE's or GNOME's iPhone integration) keeps
using the distro libraries. You can also call the tools directly:
`/opt/idevice/bin/idevicerestore`.

A udev rule is installed so devices in Recovery/DFU mode are accessible without root.

Built for Ubuntu 26.04 (amd64). It should work on other Debian-based distros that have
the same library versions.

## Run from source

```sh
sudo apt install python3-pyqt6 usbmuxd usbutils libimobiledevice-utils idevicerestore irecovery
./run.sh
```

To use git builds of the restore tools, as the .deb does, build them into `/opt/idevice`
with `packaging/update-stack.sh`. The app prefers `/opt/idevice/bin` when it exists.

## Build the .deb

```sh
packaging/update-stack.sh      # build/refresh the restore stack in /opt/idevice
packaging/build-deb.sh         # -> dist/idevice-toolkit_<version>_amd64.deb
```

Source tree locations can be overridden with `IDEVICE_TREES="libtatsu libirecovery libimobiledevice idevicerestore"`.

## Licence

The GUI is MIT licensed (see [LICENSE](LICENSE)). The bundled idevicerestore,
libirecovery, libtatsu and libimobiledevice are LGPL. Their licence texts and the exact
upstream commits are in `/usr/share/doc/idevice-toolkit/` in the installed package.

Not affiliated with Apple or 3uTools.
