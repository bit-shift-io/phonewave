#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Host tests for the Barbet boot image packer.

Run with: python3 devices/barbet/boot/test-pack-boot.py
"""
import ctypes
import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKER = HERE / "pack-boot.py"
PAGE = 4096
PARTITION = 1 << 20
CMDLINE = "console=ttyMSM0,115200n8"
SALT = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
ROLLBACK = 1783209600
TEXT_OFFSET = 0x80000


def run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


class FrameInfo(ctypes.Structure):
    _fields_ = [("blockSizeID", ctypes.c_int), ("blockMode", ctypes.c_int),
                ("contentChecksumFlag", ctypes.c_int), ("frameType", ctypes.c_int),
                ("contentSize", ctypes.c_ulonglong), ("dictID", ctypes.c_uint),
                ("blockChecksumFlag", ctypes.c_int)]


class Preferences(ctypes.Structure):
    _fields_ = [("frameInfo", FrameInfo), ("compressionLevel", ctypes.c_int),
                ("autoFlush", ctypes.c_uint), ("favorDecSpeed", ctypes.c_uint),
                ("reserved", ctypes.c_uint * 3)]


LZ4 = ctypes.CDLL("liblz4.so.1")
LZ4.LZ4F_compressFrameBound.restype = ctypes.c_size_t
LZ4.LZ4F_compressFrameBound.argtypes = [ctypes.c_size_t, ctypes.POINTER(Preferences)]
LZ4.LZ4F_compressFrame.restype = ctypes.c_size_t
LZ4.LZ4F_compressFrame.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(Preferences)]
LZ4.LZ4F_createDecompressionContext.restype = ctypes.c_size_t
LZ4.LZ4F_decompress.restype = ctypes.c_size_t
LZ4.LZ4F_freeDecompressionContext.restype = ctypes.c_size_t


def lz4_compress(data):
    preferences = Preferences()
    preferences.frameInfo.blockSizeID = 7
    preferences.frameInfo.blockMode = 1
    preferences.frameInfo.contentChecksumFlag = 1
    bound = LZ4.LZ4F_compressFrameBound(len(data), ctypes.byref(preferences))
    out = ctypes.create_string_buffer(bound)
    size = LZ4.LZ4F_compressFrame(out, bound, data, len(data), ctypes.byref(preferences))
    if size <= 0:
        raise AssertionError(f"LZ4F_compressFrame: {size}")
    return out.raw[:size]


def lz4_decompress(data):
    context = ctypes.c_void_p()
    if LZ4.LZ4F_createDecompressionContext(ctypes.byref(context), 100):
        raise AssertionError("cannot create an LZ4 frame context")
    out = bytearray()
    position = 0
    while position < len(data):
        chunk = ctypes.create_string_buffer(1 << 20)
        chunk_size = ctypes.c_size_t(len(chunk))
        source_size = ctypes.c_size_t(len(data) - position)
        result = LZ4.LZ4F_decompress(context, chunk, ctypes.byref(chunk_size),
                                     data[position:], ctypes.byref(source_size), None)
        if result < 0:
            raise AssertionError(f"LZ4F_decompress: {result}")
        out += chunk.raw[:chunk_size.value]
        position += source_size.value
        if result == 0:
            break
    LZ4.LZ4F_freeDecompressionContext(context)
    return bytes(out)


def arm64_image(payload, text_offset=0):
    """A payload with the arm64 Image header the packer has to understand."""
    image = bytearray(payload)
    struct.pack_into("<Q", image, 8, text_offset)
    image[0x38:0x3c] = b"ARM\x64"
    struct.pack_into("<Q", image, 16, len(image))
    return bytes(image)


def make_template(directory, compress_kernel=True):
    """A small version 2 image with the template's header and AVB footer."""
    kernel = arm64_image(b"K" * (3 * PAGE), TEXT_OFFSET)
    ramdisk = b"R" * (5 * PAGE)
    dtb = b"\xd0\x0d\xfe\xed" + b"D" * (PAGE - 4)
    payload = lz4_compress(kernel) if compress_kernel else kernel
    (directory / "kernel").write_bytes(payload)
    (directory / "ramdisk").write_bytes(ramdisk)
    (directory / "dtb").write_bytes(dtb)
    image = directory / "payload.img"
    result = run([
        "mkbootimg",
        "--kernel", str(directory / "kernel"),
        "--ramdisk", str(directory / "ramdisk"),
        "--dtb", str(directory / "dtb"),
        "--header_version", "2",
        "--pagesize", str(PAGE),
        "--base", "0",
        "--kernel_offset", "0x8000",
        "--ramdisk_offset", "0x10000000",
        "--tags_offset", "0x100",
        "--dtb_offset", "0x18000000",
        "--cmdline", CMDLINE,
        "-o", str(image),
    ])
    if result.returncode:
        raise AssertionError(result.stderr)
    template = directory / "boot_b.img"
    shutil.copyfile(image, template)
    result = run([
        "avbtool", "add_hash_footer",
        "--image", str(template),
        "--partition_size", str(PARTITION),
        "--partition_name", "boot",
        "--algorithm", "NONE",
        "--rollback_index", str(ROLLBACK),
        "--salt", SALT,
    ])
    if result.returncode:
        raise AssertionError(result.stderr)
    return template, kernel, ramdisk, dtb, payload


