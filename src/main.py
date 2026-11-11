#!/usr/bin/env python3
"""iDevice Toolkit - a 3uTools-style GUI for libimobiledevice + idevicerestore on Linux.

Every action shells out to the CLI tools (idevicerestore, irecovery, ideviceinfo,
idevicebackup2, afcclient, ideviceinstaller, ...) so it always uses the builds
installed in /usr/local/bin.
"""

import csv
import hashlib
import json
import os
import plistlib
import posixpath
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

from PyQt6.QtCore import (QObject, QProcess, QProcessEnvironment, QSettings, QSize,
                          Qt, QThread, QTimer, QUrl, pyqtSignal)
from PyQt6.QtGui import (QBrush, QColor, QDesktopServices, QFont, QIcon, QPainter,
                         QPen, QPixmap, QTextCursor)
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox,
                             QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                             QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox,
                             QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
                             QPlainTextEdit, QProgressBar, QPushButton, QRadioButton,
                             QScrollArea, QSizePolicy, QStackedWidget, QStyle, QTableWidget,
                             QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget)

APP_NAME = "iDevice Toolkit"
APP_VERSION = "1.0.0"
HOME = os.path.expanduser("~")
CACHE = os.path.join(HOME, ".cache", "idevice-toolkit")
os.makedirs(CACHE, exist_ok=True)
HERE = os.path.dirname(os.path.abspath(__file__))
UDEV_RULE = "/etc/udev/rules.d/39-idevice-toolkit.rules"

# The git-built restore stack lives in /opt/idevice (kept out of /usr/local so the
# desktop's KDE iPhone integration keeps using the distro libimobiledevice).
IDEVICE_PREFIX = "/opt/idevice"
if os.path.isdir(os.path.join(IDEVICE_PREFIX, "bin")):
    os.environ["PATH"] = os.path.join(IDEVICE_PREFIX, "bin") + os.pathsep + os.environ.get("PATH", "")

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")

# Apple USB product IDs
PID_DFU = {0x1227, 0x1222}
PID_RECOVERY = {0x1280, 0x1281, 0x1282, 0x1283}


def is_normal_pid(pid):
    return 0x1290 <= pid <= 0x12AF


RESTORE_STEPS = ["Detecting device", "Preparing restore", "Uploading filesystem",
                 "Verifying filesystem", "Flashing firmware", "Updating baseband",
                 "Updating firmware components", "Uploading images"]
RESTORE_WEIGHTS = [3, 7, 55, 10, 15, 4, 4, 2]

ERROR_HINTS = [
    ("Yonkers", "Your idevicerestore/libtatsu may be outdated. Rebuild from git master."),
    ("not being signed", "This firmware is no longer signed by Apple. Pick a version marked Signed."),
    ("Unable to connect to device", "Put the device into Recovery or DFU mode and try again. "
                                    "If it is in Recovery mode, check USB permissions (Settings)."),
    ("LIBUSB_ERROR_ACCESS", "USB permission denied. Tick 'Run as root' or install the udev rule in Settings."),
    ("Permission denied", "Permission problem. Tick 'Run as root' or install the udev rule in Settings."),
    ("ASR", "Filesystem upload failed. Try another USB port/cable (USB 2 ports are often more reliable)."),
    ("baseband", "Baseband update failed. This can be a hardware fault (modem) on the logic board."),
    ("FDR", "FDR/factory data problem. Often caused by replaced/unpaired parts or a board fault."),
    ("timed out", "The device stopped responding. Check cable/port and try DFU mode."),
]


# ----------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------

def run(args, timeout=60, env=None):
    """Run a command, return (rc, combined text output)."""
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           stdin=subprocess.DEVNULL, timeout=timeout, env=env)
        return p.returncode, ANSI.sub("", p.stdout.decode("utf-8", "replace"))
    except FileNotFoundError:
        return 127, f"{args[0]}: command not found"
    except subprocess.TimeoutExpired:
        return -1, f"{args[0]}: timed out after {timeout}s"


def run_plist(args, timeout=60):
    """Run a command that prints an XML plist on stdout. Return (obj or None, error text)."""
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           stdin=subprocess.DEVNULL, timeout=timeout)
    except FileNotFoundError:
        return None, f"{args[0]}: command not found"
    except subprocess.TimeoutExpired:
        return None, "timed out"
    out = p.stdout
    i = out.find(b"<?xml")
    if p.returncode != 0 or i < 0:
        return None, (p.stderr + p.stdout).decode("utf-8", "replace").strip()
    try:
        return plistlib.loads(out[i:]), ""
    except Exception as e:  # noqa: BLE001
        return None, f"plist parse error: {e}"


