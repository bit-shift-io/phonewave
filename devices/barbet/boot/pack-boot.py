#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pack a Perpetua kernel into the Pixel 5a's version 2 boot image.

The working boot_b image is the template: its header fields, ramdisk, DTB and
AVB footer are kept, and only the kernel and the sizes that follow from it
change. The output is a complete partition image. Nothing here reaches the
phone; write the result yourself and keep the template.

Usage: pack-boot.py TEMPLATE IMAGE RAMDISK OUTPUT [EVIDENCE]
"""
import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

PAGE = 4096
HEADER_SIZE = 1660
AVB_FOOTER_SIZE = 64


def die(message):
    print(f"pack-boot: {message}", file=sys.stderr)
    sys.exit(1)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def align(size):
    return (size + PAGE - 1) & ~(PAGE - 1)


def parse_template(path):
    data = path.read_bytes()
    if len(data) < PAGE + HEADER_SIZE:
        die(f"{path}: truncated, shorter than a boot image header")
    if data[:8] != b"ANDROID!":
        die(f"{path}: not an Android boot image (no ANDROID magic)")

    fields = struct.unpack_from("<10I", data, 8)
    (kernel_size, kernel_addr, ramdisk_size, ramdisk_addr, second_size,
     second_addr, tags_addr, pagesize, header_version, os_version) = fields
    if header_version != 2:
        die(f"{path}: header version {header_version}, this packer writes version 2")
    if pagesize != PAGE:
        die(f"{path}: page size {pagesize}, this packer writes {PAGE}")
    header_size = struct.unpack_from("<I", data, 1644)[0]
    if header_size != HEADER_SIZE:
        die(f"{path}: header size {header_size}, want {HEADER_SIZE}")
    recovery_dtbo_size = struct.unpack_from("<I", data, 1632)[0]
    dtb_size = struct.unpack_from("<I", data, 1648)[0]
    dtb_addr = struct.unpack_from("<Q", data, 1652)[0]
    if second_size:
        die(f"{path}: a second stage is not supported")
    if recovery_dtbo_size:
        die(f"{path}: a recovery DTBO is not supported")
    if not dtb_size:
        die(f"{path}: the template has no DTB")

    ramdisk_offset = PAGE + align(kernel_size)
    dtb_offset = ramdisk_offset + align(ramdisk_size)
    if dtb_offset + dtb_size > len(data):
        die(f"{path}: truncated, its DTB lies past the end of the image")

    return {
        "data": data,
        "kernel_size": kernel_size,
        "kernel_addr": kernel_addr,
        "ramdisk_size": ramdisk_size,
        "ramdisk_addr": ramdisk_addr,
        "tags_addr": tags_addr,
        "os_version": os_version,
        "cmdline": data[64:576].split(b"\0", 1)[0].decode(),
        "board": data[48:64].split(b"\0", 1)[0].decode(),
        "dtb_addr": dtb_addr,
        "dtb": data[dtb_offset:dtb_offset + dtb_size],
        "ramdisk": data[ramdisk_offset:ramdisk_offset + ramdisk_size],
    }


def parse_avb(data):
    footer = data[-AVB_FOOTER_SIZE:]
    if footer[:4] != b"AVBf":
        die("the template has no AVB footer")
    major, minor, original_size, vbmeta_offset, vbmeta_size = \
        struct.unpack_from(">IIQQQ", footer, 4)
    if (major, minor) != (1, 0) or vbmeta_offset + vbmeta_size > len(data):
        die("the template has a malformed AVB footer")
    vbmeta = data[vbmeta_offset:vbmeta_offset + vbmeta_size]
    if vbmeta[:4] != b"AVB0":
        die("the template's AVB footer does not point at a vbmeta struct")
    auth_size, aux_size = struct.unpack_from(">QQ", vbmeta, 12)
    algorithm = struct.unpack_from(">I", vbmeta, 28)[0]
    descriptors_offset, descriptors_size = struct.unpack_from(">QQ", vbmeta, 96)
    rollback_index = struct.unpack_from(">Q", vbmeta, 112)[0]
    rollback_index_location = struct.unpack_from(">I", vbmeta, 124)[0]
    if algorithm != 0:
        die(f"the template uses AVB algorithm {algorithm}, not NONE")

    aux = vbmeta[256 + auth_size:256 + auth_size + aux_size]
    position = descriptors_offset
    limit = min(len(aux), descriptors_offset + descriptors_size)
    while position + 16 <= limit:
        tag, following = struct.unpack_from(">QQ", aux, position)
        if tag == 2:
            image_size, = struct.unpack_from(">Q", aux, position + 16)
            partition_len, salt_len, digest_len = struct.unpack_from(
                ">III", aux, position + 56)
            name_at = position + 132
            name = aux[name_at:name_at + partition_len].decode()
            salt = aux[name_at + partition_len:name_at + partition_len + salt_len]
            if name == "boot":
                return {
                    "original_size": original_size,
                    "rollback_index": rollback_index,
                    "rollback_index_location": rollback_index_location,
                    "salt": salt.hex(),
                    "hash_image_size": image_size,
                    "digest": aux[name_at + partition_len + salt_len:
                                 name_at + partition_len + salt_len + digest_len].hex(),
                }
        position += 16 + following
    die("the template has no AVB hash descriptor for the boot partition")


def make_payload(template, image, ramdisk, work):
    dtb = work / "template.dtb"
    dtb.write_bytes(template["dtb"])
    payload = work / "payload.img"
    argv = [
        "mkbootimg",
        "--kernel", str(image),
        "--ramdisk", str(ramdisk),
        "--dtb", str(dtb),
        "--header_version", "2",
        "--pagesize", str(PAGE),
        "--base", "0",
        "--kernel_offset", hex(template["kernel_addr"]),
        "--ramdisk_offset", hex(template["ramdisk_addr"]),
        "--tags_offset", hex(template["tags_addr"]),
        "--dtb_offset", hex(template["dtb_addr"]),
        "--output", str(payload),
    ]
    if template["cmdline"]:
        argv += ["--cmdline", template["cmdline"]]
    if template["board"]:
        argv += ["--board", template["board"]]
    if template["os_version"]:
        os_version, os_patch_level = decode_os_version(template["os_version"])
        argv += ["--os_version", os_version, "--os_patch_level", os_patch_level]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode:
        die(f"mkbootimg failed: {result.stderr.strip()}")
    return payload


def decode_os_version(value):
    return (f"{(value >> 25) & 0x7f}.{(value >> 18) & 0x7f}.{(value >> 11) & 0x7f}",
            f"{2000 + ((value >> 4) & 0x7f)}-{value & 0xf:02d}")


def add_footer(payload, avb, partition_size):
    argv = [
        "avbtool", "add_hash_footer",
        "--image", str(payload),
        "--partition_size", str(partition_size),
        "--partition_name", "boot",
        "--hash_algorithm", "sha256",
        "--algorithm", "NONE",
        "--rollback_index", str(avb["rollback_index"]),
        "--rollback_index_location", str(avb["rollback_index_location"]),
        "--salt", avb["salt"],
    ]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode:
        die(f"avbtool failed: {result.stderr.strip()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path)
    parser.add_argument("image", type=Path)
    parser.add_argument("ramdisk", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("evidence", type=Path, nargs="?")
    args = parser.parse_args()

    if args.output.exists():
        die(f"{args.output}: exists; refusing to replace it")
    for path in (args.template, args.image, args.ramdisk):
        if not path.is_file():
            die(f"{path}: missing")
    if not args.image.stat().st_size:
        die(f"{args.image}: empty")
    if not args.ramdisk.stat().st_size:
        die(f"{args.ramdisk}: empty")

    template = parse_template(args.template)
    avb = parse_avb(template["data"])
    partition_size = len(template["data"])

    # mkbootimg and avbtool add a page-aligned DTB after the ramdisk; the AVB
    # footer and its 512-byte vbmeta blob cannot overlap the payload.
    payload_size = (PAGE + align(args.image.stat().st_size) +
                    align(args.ramdisk.stat().st_size) +
                    align(len(template["dtb"])))
    if payload_size + AVB_FOOTER_SIZE + 512 > partition_size:
        die(f"{args.image}: kernel does not fit with the ramdisk and DTB "
            f"in {partition_size} bytes")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="barbet-pack-", dir=args.output.parent))
    try:
        payload = make_payload(template, args.image, args.ramdisk, work)
        add_footer(payload, avb, partition_size)
        try:
            os.link(payload, args.output)
        except FileExistsError:
            die(f"{args.output}: exists; refusing to replace it")
    finally:
        shutil.rmtree(work)

    record = {
        "template": {"sha256": sha(args.template), "bytes": args.template.stat().st_size},
        "kernel": {"sha256": sha(args.image), "bytes": args.image.stat().st_size},
        "ramdisk": {"sha256": sha(args.ramdisk), "bytes": args.ramdisk.stat().st_size},
        "dtb": {"sha256": hashlib.sha256(template["dtb"]).hexdigest(),
                "bytes": len(template["dtb"])},
        "output": {"sha256": sha(args.output), "bytes": args.output.stat().st_size},
        "header": {
            "header_version": 2,
            "page_size": PAGE,
            "cmdline": template["cmdline"],
            "kernel_addr": hex(template["kernel_addr"]),
            "ramdisk_addr": hex(template["ramdisk_addr"]),
            "tags_addr": hex(template["tags_addr"]),
            "dtb_addr": hex(template["dtb_addr"]),
        },
        "avb": {
            "original_size": avb["original_size"],
            "rollback_index": avb["rollback_index"],
            "rollback_index_location": avb["rollback_index_location"],
            "salt": avb["salt"],
            "previous_digest": avb["digest"],
        },
    }
    if args.evidence:
        args.evidence.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(args.output), "sha256": record["output"]["sha256"],
                      "bytes": record["output"]["bytes"]}))


if __name__ == "__main__":
    main()
