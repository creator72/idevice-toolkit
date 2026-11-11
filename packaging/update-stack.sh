#!/bin/bash
# Update the git-built restore stack (libtatsu, libirecovery, libimobiledevice, idevicerestore)
# and install it into /opt/idevice — NOT /usr/local, so KDE keeps using the distro libimobiledevice.
set -euo pipefail
PREFIX=/opt/idevice
# Source trees, in build order. Override with IDEVICE_TREES="dir1 dir2 ..."
if [ -n "${IDEVICE_TREES:-}" ]; then
  read -ra TREES <<<"$IDEVICE_TREES"
else
  TREES=("$HOME/Music/idevicerestore/libtatsu" "$HOME/Music/idevicerestore/libirecovery" "$HOME/libimobiledevice" "$HOME/Music/idevicerestore")
fi

if pgrep -x idevicerestore >/dev/null; then
  echo "idevicerestore is running — wait for the restore to finish." >&2
  exit 1
fi

export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"
export LDFLAGS="-Wl,-rpath,$PREFIX/lib -L$PREFIX/lib"
export CFLAGS="-O2 -I$PREFIX/include"
for d in "${TREES[@]}"; do
  echo "--- $(basename "$d")"
  cd "$d"
  git pull --ff-only
  ./autogen.sh --prefix="$PREFIX" >build.log 2>&1 || { tail -20 build.log; exit 1; }
  make -j"$(nproc)" >>build.log 2>&1 || { grep -iE "error" build.log | tail -20; exit 1; }
  sudo make install >>build.log 2>&1 || { tail -20 build.log; exit 1; }
done
for f in "$PREFIX"/bin/*; do sudo ln -sfn "$f" "/usr/local/bin/$(basename "$f")"; done
sudo ldconfig
idevicerestore --version
