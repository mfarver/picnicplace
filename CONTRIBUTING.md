# Contributing to PicnicPlace

Thanks for helping. Start with the [specification](docs/SPEC.md), which explains the
architecture and the decisions behind it.

## Repository layout

| Path | Contents |
|---|---|
| `core/` | `picnicd`, the Python core: API, machine service, drivers, presence, operations |
| `studio/` | PicnicPlace Studio, the React + TypeScript GUI |
| `docs/` | The website (picnicplace.org): specification, guides, hardware notes, and the generated OpenAPI document (`docs/api/openapi.json`). Preview it with `make docs-serve`. |

The [code map](docs/developers/code-map.md) goes module by module, and
[AGENTS.md](AGENTS.md) sums up the rules below for coding agents.

## Development setup

You need [uv](https://docs.astral.sh/uv/) and Node.js 22 or newer. No machine is required:
the core runs against a built-in simulator.

```sh
make setup        # install dependencies
make core         # build the Studio and run the core at http://127.0.0.1:7420
```

For GUI work with hot reload, run the core and the Vite dev server side by side:

```sh
cd core && uv run picnicd --sim-time-scale 5    # terminal 1
make dev                                        # terminal 2, then open http://localhost:5173
```

Run everything CI runs before opening a pull request:

```sh
make check
```

The 1.0 core targets Linux. macOS works for development with the simulator.

## Changing the API

The OpenAPI document is generated from the core's Pydantic models, and the Studio's
TypeScript types are generated from that document. After changing anything in
`core/src/picnicplace/schemas.py` or the routes, run:

```sh
make api
```

and commit the updated `docs/api/openapi.json` and `studio/src/api/schema.ts`. CI fails if
they are out of date.

## Code style

- **Python:** formatted and linted with `ruff`; `pyright` runs in strict mode on `core/src`.
  Type hints are required.
- **TypeScript:** `eslint` and `tsc` in strict mode.
- **Language:** write code, comments, docs, and UI text in American English.
- **Units:** millimeters, degrees, and seconds everywhere.

## Safety rules

Safety checks (presence, E-stop, homing, soft limits) live in the core's machine service,
never only in a client. A change that lets a client bypass them will not be accepted.

## Sign your commits (DCO)

Contributions are accepted under the
[Developer Certificate of Origin](https://developercertificate.org/). Add a sign-off to
each commit with:

```sh
git commit -s
```

By contributing, you agree that your contribution is licensed under the
[Mozilla Public License 2.0](LICENSE).
