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


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="picnicd", description="Run the PicnicPlace core.")
    p.add_argument("--version", action="version", version=f"picnicd {__version__}")
    p.add_argument("-c", "--config", type=Path, help="TOML configuration file")
    p.add_argument("--host", help="override server.host")
    p.add_argument("--port", type=int, help="override server.port")
    p.add_argument("--studio-dir", type=Path, help="override server.studio_dir")
    p.add_argument(
        "--sim-time-scale", type=float, help="override machine.sim_time_scale (simulator)"
    )
    p.add_argument(
        "--print-openapi",
        action="store_true",
        help="print the OpenAPI document as JSON and exit",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def _build_config(args: argparse.Namespace) -> Config:
    config = load_config(args.config)
    data = config.model_dump()
    overrides = {
        ("server", "host"): args.host,
        ("server", "port"): args.port,
        ("server", "studio_dir"): args.studio_dir,
        ("machine", "sim_time_scale"): args.sim_time_scale,
    }
    for (section, key), value in overrides.items():
        if value is not None:
            data[section][key] = value
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
    try:
        config = _build_config(args)
    except (ValidationError, OSError) as e:
        sys.exit(f"picnicd: invalid configuration: {e}")

    app = create_app(config)
    uvicorn.run(app, host=config.server.host, port=config.server.port, log_config=None)


if __name__ == "__main__":
    main()
