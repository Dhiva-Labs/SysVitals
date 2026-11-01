#!/usr/bin/env bash
# Builds the installed file tree that pack.sh turns into the .rpm and the
# Arch package. SysVitals has no .deb to unpack: it is one Python script, so
# the tree is the script as /usr/bin/sysvitals plus its documentation.
#
# usage: build-tree.sh <root>
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
[ $# -eq 1 ] || { echo "usage: $0 <root>" >&2; exit 2; }
root="$1"
# shellcheck source=/dev/null
. "$here/distro.conf"

mkdir -p "$root/usr/bin"
# The distribution's own interpreter, not whatever python3 is first on PATH
# (a virtualenv or pyenv shim must not capture a system command).
sed '1s|^#!.*$|#!/usr/bin/python3|' "$repo/sysvitals.py" > "$root/usr/bin/$NAME"
chmod 0755 "$root/usr/bin/$NAME"
head -n 1 "$root/usr/bin/$NAME" | grep -qx '#!/usr/bin/python3'
install -Dm644 -t "$root/usr/share/doc/$NAME" "$repo/README.md" "$repo/CHANGELOG.md"
