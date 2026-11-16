# Wi-Fi (WCN3990)

The Pixel 5a's Wi-Fi is a WCN3990 behind the modem. The modem loads the WLAN
firmware, and `ath10k_snoc` talks to it through QRTR (WLFW) and copy engines.
There is no firmware download from Linux: `firmware-5.bin` is only a
descriptor, and `board-2.bin`/`board.bin` are the phone's own calibration.

## What makes it work

- **The modem boots the WLAN firmware, but only while the ADSP is offline.**
  `barbet-wifi-start` stops the ADSP, starts the modem, and only then lets the
  audio unit start the ADSP again.
- **`ath10k` must already be bound when the modem announces WLFW.** Its QMI
  client exists before the modem starts. Rebinding `ath10k_snoc` afterwards
  makes the next request race the firmware startup: the firmware answers the
  host-capability request with `QMI_ERR_INCOMPATIBLE_STATE` (`host capability
  request rejected: 90`) and no `wlan0` appears. Do not unbind/bind as part of
  the bring-up.
- **`rmtfs` must run before the modem.** The packaged unit has no `[Install]`
  section; `rmtfs-enable.conf` adds one so the unit is really enabled.
- **tqftpserv serves the modem's firmware requests from the modem remoteproc
  firmware directory**, `/usr/lib/firmware/qcom/sm7250/google/barbet/`. A bind
  mount of the stock vendor partition does not help: a request for
  `/readonly/vendor/firmware/wlanmdsp.mbn` is resolved next to `modem.mdt`.

## Firmware sources

All of it is the phone's own stock software; nothing is downloaded:

| file | from |
|---|---|
| `modem.mdt`, `modem.b*` | `modem` partition, `image/` |
| `wlanmdsp.mbn` -> `wlanmdsp.otaupdate` | `vendor_a`, `firmware/` |
| `bdwlan-barbet.bin` -> `board.bin` and `board-2.bin` | `vendor_a`, `firmware/` |
| `firmware-5.bin` | a 60-byte API-5 non-BMI stub written by `provision` |

The vendor image inside `super` is an Android logical partition. It can be
extracted once on the phone with `lpunpack -p vendor_a` (the output used
here is `/var/tmp/super/vendor_a.img`), or mounted on the host and the two
files copied in.

## Provisioning

Run `provision` as root on the phone, with `VENDOR_IMG` pointing at a
`vendor_a.img` (default `/var/tmp/super/vendor_a.img`) or `VENDOR_DIR` at an
already-mounted stock vendor tree, then reboot. The script installs the unit
and service files that sit beside it, so copy the folder first:

```sh
scp -r devices/barbet/wifi root@10.77.71.2:/tmp/
ssh root@10.77.71.2 VENDOR_IMG=/var/tmp/super/vendor_a.img \
    sh /tmp/wifi/provision
```

`tqftpserv` is not packaged in the image; build it from the
`linux-msm/tqftpserv` sources and install it as `/usr/local/bin/tqftpserv`.

## Verifying

After the reboot the expected sequence is: the modem comes up, the kernel
prints `wlfw new_server`, `qmi chip_id 0x320`, `fw indication`, `htt-ver`,
and `wlan0` appears. `qrtr-lookup 69` shows the ATH10k WLAN firmware service.
NetworkManager then owns association. `ip -br addr show wlan0` and a ping
through `wlan0` are the acceptance check.
