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
and the packed images. The ramdisk is the Luma one only for the kernel
baseline; the boot userspace comes from the shared image later.

```sh
devices/barbet/boot/pack-boot.py TEMPLATE IMAGE RAMDISK OUTPUT [EVIDENCE]
```

`IMAGE` is a linux-perpetua release's `artifacts/Image`; `RAMDISK` is the
ramdisk extracted from the template. The output is a complete
100663296-byte partition image: the template's header addresses, command
line and DTB, the new kernel, the given ramdisk, and an AVB footer with
algorithm NONE, the template's salt and rollback index 1783209600. The
packer refuses to replace an existing output and never runs a phone command;
writing the image is a separate, explicit step.

The v2 `kernel_addr` field (0x8000) is kept as the template has it. The
bootloader ignores it on arm64 and places the Image itself, so the packer
does not change the Image bytes or claim an address for them.

Verify before writing:

```sh
unpack_bootimg --boot_img OUTPUT --out DIR
sha256sum DIR/kernel artifacts/Image DIR/ramdisk
avbtool info_image --image OUTPUT
avbtool verify_image --image OUTPUT
```

Host tests: `python3 devices/barbet/boot/test-pack-boot.py`.
