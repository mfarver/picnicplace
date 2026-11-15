"""The ``picnicd`` command: run the PicnicPlace core."""

import argparse
import logging
import os
import sys
import threading
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from picnicplace import __version__
from picnicplace.api.app import create_app, openapi_json
from picnicplace.machine_setup import Layered, load_layered
from picnicplace.recorder import RecorderError, replay_vision


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="picnicd", description="Run the PicnicPlace core.")
    p.add_argument("--version", action="version", version=f"picnicd {__version__}")
    p.add_argument("-c", "--config", type=Path, help="TOML configuration file")
    p.add_argument("--host", help="override server.host")
    p.add_argument("--port", type=int, help="override server.port")
    p.add_argument("--studio-dir", type=Path, help="override server.studio_dir")
    p.add_argument("--profile", help="override machine.profile (built-in ID or TOML path)")
    p.add_argument(
        "--driver", choices=["simulator", "marlin", "fake-marlin"], help="override machine.driver"
    )
    p.add_argument("--serial-port", help="override machine.marlin.port")
    p.add_argument(
        "--speed-scale", type=float, help="override machine.speed_scale (0.25 = quarter speed)"
    )
    p.add_argument(
        "--sim-time-scale", type=float, help="override machine.sim_time_scale (simulator)"
    )
    p.add_argument(
        "--print-openapi",
        action="store_true",
        help="print the OpenAPI document as JSON and exit",
    )
    p.add_argument(
        "--replay",
        type=Path,
        metavar="RECORDING",
        help=(
            "run today's vision code on a flight recording's frames (its folder or zip), "
            "say what answers changed, and exit"
        ),
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def replay(source: Path) -> int:
    """Print what changed between a recording's vision answers and today's; the count."""
    checks = replay_vision(source)
    changed = [c for c in checks if not c.same]
    for c in changed:
        what = (
            "found nothing or a different count"
            if c.difference is None
            else (f"moved by up to {c.difference:.3g}")
        )
        print(f"{c.frame} ({c.fn}): {what}\n  then: {c.recorded}\n  now:  {c.now}")
    print(
        f"{len(checks)} vision calls: {len(checks) - len(changed)} the same, {len(changed)} changed"
    )
    return len(changed)


SIMULATOR_NOTICE = """\
picnicd: no machine chosen, so this is the built-in SIMULATOR, not a real machine.
  To run your machine, choose it in the Studio's setup wizard (Machine workspace), or
  give a config: picnicd -c ~/.config/picnicplace/picnicd.toml
  (see core/picnicd.example.toml; set driver = "marlin" under [machine]).
"""


def _build_config(args: argparse.Namespace) -> Layered:
    """The config file, with the command line's overrides, over the setup wizard's
    choices."""
    overrides = {
        "server.host": args.host,
        "server.port": args.port,
        "server.studio_dir": str(args.studio_dir) if args.studio_dir else None,
        "machine.profile": args.profile,
        "machine.driver": args.driver,
        "machine.sim_time_scale": args.sim_time_scale,
        "machine.speed_scale": args.speed_scale,
        "machine.marlin.port": args.serial_port,
    }
    return load_layered(args.config, {k: v for k, v in overrides.items() if v is not None})


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if args.print_openapi:
        sys.stdout.write(openapi_json())
        return
    if args.replay is not None:
        try:
            sys.exit(1 if replay(args.replay) else 0)
        except (OSError, RecorderError, ValueError) as e:
            sys.exit(f"picnicd: cannot replay {args.replay}: {e}")
    try:
        layered = _build_config(args)
    except (ValidationError, OSError) as e:
        sys.exit(f"picnicd: invalid configuration: {e}")
    config = layered.config
    if layered.warning:
        logging.getLogger("picnicd").warning("%s", layered.warning)

    if config.machine.driver == "simulator" and "machine.driver" not in layered.pinned:
        sys.stderr.write(SIMULATOR_NOTICE)
    restart = threading.Event()
    server: uvicorn.Server | None = None

    def request_restart() -> None:
        """Stop serving, close the machine and cameras, and start again (below)."""
        restart.set()
        if server is not None:
            server.should_exit = True

    app = create_app(config, pinned=layered.pinned, restart=request_restart)
    server = uvicorn.Server(
        uvicorn.Config(app, host=config.server.host, port=config.server.port, log_config=None)
    )
    server.run()
    if restart.is_set():
        logging.getLogger("picnicd").info("restarting with the setup wizard's choices")
        os.execv(sys.executable, [sys.executable, *sys.argv])


if __name__ == "__main__":
    main()
