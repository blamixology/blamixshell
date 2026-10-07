#!/usr/bin/env bash
# apt-get install for CI that can't hang: skips apt when the packages are already there (the runner images have
# most of them), gives up on a silent mirror after a few seconds, and if the runner's Azure mirror doesn't answer,
# tries again with archive.ubuntu.com.
#   .github/apt-install.sh pkg1 pkg2 ...
set -u
missing=()
for p in "$@"; do
  dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || missing+=("$p")
done
[ ${#missing[@]} -eq 0 ] && { echo "already installed: $*"; exit 0; }
echo "installing: ${missing[*]}"

APT=(sudo apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20
     -o DPkg::Lock::Timeout=120)

attempt() {
  timeout 300 "${APT[@]}" update -q && timeout 600 "${APT[@]}" install -y -q --no-install-recommends "${missing[@]}"
}

attempt && exit 0
echo "::warning::apt failed with the runner's mirror; switching to archive.ubuntu.com"
for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
  [ -f "$f" ] || continue
  sudo sed -i -e 's#mirror+file:/etc/apt/apt-mirrors.txt#http://archive.ubuntu.com/ubuntu/#g' \
              -e 's#http://azure.archive.ubuntu.com/ubuntu#http://archive.ubuntu.com/ubuntu#g' "$f"
done
attempt