def unpack(image, directory):
    result = run([
        "unpack_bootimg", "--boot_img", str(image), "--out", str(directory),
    ])
    if result.returncode:
        raise AssertionError(result.stderr)
    return result


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PackBootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="barbet-pack-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.template, self.template_kernel, self.ramdisk, self.dtb, self.payload = \
            make_template(self.tmp)

    def pack(self, template=None, image=None, ramdisk=None, output=None,
             evidence=None, dtb=None, append_cmdline=None, env=None):
        template = template or self.template
        image = image or self.tmp / "Image"
        ramdisk = ramdisk or self.tmp / "ramdisk"
        output = output or self.tmp / "boot.img"
        if not Path(image).exists():
            Path(image).write_bytes(arm64_image(b"P" * (4 * PAGE) + b"kernel-ends"))
        if not Path(ramdisk).exists():
            Path(ramdisk).write_bytes(self.ramdisk)
        argv = [str(PACKER), str(template), str(image), str(ramdisk), str(output)]
        if evidence:
            argv.append(str(evidence))
        if dtb:
            argv += ["--dtb", str(dtb)]
        if append_cmdline:
            argv += ["--append-cmdline", append_cmdline]
        return run(argv, env=env)

    def test_packs_the_compressed_kernel_over_the_template_payload(self):
        image = self.tmp / "Image"
        raw = arm64_image(os.urandom(2 * PAGE), text_offset=0)
        image.write_bytes(raw)
        evidence = self.tmp / "evidence.json"
        result = self.pack(image=image, evidence=evidence)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.tmp / "boot.img"
        self.assertEqual(output.stat().st_size, PARTITION)

        extracted = self.tmp / "unpacked"
        unpack(output, extracted)
        frame = (extracted / "kernel").read_bytes()
        self.assertEqual(frame[:4], b"\x04\x22\x4d\x18", "kernel is not an LZ4 frame")
        patched = lz4_decompress(frame)
        expected = bytearray(raw)
        struct.pack_into("<Q", expected, 8, TEXT_OFFSET)
        self.assertEqual(patched, bytes(expected))
        self.assertEqual(patched[0x38:0x3c], b"ARM\x64")
        self.assertEqual((extracted / "ramdisk").read_bytes(), self.ramdisk)
        self.assertEqual((extracted / "dtb").read_bytes(), self.dtb)

        fields = struct.unpack_from("<10I", output.read_bytes(), 8)
        self.assertEqual(fields[0], len(frame))
        self.assertEqual(fields[2], len(self.ramdisk))
        self.assertEqual(fields[7], PAGE)
        self.assertEqual(fields[8], 2)
        self.assertEqual(int.from_bytes(output.read_bytes()[1648:1652], "little"),
                         len(self.dtb))

        info = run(["avbtool", "info_image", "--image", str(output)])
        self.assertEqual(info.returncode, 0, info.stderr)
        self.assertIn("Algorithm:                NONE", info.stdout)
        self.assertIn(f"Rollback Index:           {ROLLBACK}", info.stdout)
        self.assertIn("Partition Name:        boot", info.stdout)

        record = json.loads(evidence.read_text())
        self.assertEqual(record["output"]["sha256"], sha(output))
        self.assertEqual(record["output"]["bytes"], PARTITION)
        self.assertEqual(record["kernel"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(record["kernel"]["text_offset"], 0)
        self.assertEqual(record["kernel"]["patched_text_offset"], TEXT_OFFSET)
        self.assertEqual(record["kernel_payload"]["sha256"],
                         hashlib.sha256(frame).hexdigest())
        expected_payload = (PAGE + ((len(frame) + PAGE - 1) // PAGE) * PAGE +
                            ((len(self.ramdisk) + PAGE - 1) // PAGE) * PAGE +
                            ((len(self.dtb) + PAGE - 1) // PAGE) * PAGE)
        self.assertEqual(record["avb"]["original_size"], expected_payload)
        self.assertEqual(record["ramdisk"]["sha256"], sha(self.tmp / "ramdisk"))
        self.assertEqual(record["template"]["sha256"], sha(self.template))

    def test_packs_a_supplied_dtb_instead_of_the_template_one(self):
        dtb = b"\xd0\x0d\xfe\xed" + os.urandom(PAGE - 4)
        supplied = self.tmp / "our.dtb"
        supplied.write_bytes(dtb)
        evidence = self.tmp / "evidence.json"
        result = self.pack(dtb=supplied, evidence=evidence)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.tmp / "boot.img"
        extracted = self.tmp / "unpacked"
        unpack(output, extracted)
        self.assertEqual((extracted / "dtb").read_bytes(), dtb)
        self.assertEqual(int.from_bytes(output.read_bytes()[1648:1652], "little"), len(dtb))
        record = json.loads(evidence.read_text())
        self.assertEqual(record["dtb"]["sha256"], hashlib.sha256(dtb).hexdigest())
        self.assertEqual(record["dtb"]["source"], "supplied")

    def test_appends_words_to_the_template_command_line(self):
        result = self.pack(append_cmdline="clk_ignore_unused pd_ignore_unused")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.tmp / "boot.img"
        cmdline = output.read_bytes()[64:576].split(b"\0")[0].decode()
        self.assertEqual(cmdline,
                         CMDLINE + " clk_ignore_unused pd_ignore_unused")

    def test_rejects_a_command_line_that_does_not_fit(self):
        result = self.pack(append_cmdline="x" * 500)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("command line", result.stderr)
        self.assertFalse((self.tmp / "boot.img").exists())

    def test_rejects_a_supplied_file_that_is_not_a_dtb(self):
        supplied = self.tmp / "not.dtb"
        supplied.write_bytes(b"not a device tree" * PAGE)
        result = self.pack(dtb=supplied)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FDT", result.stderr)
        self.assertFalse((self.tmp / "boot.img").exists())

    def test_rejects_a_template_without_the_android_magic(self):
        template = self.tmp / "not-boot.img"
        template.write_bytes(b"X" * PARTITION)
        result = self.pack(template=template)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ANDROID", result.stderr)
        self.assertFalse((self.tmp / "boot.img").exists())

    def test_rejects_a_truncated_template(self):
        template = self.tmp / "short.img"
        template.write_bytes(self.template.read_bytes()[:100])
        result = self.pack(template=template)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("truncated", result.stderr)
        self.assertFalse((self.tmp / "boot.img").exists())

    def test_rejects_a_template_with_an_uncompressed_kernel(self):
        (self.tmp / "raw").mkdir()
        template, _, _, _, _ = make_template(self.tmp / "raw", compress_kernel=False)
        result = self.pack(template=template)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LZ4", result.stderr)
        self.assertFalse((self.tmp / "boot.img").exists())

    def test_rejects_an_image_without_the_arm64_magic(self):
        image = self.tmp / "Image"
        image.write_bytes(b"not a kernel" * PAGE)
        result = self.pack(image=image)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("arm64", result.stderr)
        self.assertFalse((self.tmp / "boot.img").exists())

    def test_rejects_a_kernel_that_does_not_fit(self):
        image = self.tmp / "Image"
        image.write_bytes(arm64_image(os.urandom(PARTITION)))
        result = self.pack(image=image)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not fit", result.stderr)
        self.assertFalse((self.tmp / "boot.img").exists())

    def test_leaves_an_existing_output_alone(self):
        output = self.tmp / "boot.img"
        output.write_bytes(b"keep me")
        result = self.pack(output=output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exists", result.stderr)
        self.assertEqual(output.read_bytes(), b"keep me")

    def test_never_runs_a_phone_command(self):
        marker = self.tmp / "phone-command-ran"
        fake = self.tmp / "fake-bin"
        fake.mkdir()
        for name in ["adb", "fastboot", "scp", "ssh", "sudo"]:
            script = fake / name
            script.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
            script.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{fake}:{env['PATH']}"
        result = self.pack(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
