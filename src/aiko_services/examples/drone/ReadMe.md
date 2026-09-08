# Aiko Services example: NetopSun XR872 WiFi drone

A small consumer WiFi toy drone (NetopSun XR872 chipset) with a
dearth of useful features.

The drone runs its own WiFi access point; to talk to the drone one
must connect to the access point.  Camera video and flight control
are two independent, unauthenticated raw-UDP channels on that network
— no vendor SDK or public protocol documentation exists.

See [documentation/examples/drone](../../../../documentation/examples/drone/ReadMe.md)
for the full write-up.

### Running the pipeline

Start an MQTT bus and the registrar, then create the pipeline:

```
hatch run aiko_pipeline create --log_level _all --log_mqtt all -r src/aiko_services/examples/drone/drone_pipeline.json
```
Once the pipeline is created, create the streams in another window:
```
hatch run aiko_pipeline update p_drone -s 1 -gp ImageReadDroneXR872
hatch run aiko_pipeline update p_drone -s 2 -gp ControlReadJoystick
```

### Debugging and generating controller mappings

The `controller_input.py` includes a Click app for determining button IDs, etc.
```
hatch run python src/aiko_services/examples/drone/controller_input.py --inspect
```

The included pipeline definition contains all of the configurable
parameters for using your own controller.  If the value is set to
`null`, it uses the default, which is based on an Xbox Series X
controller.
