# Try the simulator

PicnicPlace includes a simulated machine, so you can run the core and the Studio without
any hardware. The simulator models a LumenPnP v4.1: its motion, home switches, vacuum,
lights, and both cameras, whose images are drawn from the machine's real geometry.

## Requirements

- [uv](https://docs.astral.sh/uv/) for the Python core
- Node.js 22 or newer for the Studio
- Linux or macOS

## Run it

```sh
git clone https://github.com/mfarver/picnicplace.git
cd picnicplace
make setup     # install the core's and the Studio's dependencies
make core      # build the Studio and start the core
```

Open <http://127.0.0.1:7420> in a browser.

## Things to try

1. Press **Home**. The simulator's homing stops slightly off, like real endstops, and
   visual homing then centers the top camera on the datum fiducial and corrects the
   position. The Control panel reports how far off it was.
2. Jog with the arrows in the **Jog** panel, or click anywhere on the machine map to
   move there. Moves always lift the nozzles to safe Z first.
3. In the **Head** inset, turn on a nozzle's pump and valve and watch its vacuum reading.
   Lower the nozzle over the bottom camera with suction on to "pick" a part.
4. Open the Studio in a second browser tab. Both tabs see the machine, but only one has
   control; the other can take control after confirming.
5. Press **STOP**. Motion stops, outputs turn off, and the machine must be reset and
   homed again.

## Use the real driver without a machine

The fake firmware runs PicnicPlace's real Marlin driver against a simulated LumenPnP
controller, so serial handling, firmware replies, and the emergency stop are exercised
too:

```sh
cd core
uv run picnicd --studio-dir ../studio/dist --driver fake-marlin
```

## Next

When you're ready to connect a real machine, see [Bring up a machine](bring-up.md).
