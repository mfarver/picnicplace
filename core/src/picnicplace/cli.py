"""The ``picnicd`` command: run the PicnicPlace core."""

import argparse
import logging
import sys
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from picnicplace import __version__
from picnicplace.api.app import create_app, openapi_json
from picnicplace.config import Config, load_config
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


def _build_config(args: argparse.Namespace) -> Config:
    config = load_config(args.config)
    data = config.model_dump()
    overrides = {
        ("server", "host"): args.host,
        ("server", "port"): args.port,
        ("server", "studio_dir"): args.studio_dir,
        ("machine", "profile"): args.profile,
        ("machine", "driver"): args.driver,
        ("machine", "sim_time_scale"): args.sim_time_scale,
        ("machine", "speed_scale"): args.speed_scale,
    }
    for (section, key), value in overrides.items():
        if value is not None:
            data[section][key] = value
    if args.serial_port is not None:
        data["machine"]["marlin"]["port"] = args.serial_port
    return Config.model_validate(data)


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
        config = _build_config(args)
    except (ValidationError, OSError) as e:
        sys.exit(f"picnicd: invalid configuration: {e}")

    app = create_app(config)
    uvicorn.run(app, host=config.server.host, port=config.server.port, log_config=None)


if __name__ == "__main__":
    main()
