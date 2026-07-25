# Aiko Services example: NetopSun XR872 WiFi drone

A small consumer WiFi toy drone (NetopSun XR872 chipset), controlled in
the vendor world by the Android app `com.netopsun.zerox_air`. The drone
runs its own WiFi access point; camera video and flight control are two
independent, unauthenticated raw-UDP channels on that network — no
vendor SDK or public protocol documentation exists.

See [documentation/examples/drone](../../../../documentation/examples/drone/ReadMe.md)
for the full write-up: protocol details, current wiring state (camera
works end-to-end; flight control is implemented but not yet connected
to any Pipeline), and the open task list.