def deep_get(obj, key):
    """Find first occurrence of key in nested dicts/lists."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = deep_get(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = deep_get(v, key)
            if r is not None:
                return r
    return None


def human(n):
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1000.0
    return str(n)


def open_path(path):
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def udev_rule_installed():
    # /usr/lib/udev/rules.d is where the .deb ships it; /etc is where the in-app button installs it
    return os.path.exists(UDEV_RULE) or os.path.exists("/usr/lib/udev/rules.d/39-idevice-toolkit.rules")


def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "idevice-toolkit/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


_name_cache_file = os.path.join(CACHE, "device_names.json")
try:
    with open(_name_cache_file) as f:
        _NAME_CACHE = json.load(f)
except Exception:  # noqa: BLE001
    _NAME_CACHE = {}


def marketing_name(identifier):
    """'iPhone14,7' -> 'iPhone 14' using ipsw.me (cached)."""
    if not identifier:
        return ""
    if identifier in _NAME_CACHE:
        return _NAME_CACHE[identifier]
    try:
        d = http_json(f"https://api.ipsw.me/v4/device/{identifier}?type=ipsw", timeout=8)
        _NAME_CACHE[identifier] = d.get("name", identifier)
        with open(_name_cache_file, "w") as f:
            json.dump(_NAME_CACHE, f)
    except Exception:  # noqa: BLE001
        return identifier
    return _NAME_CACHE[identifier]


# ----------------------------------------------------------------------------------
# Device detection / info gathering (run in worker threads)
# ----------------------------------------------------------------------------------

def usb_serial_info():
    """Read the Recovery/DFU USB serial string from sysfs (no root needed), e.g.
    'SDOM:01 CPID:8110 CPRV:11 CPFM:03 SCEP:01 BDID:18 ECID:000C78E63641401E IBFL:3D SRTG:[iBoot-..]'."""
    base = "/sys/bus/usb/devices"
    try:
        entries = os.listdir(base)
    except OSError:
        return {}
    for e in entries:
        p = os.path.join(base, e)
        try:
            with open(os.path.join(p, "idVendor")) as f:
                if f.read().strip() != "05ac":
                    continue
            with open(os.path.join(p, "idProduct")) as f:
                if int(f.read().strip(), 16) not in PID_DFU | PID_RECOVERY:
                    continue
            with open(os.path.join(p, "serial")) as f:
                serial = f.read().strip()
        except (OSError, ValueError):
            continue
        kv = dict(re.findall(r"(\w+):(\[[^\]]*\]|\S+)", serial))
        if "CPID" in kv:
            return kv
    return {}


_devices_file = os.path.join(CACHE, "devices.json")


def lookup_board(cpid_hex, bdid_hex):
    """Map CPID/BDID (hex strings) to {'identifier','name','boardconfig'} via ipsw.me (cached a week)."""
    if not cpid_hex or not bdid_hex:
        return None
    try:
        cpid, bdid = int(cpid_hex, 16), int(bdid_hex, 16)
    except ValueError:
        return None
    devices = None
    try:
        if time.time() - os.path.getmtime(_devices_file) < 7 * 86400:
            with open(_devices_file) as f:
                devices = json.load(f)
    except (OSError, ValueError):
        pass
    if devices is None:
        try:
            devices = http_json("https://api.ipsw.me/v4/devices", timeout=15)
            with open(_devices_file, "w") as f:
                json.dump(devices, f)
        except Exception:  # noqa: BLE001
            try:  # offline: fall back to a stale cache
                with open(_devices_file) as f:
                    devices = json.load(f)
            except (OSError, ValueError):
                return None
    for d in devices:
        for b in d.get("boards", []) or [d]:
            if b.get("cpid") == cpid and b.get("bdid") == bdid:
                return {"identifier": d["identifier"], "name": d["name"],
                        "boardconfig": b.get("boardconfig", "")}
    return None

def detect_device():
    st = {"mode": "none"}
    rc, out = run(["lsusb"], timeout=10)
    for pid_s in re.findall(r"ID 05ac:([0-9a-fA-F]{4})", out):
        pid = int(pid_s, 16)
        if pid in PID_DFU:
            st["mode"] = "dfu"
        elif pid in PID_RECOVERY:
            st["mode"] = "recovery"
        elif is_normal_pid(pid) and st["mode"] == "none":
            st["mode"] = "normal"
    if st["mode"] in ("dfu", "recovery"):
        rc, out = run(["irecovery", "-q"], timeout=10)
        kv = {k: v for k, v in re.findall(r"^(\w+):\s*(.*)$", out, re.M) if k != "ERROR"}
        st["irecovery"] = kv
        st["ecid"] = kv.get("ECID", "")
        st["product_type"] = kv.get("PRODUCT", "")
        st["hw_model"] = kv.get("MODEL", "")
        st["cpid"] = kv.get("CPID", "")
        if kv.get("MODE"):
            st["mode"] = "dfu" if "DFU" in kv["MODE"].upper() else "recovery"
        if not st["product_type"]:
            # irecovery failed (usually no USB permission without root). The USB serial
            # string in sysfs is world-readable and carries CPID/BDID/ECID, which is
            # enough to identify the model.
            sysfs = usb_serial_info()
            if sysfs:
                st["irecovery"] = {**sysfs, **kv}
                st["cpid"] = "0x" + sysfs.get("CPID", "").lower()
                if sysfs.get("ECID"):
                    st["ecid"] = "0x" + sysfs["ECID"].lower()
                dev = lookup_board(sysfs.get("CPID"), sysfs.get("BDID"))
                if dev:
                    st["product_type"] = dev["identifier"]
                    st["name"] = dev["name"]
                    st["hw_model"] = dev.get("boardconfig", "").lower()
                    st["irecovery"].setdefault("PRODUCT", dev["identifier"])
                    st["irecovery"].setdefault("MODEL", st["hw_model"])
                st["no_usb_access"] = bool(not kv)
    elif st["mode"] == "normal":
        rc, out = run(["idevice_id", "-l"], timeout=10)
        udids = [l.strip() for l in out.splitlines() if l.strip() and " " not in l.strip()]
        st["udid"] = udids[0] if udids else ""
    return st


def fetch_info(udid):
    res = {"paired": True, "error": ""}
    info, err = run_plist(["ideviceinfo", "-u", udid, "-x"])
    if info is None:
        res["paired"] = False
        res["error"] = err
        info, _ = run_plist(["ideviceinfo", "-u", udid, "-s", "-x"])
        res["info"] = info or {}
        return res
    res["info"] = info
    res["battery"], _ = run_plist(["ideviceinfo", "-u", udid, "-q", "com.apple.mobile.battery", "-x"])
    res["disk"], _ = run_plist(["ideviceinfo", "-u", udid, "-q", "com.apple.disk_usage", "-x"])
    res["ioreg"], _ = run_plist(["idevicediagnostics", "-u", udid, "ioregentry", "AppleSmartBattery"])
    res["name"] = marketing_name(info.get("ProductType", ""))
    return res


# ----------------------------------------------------------------------------------
# Qt plumbing
# ----------------------------------------------------------------------------------

class Task(QThread):
    result = pyqtSignal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            r = self.fn()
        except Exception as e:  # noqa: BLE001
            r = e
        self.result.emit(r)


class Proc(QObject):
    """Streaming QProcess wrapper; emits cleaned lines (splits on \\r and \\n)."""
    line = pyqtSignal(str)
    done = pyqtSignal(int)

    def __init__(self, args, env=None, cwd=None, parent=None):
        super().__init__(parent)
        self.args = args
        self.p = QProcess(self)
        self.p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        if env:
            e = QProcessEnvironment.systemEnvironment()
            for k, v in env.items():
                e.insert(k, v)
            self.p.setProcessEnvironment(e)
        if cwd:
            self.p.setWorkingDirectory(cwd)
        self.buf = ""
        self.p.readyReadStandardOutput.connect(self._read)
        self.p.finished.connect(self._fin)
        self.p.errorOccurred.connect(self._err)
        self._finished = False

    def start(self):
        self.p.start(self.args[0], self.args[1:])

    def write(self, text):
        self.p.write(text.encode())

    def kill(self):
        self.p.terminate()
        QTimer.singleShot(3000, self.p.kill)

    def running(self):
        return self.p.state() != QProcess.ProcessState.NotRunning

    def _read(self):
        self.buf += ANSI.sub("", bytes(self.p.readAllStandardOutput()).decode("utf-8", "replace"))
        parts = re.split(r"[\r\n]", self.buf)
        self.buf = parts.pop()
        for s in parts:
            if s.strip():
                self.line.emit(s.rstrip())

    def _fin(self, code, _status):
        if self._finished:
            return
        self._finished = True
        self._read()
        if self.buf.strip():
            self.line.emit(self.buf.rstrip())
        self.done.emit(code)

    def _err(self, err):
        if err == QProcess.ProcessError.FailedToStart and not self._finished:
            self._finished = True
            self.line.emit(f"ERROR: failed to start {self.args[0]}")
            self.done.emit(127)


class Downloader(QThread):
    progress = pyqtSignal(int, int, float)
    status = pyqtSignal(str)
    ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, url, dest, sha1=None, parent=None):
        super().__init__(parent)
        self.url, self.dest, self.sha1 = url, dest, sha1
        self.stop = False

    def run(self):
        part = self.dest + ".part"
        try:
            have = os.path.getsize(part) if os.path.exists(part) else 0
            headers = {"User-Agent": "idevice-toolkit/1.0"}
            if have:
                headers["Range"] = f"bytes={have}-"
            req = urllib.request.Request(self.url, headers=headers)
            self.status.emit("Resuming download…" if have else "Downloading…")
            with urllib.request.urlopen(req, timeout=60) as r:
                mode = "ab"
                if have and r.status != 206:
                    have, mode = 0, "wb"
                total = have + int(r.headers.get("Content-Length", 0) or 0)
                done, t0, last, d0 = have, time.time(), 0.0, have
                with open(part, mode) as f:
                    while True:
                        if self.stop:
                            self.failed.emit("Download paused. Partial file kept; press Download again to resume.")
                            return
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        if now - last > 0.3:
                            speed = (done - d0) / max(now - t0, 0.001)
                            self.progress.emit(done, total, speed)
                            last = now
            if self.sha1:
                self.status.emit("Verifying SHA1…")
                h = hashlib.sha1()
                size = os.path.getsize(part)
                n = 0
                with open(part, "rb") as f:
                    while True:
                        if self.stop:
                            self.failed.emit("Verification cancelled.")
                            return
                        b = f.read(8 << 20)
                        if not b:
                            break
                        h.update(b)
                        n += len(b)
                        self.progress.emit(n, size, 0)
                if h.hexdigest().lower() != self.sha1.lower():
                    os.replace(part, self.dest + ".bad")
                    self.failed.emit("SHA1 mismatch! The file is corrupt (renamed to .bad).")
                    return
            os.replace(part, self.dest)
            self.ok.emit(self.dest)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"Download failed: {e}")


def btn(text, slot=None, icon=None, primary=False, danger=False):
    b = QPushButton(text)
    if icon is not None:
        b.setIcon(QApplication.style().standardIcon(icon))
    if primary:
        b.setProperty("class", "primary")
    if danger:
        b.setProperty("class", "danger")
    if slot:
        b.clicked.connect(slot)
    return b


def table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.verticalHeader().setVisible(False)
    t.setAlternatingRowColors(True)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def ro_item(text, data=None):
    it = QTableWidgetItem(str(text))
    if data is not None:
        it.setData(Qt.ItemDataRole.UserRole, data)
    return it


def title(text):
    l = QLabel(text)
    l.setProperty("class", "h1")
    return l


# ----------------------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------------------

class Page(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win

    def on_device(self, dev):
        pass


class DevicePage(Page):
    FIELDS = [
        ("Model", "model"), ("Device name", "DeviceName"), ("iOS version", "ios"),
        ("Serial number", "SerialNumber"), ("IMEI", "InternationalMobileEquipmentIdentity"),
        ("IMEI 2", "InternationalMobileEquipmentIdentity2"), ("Phone number", "PhoneNumber"),
        ("Model number", "model_number"), ("Hardware", "hw"), ("Activation", "ActivationState"),
        ("Baseband", "BasebandVersion"), ("Wi-Fi MAC", "WiFiAddress"),
        ("Bluetooth MAC", "BluetoothAddress"), ("ECID", "ecid"), ("UDID", "udid"),
    ]

    def __init__(self, win):
        super().__init__(win)
        v = QVBoxLayout(self)
        v.addWidget(title("Device"))

        self.banner = QFrame()
        self.banner.setProperty("class", "banner")
        bl = QHBoxLayout(self.banner)
        self.banner_text = QLabel()
        self.banner_text.setWordWrap(True)
        bl.addWidget(self.banner_text, 1)
        self.pair_btn = btn("Pair / Trust", self.pair, primary=True)
        bl.addWidget(self.pair_btn)
        self.banner.hide()
        v.addWidget(self.banner)

        h = QHBoxLayout()
        v.addLayout(h, 1)
        g = QGroupBox("Device information")
        self.form = QFormLayout(g)
        self.vals = {}
        for label, key in self.FIELDS:
            l = QLabel("-")
            l.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.vals[key] = l
            self.form.addRow(label + ":", l)
        h.addWidget(g, 3)

        right = QVBoxLayout()
        h.addLayout(right, 2)
        bg = QGroupBox("Battery")
        bf = QFormLayout(bg)
        self.health = QProgressBar()
        self.health.setFormat("%p%")
        bf.addRow("Health:", self.health)
        self.bvals = {}
        for k in ("Level", "Charging", "Cycle count", "Design capacity", "Full charge capacity",
                  "Temperature", "Voltage"):
            l = QLabel("-")
            self.bvals[k] = l
            bf.addRow(k + ":", l)
        bf.addRow(btn("Battery details…", self.battery_details))
        right.addWidget(bg)

        sg = QGroupBox("Storage")
        sl = QVBoxLayout(sg)
        self.storage = QProgressBar()
        self.storage_text = QLabel("-")
        sl.addWidget(self.storage)
        sl.addWidget(self.storage_text)
        right.addWidget(sg)
        right.addStretch(1)

        row = QHBoxLayout()
        row.addWidget(btn("Refresh", self.refresh, QStyle.StandardPixmap.SP_BrowserReload))
        row.addWidget(btn("All properties…", self.all_props))
        row.addWidget(btn("Copy info", self.copy_info))
        row.addStretch(1)
        row.addWidget(btn("Reboot", lambda: win.toolbox.diag("restart")))
        row.addWidget(btn("Shut down", lambda: win.toolbox.diag("shutdown")))
        row.addWidget(btn("Enter Recovery", win.toolbox.enter_recovery))
        row.addWidget(btn("Exit Recovery", win.toolbox.exit_recovery))
        v.addLayout(row)
        self.data = {}

    def clear(self):
        for l in self.vals.values():
            l.setText("-")
        for l in self.bvals.values():
            l.setText("-")
        self.health.setValue(0)
        self.storage.setValue(0)
        self.storage_text.setText("-")

    def on_device(self, dev):
        self.clear()
        self.banner.hide()
        mode = dev["mode"]
        if mode == "none":
            self.show_banner("No device connected. Plug in an iPhone/iPad with a data cable.", False)
            return
        if mode in ("recovery", "dfu"):
            kv = dev.get("irecovery", {})
            self.vals["model"].setText(f"{dev.get('name') or ''} ({dev.get('product_type')})")
            self.vals["hw"].setText(f"{kv.get('MODEL', '')}  CPID {kv.get('CPID', '')}  BDID {kv.get('BDID', '')}")
            self.vals["ecid"].setText(dev.get("ecid", "-"))
            self.vals["SerialNumber"].setText(kv.get("SRNM", "-"))
            self.vals["InternationalMobileEquipmentIdentity"].setText(kv.get("IMEI", "-"))
            msg = (f"Device is in {mode.upper()} mode. Go to Flash to restore it, "
                   "or use Exit Recovery to try booting normally.")
            if dev.get("no_usb_access"):
                msg += ("\nModel identified from the USB serial number. Full details need USB access: "
                        "install the udev rule in Settings (or flash with 'Run as root').")
            self.show_banner(msg, False)
            return
        self.refresh()

    def show_banner(self, text, pair):
        self.banner_text.setText(text)
        self.pair_btn.setVisible(pair)
        self.banner.show()

    def refresh(self):
        dev = self.win.dev
        if dev["mode"] != "normal" or not dev.get("udid"):
            return
        udid = dev["udid"]
        self.win.status("Reading device information…")
        self.win.bg(lambda: fetch_info(udid), self.got_info)

    def got_info(self, res):
        if isinstance(res, Exception):
            self.win.log(f"Info error: {res}", "err")
            return
        self.data = res
        info = res.get("info", {})
        dev = self.win.dev
        dev["info"] = info
        dev["paired"] = res["paired"]
        if info.get("ProductType"):
            dev["product_type"] = info["ProductType"]
        if res.get("name"):
            dev["name"] = res["name"]
        if info.get("UniqueChipID"):
            dev["ecid"] = hex(info["UniqueChipID"])
        if info.get("DeviceName"):
            dev["device_name"] = info["DeviceName"]
        dev["ios"] = info.get("ProductVersion", "")
        self.win.update_header()
        self.win.status("Ready")

        def s(k):
            v = info.get(k)
            return "-" if v in (None, "") else str(v)
        for _, key in self.FIELDS:
            if key in info:
                self.vals[key].setText(s(key))
        self.vals["model"].setText(f"{dev.get('name') or ''} ({info.get('ProductType', '')})")
        self.vals["ios"].setText(f"{s('ProductVersion')} ({s('BuildVersion')})")
        self.vals["model_number"].setText(f"{info.get('ModelNumber', '')}{info.get('RegionInfo', '')}" or "-")
        self.vals["hw"].setText(f"{s('HardwareModel')}  {s('CPUArchitecture')}  "
                                f"colour {info.get('DeviceColor', '-')}/{info.get('DeviceEnclosureColor', '-')}")
        self.vals["ecid"].setText(dev.get("ecid", "-"))
        self.vals["udid"].setText(dev.get("udid", "-"))

        if not res["paired"]:
            err = res.get("error", "")
            self.show_banner("The device is not trusted yet. Unlock it, press Pair / Trust, then tap "
                             "'Trust' on the phone and enter the passcode.\n"
                             f"(lockdownd said: {err.splitlines()[-1] if err else 'not paired'})", True)
            return
        self.banner.hide()

        bat = res.get("battery") or {}
        io = res.get("ioreg") or {}
        lvl = bat.get("BatteryCurrentCapacity")
        self.bvals["Level"].setText(f"{lvl}%" if lvl is not None else "-")
        self.bvals["Charging"].setText("Yes" if bat.get("BatteryIsCharging") else "No")
        design = deep_get(io, "DesignCapacity")
        full = deep_get(io, "NominalChargeCapacity") or deep_get(io, "AppleRawMaxCapacity")
        cyc = deep_get(io, "CycleCount")
        temp = deep_get(io, "Temperature")
        volt = deep_get(io, "Voltage")
        self.bvals["Cycle count"].setText(str(cyc) if cyc is not None else "-")
        self.bvals["Design capacity"].setText(f"{design} mAh" if design else "-")
        self.bvals["Full charge capacity"].setText(f"{full} mAh" if full else "-")
        self.bvals["Temperature"].setText(f"{temp / 100:.1f} °C" if isinstance(temp, int) else "-")
        self.bvals["Voltage"].setText(f"{volt / 1000:.2f} V" if isinstance(volt, int) else "-")
        if design and full:
            pct = int(round(100 * full / design))
            self.health.setValue(min(pct, 100))
            self.health.setFormat(f"{pct}%")

        disk = res.get("disk") or {}
        total = disk.get("TotalDiskCapacity")
        dcap = disk.get("TotalDataCapacity")
        avail = disk.get("TotalDataAvailable", disk.get("AmountDataAvailable"))
        if total and dcap is not None and avail is not None:
            used = total - avail
            self.storage.setValue(int(100 * used / total))
            self.storage_text.setText(f"{human(used)} used of {human(total)}  ({human(avail)} free)")

    def pair(self):
        udid = self.win.dev.get("udid")
        if not udid:
            return
        self.win.status("Pairing… tap Trust on the device")
        self.win.bg(lambda: run(["idevicepair", "-u", udid, "pair"], 60), self._paired)

    def _paired(self, r):
        rc, out = r
        self.win.log(out.strip(), "ok" if rc == 0 else "err")
        if rc == 0:
            self.refresh()
        else:
            QMessageBox.information(self, "Pairing", out.strip() +
                                    "\n\nUnlock the device, tap Trust when asked, then press Pair again.")

    def all_props(self):
        d = self.data
        if not d:
            return
        lines = []
        for section, key in (("Device", "info"), ("Battery domain", "battery"),
                             ("Disk usage", "disk"), ("Battery (IORegistry)", "ioreg")):
            obj = d.get(key)
            if not obj:
                continue
            lines.append(f"===== {section} =====")
            lines += [f"{k}: {v}" for k, v in sorted(flatten(obj).items())]
            lines.append("")
        text_dialog(self, "All properties", "\n".join(lines))

    def battery_details(self):
        io = (self.data or {}).get("ioreg")
        if not io:
            QMessageBox.information(self, "Battery", "No battery data (device not paired?)")
            return
        text_dialog(self, "Battery details", "\n".join(f"{k}: {v}" for k, v in sorted(flatten(io).items())))

    def copy_info(self):
        lines = [f"{lbl}: {self.vals[k].text()}" for lbl, k in self.FIELDS]
        lines += [f"Battery {k}: {l.text()}" for k, l in self.bvals.items()]
        lines.append(f"Battery health: {self.health.text()}")
        lines.append(f"Storage: {self.storage_text.text()}")
        QApplication.clipboard().setText("\n".join(lines))
        self.win.status("Device info copied to clipboard")


def flatten(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}{k}."))
    elif isinstance(obj, list) and any(isinstance(x, (dict, list)) for x in obj):
        for i, v in enumerate(obj[:50]):
            out.update(flatten(v, f"{prefix}{i}."))
    else:
        if isinstance(obj, bytes):
            obj = obj.hex()[:64] + ("…" if len(obj) > 32 else "")
        out[prefix.rstrip(".")] = obj
    return out


def text_dialog(parent, title_, text):
    d = QDialog(parent)
    d.setWindowTitle(title_)
    d.resize(760, 600)
    v = QVBoxLayout(d)
    t = QPlainTextEdit(text)
    t.setReadOnly(True)
    t.setFont(QFont("monospace", 9))
    v.addWidget(t)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    copy = bb.addButton("Copy", QDialogButtonBox.ButtonRole.ActionRole)
    copy.clicked.connect(lambda: QApplication.clipboard().setText(text))
    bb.rejected.connect(d.reject)
    v.addWidget(bb)
    d.exec()


class FlashPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.proc = None
        self.dl = None
        self.fw = []
        v = QVBoxLayout(self)
        v.addWidget(title("Flash firmware"))

        top = QHBoxLayout()
        top.addWidget(QLabel("Device identifier:"))
        self.ident = QLineEdit()
        self.ident.setPlaceholderText("e.g. iPhone14,7 (auto-filled)")
        self.ident.setMaximumWidth(200)
        top.addWidget(self.ident)
        top.addWidget(btn("Get firmware list", self.load_fw, QStyle.StandardPixmap.SP_BrowserReload))
        self.devname = QLabel("")
        top.addWidget(self.devname)
        top.addStretch(1)
        v.addLayout(top)

        self.tbl = table(["Version", "Build", "Released", "Size", "Signed", "Local file"])
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.itemSelectionChanged.connect(self.sel_changed)
        v.addWidget(self.tbl, 1)

        dl = QHBoxLayout()
        self.dl_btn = btn("Download selected", self.download, QStyle.StandardPixmap.SP_ArrowDown)
        self.dl_cancel = btn("Pause", self.cancel_dl)
        self.dl_cancel.setEnabled(False)
        self.verify = QCheckBox("Verify SHA1")
        self.verify.setChecked(True)
        self.dl_bar = QProgressBar()
        self.dl_bar.setFormat("")
        self.dl_lbl = QLabel("")
        dl.addWidget(self.dl_btn)
        dl.addWidget(self.dl_cancel)
        dl.addWidget(self.verify)
        dl.addWidget(self.dl_bar, 1)
        dl.addWidget(self.dl_lbl)
        v.addLayout(dl)

        og = QGroupBox("Restore")
        ol = QGridLayout(og)
        ol.addWidget(QLabel("IPSW file:"), 0, 0)
        self.ipsw = QLineEdit()
        ol.addWidget(self.ipsw, 0, 1, 1, 3)
        ol.addWidget(btn("Browse…", self.browse), 0, 4)
        self.keep = QRadioButton("Retain user data (update)")
        self.erase = QRadioButton("Erase everything (full restore, recommended for repairs)")
        self.erase.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.keep)
        grp.addButton(self.erase)
        ol.addWidget(self.keep, 1, 1)
        ol.addWidget(self.erase, 1, 2, 1, 2)
        self.root = QCheckBox("Run as root (pkexec)")
        self.root.setChecked(not udev_rule_installed() and os.geteuid() != 0)
        self.root.setToolTip("Needed to access devices in Recovery/DFU mode unless the udev rule is installed (Settings).")
        self.debug = QCheckBox("Debug log")
        ol.addWidget(self.root, 2, 1)
        ol.addWidget(self.debug, 2, 2)
        v.addWidget(og)

        self.step_lbl = QLabel("Idle")
        self.step_lbl.setProperty("class", "h2")
        v.addWidget(self.step_lbl)
        self.overall = QProgressBar()
        self.sub = QProgressBar()
        self.sub.setMaximumHeight(14)
        self.sub.setTextVisible(False)
        v.addWidget(self.overall)
        v.addWidget(self.sub)
        self.last_line = QLabel("")
        self.last_line.setProperty("class", "muted")
        v.addWidget(self.last_line)

        row = QHBoxLayout()
        row.addWidget(btn("Enter Recovery", win.toolbox.enter_recovery))
        row.addWidget(btn("Exit Recovery", win.toolbox.exit_recovery))
        row.addWidget(btn("DFU mode guide…", win.toolbox.dfu_guide))
        row.addWidget(btn("Save SHSH blobs", self.shsh))
        row.addStretch(1)
        self.cancel_btn = btn("Cancel", self.cancel_flash, danger=True)
        self.cancel_btn.setEnabled(False)
        row.addWidget(self.cancel_btn)
        self.flash_btn = btn("  Flash  ", self.flash, QStyle.StandardPixmap.SP_MediaPlay, primary=True)
        row.addWidget(self.flash_btn)
        v.addLayout(row)

    # -- firmware list
    def on_device(self, dev):
        pt = dev.get("product_type")
        if pt and pt != self.ident.text():
            self.ident.setText(pt)
            self.load_fw()

    def load_fw(self):
        ident = self.ident.text().strip()
        if not ident:
            return
        self.win.status(f"Fetching firmware list for {ident}…")
        self.win.bg(lambda: http_json(f"https://api.ipsw.me/v4/device/{ident}?type=ipsw"), self.got_fw)

    def search_dirs(self):
        dirs = [self.win.download_dir(), HOME, os.path.join(HOME, "Downloads"),
                os.path.join(HOME, "Music", "idevicerestore")]
        return list(dict.fromkeys(d for d in dirs if os.path.isdir(d)))

    def local_for(self, url):
        name = os.path.basename(url)
        for d in self.search_dirs():
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
        return None

    def got_fw(self, d):
        if isinstance(d, Exception):
            self.win.log(f"Firmware list failed: {d}", "err")
            self.win.status("Could not fetch firmware list")
            return
        self.devname.setText(d.get("name", ""))
        self.fw = d.get("firmwares", [])
        self.tbl.setRowCount(0)
        first_signed = None
        for fw in self.fw:
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)
            local = self.local_for(fw["url"])
            items = [fw["version"], fw["buildid"], (fw.get("releasedate") or "")[:10],
                     human(fw.get("filesize")), "Signed" if fw.get("signed") else "No",
                     local or ""]
            for c, t in enumerate(items):
                it = ro_item(t, fw)
                if not fw.get("signed"):
                    it.setForeground(QBrush(QColor("#999")))
                elif c == 4:
                    it.setForeground(QBrush(QColor("#16a34a")))
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                self.tbl.setItem(r, c, it)
            if fw.get("signed") and first_signed is None:
                first_signed = r
        if first_signed is not None:
            self.tbl.selectRow(first_signed)
        self.win.status(f"{len(self.fw)} firmwares for {d.get('name', '')}")

    def selected_fw(self):
        rows = self.tbl.selectionModel().selectedRows()
        if not rows:
            return None
        return self.tbl.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)

    def sel_changed(self):
        fw = self.selected_fw()
        if fw:
            local = self.local_for(fw["url"])
            if local:
                self.ipsw.setText(local)

    def browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "Select IPSW", self.win.download_dir(), "IPSW (*.ipsw)")
        if p:
            self.ipsw.setText(p)

    # -- download
    def download(self):
        fw = self.selected_fw()
        if not fw:
            QMessageBox.information(self, "Download", "Select a firmware first.")
            return
        if not fw.get("signed"):
            if QMessageBox.question(self, "Not signed", "This firmware is not signed by Apple, so it "
                                    "cannot be restored. Download anyway?") != QMessageBox.StandardButton.Yes:
                return
        dest = os.path.join(self.win.download_dir(), os.path.basename(fw["url"]))
        if os.path.exists(dest):
            self.ipsw.setText(dest)
            QMessageBox.information(self, "Download", f"Already downloaded:\n{dest}")
            return
        self.dl = Downloader(fw["url"], dest, fw.get("sha1sum") if self.verify.isChecked() else None, self)
        self.dl.progress.connect(self.dl_progress)
        self.dl.status.connect(self.dl_lbl.setText)
        self.dl.ok.connect(self.dl_ok)
        self.dl.failed.connect(self.dl_failed)
        self.dl_btn.setEnabled(False)
        self.dl_cancel.setEnabled(True)
        self.dl.start()
        self.win.log(f"Downloading {fw['url']} -> {dest}")

    def dl_progress(self, done, total, speed):
        if total:
            self.dl_bar.setMaximum(1000)
            self.dl_bar.setValue(int(1000 * done / total))
            s = f"{human(done)} / {human(total)}"
            if speed:
                eta = (total - done) / speed if speed else 0
                s += f"  {human(speed)}/s  ETA {int(eta // 60)}m{int(eta % 60):02d}s"
            self.dl_bar.setFormat(s)

    def dl_ok(self, path):
        self.dl_btn.setEnabled(True)
        self.dl_cancel.setEnabled(False)
        self.dl_lbl.setText("Done")
        self.ipsw.setText(path)
        self.win.log(f"Downloaded {path}", "ok")
        self.got_fw({"name": self.devname.text(), "firmwares": self.fw})

    def dl_failed(self, msg):
        self.dl_btn.setEnabled(True)
        self.dl_cancel.setEnabled(False)
        self.dl_lbl.setText(msg)
        self.win.log(msg, "err")

    def cancel_dl(self):
        if self.dl:
            self.dl.stop = True

    # -- flash
    def flash(self):
        ipsw = self.ipsw.text().strip()
        dev = self.win.dev
        if not ipsw or not os.path.exists(ipsw):
            QMessageBox.warning(self, "Flash", "Choose or download an IPSW file first.")
            return
        if dev["mode"] == "none":
            QMessageBox.warning(self, "Flash", "No device connected.")
            return
        pt = dev.get("product_type", "")
        base = os.path.basename(ipsw)
        if pt and pt not in base and "," in base.split("_")[0]:
            if QMessageBox.question(self, "Check firmware", f"The file name '{base}' doesn't mention "
                                    f"{pt}. Continue anyway?") != QMessageBox.StandardButton.Yes:
                return
        erase = self.erase.isChecked()
        msg = (f"Restore {dev.get('name') or pt} with\n{base}\n\n" +
               ("ALL DATA ON THE DEVICE WILL BE ERASED." if erase else
                "Update restore: user data is kept (if the device can boot that data)."))
        if QMessageBox.warning(self, "Confirm flash", msg,
                               QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
                               ) != QMessageBox.StandardButton.Ok:
            return
        cache = os.path.join(CACHE, "restore")
        os.makedirs(cache, exist_ok=True)
        as_root = self.root.isChecked() and os.geteuid() != 0
        # A previous `sudo idevicerestore` leaves root-owned <ipsw>.lock files that a
        # non-root restore can't open. Remove them if stale (we own the directory).
        lock = ipsw + ".lock"
        if not as_root and os.path.exists(lock) and not os.access(lock, os.W_OK):
            if run(["pgrep", "-x", "idevicerestore"], 5)[0] != 0:
                try:
                    os.remove(lock)
                    self.win.log(f"Removed stale root-owned lock {lock}")
                except OSError as e:
                    self.win.log(f"Could not remove {lock}: {e}", "warn")
        if not as_root and not os.access(cache, os.W_OK | os.X_OK):
            cache = os.path.join(CACHE, "restore-user")
            os.makedirs(cache, exist_ok=True)
        logfile = os.path.join(CACHE, f"restore-{datetime.now():%Y%m%d-%H%M%S}.log")
        args = [shutil.which("stdbuf") or "stdbuf", "-o0", "-e0",
                shutil.which("idevicerestore") or "idevicerestore", "-P", "-y",
                "-C", cache, f"--logfile={logfile}"]
        if erase:
            args.append("-e")
        if self.debug.isChecked():
            args.append("-d")
        if dev["mode"] == "normal" and dev.get("udid"):
            args += ["-u", dev["udid"]]
        elif dev.get("ecid"):
            args += ["-i", dev["ecid"]]
        args.append(ipsw)
        self.as_root = self.root.isChecked() and os.geteuid() != 0
        if self.as_root:
            args = ["pkexec"] + args
        self.run_restore(args, logfile)

    def run_restore(self, args, logfile):
        self.win.set_busy(True, "Flashing — do not unplug the device")
        self.errors = []
        self.max_overall = 0
        self.overall.setMaximum(1000)
        self.overall.setValue(0)
        self.sub.setValue(0)
        self.step_lbl.setText("Starting…")
        self.flash_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.logfile = logfile
        self.win.log("$ " + " ".join(args), "cmd")
        self.proc = Proc(args, parent=self)
        self.proc.line.connect(self.on_line)
        self.proc.done.connect(self.on_done)
        self.proc.start()
        self.win.show_page("Flash")

    def on_line(self, s):
        m = re.match(r"^progress: (\d+) ([\d.]+)$", s)
        if m:
            step, frac = int(m.group(1)), float(m.group(2))
            if 0 <= step < len(RESTORE_STEPS):
                self.step_lbl.setText(f"Step {step + 1}/{len(RESTORE_STEPS)}: {RESTORE_STEPS[step]}")
                ov = (sum(RESTORE_WEIGHTS[:step]) + RESTORE_WEIGHTS[step] * min(frac, 1.0)) / sum(RESTORE_WEIGHTS)
                self.max_overall = max(self.max_overall, int(ov * 1000))
                self.overall.setValue(self.max_overall)
                self.overall.setFormat(f"{self.max_overall / 10:.1f}%")
            return
        m = re.match(r"^(.{1,60}?):\s+(\d+\.\d)$", s)
        if m:
            self.sub.setValue(int(float(m.group(2))))
            self.last_line.setText(f"{m.group(1)}: {m.group(2)}%")
            return
        kind = "err" if s.startswith("ERROR") else ("warn" if s.startswith("WARNING") else "")
        if kind == "err":
            self.errors.append(s)
        self.win.log(s, kind)
        self.last_line.setText(s[:160])

    def on_done(self, rc):
        self.win.set_busy(False)
        self.flash_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        if rc == 0:
            self.overall.setValue(1000)
            self.overall.setFormat("100%")
            self.step_lbl.setText("Restore completed ✔")
            self.win.log("Restore completed successfully", "ok")
            QMessageBox.information(self, "Flash", "Restore completed successfully.\n"
                                    "The device will reboot to the Hello screen.")
        else:
            self.step_lbl.setText(f"Restore failed (exit code {rc})")
            text = "\n".join(self.errors[-6:]) or f"idevicerestore exited with code {rc}"
            hints = [h for k, h in ERROR_HINTS if k.lower() in text.lower()]
            if hints:
                text += "\n\nHint: " + "\n".join(dict.fromkeys(hints))
            text += f"\n\nFull log: {self.logfile}"
            QMessageBox.critical(self, "Flash failed", text)

    def cancel_flash(self):
        if not self.proc or not self.proc.running():
            return
        if QMessageBox.warning(self, "Cancel", "Cancelling mid-restore can leave the device in "
                               "recovery mode (you can restore again). Cancel?",
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                               ) != QMessageBox.StandardButton.Yes:
            return
        if getattr(self, "as_root", False):
            subprocess.Popen(["pkexec", "pkill", "-INT", "-x", "idevicerestore"])
        else:
            self.proc.kill()

    def shsh(self):
        ipsw = self.ipsw.text().strip()
        dev = self.win.dev
        if not ipsw or not os.path.exists(ipsw) or dev["mode"] == "none":
            QMessageBox.information(self, "SHSH", "Connect a device and select the IPSW to save blobs for.")
            return
        out = os.path.join(HOME, "shsh")
        os.makedirs(out, exist_ok=True)
        args = ["idevicerestore", "-t", "-y"]
        if dev.get("ecid"):
            args += ["-i", dev["ecid"]]
        args.append(ipsw)
        if self.root.isChecked() and dev["mode"] != "normal" and os.geteuid() != 0:
            args = ["pkexec", shutil.which("idevicerestore")] + args[1:]
        self.win.run_logged(args, cwd=out, title="Saving SHSH blobs",
                            done=lambda rc: rc == 0 and open_path(os.path.join(out, "shsh")))


class BackupPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.proc = None
        v = QVBoxLayout(self)
        v.addWidget(title("Backup & Restore"))
        r = QHBoxLayout()
        r.addWidget(QLabel("Backup folder:"))
        self.dir = QLineEdit(win.settings.value("backup_dir", os.path.join(HOME, "iPhoneBackups")))
        r.addWidget(self.dir, 1)
        r.addWidget(btn("Browse…", self.browse))
        r.addWidget(btn("Open", lambda: open_path(self.dir.text())))
        v.addLayout(r)
        self.tbl = table(["Device", "Model", "iOS", "Date", "Encrypted", "Size", "UDID"])
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        v.addWidget(self.tbl, 1)
        opt = QHBoxLayout()
        self.o_system = QCheckBox("Restore system files")
        self.o_settings = QCheckBox("Restore settings")
        self.o_settings.setChecked(True)
        self.o_reboot = QCheckBox("Reboot when done")
        self.o_reboot.setChecked(True)
        self.o_copy = QCheckBox("Copy backup first")
        for w in (self.o_system, self.o_settings, self.o_reboot, self.o_copy):
            opt.addWidget(w)
        opt.addStretch(1)
        v.addLayout(opt)
        self.bar = QProgressBar()
        self.lbl = QLabel("")
        self.lbl.setProperty("class", "muted")
        v.addWidget(self.bar)
        v.addWidget(self.lbl)
        row = QHBoxLayout()
        row.addWidget(btn("Refresh", self.refresh, QStyle.StandardPixmap.SP_BrowserReload))
        row.addWidget(btn("Encryption…", self.encryption))
        row.addWidget(btn("Unpack selected", self.unpack))
        row.addWidget(btn("Delete selected", self.delete, danger=True))
        row.addStretch(1)
        self.cancel_btn = btn("Cancel", self.cancel)
        self.cancel_btn.setEnabled(False)
        row.addWidget(self.cancel_btn)
        row.addWidget(btn("Restore selected", self.restore))
        row.addWidget(btn("Back up now", self.backup, QStyle.StandardPixmap.SP_DialogSaveButton, primary=True))
        v.addLayout(row)
        QTimer.singleShot(500, self.refresh)

    def browse(self):
        d = QFileDialog.getExistingDirectory(self, "Backup folder", self.dir.text())
        if d:
            self.dir.setText(d)
            self.win.settings.setValue("backup_dir", d)
            self.refresh()

    def refresh(self):
        root = self.dir.text()

        def scan():
            rows = []
            if not os.path.isdir(root):
                return rows
            for name in sorted(os.listdir(root)):
                p = os.path.join(root, name)
                ip = os.path.join(p, "Info.plist")
                if not os.path.isfile(ip):
                    continue
                try:
                    with open(ip, "rb") as f:
                        info = plistlib.load(f)
                except Exception:  # noqa: BLE001
                    info = {}
                enc = False
                try:
                    with open(os.path.join(p, "Manifest.plist"), "rb") as f:
                        enc = bool(plistlib.load(f).get("IsEncrypted"))
                except Exception:  # noqa: BLE001
                    pass
                size = 0
                for dp, _, fs in os.walk(p):
                    for fn in fs:
                        try:
                            size += os.path.getsize(os.path.join(dp, fn))
                        except OSError:
                            pass
                rows.append((info.get("Device Name", "?"), info.get("Product Type", ""),
                             info.get("Product Version", ""), str(info.get("Last Backup Date", ""))[:19],
                             "Yes" if enc else "No", human(size), name))
            return rows
        self.win.bg(scan, self.got_rows)

    def got_rows(self, rows):
        if isinstance(rows, Exception):
            return
        self.tbl.setRowCount(0)
        for row in rows:
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)
            for c, t in enumerate(row):
                self.tbl.setItem(r, c, ro_item(t))

    def sel_udid(self):
        rows = self.tbl.selectionModel().selectedRows()
        return self.tbl.item(rows[0].row(), 6).text() if rows else None

    def _start(self, args, env=None, done=None):
        if self.proc and self.proc.running():
            QMessageBox.information(self, "Busy", "Another backup operation is running.")
            return
        self.bar.setMaximum(100)
        self.bar.setValue(0)
        self.win.log("$ " + " ".join(args), "cmd")
        self.proc = Proc(args, env=env, parent=self)
        self.proc.line.connect(self.on_line)
        self.proc.done.connect(lambda rc: self.on_done(rc, done))
        self.cancel_btn.setEnabled(True)
        self.proc.start()

    def on_line(self, s):
        m = re.search(r"(\d+(?:\.\d+)?)%", s)
        if m:
            self.bar.setValue(int(float(m.group(1))))
        self.lbl.setText(s[:160])
        if not m:
            self.win.log(s, "err" if "ERROR" in s else "")

    def on_done(self, rc, done):
        self.cancel_btn.setEnabled(False)
        self.win.log(f"idevicebackup2 finished (exit {rc})", "ok" if rc == 0 else "err")
        if rc == 0:
            self.bar.setValue(100)
        else:
            msg = self.lbl.text()
            if "Find My" in msg or "FindMy" in msg:
                msg += "\n\nTurn off Find My on the device before restoring a backup."
            QMessageBox.warning(self, "Backup", f"Operation failed (exit {rc}).\n{msg}")
        self.refresh()
        if done:
            done(rc)

    def cancel(self):
        if self.proc:
            self.proc.kill()

    def need_dev(self):
        return self.win.require_normal()

    def password_env(self, prompt):
        pw, ok = QInputDialog.getText(self, "Backup password", prompt, QLineEdit.EchoMode.Password)
        return ({"BACKUP_PASSWORD": pw} if pw else {}) if ok else None

    def backup(self):
        if not self.need_dev():
            return
        os.makedirs(self.dir.text(), exist_ok=True)
        self._start(["idevicebackup2", "-u", self.win.dev["udid"], "backup", "--full", self.dir.text()])

    def restore(self):
        src = self.sel_udid()
        if not src or not self.need_dev():
            QMessageBox.information(self, "Restore", "Select a backup and connect the device.")
            return
        if QMessageBox.warning(self, "Restore backup", f"Restore backup {src} to the connected device?\n"
                               "Data on the device will be replaced. Find My must be turned off.",
                               QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
                               ) != QMessageBox.StandardButton.Ok:
            return
        env = {}
        row = self.tbl.selectionModel().selectedRows()[0].row()
        if self.tbl.item(row, 4).text() == "Yes":
            env = self.password_env("This backup is encrypted. Password:")
            if env is None:
                return
        args = ["idevicebackup2", "-u", self.win.dev["udid"], "-s", src, "restore"]
        if self.o_system.isChecked():
            args.append("--system")
        if self.o_settings.isChecked():
            args.append("--settings")
        if not self.o_reboot.isChecked():
            args.append("--no-reboot")
        if self.o_copy.isChecked():
            args.append("--copy")
        args.append(self.dir.text())
        self._start(args, env)

    def encryption(self):
        if not self.need_dev():
            return
        items = ["Turn encryption ON", "Turn encryption OFF", "Change password"]
        choice, ok = QInputDialog.getItem(self, "Backup encryption", "Action:", items, 0, False)
        if not ok:
            return
        udid = self.win.dev["udid"]
        if choice == items[2]:
            old, ok1 = QInputDialog.getText(self, "Password", "Current password:", QLineEdit.EchoMode.Password)
            new, ok2 = QInputDialog.getText(self, "Password", "New password:", QLineEdit.EchoMode.Password)
            if ok1 and ok2:
                self._start(["idevicebackup2", "-u", udid, "changepw", self.dir.text()],
                            {"BACKUP_PASSWORD": old, "BACKUP_PASSWORD_NEW": new})
            return
        env = self.password_env("Backup password:")
        if env is None:
            return
        self._start(["idevicebackup2", "-u", udid, "encryption", "on" if choice == items[0] else "off",
                     self.dir.text()], env)

    def unpack(self):
        src = self.sel_udid()
        if not src:
            return
        self._start(["idevicebackup2", "-s", src, "unback", self.dir.text()],
                    done=lambda rc: rc == 0 and open_path(os.path.join(self.dir.text(), "_unback_")))

    def delete(self):
        src = self.sel_udid()
        if not src:
            return
        p = os.path.join(self.dir.text(), src)
        if QMessageBox.warning(self, "Delete backup", f"Permanently delete\n{p}?",
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                               ) == QMessageBox.StandardButton.Yes:
            shutil.rmtree(p, ignore_errors=True)
            self.refresh()


class AppsPage(Page):
    def __init__(self, win):
        super().__init__(win)
        v = QVBoxLayout(self)
        v.addWidget(title("Apps"))
        self.missing = QLabel(
            "<b>ideviceinstaller</b> is not installed, so app management is unavailable.<br>"
            "Install it with <code>sudo apt install ideviceinstaller</code> or build it from "
            "github.com/libimobiledevice/ideviceinstaller, then restart this app.")
        self.missing.setWordWrap(True)
        self.missing.setProperty("class", "banner")
        v.addWidget(self.missing)
        f = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter apps…")
        self.filter.textChanged.connect(self.apply_filter)
        self.kind = QComboBox()
        self.kind.addItems(["User apps", "System apps", "All apps"])
        self.kind.currentIndexChanged.connect(self.refresh)
        f.addWidget(self.filter, 1)
        f.addWidget(self.kind)
        v.addLayout(f)
        self.tbl = table(["Name", "Version", "Bundle ID"])
        self.tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        v.addWidget(self.tbl, 1)
        row = QHBoxLayout()
        row.addWidget(btn("Refresh", self.refresh, QStyle.StandardPixmap.SP_BrowserReload))
        row.addWidget(btn("Browse app files", self.browse_docs))
        row.addStretch(1)
        row.addWidget(btn("Uninstall selected", self.uninstall, danger=True))
        row.addWidget(btn("Install IPA…", self.install, primary=True))
        v.addLayout(row)
        self.has = bool(shutil.which("ideviceinstaller"))
        self.new_syntax = False
        if self.has:
            rc, out = run(["ideviceinstaller", "--help"], 10)
            self.new_syntax = bool(re.search(r"^\s+list\b", out, re.M))
        self.missing.setVisible(not self.has)

    def on_device(self, dev):
        if dev["mode"] == "normal" and dev.get("paired", True):
            self.refresh()

    def refresh(self):
        if not self.has or self.win.dev["mode"] != "normal":
            return
        udid = self.win.dev["udid"]
        k = ["user", "system", "all"][self.kind.currentIndex()]
        if self.new_syntax:
            args = ["ideviceinstaller", "-u", udid, "list"] + ({"user": [], "system": ["--system"], "all": ["--all"]}[k])
        else:
            args = ["ideviceinstaller", "-u", udid, "-l", "-o", f"list_{k}"]
        self.win.bg(lambda: run(args, 120), self.got)

    def got(self, r):
        if isinstance(r, Exception):
            return
        rc, out = r
        self.tbl.setRowCount(0)
        rows = []
        for rec in csv.reader(out.splitlines(), skipinitialspace=True):
            if len(rec) < 3 or rec[0] in ("CFBundleIdentifier",) or rec[0].startswith(("ERROR", "Total")):
                continue
            rows.append((rec[2], rec[1], rec[0]))
        for name, ver, bid in sorted(rows, key=lambda x: x[0].lower()):
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)
            for c, t in enumerate((name, ver, bid)):
                self.tbl.setItem(r, c, ro_item(t))
        if rc != 0 and not rows:
            self.win.log(out.strip(), "err")
        self.win.status(f"{len(rows)} apps")
        self.apply_filter()

    def apply_filter(self):
        q = self.filter.text().lower()
        for r in range(self.tbl.rowCount()):
            text = " ".join(self.tbl.item(r, c).text().lower() for c in range(3))
            self.tbl.setRowHidden(r, q not in text)

    def sel_bundle(self):
        rows = self.tbl.selectionModel().selectedRows()
        return self.tbl.item(rows[0].row(), 2).text() if rows else None

    def install(self):
        if not self.has or not self.win.require_normal():
            return
        p, _ = QFileDialog.getOpenFileName(self, "Install IPA", HOME, "iOS apps (*.ipa)")
        if p:
            udid = self.win.dev["udid"]
            args = ["ideviceinstaller", "-u", udid] + (["install", p] if self.new_syntax else ["-i", p])
            self.win.run_logged(args, title="Installing app", done=lambda rc: self.refresh())

    def uninstall(self):
        bid = self.sel_bundle()
        if not bid or not self.has or not self.win.require_normal():
            return
        if QMessageBox.question(self, "Uninstall", f"Uninstall {bid}?") != QMessageBox.StandardButton.Yes:
            return
        udid = self.win.dev["udid"]
        args = ["ideviceinstaller", "-u", udid] + (["uninstall", bid] if self.new_syntax else ["-U", bid])
        self.win.run_logged(args, title="Uninstalling", done=lambda rc: self.refresh())

    def browse_docs(self):
        bid = self.sel_bundle()
        if bid:
            self.win.files.open_container(bid)
            self.win.show_page("Files")


class FilesPage(Page):
    LS = re.compile(r"^(\S{10})\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\d+ \S+ \d{4} \d\d:\d\d:\d\d) (.*)$")

    def __init__(self, win):
        super().__init__(win)
        self.cwd = "/"
        self.container = None
        v = QVBoxLayout(self)
        v.addWidget(title("Files (Media)"))
        r = QHBoxLayout()
        r.addWidget(btn("", self.up, QStyle.StandardPixmap.SP_ArrowUp))
        r.addWidget(btn("", lambda: self.go("/"), QStyle.StandardPixmap.SP_DirHomeIcon))
        self.where = QLabel("")
        self.path = QLineEdit("/")
        self.path.returnPressed.connect(lambda: self.go(self.path.text()))
        r.addWidget(self.where)
        r.addWidget(self.path, 1)
        r.addWidget(btn("Photos (DCIM)", lambda: self.go("/DCIM")))
        r.addWidget(btn("Refresh", self.refresh, QStyle.StandardPixmap.SP_BrowserReload))
        v.addLayout(r)
        self.tbl = table(["Name", "Size", "Modified"])
        self.tbl.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tbl.horizontalHeader().setStretchLastSection(False)
        self.tbl.doubleClicked.connect(self.open_item)
        v.addWidget(self.tbl, 1)
        row = QHBoxLayout()
        row.addWidget(btn("New folder", self.mkdir))
        row.addWidget(btn("Rename", self.rename))
        row.addWidget(btn("Delete", self.delete, danger=True))
        row.addStretch(1)
        row.addWidget(btn("Export all photos", self.export_photos))
        row.addWidget(btn("Upload…", self.upload, QStyle.StandardPixmap.SP_ArrowUp))
        row.addWidget(btn("Download selected", self.download, QStyle.StandardPixmap.SP_ArrowDown, primary=True))
        v.addLayout(row)

    def base(self):
        a = ["afcclient", "-u", self.win.dev.get("udid", "")]
        if self.container:
            a += ["--documents", self.container]
        return a

    def on_device(self, dev):
        if dev["mode"] == "normal" and dev.get("paired", True):
            self.container = None
            self.go("/")
        else:
            self.tbl.setRowCount(0)

    def open_container(self, bundle):
        self.container = bundle
        self.go("/")

    def go(self, path):
        self.cwd = posixpath.normpath(path) if path else "/"
        if not self.cwd.startswith("/"):
            self.cwd = "/" + self.cwd
        self.path.setText(self.cwd)
        self.where.setText(f"[{self.container}]" if self.container else "")
        self.refresh()

    def up(self):
        if self.cwd == "/" and self.container:
            self.container = None
        self.go(posixpath.dirname(self.cwd))

    def refresh(self):
        if self.win.dev["mode"] != "normal":
            return
        args = self.base() + ["ls", "-l", self.cwd]
        self.win.bg(lambda: run(args, 60), self.got)

    def got(self, r):
        if isinstance(r, Exception):
            return
        rc, out = r
        self.tbl.setRowCount(0)
        entries = []
        for line in out.splitlines():
            m = self.LS.match(line)
            if m:
                perm, size, mtime, name = m.groups()
                entries.append((perm[0] == "d", name, int(size), mtime))
            elif line.startswith("Error") or "ERROR" in line:
                self.win.log(line, "err")
                self.win.status(line)
        entries.sort(key=lambda e: (not e[0], e[1].lower()))
        st = QApplication.style()
        for is_dir, name, size, mtime in entries:
            r_ = self.tbl.rowCount()
            self.tbl.insertRow(r_)
            it = ro_item(name, is_dir)
            it.setIcon(st.standardIcon(QStyle.StandardPixmap.SP_DirIcon if is_dir else QStyle.StandardPixmap.SP_FileIcon))
            self.tbl.setItem(r_, 0, it)
            self.tbl.setItem(r_, 1, ro_item("" if is_dir else human(size)))
            self.tbl.setItem(r_, 2, ro_item(mtime))

    def selected(self):
        out = []
        for idx in self.tbl.selectionModel().selectedRows():
            it = self.tbl.item(idx.row(), 0)
            out.append((posixpath.join(self.cwd, it.text()), it.data(Qt.ItemDataRole.UserRole)))
        return out

    def open_item(self, idx):
        it = self.tbl.item(idx.row(), 0)
        if it.data(Qt.ItemDataRole.UserRole):
            self.go(posixpath.join(self.cwd, it.text()))

    def download(self):
        sel = self.selected()
        if not sel:
            return
        dest = QFileDialog.getExistingDirectory(self, "Save to", os.path.join(HOME, "Downloads"))
        if not dest:
            return
        cmds = [self.base() + ["get", "-rf", p, os.path.join(dest, posixpath.basename(p))] for p, _ in sel]
        self.win.run_many(cmds, "Downloading files", done=lambda: open_path(dest))

    def export_photos(self):
        if not self.win.require_normal():
            return
        dest = os.path.join(HOME, "Pictures", f"iPhone-{datetime.now():%Y%m%d-%H%M%S}")
        os.makedirs(dest, exist_ok=True)
        self.win.run_many([["afcclient", "-u", self.win.dev["udid"], "get", "-rf", "/DCIM", dest]],
                          "Exporting photos", done=lambda: open_path(dest))

    def upload(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Upload files", HOME)
        if files:
            cmds = [self.base() + ["put", "-rf", f, posixpath.join(self.cwd, os.path.basename(f))] for f in files]
            self.win.run_many(cmds, "Uploading files", done=self.refresh)

    def mkdir(self):
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if ok and name:
            self.win.run_many([self.base() + ["mkdir", posixpath.join(self.cwd, name)]], "Creating folder",
                              done=self.refresh)

    def rename(self):
        sel = self.selected()
        if len(sel) != 1:
            return
        old = sel[0][0]
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=posixpath.basename(old))
        if ok and name:
            self.win.run_many([self.base() + ["mv", old, posixpath.join(self.cwd, name)]], "Renaming",
                              done=self.refresh)

    def delete(self):
        sel = self.selected()
        if not sel:
            return
        if QMessageBox.warning(self, "Delete", f"Delete {len(sel)} item(s) from the device permanently?",
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                               ) != QMessageBox.StandardButton.Yes:
            return
        cmds = [self.base() + (["rm", "-rf", p] if d else ["rm", p]) for p, d in sel]
        self.win.run_many(cmds, "Deleting", done=self.refresh)


class ToolboxPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.syslog = None
        v = QVBoxLayout(self)
        v.addWidget(title("Toolbox"))
        grid = QGridLayout()
        tools = [
            ("Screenshot", "📷", self.screenshot),
            ("Real-time log", "📜", lambda: win.show_page("Logs")),
            ("Crash / panic logs", "💥", self.crashlogs),
            ("Battery details", "🔋", lambda: win.device.battery_details()),
            ("Reboot", "🔄", lambda: self.diag("restart")),
            ("Shut down", "⏻", lambda: self.diag("shutdown")),
            ("Sleep", "🌙", lambda: self.diag("sleep")),
            ("Enter Recovery", "🛠", self.enter_recovery),
            ("Exit Recovery", "↩", self.exit_recovery),
            ("DFU mode guide", "⏱", self.dfu_guide),
            ("Rename device", "✏", self.rename),
            ("Virtual location", "📍", self.location),
            ("Developer mode", "🧑‍💻", self.devmode),
            ("Pair / Trust", "🤝", lambda: win.device.pair()),
            ("Unpair", "✂", self.unpair),
            ("Process list", "📋", self.pidlist),
            ("Device info dump", "🧾", lambda: win.device.all_props()),
            ("USB permissions", "🔐", win.fix_udev),
        ]
        for i, (name, emoji, fn) in enumerate(tools):
            b = QToolButton()
            b.setText(f"{emoji}\n{name}")
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            b.setMinimumSize(QSize(150, 90))
            b.setProperty("class", "tile")
            b.clicked.connect(fn)
            grid.addWidget(b, i // 6, i % 6)
        v.addLayout(grid)
        v.addStretch(1)

    def udid(self):
        return self.win.dev.get("udid", "")

    def diag(self, what):
        if not self.win.require_normal():
            return
        if what in ("restart", "shutdown") and QMessageBox.question(
                self, what.title(), f"{what.title()} the device now?") != QMessageBox.StandardButton.Yes:
            return
        self.win.run_logged(["idevicediagnostics", "-u", self.udid(), what], title=what.title())

    def enter_recovery(self):
        if not self.win.require_normal():
            return
        if QMessageBox.question(self, "Enter Recovery", "Reboot the device into Recovery mode?"
                                ) == QMessageBox.StandardButton.Yes:
            self.win.run_logged(["ideviceenterrecovery", self.udid()], title="Entering recovery")

    def exit_recovery(self):
        if self.win.dev["mode"] not in ("recovery", "dfu"):
            QMessageBox.information(self, "Exit Recovery", "The device is not in Recovery mode.")
            return
        args = ["irecovery", "-n"]
        if not udev_rule_installed() and os.geteuid() != 0:
            args = ["pkexec", shutil.which("irecovery")] + args[1:]
        self.win.run_logged(args, title="Exiting recovery (setting auto-boot)")

    def screenshot(self):
        if not self.win.require_normal():
            return
        d = os.path.join(HOME, "Pictures", "iPhone-Screenshots")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png")
        udid = self.udid()
        self.win.bg(lambda: run(["idevicescreenshot", "-u", udid, p], 60) + (p,), self._shot)

    def _shot(self, r):
        rc, out, p = r
        if rc != 0 or not os.path.exists(p):
            QMessageBox.warning(self, "Screenshot", out.strip() + "\n\niOS 17+ needs the developer disk image "
                                "mounted (Developer mode on + ideviceimagemounter).")
            return
        d = QDialog(self)
        d.setWindowTitle(os.path.basename(p))
        v = QVBoxLayout(d)
        l = QLabel()
        pm = QPixmap(p)
        l.setPixmap(pm.scaled(420, 820, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation))
        v.addWidget(l)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        ob = bb.addButton("Open folder", QDialogButtonBox.ButtonRole.ActionRole)
        ob.clicked.connect(lambda: open_path(os.path.dirname(p)))
        cb = bb.addButton("Copy", QDialogButtonBox.ButtonRole.ActionRole)
        cb.clicked.connect(lambda: QApplication.clipboard().setPixmap(pm))
        bb.rejected.connect(d.reject)
        v.addWidget(bb)
        d.exec()

    def crashlogs(self):
        if not self.win.require_normal():
            return
        d = os.path.join(HOME, "iphone_crash_logs", f"{self.win.dev.get('device_name', 'device')}-{datetime.now():%Y%m%d-%H%M%S}")
        os.makedirs(d, exist_ok=True)
        self.win.run_logged(["idevicecrashreport", "-u", self.udid(), "-e", "-k", d],
                            title="Copying crash logs", done=lambda rc: self._crash_done(d))

    def _crash_done(self, d):
        panics = []
        for dp, _, fs in os.walk(d):
            for f in fs:
                if "panic" in f.lower():
                    panics.append(os.path.join(dp, f))
        if panics:
            panics.sort(key=os.path.getmtime, reverse=True)
            try:
                with open(panics[0], errors="replace") as f:
                    txt = f.read()
            except OSError:
                txt = ""
            m = re.search(r'panicString"\s*:\s*"(.{0,600})', txt) or re.search(r"panic\(.{0,600}", txt)
            text_dialog(self, f"{len(panics)} panic log(s) — latest", f"{panics[0]}\n\n" +
                        (m.group(0).replace("\\n", "\n") if m else txt[:4000]))
        open_path(d)

    def rename(self):
        if not self.win.require_normal():
            return
        name, ok = QInputDialog.getText(self, "Rename device", "New name:",
                                        text=self.win.dev.get("device_name", ""))
        if ok and name:
            self.win.run_logged(["idevicename", "-u", self.udid(), name], title="Renaming",
                                done=lambda rc: self.win.device.refresh())

    def location(self):
        if not self.win.require_normal():
            return
        d = QDialog(self)
        d.setWindowTitle("Virtual location")
        f = QFormLayout(d)
        lat = QDoubleSpinBox()
        lat.setRange(-90, 90)
        lat.setDecimals(6)
        lat.setValue(float(self.win.settings.value("lat", 51.5007)))
        lon = QDoubleSpinBox()
        lon.setRange(-180, 180)
        lon.setDecimals(6)
        lon.setValue(float(self.win.settings.value("lon", -0.1246)))
        f.addRow("Latitude:", lat)
        f.addRow("Longitude:", lon)
        f.addRow(QLabel("Requires Developer mode + mounted developer disk image."))
        bb = QDialogButtonBox()
        setb = bb.addButton("Set location", QDialogButtonBox.ButtonRole.AcceptRole)
        resb = bb.addButton("Reset to real", QDialogButtonBox.ButtonRole.ResetRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        f.addRow(bb)
        res = {}
        setb.clicked.connect(lambda: (res.update(a="set"), d.accept()))
        resb.clicked.connect(lambda: (res.update(a="reset"), d.accept()))
        bb.rejected.connect(d.reject)
        if d.exec() and res:
            if res["a"] == "set":
                self.win.settings.setValue("lat", lat.value())
                self.win.settings.setValue("lon", lon.value())
                args = ["idevicesetlocation", "-u", self.udid(), "--", f"{lat.value():.6f}", f"{lon.value():.6f}"]
            else:
                args = ["idevicesetlocation", "-u", self.udid(), "reset"]
            self.win.run_logged(args, title="Virtual location")

    def devmode(self):
        if not self.win.require_normal():
            return
        items = ["Reveal Developer Mode in Settings", "Enable (device will reboot)", "Show status"]
        c, ok = QInputDialog.getItem(self, "Developer mode", "Action:", items, 0, False)
        if ok:
            cmd = {items[0]: "reveal", items[1]: "enable", items[2]: "list"}[c]
            self.win.run_logged(["idevicedevmodectl", "-u", self.udid(), cmd], title="Developer mode")

    def unpair(self):
        if self.win.require_normal() and QMessageBox.question(
                self, "Unpair", "Remove the pairing with this computer?") == QMessageBox.StandardButton.Yes:
            self.win.run_logged(["idevicepair", "-u", self.udid(), "unpair"], title="Unpair")

    def pidlist(self):
        if not self.win.require_normal():
            return
        udid = self.udid()
        self.win.bg(lambda: run(["idevicesyslog", "-u", udid, "pidlist"], 30),
                    lambda r: text_dialog(self, "Processes", r[1] if not isinstance(r, Exception) else str(r)))

    def dfu_guide(self):
        DfuDialog(self.win).exec()


class DfuDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("DFU mode guide")
        self.resize(520, 300)
        pt = win.dev.get("product_type", "") or ""
        m = re.match(r"(iPhone|iPad|iPod)(\d+),", pt)
        major = int(m.group(2)) if m else 99
        family = m.group(1) if m else "iPhone"
        if family == "iPhone" and major >= 10 or family == "iPad" and major >= 8:
            self.steps = [("Get ready… (cable plugged in)", 3),
                          ("Press VOL UP, press VOL DOWN, then HOLD the SIDE button", 10),
                          ("Keep holding SIDE + also hold VOL DOWN", 5),
                          ("RELEASE SIDE — keep holding VOL DOWN", 10)]
            kind = "Face ID / iPhone 8 and newer"
        elif family == "iPhone" and major == 9:
            self.steps = [("Get ready… (cable plugged in)", 3),
                          ("HOLD SIDE + VOL DOWN", 8),
                          ("RELEASE SIDE — keep holding VOL DOWN", 8)]
            kind = "iPhone 7 / 7 Plus"
        else:
            self.steps = [("Get ready… (cable plugged in)", 3),
                          ("HOLD POWER + HOME", 8),
                          ("RELEASE POWER — keep holding HOME", 8)]
            kind = "Home-button device"
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"<b>{win.dev.get('name') or pt or 'Device'}</b> — {kind}"))
        self.big = QLabel("Press Start and follow the prompts.\nThe screen stays BLACK in DFU mode.")
        self.big.setWordWrap(True)
        self.big.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = self.big.font()
        f.setPointSize(15)
        f.setBold(True)
        self.big.setFont(f)
        v.addWidget(self.big, 1)
        self.count = QLabel("")
        self.count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f2 = self.count.font()
        f2.setPointSize(28)
        self.count.setFont(f2)
        v.addWidget(self.count)
        r = QHBoxLayout()
        self.start_b = btn("Start", self.start, primary=True)
        r.addStretch(1)
        r.addWidget(self.start_b)
        r.addWidget(btn("Close", self.reject))
        v.addLayout(r)
        self.t = QTimer(self)
        self.t.timeout.connect(self.tick)

    def start(self):
        self.i, self.left = 0, self.steps[0][1]
        self.start_b.setEnabled(False)
        self.show_step()
        self.t.start(1000)

    def show_step(self):
        self.big.setText(self.steps[self.i][0])
        self.count.setText(str(self.left))

    def tick(self):
        self.left -= 1
        if self.i == len(self.steps) - 1 and self.left % 2 == 0:
            rc, out = run(["lsusb"], 5)
            if re.search(r"05ac:(1227|1222)", out):
                self.finish(True)
                return
        if self.left <= 0:
            self.i += 1
            if self.i >= len(self.steps):
                rc, out = run(["lsusb"], 5)
                self.finish(bool(re.search(r"05ac:(1227|1222)", out)))
                return
            self.left = self.steps[self.i][1]
        self.show_step()

    def finish(self, ok):
        self.t.stop()
        self.start_b.setEnabled(True)
        self.count.setText("✔" if ok else "✖")
        self.big.setText("DFU mode detected! Release the buttons. You can now Flash." if ok else
                         "DFU not detected. If the Apple logo appeared you held too long; if the "
                         "recovery screen shows you are in Recovery (also fine for restoring). Try again.")
        self.win.poll(force=True)


class LogsPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.sys = None
        v = QVBoxLayout(self)
        v.addWidget(title("Logs"))
        tabs = QTabWidget()
        v.addWidget(tabs, 1)

        w = QWidget()
        l = QVBoxLayout(w)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(20000)
        self.console.setFont(QFont("monospace", 9))
        l.addWidget(self.console)
        r = QHBoxLayout()
        r.addStretch(1)
        r.addWidget(btn("Clear", self.console.clear))
        r.addWidget(btn("Save…", lambda: self.save(self.console)))
        r.addWidget(btn("Open log folder", lambda: open_path(CACHE)))
        l.addLayout(r)
        tabs.addTab(w, "Toolkit console")

        w2 = QWidget()
        l2 = QVBoxLayout(w2)
        r2 = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter (text or process name)…")
        self.start_b = btn("Start", self.toggle, QStyle.StandardPixmap.SP_MediaPlay, primary=True)
        r2.addWidget(self.filter, 1)
        r2.addWidget(self.start_b)
        r2.addWidget(btn("Clear", lambda: self.sysview.clear()))
        r2.addWidget(btn("Save…", lambda: self.save(self.sysview)))
        l2.addLayout(r2)
        self.sysview = QPlainTextEdit()
        self.sysview.setReadOnly(True)
        self.sysview.setMaximumBlockCount(10000)
        self.sysview.setFont(QFont("monospace", 9))
        l2.addWidget(self.sysview)
        tabs.addTab(w2, "Device syslog (real-time)")
        tabs.setCurrentIndex(0)

    def append(self, text, kind=""):
        color = {"err": "#dc2626", "ok": "#16a34a", "warn": "#d97706", "cmd": "#2563eb"}.get(kind)
        ts = datetime.now().strftime("%H:%M:%S")
        esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if color:
            self.console.appendHtml(f'<span style="color:#888">{ts}</span> <span style="color:{color}">{esc}</span>')
        else:
            self.console.appendHtml(f'<span style="color:#888">{ts}</span> {esc}')

    def toggle(self):
        if self.sys and self.sys.running():
            self.sys.kill()
            return
        if not self.win.require_normal():
            return
        self.sys = Proc(["idevicesyslog", "-u", self.win.dev["udid"], "--no-colors"], parent=self)
        self.sys.line.connect(self.sysline)
        self.sys.done.connect(lambda rc: (self.start_b.setText("Start"),
                                          self.sysview.appendPlainText(f"--- syslog stopped ({rc}) ---")))
        self.sys.start()
        self.start_b.setText("Stop")

    def sysline(self, s):
        q = self.filter.text().strip().lower()
        if q and q not in s.lower():
            return
        self.sysview.appendPlainText(s)

    def save(self, w):
        p, _ = QFileDialog.getSaveFileName(self, "Save log", os.path.join(HOME, f"log-{datetime.now():%Y%m%d-%H%M%S}.txt"))
        if p:
            with open(p, "w") as f:
                f.write(w.toPlainText())


class SettingsPage(Page):
    def __init__(self, win):
        super().__init__(win)
        v = QVBoxLayout(self)
        v.addWidget(title("Settings"))
        g = QGroupBox("Firmware downloads")
        f = QFormLayout(g)
        r = QHBoxLayout()
        self.dl = QLineEdit(win.download_dir())
        r.addWidget(self.dl, 1)
        r.addWidget(btn("Browse…", self.browse))
        f.addRow("IPSW folder:", r)
        v.addWidget(g)

        g2 = QGroupBox("USB permissions")
        l2 = QVBoxLayout(g2)
        self.udev_lbl = QLabel()
        self.udev_lbl.setWordWrap(True)
        l2.addWidget(self.udev_lbl)
        l2.addWidget(btn("Install udev rule (needs admin password)", win.fix_udev))
        v.addWidget(g2)

        g3 = QGroupBox("Tool versions")
        l3 = QVBoxLayout(g3)
        self.versions = QLabel("…")
        self.versions.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.versions.setFont(QFont("monospace", 9))
        l3.addWidget(self.versions)
        v.addWidget(g3)
        v.addStretch(1)
        self.update_udev()

        def vers():
            out = []
            for t in ("idevicerestore", "irecovery", "ideviceinfo", "idevicebackup2", "afcclient",
                      "ideviceinstaller", "usbmuxd"):
                p = shutil.which(t)
                if not p:
                    out.append(f"{t:18} not installed")
                    continue
                rc, o = run([t, "--version"] if t != "irecovery" else [t, "-V"], 10)
                out.append(f"{t:18} {(o.strip().splitlines() or ['?'])[0][:90]}")
            return "\n".join(out)
        win.bg(vers, lambda s: self.versions.setText(str(s)))

    def update_udev(self):
        self.udev_lbl.setText(
            "✔ udev rule installed — Recovery/DFU devices are accessible without root." if udev_rule_installed()
            else "Recovery/DFU devices are only writable by root right now, so flashing uses pkexec. "
                 "Installing the udev rule lets your user access them directly.")

    def browse(self):
        d = QFileDialog.getExistingDirectory(self, "IPSW folder", self.dl.text())
        if d:
            self.dl.setText(d)
            self.win.settings.setValue("download_dir", d)


# ----------------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------------

def make_icon():
    pm = QPixmap(128, 128)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#2563eb"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(8, 8, 112, 112, 26, 26)
    p.setBrush(QColor("white"))
    p.drawRoundedRect(40, 22, 48, 84, 10, 10)
    p.setBrush(QColor("#2563eb"))
    p.drawRoundedRect(45, 30, 38, 62, 4, 4)
    p.setBrush(QColor("white"))
    p.drawEllipse(59, 95, 10, 7)
    p.setPen(QPen(QColor("white"), 5))
    p.drawLine(56, 62, 64, 72)
    p.drawLine(64, 72, 76, 50)
    p.end()
    return pm


STYLE = """
QMainWindow, QWidget#content { background: #f6f7f9; }
QListWidget#nav { background: #111827; color: #e5e7eb; border: none; font-size: 13px; padding-top: 8px; }
QListWidget#nav::item { padding: 12px 14px; border-left: 3px solid transparent; }
QListWidget#nav::item:selected { background: #1f2937; color: white; border-left: 3px solid #3b82f6; }
QListWidget#nav::item:hover { background: #1f2937; }
QFrame#header { background: white; border-bottom: 1px solid #e5e7eb; }
QLabel[class="h1"] { font-size: 20px; font-weight: 600; color: #111827; padding: 4px 0 8px 0; }
QLabel[class="h2"] { font-size: 14px; font-weight: 600; color: #111827; }
QLabel[class="muted"] { color: #6b7280; }
QFrame[class="banner"], QLabel[class="banner"] { background: #fef3c7; border: 1px solid #fcd34d; border-radius: 6px; padding: 8px; color: #78350f; }
QGroupBox { background: white; border: 1px solid #e5e7eb; border-radius: 8px; margin-top: 14px; padding: 10px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #374151; }
QGroupBox QLabel { font-weight: normal; }
QPushButton { background: white; border: 1px solid #d1d5db; border-radius: 6px; padding: 6px 12px; color: #111827; }
QPushButton:hover { background: #f3f4f6; }
QPushButton:disabled { color: #9ca3af; }
QPushButton[class="primary"] { background: #2563eb; color: white; border: 1px solid #1d4ed8; font-weight: 600; }
QPushButton[class="primary"]:hover { background: #1d4ed8; }
QPushButton[class="primary"]:disabled { background: #93c5fd; }
QPushButton[class="danger"] { color: #b91c1c; border-color: #fca5a5; }
QToolButton[class="tile"] { background: white; border: 1px solid #e5e7eb; border-radius: 10px; font-size: 14px; padding: 8px; color: #111827; }
QToolButton[class="tile"]:hover { border-color: #3b82f6; background: #eff6ff; }
QProgressBar { border: 1px solid #d1d5db; border-radius: 5px; background: white; text-align: center; height: 20px; color: #111827; }
QProgressBar::chunk { background: #3b82f6; border-radius: 4px; }
QTableWidget { background: white; alternate-background-color: #f9fafb; border: 1px solid #e5e7eb; border-radius: 6px; gridline-color: #f3f4f6; color: #111827; }
QHeaderView::section { background: #f3f4f6; border: none; border-bottom: 1px solid #e5e7eb; padding: 5px; font-weight: 600; color: #374151; }
QLineEdit, QComboBox, QDoubleSpinBox { background: white; border: 1px solid #d1d5db; border-radius: 5px; padding: 4px 6px; color: #111827; }
QPlainTextEdit { background: #0b1020; color: #e5e7eb; border-radius: 6px; }
QLabel, QCheckBox, QRadioButton { color: #111827; }
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("idevice-toolkit", "idevice-toolkit")
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(make_icon()))
        self.resize(1240, 820)
        self.tasks = set()
        self.busy = False
        self.polling = False
        self.dev = {"mode": "none"}
        self.sig = None

        central = QWidget()
        self.setCentralWidget(central)
        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(180)
        h.addWidget(self.nav)
        right = QVBoxLayout()
        right.setSpacing(0)
        h.addLayout(right, 1)

        hdr = QFrame()
        hdr.setObjectName("header")
        hl = QHBoxLayout(hdr)
        self.h_icon = QLabel()
        self.h_icon.setPixmap(make_icon().scaled(36, 36, Qt.AspectRatioMode.KeepAspectRatio,
                                                 Qt.TransformationMode.SmoothTransformation))
        self.h_name = QLabel("No device")
        f = self.h_name.font()
        f.setPointSize(13)
        f.setBold(True)
        self.h_name.setFont(f)
        self.h_sub = QLabel("")
        self.h_sub.setProperty("class", "muted")
        self.h_mode = QLabel("")
        hl.addWidget(self.h_icon)
        col = QVBoxLayout()
        col.addWidget(self.h_name)
        col.addWidget(self.h_sub)
        hl.addLayout(col)
        hl.addStretch(1)
        hl.addWidget(self.h_mode)
        right.addWidget(hdr)

        self.stack = QStackedWidget()
        self.stack.setObjectName("content")
        right.addWidget(self.stack, 1)

        self.logs = LogsPage(self)          # first so log() works for the rest
        self.toolbox = ToolboxPage(self)
        self.device = DevicePage(self)
        self.flash = FlashPage(self)
        self.backup = BackupPage(self)
        self.apps = AppsPage(self)
        self.files = FilesPage(self)
        self.settings_page = SettingsPage(self)
        self.pages = {}
        st = QApplication.style()
        for name, page, icon in (
                ("Device", self.device, QStyle.StandardPixmap.SP_ComputerIcon),
                ("Flash", self.flash, QStyle.StandardPixmap.SP_DriveHDIcon),
                ("Backup", self.backup, QStyle.StandardPixmap.SP_DialogSaveButton),
                ("Apps", self.apps, QStyle.StandardPixmap.SP_FileDialogDetailedView),
                ("Files", self.files, QStyle.StandardPixmap.SP_DirIcon),
                ("Toolbox", self.toolbox, QStyle.StandardPixmap.SP_FileDialogContentsView),
                ("Logs", self.logs, QStyle.StandardPixmap.SP_FileDialogInfoView),
                ("Settings", self.settings_page, QStyle.StandardPixmap.SP_FileDialogListView)):
            wrap = QWidget()
            wl = QVBoxLayout(wrap)
            wl.setContentsMargins(18, 10, 18, 14)
            wl.addWidget(page)
            self.stack.addWidget(wrap)
            self.pages[name] = self.stack.count() - 1
            QListWidgetItem(st.standardIcon(icon), name, self.nav)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self.statusBar().showMessage("Starting…")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(2000)
        QTimer.singleShot(100, lambda: self.poll(force=True))
        QTimer.singleShot(400, self.check_permissions)
        self.log(f"{APP_NAME} started. idevicerestore: {shutil.which('idevicerestore')}")

    # -- infra
    def bg(self, fn, cb):
        t = Task(fn, self)
        self.tasks.add(t)
        t.result.connect(cb)
        t.finished.connect(lambda: self.tasks.discard(t))
        t.start()

    def log(self, text, kind=""):
        for line in str(text).splitlines() or [""]:
            self.logs.append(line, kind)

    def status(self, text):
        self.statusBar().showMessage(text)

    def show_page(self, name):
        self.nav.setCurrentRow(self.pages[name])

    def download_dir(self):
        return self.settings.value("download_dir", HOME)

    def set_busy(self, busy, msg=""):
        self.busy = busy
        self.status(msg if busy else "Ready")

    def require_normal(self):
        if self.dev["mode"] != "normal" or not self.dev.get("udid"):
            QMessageBox.information(self, APP_NAME, "This needs a device connected and booted normally "
                                    "(not in Recovery/DFU).")
            return False
        if not self.dev.get("paired", True):
            QMessageBox.information(self, APP_NAME, "The device isn't trusted yet. Unlock it and use "
                                    "Pair / Trust on the Device page.")
            self.show_page("Device")
            return False
        return True

    def run_logged(self, args, cwd=None, title="Working", done=None):
        """Run a command, stream output to the console, show result in status bar."""
        self.log("$ " + " ".join(args), "cmd")
        self.status(title + "…")
        p = Proc(args, cwd=cwd, parent=self)
        out = []
        p.line.connect(lambda s: (out.append(s), self.log(s, "err" if "ERROR" in s else "")))

        def fin(rc):
            self.status(f"{title}: {'done' if rc == 0 else 'failed (exit %d)' % rc}")
            if rc != 0:
                QMessageBox.warning(self, title, "\n".join(out[-8:]) or f"Failed (exit {rc})")
            if done:
                done(rc)
        p.done.connect(fin)
        p.start()
        return p

    def run_many(self, cmds, title, done=None):
        cmds = list(cmds)
        self.status(f"{title}…")

        def nxt(rc=0):
            if rc != 0:
                self.log(f"{title}: a step failed (exit {rc})", "err")
            if not cmds:
                self.status(f"{title}: done")
                if done:
                    done()
                return
            c = cmds.pop(0)
            self.log("$ " + " ".join(c), "cmd")
            p = Proc(c, parent=self)
            p.line.connect(lambda s: (self.log(s, "err" if s.startswith(("Error", "ERROR")) else ""),
                                      self.status(f"{title}: {s[:100]}")))
            p.done.connect(nxt)
            p.start()
        nxt()

    def check_permissions(self):
        """Ask for admin rights at startup if Recovery/DFU USB access isn't set up yet."""
        if os.geteuid() == 0 or udev_rule_installed():
            return
        if self.settings.value("skip_perm_prompt", False, type=bool):
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(APP_NAME)
        box.setText("<b>Administrator permission needed</b>")
        box.setInformativeText(
            "To flash and query iPhones/iPads in Recovery or DFU mode, this app needs access to "
            "Apple USB devices, which is root-only by default.\n\n"
            "Grant it now? You'll be asked for your password once. A udev rule is installed so "
            "you won't be asked again.\n\n"
            "If you choose Not now, you'll be asked for your password each time you flash.")
        dont = QCheckBox("Don't ask again at startup")
        box.setCheckBox(dont)
        grant = box.addButton("Grant access", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(grant)
        box.exec()
        self.settings.setValue("skip_perm_prompt", dont.isChecked())
        if box.clickedButton() is grant:
            self.fix_udev(confirm=False)

    def fix_udev(self, confirm=True):
        rule = ('# idevice-toolkit: let logged-in users access Apple devices in Recovery/DFU mode\n'
                'SUBSYSTEM=="usb", ATTR{idVendor}=="05ac", ATTR{idProduct}=="122[0-9a-f]", MODE="0666", TAG+="uaccess"\n'
                'SUBSYSTEM=="usb", ATTR{idVendor}=="05ac", ATTR{idProduct}=="128[0-3]", MODE="0666", TAG+="uaccess"\n'
                'SUBSYSTEM=="usb", ATTR{idVendor}=="05ac", ATTR{idProduct}=="1338", MODE="0666", TAG+="uaccess"\n')
        if confirm and QMessageBox.question(self, "USB permissions",
                                            f"Install this rule to {UDEV_RULE}?\n\n{rule}"
                                            ) != QMessageBox.StandardButton.Yes:
            return
        tmp = os.path.join(CACHE, "39-idevice-toolkit.rules")
        with open(tmp, "w") as f:
            f.write(rule)
        script = f"install -m 644 '{tmp}' '{UDEV_RULE}' && udevadm control --reload-rules && udevadm trigger --subsystem-match=usb"

        def done(rc):
            self.settings_page.update_udev()
            if rc == 0:
                self.flash.root.setChecked(False)
                self.sig = None          # re-detect so irecovery details appear
                self.poll(force=True)
                QMessageBox.information(self, "USB permissions", "Access granted. If a device is in "
                                        "Recovery/DFU right now and still isn't readable, replug it.")
        self.run_logged(["pkexec", "sh", "-c", script], title="Installing udev rule", done=done)

    # -- device monitor
    def poll(self, force=False):
        if (self.busy and not force) or self.polling:
            return
        self.polling = True
        self.bg(detect_device, self.on_detect)

    def on_detect(self, st):
        self.polling = False
        if isinstance(st, Exception) or self.busy:
            return
        sig = (st["mode"], st.get("udid"), st.get("ecid"))
        if sig == self.sig:
            return
        if st["mode"] == "normal" and not st.get("udid"):
            return  # usbmuxd hasn't picked it up yet
        self.sig = sig
        old = self.dev
        self.dev = st
        # keep known identity across mode changes
        for k in ("product_type", "name"):
            if not st.get(k) and old.get(k) and (old.get("ecid") == st.get("ecid") or not st.get("ecid")):
                st[k] = old[k]
        if st.get("product_type") and not st.get("name"):
            pt = st["product_type"]
            self.bg(lambda: marketing_name(pt), lambda n: (self.dev.__setitem__("name", n), self.update_header()))
        self.log(f"Device: {st['mode'].upper()} {st.get('product_type', '')} "
                 f"{st.get('udid') or st.get('ecid') or ''}", "ok" if st["mode"] != "none" else "")
        self.update_header()
        for page in (self.device, self.flash, self.apps, self.files):
            page.on_device(st)

    def update_header(self):
        d = self.dev
        mode = d["mode"]
        colors = {"normal": "#16a34a", "recovery": "#d97706", "dfu": "#7c3aed", "none": "#6b7280"}
        label = {"normal": "Connected", "recovery": "Recovery mode", "dfu": "DFU mode", "none": "Disconnected"}[mode]
        self.h_mode.setText(f'<span style="background:{colors[mode]};color:white;padding:4px 10px;'
                            f'border-radius:10px;font-weight:600">&nbsp;{label}&nbsp;</span>')
        if mode == "none":
            self.h_name.setText("No device")
            self.h_sub.setText("Connect an iPhone or iPad via USB")
            self.status("Waiting for device…")
            return
        name = d.get("name") or d.get("product_type") or "Apple device"
        if d.get("device_name"):
            name = f"{d['device_name']} — {name}"
        self.h_name.setText(name)
        bits = [d.get("product_type", "")]
        if d.get("ios"):
            bits.append(f"iOS {d['ios']}")
        if d.get("ecid"):
            bits.append(f"ECID {d['ecid']}")
        if d.get("udid"):
            bits.append(d["udid"])
        self.h_sub.setText("   ·   ".join(b for b in bits if b))
        if not self.busy:
            self.status("Ready")

    def closeEvent(self, e):
        if self.busy:
            if QMessageBox.warning(self, APP_NAME, "A restore is running. Quitting may interrupt it. Quit anyway?",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                   ) != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        if self.logs.sys and self.logs.sys.running():
            self.logs.sys.kill()
        self.timer.stop()
        for t in list(self.tasks):
            t.wait(3000)
        e.accept()


def light_palette():
    from PyQt6.QtGui import QPalette
    p = QPalette()
    R = QPalette.ColorRole
    for role, col in ((R.Window, "#f6f7f9"), (R.WindowText, "#111827"), (R.Base, "#ffffff"),
                      (R.AlternateBase, "#f9fafb"), (R.Text, "#111827"), (R.Button, "#ffffff"),
                      (R.ButtonText, "#111827"), (R.Highlight, "#2563eb"), (R.HighlightedText, "#ffffff"),
                      (R.ToolTipBase, "#111827"), (R.ToolTipText, "#ffffff"), (R.PlaceholderText, "#9ca3af"),
                      (R.Link, "#2563eb"), (R.Light, "#ffffff"), (R.Midlight, "#e5e7eb"),
                      (R.Mid, "#d1d5db"), (R.Dark, "#9ca3af"), (R.Shadow, "#6b7280")):
        p.setColor(role, QColor(col))
    for role in (R.WindowText, R.Text, R.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor("#9ca3af"))
    return p


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("idevice-toolkit")
    app.setStyle("Fusion")
    app.setPalette(light_palette())   # don't inherit a dark desktop palette under our light stylesheet
    app.setStyleSheet(STYLE)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
