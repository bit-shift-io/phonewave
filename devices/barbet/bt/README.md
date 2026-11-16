# Bluetooth

**Works.** The controller enumerates, stays powered, and a discovery finds
nearby devices (both LE and BR/EDR). It is brought up by
`barbet-bt.service` after the Wi-Fi has powered the combo chip.

## What made it work

- UART5 (QUP1 SE5, TLMM 38-41) is enabled in the board DTS, using the
  WCN3991 protocol form (`qcom,wcn3991-bt`) and the WCN3990 patch/NVM files
  (`qca/crbtfw32.tlv`, `qca/crnv32.bin`).
- The modem powers the combo chip while booting the WLAN firmware. The BT
  side is dead before that, so the boot-time probe fails; `barbet-bt-start`
  rebinds `serial0-0` after `wcn3990-wifi.service` and restarts bluetoothd.
- Kernel quirks:
  - `qcom,disable-ibs`: with the QCA in-band-sleep handshake enabled,
    standard HCI commands (`0x0c03`, `0x1003`) time out and the controller
    dumps, taking the shared Wi-Fi with it. Disabling IBS keeps the command
    path working.
  - `qcom,use-firmware-bdaddr`: btqca otherwise asks the HCI core for
    `local-bd-address` when the address the controller reports equals the
    one in its NVM file. This ROM has no other address to give, so without
    the property the controller starts unconfigured and BlueZ never sees
    it. The property keeps the address the firmware reports.

## Address

The controller runs on the address its firmware reports, not the factory
address the bootloader publishes in `/chosen/cdt/cdb2/bt_addr`. That ROM
accepts no address write, and the host-supplied address does not reach the
controller, so the firmware address is what BlueZ uses. It is a valid
public address, and the kernel is told to keep it rather than require a
board-specific one.

The board device tree carries no unit-specific addresses, so Wi-Fi uses a
random MAC as well. A userspace service can still read the CDT and set
both before the radios come up.

## Provisioning

```sh
scp -r devices/barbet/bt root@10.77.71.2:/tmp/
ssh root@10.77.71.2 VENDOR_IMG=/var/tmp/super/vendor_a.img \
    sh /tmp/bt/provision
```

then reboot. The expected order is `wcn3990-wifi` -> `barbet-bt` -> the
controller in `hci0` -> `bluetooth.service`. Verify with
`bluetoothctl show` and `bluetoothctl scan on`.
