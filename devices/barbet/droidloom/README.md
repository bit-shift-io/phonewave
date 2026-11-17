# Droidloom cell

The pinned Droidloom Android image runs the AOSP example health HAL,
`android.hardware.health-service.example`, as the `vendor.health-default`
service. It polls the battery through a `CLOCK_BOOTTIME_ALARM` timerfd every
~38 s, and that alarm wakes the phone out of `s2idle` even when the cell is
idle. The cell keeps reporting the last battery state it received after the
service stops.

`droidloom-health-quiet` stops the service when it is running and logs one
line. Android init starts it again with the next cell start, so a 30 s timer
repeats the check rather than running once. It does nothing when no Android
cell is running.

This belongs here rather than in the kernel: the wake comes from the example
HAL shipped in the Android image, not from a hardware wake source. A real
health HAL without a wake timer would make the unit unnecessary.
