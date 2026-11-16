# Google Pixel 5a (barbet)

The phone runs linux-perpetua from slot B. The bootloader uses a version 2
boot image with the kernel, the ramdisk and the DTB in the same partition;
init_boot is not used. The kernel carries its own Barbet tree and hands the
bootloader's Google tree over to it. Slot A keeps stock Android and is never
part of these images.

## Packing a boot image

Private inputs live in the vault,
`${PHONEWAVE_VAULT:-/mnt/development/denial-os/device-vault}/barbet`:
`backups/boot_b.img` is the verified working image whose header, ramdisk, DTB
and AVB properties are the template, and `boot/` holds the retained ramdisk
and the packed images. The retained ramdisk is the Luma one only as a
carrier: it supplies `/bin/busybox` and its loader, and the Phonewave boot
userspace is layered on top of it.

### Building the image

`build-boot.sh` builds the ramdisk and the image in one step: it appends the
shared early userspace (`image/initramfs/`) and `flash-boot` to the carrier
ramdisk as one more gzip member, then calls `pack-boot.py`. The kernel unpacks
concatenated gzip members in order and a later entry replaces an earlier one,
so only `/init` changes and nothing from the carrier is rebuilt.

```sh
devices/barbet/boot/build-boot.sh OUTPUT --kernel IMAGE --dtb DTB
                                [--template BOOT_IMG] [--ramdisk CARRIER.gz]
                                [--rescue] [--append-cmdline WORDS]
                                [--evidence FILE]
```

`--template` and `--ramdisk` default to the vault paths above. `--rescue`
adds `/etc/stay-in-rescue` to the overlay, so the image always stops in the
rescue; that is for bring-up only. The script refuses a carrier without the
expected overlay entries and checks that the packed ramdisk is the one it
built.

The overlay holds files only, never a directory entry. The carrier is
merged-usr, so its `/bin` is a symlink to `usr/bin`; the kernel's initramfs
extractor unlinks an entry whose type differs from what is already there, so a
`bin` directory entry would remove the symlink, leave `/bin/busybox` missing
and make `/init` fail with `Failed to execute /init (error -2)`. A file named
under the symlink is extracted into `usr/bin`, which is what
`bin/flash-boot` does.

The low-level packer can also be run directly:

```sh
devices/barbet/boot/pack-boot.py TEMPLATE IMAGE RAMDISK OUTPUT [EVIDENCE]
                                   [--dtb DTB] [--append-cmdline WORDS]
```

`IMAGE` is a linux-perpetua release's `artifacts/Image`; `RAMDISK` is the
ramdisk extracted from the template. The output is a complete
100663296-byte partition image: the template's header addresses, command
line and DTB, the new kernel, the given ramdisk, and an AVB footer with
algorithm NONE, the template's salt and rollback index 1783209600. The
packer refuses to replace an existing output and never runs a phone command;
writing the image is a separate, explicit step.

`--append-cmdline` adds words to the template command line, for example a
bring-up flag; it refuses a line that no longer fits the 512-byte field.

By default the template's DTB is kept. `--dtb` packs another tree, such as
the one linux-perpetua builds: that tree is the one ABL passes to the kernel,
so the kernel's own description is used instead of the template's. It is also
the tree the handoff compares against, so the two must agree.

The bootloader requires two things of the kernel payload, both taken from
the working template: the Image is LZ4-frame compressed, and its arm64
`text_offset` field is set to the template's 0x80000 instead of the 0 the
build writes. Only those eight header bytes differ; the packer changes no
other Image byte. A raw or differently placed Image is rejected by ABL before
the kernel runs, with `kernel compression not supported` or
`Kernel TextOffset does not match`.

The v2 `kernel_addr` field (0x8000) is kept as the template has it. The
bootloader ignores it on arm64 and places the Image itself.

Verify before writing:

```sh
unpack_bootimg --boot_img OUTPUT --out DIR
avbtool info_image --image OUTPUT
avbtool verify_image --image OUTPUT
# DIR/kernel is the LZ4 frame; decompress it and compare with the release
# Image after setting its text_offset field to 0x80000.
```

Host tests: `python3 devices/barbet/boot/test-pack-boot.py`.

## Flashing from the boot userspace

`flash-boot` writes `boot_b` from the running boot userspace, so a bad kernel
does not cost a fastboot round trip. It downloads the image over the USB
network, checks the SHA-256, finds `boot_b` by its GPT partition name, checks
its size, writes it, verifies the readback and reboots. It never touches any
other partition.

On the workstation:

```sh
devices/barbet/boot/serve-boot.sh /path/to/boot.img
```

In the phone boot userspace (UART or SSH):

```sh
flash-boot http://172.16.42.2:8000/boot.img <sha256>
```

The USB network gives the workstation `172.16.42.2` and the phone
`172.16.42.1`. The script must be present in the boot userspace; for the
temporary Luma ramdisk it is installed under `/usr/bin`.

## Installing the root from the boot userspace

The published image (`https://packages.denialwm.org/images/`, the zip's
`userdata.img`) can be written from the rescue the same way, with
`image/flash-root`:

```sh
flash-root http://10.77.71.1:8000/userdata.img <sha256>
```

It writes the first of the `denial` and `userdata` partitions, as the early
userspace picks its root, and compares the readback before deleting the
image. A rescue with no installed root answers at `10.77.71.2`; the
workstation needs `10.77.71.1/30` on the phone's USB network interface.
`devices/barbet/boot/build-boot.sh` installs `flash-root` beside
`flash-boot`.

## Wi-Fi

The WCN3990 is behind the modem: the modem loads the WLAN firmware while the
ADSP is offline, and `ath10k_snoc` attaches over QRTR. `devices/barbet/wifi/`
holds the one-time `provision` script (modem, WLAN and board firmware from the
phone's own stock partitions, the `rmtfs`/`tqftpserv` units and the start
ordering) and the reason `ath10k` must not be rebound after the modem starts.
See `devices/barbet/wifi/README.md`.

## Audio

The speaker path (TAS2562 pair on QUIN TDM RX) is verified with an audible
tone. The ADSP has to run for the `q6afecc` clock the sound card needs, and it
must start only after Wi-Fi has finished booting the WLAN firmware.
`devices/barbet/audio/` provisions the ADSP image, the RT5514P DSP blobs, the
`barbet-audio.service` unit and the verified mixer route; see
`devices/barbet/audio/README.md`. Microphone capture is mono and still
partial: the DSP loads, but the samples carry a DC offset.

## Bluetooth

The WCN3990 BT side works. The modem powers the combo chip while booting the
WLAN firmware, so `barbet-bt.service` rebinds the BT serdev after
`wcn3990-wifi.service` and starts bluetoothd. The kernel disables the QCA
in-band-sleep handshake and keeps the public address the firmware reports.
See `devices/barbet/bt/README.md`.
