#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pack a Perpetua kernel into the Pixel 5a's version 2 boot image.

The working boot_b image is the template: its header fields, ramdisk, DTB and
AVB footer are kept, and only the kernel and the sizes that follow from it
change. The kernel is LZ4-frame compressed and its arm64 text_offset adopts
the template's value, both of which this bootloader requires. The output is a
complete partition image. Nothing here reaches the phone; write the result
yourself and keep the template.

Usage: pack-boot.py TEMPLATE IMAGE RAMDISK OUTPUT [EVIDENCE]
"""
import argparse
import ctypes
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
ARM64_IMAGE_MAGIC = b"ARM\x64"
IMAGE_MAGIC_OFFSET = 0x38
LZ4_FRAME_MAGIC = b"\x04\x22\x4d\x18"


def die(message):
    print(f"pack-boot: {message}", file=sys.stderr)
    sys.exit(1)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def align(size):
    return (size + PAGE - 1) & ~(PAGE - 1)


class FrameInfo(ctypes.Structure):
    _fields_ = [("blockSizeID", ctypes.c_int), ("blockMode", ctypes.c_int),
                ("contentChecksumFlag", ctypes.c_int), ("frameType", ctypes.c_int),
                ("contentSize", ctypes.c_ulonglong), ("dictID", ctypes.c_uint),
                ("blockChecksumFlag", ctypes.c_int)]


class Preferences(ctypes.Structure):
    _fields_ = [("frameInfo", FrameInfo), ("compressionLevel", ctypes.c_int),
                ("autoFlush", ctypes.c_uint), ("favorDecSpeed", ctypes.c_uint),
                ("reserved", ctypes.c_uint * 3)]


LZ4 = None


def lz4():
    global LZ4
    if LZ4 is None:
        try:
            LZ4 = ctypes.CDLL("liblz4.so.1")
        except OSError as error:
            die(f"liblz4 is required to read and write the kernel payload: {error}")
        LZ4.LZ4F_compressFrameBound.restype = ctypes.c_size_t
        LZ4.LZ4F_compressFrameBound.argtypes = [ctypes.c_size_t,
                                                ctypes.POINTER(Preferences)]
        LZ4.LZ4F_compressFrame.restype = ctypes.c_size_t
        LZ4.LZ4F_compressFrame.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                           ctypes.c_void_p, ctypes.c_size_t,
                                           ctypes.POINTER(Preferences)]
        LZ4.LZ4F_createDecompressionContext.restype = ctypes.c_size_t
        LZ4.LZ4F_decompress.restype = ctypes.c_size_t
        LZ4.LZ4F_freeDecompressionContext.restype = ctypes.c_size_t
    return LZ4


def frame_preferences(frame, path):
    """The LZ4 frame settings the working template uses, so ours match."""
    if frame[:4] != LZ4_FRAME_MAGIC:
        die(f"{path}: the template kernel is not an LZ4 frame, so it cannot "
            "be checked against this packer")
    flags, descriptor = frame[4], frame[5]
    if flags & 0x08:
        die(f"{path}: the template LZ4 frame records its content size")
    if flags & 0x01:
        die(f"{path}: the template LZ4 frame uses a dictionary")
    preferences = Preferences()
    preferences.frameInfo.blockSizeID = (descriptor >> 4) & 0x7
    preferences.frameInfo.blockMode = 1 if flags & 0x20 else 0
    preferences.frameInfo.blockChecksumFlag = 1 if flags & 0x10 else 0
    preferences.frameInfo.contentChecksumFlag = 1 if flags & 0x04 else 0
    return preferences


def lz4_decompress(data, path):
    context = ctypes.c_void_p()
    if lz4().LZ4F_createDecompressionContext(ctypes.byref(context), 100):
        die(f"{path}: cannot create an LZ4 frame context")
    out = bytearray()
    position = 0
    while position < len(data):
        chunk = ctypes.create_string_buffer(1 << 20)
        chunk_size = ctypes.c_size_t(len(chunk))
        source_size = ctypes.c_size_t(len(data) - position)
        result = lz4().LZ4F_decompress(context, chunk, ctypes.byref(chunk_size),
                                       data[position:], ctypes.byref(source_size), None)
        if result < 0:
            die(f"{path}: the template kernel is not a valid LZ4 frame")
        out += chunk.raw[:chunk_size.value]
        position += source_size.value
        if result == 0:
            break
    lz4().LZ4F_freeDecompressionContext(context)
    return bytes(out)


def lz4_compress(data, preferences, path):
    bound = lz4().LZ4F_compressFrameBound(len(data), ctypes.byref(preferences))
    out = ctypes.create_string_buffer(bound)
    size = lz4().LZ4F_compressFrame(out, bound, data, len(data),
                                    ctypes.byref(preferences))
    if size <= 0:
        die(f"{path}: LZ4 compression failed ({size})")
    return out.raw[:size]



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
    kernel_payload = data[PAGE:PAGE + kernel_size]
    if len(kernel_payload) != kernel_size:
        die(f"{path}: truncated, its kernel lies past the end of the image")
    preferences = frame_preferences(kernel_payload, path)
    kernel = lz4_decompress(kernel_payload, path)
    if (len(kernel) < IMAGE_MAGIC_OFFSET + 4 or
            kernel[IMAGE_MAGIC_OFFSET:IMAGE_MAGIC_OFFSET + 4] != ARM64_IMAGE_MAGIC):
        die(f"{path}: the template kernel is not an arm64 Image")

    return {
        "data": data,
        "kernel_size": kernel_size,
        "kernel_addr": kernel_addr,
        "kernel_text_offset": struct.unpack_from("<Q", kernel, 8)[0],
        "kernel_preferences": preferences,
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


def prepare_kernel(path, template):
    """Compress the Image the way the template does, and adopt its placement.

    The bootloader rejects an Image whose text_offset field does not match the
    address it places the kernel at. The working template records 0x80000, so
    the packer applies the same value and changes no other Image byte.
    """
    raw = path.read_bytes()
    if (len(raw) < IMAGE_MAGIC_OFFSET + 4 or
            raw[IMAGE_MAGIC_OFFSET:IMAGE_MAGIC_OFFSET + 4] != ARM64_IMAGE_MAGIC):
        die(f"{path}: not an arm64 Image")
    text_offset = struct.unpack_from("<Q", raw, 8)[0]
    patched = bytearray(raw)
    struct.pack_into("<Q", patched, 8, template["kernel_text_offset"])
    return lz4_compress(bytes(patched), template["kernel_preferences"], path), text_offset


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


def make_payload(template, kernel, ramdisk, dtb_data, work):
    dtb = work / "boot.dtb"
    dtb.write_bytes(dtb_data)
    payload = work / "payload.img"
    argv = [
        "mkbootimg",
        "--kernel", str(kernel),
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
    parser.add_argument("--dtb", type=Path,
                        help="device tree to pack instead of the template's")
    parser.add_argument("--append-cmdline",
                        help="words to append to the template command line")
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
    if args.dtb:
        if not args.dtb.is_file():
            die(f"{args.dtb}: missing")
        if args.dtb.read_bytes()[:4] != b"\xd0\x0d\xfe\xed":
            die(f"{args.dtb}: not an FDT")

    template = parse_template(args.template)
    if args.append_cmdline:
        template["cmdline"] = f'{template["cmdline"]} {args.append_cmdline}'.strip()
        if len(template["cmdline"]) >= 512:
            die(f"command line does not fit in 512 bytes: "
                f"{len(template['cmdline'])}")
    avb = parse_avb(template["data"])
    partition_size = len(template["data"])
    dtb = args.dtb.read_bytes() if args.dtb else template["dtb"]
    frame, image_text_offset = prepare_kernel(args.image, template)

    # mkbootimg and avbtool add a page-aligned DTB after the ramdisk; the AVB
    # footer and its 512-byte vbmeta blob cannot overlap the payload.
    payload_size = (PAGE + align(len(frame)) +
                    align(args.ramdisk.stat().st_size) +
                    align(len(dtb)))
    if payload_size + AVB_FOOTER_SIZE + 512 > partition_size:
        die(f"{args.image}: kernel does not fit with the ramdisk and DTB "
            f"in {partition_size} bytes")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="barbet-pack-", dir=args.output.parent))
    try:
        kernel = work / "kernel.lz4"
        kernel.write_bytes(frame)
        payload = make_payload(template, kernel, args.ramdisk, dtb, work)
        add_footer(payload, avb, partition_size)
        try:
            os.link(payload, args.output)
        except FileExistsError:
            die(f"{args.output}: exists; refusing to replace it")
    finally:
        shutil.rmtree(work)

    with open(args.output, "rb") as image:
        image.seek(-AVB_FOOTER_SIZE, os.SEEK_END)
        footer = image.read(AVB_FOOTER_SIZE)
    if footer[:4] != b"AVBf":
        die(f"{args.output}: the packed image has no AVB footer")
    output_original_size = struct.unpack_from(">Q", footer, 12)[0]

    record = {
        "template": {"sha256": sha(args.template), "bytes": args.template.stat().st_size},        "kernel": {"sha256": sha(args.image), "bytes": args.image.stat().st_size,
                   "text_offset": image_text_offset,
                   "patched_text_offset": template["kernel_text_offset"]},
        "kernel_payload": {"sha256": hashlib.sha256(frame).hexdigest(),
                           "bytes": len(frame)},
        "ramdisk": {"sha256": sha(args.ramdisk), "bytes": args.ramdisk.stat().st_size},
        "dtb": {"sha256": hashlib.sha256(dtb).hexdigest(), "bytes": len(dtb),
                "source": "supplied" if args.dtb else "template"},
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
            "original_size": output_original_size,
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
