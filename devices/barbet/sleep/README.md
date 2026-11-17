# Sleep

Denial's own power policy is the source of truth:
`power.idleSuspendEnabled` and `power.idleSuspendTimeoutSeconds`. The
compositor seeds neither on a new mobile document, so enable automatic
suspend in the shell or edit the user's `settings.json`; the compositor
watches that file and applies an atomic replacement.

`denial-idle-suspend` exists because the compositor suspends **once per idle
interval**. `idle_policy.rs` keeps `suspend_triggered` until physical input, a
policy change or an inhibitor ends. A wake from an RTC alarm, an Android job or
an alarm clock is not input, so the phone would sit awake with a dark screen
until someone touched it.

The watchdog runs every 15 s, does nothing unless automatic suspend is
enabled, and records `<dark since epoch> <boottime of the last check>` in
`/run/denial-idle-suspend.state`. It only counts a connected output whose
DPMS is on; the writeback connector reports `dpms=On` while it is not a real
output. A boottime jump larger than 45 s means the phone slept, so it starts a
fresh interval after the wake instead of suspending straight back. When the
dark time reaches Denial's timeout it calls `systemctl suspend`.

The switch to `s2idle` and the RTC wake sources are kernel concerns; this unit
only repeats the compositor's policy after a wake.

Install with `provision`; it needs the image's `bronson` user and the Denial
settings at `/home/bronson/.config/denial/settings.json`.
