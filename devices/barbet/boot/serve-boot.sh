#!/bin/sh
# SPDX-License-Identifier: MIT
#
# Serve a boot image to a phone that is running the boot userspace, so the
# image can be flashed from the phone without fastboot.
#
# Usage: serve-boot.sh IMAGE
# Then, in the phone's debug shell:
#   flash-boot http://172.16.42.2:8000/IMAGE SHA256
set -eu

image=$(realpath "${1:?image}")
[ -f "$image" ] || {
	echo "serve-boot: $image: missing" >&2
	exit 1
}

sha=$(sha256sum "$image" | cut -d' ' -f1)
host=${PHONE_USB_HOST_IP:-172.16.42.2}
port=${PORT:-8000}
name=$(basename "$image")

echo "serving $name on http://$host:$port/$name"
echo "in the phone debug shell:"
echo "  flash-boot http://$host:$port/$name $sha"

exec python3 -m http.server "$port" --bind 0.0.0.0 --directory "$(dirname "$image")"
