#!/bin/sh
# SPDX-License-Identifier: MIT
#
# Build a Google Pixel 5a (barbet) boot image whose ramdisk is the Phonewave
# early userspace on a carrier ramdisk.
#
# Usage: build-boot.sh OUTPUT --kernel IMAGE --dtb DTB
#          [--template BOOT_IMG] [--ramdisk CARRIER.gz] [--rescue]
#          [--append-cmdline WORDS] [--evidence FILE]
#
# The carrier ramdisk is kept byte for byte and the early userspace is
# appended to it as one more gzip member, the way postmarketOS and Luma build
# theirs: the kernel unpacks concatenated gzip members in order and a later
# entry replaces an earlier one. Only /init is replaced, and bin/flash-boot is
# added; the carrier supplies /bin/busybox and its loader, which is why the
# early userspace runs even though it is not packed in this tree yet.
#
# --rescue adds /etc/stay-in-rescue to the overlay, so this image always stops
# in the rescue. That is for bring-up only, never for a phone that should boot
# an installed system.
#
# The image itself is packed by pack-boot.py: the template supplies the
# header, command line and AVB properties, the kernel is LZ4-frame compressed
# and the DTB is packed beside it. Nothing here reaches the phone; writing the
# image is a separate, explicit step.
set -eu

usage='usage: build-boot.sh OUTPUT --kernel IMAGE --dtb DTB
       [--template BOOT_IMG] [--ramdisk CARRIER.gz] [--rescue]
       [--append-cmdline WORDS] [--evidence FILE]'

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../../.." && pwd)
vault=${PHONEWAVE_VAULT:-/mnt/development/denial-os/device-vault}

out=${1:?$usage}
shift
kernel= dtb= rescue= append= evidence=
template=$vault/barbet/backups/boot_b.img
ramdisk=$vault/barbet/boot/ramdisk.gz

while [ "$#" -gt 0 ]; do
	case $1 in
	--kernel) kernel=${2:?$usage}; shift 2 ;;
	--dtb) dtb=${2:?$usage}; shift 2 ;;
	--template) template=${2:?$usage}; shift 2 ;;
	--ramdisk) ramdisk=${2:?$usage}; shift 2 ;;
	--rescue) rescue=1; shift ;;
	--append-cmdline) append=${2:?$usage}; shift 2 ;;
	--evidence) evidence=${2:?$usage}; shift 2 ;;
	*) echo "$usage" >&2; exit 2 ;;
	esac
done

[ -n "$kernel" ] && [ -n "$dtb" ] || { echo "$usage" >&2; exit 2; }
for path in "$kernel" "$dtb" "$template" "$ramdisk"; do
	[ -f "$path" ] || { echo "build-boot: $path: missing" >&2; exit 1; }
done

umask 022
work=$(mktemp -d "${TMPDIR:-/tmp}/barbet-boot.XXXXXX")
trap 'rm -rf "$work"' EXIT

# The overlay: the shared early userspace and the from-phone boot writer. The
# carrier's /bin/busybox runs /init through its shebang; nothing else is taken
# from it.
#
# Only files go in, never a directory entry: the carrier is merged-usr, so its
# /bin is a symlink to usr/bin, and the kernel's initramfs extractor unlinks an
# entry whose type differs from what is there. A "bin" directory entry would
# remove that symlink, leave /bin/busybox missing and make /init fail with
# ENOENT. Naming a file under the symlink is fine; the extractor follows it.
overlay=$work/overlay
mkdir -p "$overlay/bin"
install -m 0755 "$repo/image/initramfs/init" "$overlay/init"
install -m 0755 "$here/flash-boot" "$overlay/bin/flash-boot"
entries='init
bin/flash-boot'
if [ -n "$rescue" ]; then
	mkdir -p "$overlay/etc"
	: >"$overlay/etc/stay-in-rescue"
	entries="$entries
etc/stay-in-rescue"
fi
for path in $entries; do
	touch -h -d @0 "$overlay/$path"
done
(cd "$overlay" && printf '%s\n' "$entries" |
	cpio -o -H newc -R 0:0 --reproducible --quiet) >"$work/overlay.cpio"
gzip -9n -c "$work/overlay.cpio" >"$work/overlay.cpio.gz"

found=$(gzip -dc "$work/overlay.cpio.gz" | cpio -it 2>/dev/null | LC_ALL=C sort)
want=$(printf '%s\n' "$entries" | LC_ALL=C sort)
[ "$found" = "$want" ] ||
	{ echo "build-boot: the overlay holds '$found', expected '$want'" >&2; exit 1; }

# Keep the carrier as it is: one more member, not a rebuilt archive.
cat "$ramdisk" "$work/overlay.cpio.gz" >"$work/ramdisk.gz"

set -- "$template" "$kernel" "$work/ramdisk.gz" "$out"
[ -z "$evidence" ] || set -- "$@" "$evidence"
set -- "$@" --dtb "$dtb"
[ -z "$append" ] || set -- "$@" --append-cmdline "$append"
python3 "$here/pack-boot.py" "$@"

unpack_bootimg --boot_img "$out" --out "$work/unpacked" >/dev/null
cmp -s "$work/unpacked/ramdisk" "$work/ramdisk.gz" ||
	{ echo "build-boot: the packed ramdisk differs" >&2; exit 1; }

echo "built $out"
echo "sha256 $(sha256sum "$out" | cut -d' ' -f1)"
echo "ramdisk $(sha256sum "$work/ramdisk.gz" | cut -d' ' -f1)"
echo "carrier $(sha256sum "$ramdisk" | cut -d' ' -f1)"
