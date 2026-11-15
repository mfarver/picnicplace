# Control panel (concept)

The control panel has two parts, and the operator can use either one or both:

- **A panel mounted on the machine.** It has a touchscreen for the machine's state and
  job controls, and an emergency stop.
- **A game controller for jogging.** Any controller that Linux supports, such as an
  Xbox controller, connects to the computer running the core. The controller gives
  physical controls without designing or selling a pendant.

With both, the operator watches the machine or the camera video while their hands stay
on physical controls. The Studio becomes mostly camera video.

PicnicPlace plans to publish the panel as open hardware, and it may also be sold
assembled. This is a concept: nothing here is built yet.

## Why physical controls

Jogging on a touchscreen is no better than clicking jog buttons in the Studio with a
mouse. The operator still has to look at the controls, not the nozzle or the camera
image. Sticks and buttons can be used without looking, so they are worth the cost.

OpenPnP already supports a Linux joystick, so a game controller is a proven approach.
It is also a cheap and familiar one: one stick for X and Y, another for Z and C. The
panel then needs no jog hardware. It stays small enough to mount on the machine, and
its touchscreen keys are only for things done while looking at the panel anyway, such
as starting a job, answering a prompt or pausing.

## The panel

- **A touchscreen.** One of these ESP32 boards:
  - a Waveshare ESP32-S3 4" board: 480 × 480, capacitive touch, native USB;
  - a "Cheap Yellow Display" (ESP32-2432S028): 2.8", resistive touch, USB through a
    serial bridge.

  The S3 board is preferred. Native USB makes the link simple, and capacitive touch works
  with dirty fingers. The ESP32 on the board runs the panel.
- **An emergency stop.** It is a latching mushroom switch with two poles:
  - one pole cuts the machine's power directly, without passing through the ESP32 or
    any software (see below);
  - the other is read by the ESP32, so the core knows why the machine stopped.
- **An enclosure** that mounts on the machine's frame, where the E-stop is in reach
  from wherever the operator stands. A game controller has no E-stop, so the panel's
  E-stop must always be easy to reach.

A lighter version without the touchscreen is just the E-stop in a box. It can be built
from the same enclosure design.

### How it connects

```
  E-stop pole 1 ── machine's 24 V ──► LumenPnP mainboard (controller and motors)
  E-stop pole 2 ──► ESP32 panel ── USB ──┐
                                         ├──► picnicd (core) ── USB ──► controller
  game controller ── USB or Bluetooth ───┘          │
                                                    └─ HTTP/WebSocket ──► Studio (video)
```

**Neither device talks to the machine's controller.** Both are inputs to the core, and
every move goes through the machine service. They get the same safety checks as the
Studio:

- the axis limits;
- no X/Y moves while a nozzle is below safe Z;
- the control lease, taken for the panel and controller while someone uses them, and
  shown in the Studio;
- Pause.

**The panel connects over USB, not Wi-Fi.**

- The panel could use the core's HTTP API over Wi-Fi, with no new code in the core.
- USB needs no network setup or pairing, and the core sees a disconnect at once.

The panel appears as a USB serial device and exchanges short JSON messages, one per
line, with the core. A `[panel]` section in the config names the port.

### What the screen shows

- **The state:** connected, homed, paused, the E-stop, and the lease holder.
- **Position:** X, Y, Z and C, and the controller's jog step.
- **The current step:**
  - a job: board, part, and progress;
  - a wizard step: its instruction, such as "Put the paper disc in the bare area, then
    press OK".
- **Keys:** Start, Pause/Resume, Stop, and OK/Cancel for prompts.
- **Messages:** errors from the core, in the same words the Studio shows.

**The panel's job stays narrow.** Anything that needs judging a camera image stays in the
Studio, such as finding a fiducial, checking a pick, or drawing a keep-out. This keeps
the panel from becoming a second front end. The core already sends what the panel shows
through its machine state and wizard run events.

## The game controller

