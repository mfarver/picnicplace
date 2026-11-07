# PicnicPlace

PicnicPlace is an open source software controller for automated PCB assembly
pick-and-place machines.

It is **network first**: a headless core runs the machine (motion, vision, feeders, and
jobs), and every user interface, including the modern PicnicPlace Studio GUI, connects to
it over a documented, versioned API. Closing a GUI never interrupts a running job, and
anyone can build their own client.

The reference hardware is the **Opulo LumenPnP v4**.

## Status

Early design. No usable software yet. Start with the
[specification](docs/SPEC.md).

## Planned stack

- **Core:** Python 3.12+, OpenCV, FastAPI, asyncio
- **GUI:** TypeScript web app, served by the core, optionally packaged as a desktop app
- **Platforms:** Linux (reference), macOS, Windows

## License

PicnicPlace is licensed under the [Mozilla Public License 2.0](LICENSE). Changes to
PicnicPlace files must be shared under the same license. Separate add-ons and clients
that use the API may use any license.

Contributions are accepted under the
[Developer Certificate of Origin](https://developercertificate.org/): sign off your
commits with `git commit -s`.
