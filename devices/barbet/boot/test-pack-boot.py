#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Host tests for the Barbet boot image packer.

Run with: python3 devices/barbet/boot/test-pack-boot.py
"""
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


def run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def make_template(directory):
    """A small version 2 image with the template's header and AVB footer."""
    kernel = b"K" * (3 * PAGE)
    ramdisk = b"R" * (5 * PAGE)
    dtb = b"\xd0\x0d\xfe\xed" + b"D" * (PAGE - 4)
    (directory / "kernel").write_bytes(kernel)
    (directory / "ramdisk").write_bytes(ramdisk)
    (directory / "dtb").write_bytes(dtb)
    payload = directory / "payload.img"
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
        "-o", str(payload),
    ])
    if result.returncode:
        raise AssertionError(result.stderr)
    template = directory / "boot_b.img"
    shutil.copyfile(payload, template)
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
    return template, kernel, ramdisk, dtb


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
        self.template, self.template_kernel, self.ramdisk, self.dtb = make_template(self.tmp)

    def pack(self, template=None, image=None, ramdisk=None, output=None,
             evidence=None, env=None):
        template = template or self.template
        image = image or self.tmp / "Image"
        ramdisk = ramdisk or self.tmp / "ramdisk"
        output = output or self.tmp / "boot.img"
        if not Path(image).exists():
            Path(image).write_bytes(b"P" * (4 * PAGE) + b"kernel-ends")
        if not Path(ramdisk).exists():
            Path(ramdisk).write_bytes(self.ramdisk)
        argv = [str(PACKER), str(template), str(image), str(ramdisk), str(output)]
        if evidence:
            argv.append(str(evidence))
        return run(argv, env=env)

    def test_packs_the_kernel_without_touching_the_rest_of_the_template(self):
        image = self.tmp / "Image"
        image.write_bytes(b"P" * (4 * PAGE) + b"kernel-ends")
        evidence = self.tmp / "evidence.json"
        result = self.pack(image=image, evidence=evidence)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.tmp / "boot.img"
        self.assertEqual(output.stat().st_size, PARTITION)

        extracted = self.tmp / "unpacked"
        unpack(output, extracted)
        self.assertEqual((extracted / "kernel").read_bytes(), image.read_bytes())
        self.assertEqual((extracted / "ramdisk").read_bytes(), self.ramdisk)
        self.assertEqual((extracted / "dtb").read_bytes(), self.dtb)

        fields = struct.unpack_from("<10I", output.read_bytes(), 8)
        self.assertEqual(fields[0], image.stat().st_size)
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
        self.assertEqual(record["kernel"]["sha256"], sha(image))
        self.assertEqual(record["ramdisk"]["sha256"], sha(self.tmp / "ramdisk"))
        self.assertEqual(record["template"]["sha256"], sha(self.template))

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

    def test_rejects_a_kernel_that_does_not_fit(self):
        image = self.tmp / "Image"
        image.write_bytes(b"P" * PARTITION)
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