The core reads the controller directly, on the computer it runs on. A `[controller]`
section in the config turns it on and can change the mapping. A first mapping, for an
Xbox-style controller:

| Control | Action |
|---|---|
| Left bumper (held) | Enable: nothing moves unless it is held |
| Left stick | X and Y, proportional: the further, the faster |
| Right stick, up and down | Z, proportional |
| Right stick, left and right | C (rotation), proportional |
| D-pad | X and Y, one step per press |
| Right bumper | Next jog step: 0.01, 0.1, 1 or 10 mm |
| A / B | OK / Cancel on a prompt |
| Start | Pause / Resume |

**The enable button matters.** A controller dropped on its stick, or one with a
drifting stick, must not move the machine.

**Reading the controller:**
- On Linux, the kernel's event devices (`evdev`) give the sticks and buttons and report
  when a controller is unplugged or a Bluetooth link drops.
- SDL's game controller support works on Linux, macOS and Windows. It also has a mapping
  database, so most controllers work without setup. It is a heavier dependency.

## Jogging that stops when you let go

Marlin finishes the moves it has already been sent before it acts on anything new. This
is the lesson of `M112`, which the Studio no longer sends. A jog therefore must never
send one long move. Instead, while the stick is held:

1. The core reads the stick about 20 times a second.
2. It turns each reading into a short move, about 0.1 s long at the requested speed.
   It keeps no more than two moves queued.
3. The core stops jogging when any of these happens:
   - the stick returns to center or the enable button is released;
   - the controller disconnects;
   - the machine pauses or the E-stop is pressed.

   The head then coasts for at most what is queued, a few millimeters at full speed.

The jog loop lives in the core's machine service, not in any device, so the simulator
can test it. A simulated controller in the Studio sends the same inputs. The core side
is then built and tested before any hardware exists.

## The E-stop on a LumenPnP

The LumenPnP v4 mainboard takes a single 24 V supply for both the controller and the
stepper drivers. Without changing the board, the E-stop has to cut all 24 V.

That is acceptable:

- The motors stop at once. So do the pump and the valves, so a part on a nozzle drops.
- The controller resets. The core sees the disconnect, and the sensing pole tells it
  why.

The core then:

- marks the machine as no longer homed;
- stops any job or wizard step, as Stop does;
- shows "E-stop pressed: release it, then connect and home" on the panel and in the
  Studio.

The core never moves the machine by itself after an E-stop. The switch goes between
the power supply and the mainboard. It must be rated for the supply's full current.

## Software work in PicnicPlace

- **The core:**
  - a `panel` driver for the serial link;
  - a `controller` driver for game controllers;
  - the release-to-stop jog loop in the machine service;
  - E-stop state in `MachineState`;
  - wizard prompts that the panel's OK, or the controller's A, can answer.
- **The Studio:**
  - shows when the panel or controller holds the lease;
  - a simulated controller for testing.
- **The panel's firmware:** for the ESP32, in this repository, under MPL-2.0.

## Licensing and sale

- **Hardware:** CERN-OHL-W, which matches the spirit of MPL-2.0. It covers the
  enclosure's CAD files, the wiring, and any adapter PCB.
- **Firmware:** MPL-2.0.
- **Selling it assembled:**
  - Use a pre-certified ESP32 module. The display boards above already use one.
  - Describe the E-stop as a power cutoff, specify a switch rated for the current, and
    document the wiring.
  - Game controllers are bought separately. PicnicPlace documents which ones are
    tested.

## Open questions

- Where on the LumenPnP's frame the panel mounts, and how. Its E-stop must be in reach
  from the front.
- Reading the controller with `evdev` (Linux only, light) or SDL (cross-platform,
  heavier)? The core usually runs on Linux next to the machine.
- Whether the panel itself should also offer coarse jogging, for operators with no
  controller.
- Other machines: the E-stop wiring is documented per machine. Boards with separate
  motor power can keep the controller running.
