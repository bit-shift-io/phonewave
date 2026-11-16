# Audio

The speaker path is **verified working** on the Pixel 5a: a 440 Hz tone played
through `hw:0,1` reaches the TAS2562 pair (the q6asm stream starts and the amp
unmutes the DAC). Microphone capture is **partial**: the RT5514P DSP loads and
the mono stream runs, but the samples carry a large DC offset, the same
partial state the reference bring-up reported.

## What the card needs

- The `lpass_audio_hw_vote` clock in `&sound` comes from `q6afecc`, so the
  **ADSP must be running** before the machine driver can register. Without it
  the card stays in deferred probe with `failed to enable LPASS vote clock`.
- The ADSP firmware is the phone's own `adsp.mdt`/`adsp.b*` from `vendor_a`,
  installed next to `modem.mdt` in the modem remoteproc firmware directory.
- The RT5514P requests `rt5514p_dsp_fw{1..4}.bin` by bare name when a capture
  stream starts; without them it logs `DSP run failure, reset DSP`.
- **Start the ADSP after Wi-Fi.** The modem boots the WLAN firmware while the
  ADSP is offline; `wcn3990-wifi.service` is what waits for WLFW, and the audio
  unit is ordered after it.

## Route

The verified mixer route is applied by `barbet-audio-start`:

```
QUIN_TDM_RX_0 Audio Mixer MultiMedia2  on    # playback
MultiMedia1 Mixer PRIMARY_TDM_TX_0     on    # capture
Stereo1 DMIC Mux                       DMIC1
Sto1 ADC MIXL DMIC                     on
ADC1                                   23
ASI1 Sel                               Left
Amp Gain                               16
Digital Volume Control                 90
```

`alsa-state.service` and `alsa-restore.service` are masked so a restore does
not overwrite this route.

## Provisioning

```sh
scp -r devices/barbet/audio root@10.77.71.2:/tmp/
ssh root@10.77.71.2 VENDOR_IMG=/var/tmp/super/vendor_a.img \
    sh /tmp/audio/provision
```

Then reboot. The expected order is `wcn3990-wifi` -> ADSP up -> sound card 0
`barbet` -> `audio card ready`.
