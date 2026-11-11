#!/bin/bash
# Build idevice-toolkit_<version>_<arch>.deb
#
# Bundles:
#   - the GUI                       -> /usr/share/idevice-toolkit, /usr/bin/idevice-toolkit
#   - the git-master restore stack  -> /opt/idevice (idevicerestore, irecovery, libimobiledevice
#     tools + libtatsu/libirecovery/libimobiledevice shared libs, linked with rpath=/opt/idevice/lib)
#   - a udev rule so Recovery/DFU devices work without root
#
# The stack is taken from $STACK (default /opt/idevice), which must have been built with
#   ./autogen.sh --prefix=/opt/idevice  LDFLAGS=-Wl,-rpath,/opt/idevice/lib
# (packaging/update-stack.sh does that). Source trees are read for licence files and commit ids.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${VERSION:-$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' "$HERE/src/main.py")}"
STACK="${STACK:-/opt/idevice}"
ARCH="$(dpkg --print-architecture)"
if [ -n "${IDEVICE_TREES:-}" ]; then
  read -ra TREES <<<"$IDEVICE_TREES"
else
  TREES=("$HOME/Music/idevicerestore/libtatsu" "$HOME/Music/idevicerestore/libirecovery" "$HOME/libimobiledevice" "$HOME/Music/idevicerestore")
fi
OUT="$HERE/dist"
PKG="$(mktemp -d)"
trap 'rm -rf "$PKG"' EXIT

[ -x "$STACK/bin/idevicerestore" ] || { echo "No restore stack in $STACK (run packaging/update-stack.sh)"; exit 1; }
[ -n "$VERSION" ] || { echo "Could not read APP_VERSION from src/main.py"; exit 1; }

echo ">> staging GUI"
install -Dm644 "$HERE/src/main.py" "$PKG/usr/share/idevice-toolkit/main.py"
install -Dm644 "$HERE/data/idevice-toolkit.desktop" "$PKG/usr/share/applications/idevice-toolkit.desktop"
install -Dm644 "$HERE/data/idevice-toolkit.png" "$PKG/usr/share/icons/hicolor/128x128/apps/idevice-toolkit.png"
install -Dm644 "$HERE/data/39-idevice-toolkit.rules" "$PKG/usr/lib/udev/rules.d/39-idevice-toolkit.rules"
install -d "$PKG/usr/bin"
cat > "$PKG/usr/bin/idevice-toolkit" <<'EOF'
#!/bin/sh
exec python3 /usr/share/idevice-toolkit/main.py "$@"
EOF
chmod 755 "$PKG/usr/bin/idevice-toolkit"

echo ">> staging restore stack from $STACK"
install -d "$PKG/opt/idevice/bin" "$PKG/opt/idevice/lib"
cp -a "$STACK"/bin/. "$PKG/opt/idevice/bin/"
cp -a "$STACK"/lib/*.so.* "$PKG/opt/idevice/lib/"          # runtime libs only (no .a/.la/pkgconfig/headers)
[ -d "$STACK/share/man" ] && { install -d "$PKG/opt/idevice/share"; cp -a "$STACK/share/man" "$PKG/opt/idevice/share/"; }
find "$PKG/opt/idevice" -type f \( -perm -u+x -o -name '*.so.*' \) -exec strip --strip-unneeded {} + 2>/dev/null || true

echo ">> licences + sources"
DOC="$PKG/usr/share/doc/idevice-toolkit"
install -d "$DOC/licenses"
[ -f "$HERE/LICENSE" ] && install -m644 "$HERE/LICENSE" "$DOC/copyright"
{
  echo "idevice-toolkit $VERSION bundles these unmodified upstream projects, built from git:"
  echo
  for d in "${TREES[@]}"; do
    n="$(basename "$(git -C "$d" remote get-url origin)" .git)"
    printf '  %-18s %s @ %s (%s)\n' "$n" "$(git -C "$d" remote get-url origin)" \
      "$(git -C "$d" rev-parse HEAD)" "$(git -C "$d" log -1 --format=%cs)"
    for f in COPYING COPYING.LESSER; do
      [ -f "$d/$f" ] && install -m644 "$d/$f" "$DOC/licenses/$n.$f"
    done
  done
  echo
  echo "They are licensed under the GNU LGPL (see licenses/). Source code for the exact"
  echo "commits above is available from the listed repositories."
} > "$DOC/SOURCES"

echo ">> computing library dependencies"
declare -A DEPS=()
# direct NEEDED entries only (not the transitive closure ldd prints)
while read -r soname; do
  [ -e "$PKG/opt/idevice/lib/$soname" ] && continue          # bundled
  lib="$(ldconfig -p | awk -v s="$soname" '$1 == s && /x86-64|aarch64|=>/ {print $NF; exit}')"
  [ -n "$lib" ] || { echo "   WARNING: cannot resolve $soname"; continue; }
  real="$(readlink -f "$lib")"
  for cand in "$lib" "$real" "${real#/usr}" "/usr$real"; do
    pkg="$(dpkg -S "$cand" 2>/dev/null | head -1 | cut -d: -f1)" && [ -n "$pkg" ] && { DEPS[$pkg]=1; break; }
  done
done < <(find "$PKG/opt/idevice" -type f \( -perm -u+x -o -name '*.so.*' \) -exec readelf -d {} \; 2>/dev/null \
          | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p' | sort -u)
unset 'DEPS[libc6]'
LIBDEPS="$(printf '%s\n' "${!DEPS[@]}" | sort | paste -sd, | sed 's/,/, /g')"
echo "   $LIBDEPS"

IR_VER="$("$STACK/bin/idevicerestore" --version | head -1)"
SIZE="$(du -sk "$PKG" | cut -f1)"
install -d "$PKG/DEBIAN"
cat > "$PKG/DEBIAN/control" <<EOF
Package: idevice-toolkit
Version: $VERSION
Architecture: $ARCH
Maintainer: creator72 <18284043+creator72@users.noreply.github.com>
Installed-Size: $SIZE
Depends: libc6, python3, python3-pyqt6, usbmuxd, usbutils, pkexec, coreutils, $LIBDEPS
Suggests: ideviceinstaller
Section: utils
Priority: optional
Homepage: https://github.com/creator72/idevice-toolkit
Description: 3uTools-style GUI to flash, back up and manage iPhones and iPads
 A Qt desktop app wrapping libimobiledevice and idevicerestore: device info and
 battery health, firmware download and restore (with Recovery/DFU detection),
 backups, file browser, app management, crash logs, syslog and more.
 .
 Bundles current git builds of idevicerestore, libirecovery, libtatsu and
 libimobiledevice in /opt/idevice (separate from the distro libraries):
 $IR_VER
EOF
cat > "$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
  udevadm control --reload-rules 2>/dev/null || true
  udevadm trigger --subsystem-match=usb --attr-match=idVendor=05ac 2>/dev/null || true
  command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
  command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
fi
exit 0
EOF
cat > "$PKG/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
udevadm control --reload-rules 2>/dev/null || true
exit 0
EOF
chmod 755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/postrm"

mkdir -p "$OUT"
DEB="$OUT/idevice-toolkit_${VERSION}_${ARCH}.deb"
fakeroot dpkg-deb --build -Zxz "$PKG" "$DEB" >/dev/null
echo ">> built $DEB ($(du -h "$DEB" | cut -f1))"
