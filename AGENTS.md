# Notes for coding agents

Instructions for AI coding agents (and a quick start for anyone) working in this
repository. Read [docs/developers/code-map.md](docs/developers/code-map.md) for where
things are, and [docs/SPEC.md](docs/SPEC.md) for why.

## Commands

```sh
make setup     # install core (uv) and Studio (npm) dependencies
make core      # build the Studio, run the core with the simulator at http://127.0.0.1:7420
make api       # after changing any API model or route: regenerate the OpenAPI document
               # and the Studio's types, and commit them
make check     # everything CI runs: ruff, ruff format --check, pyright (strict), pytest,
               # eslint, tsc, vitest, and the Studio build
```

Run a few core tests: `cd core && uv run pytest -q tests/test_placement.py`. No hardware is
needed: the tests and `make core` use the simulator.

## Rules

- **American English** in code, comments, docs, UI text, and commit messages.
- **Sign off every commit** (`git commit -s`), under the Developer Certificate of Origin.
- **No OpenPnP, Opulo, or LumenPnP code or artwork.** PicnicPlace is MPL-2.0 and written
  from scratch; facts and dimensions may be used, code may not.
- **Profiles hold design facts; the config holds owner choices.** A machine's axes,
  cameras, and feeder banks go in its profile (`core/src/picnicplace/profiles/`); a
  discard or park location, or a preference, goes in the config.
- **Say "footprints", not "packages"**, for the land patterns of parts.
- **Safety lives in the machine service** (`core/src/picnicplace/machine/service.py`).
  Never move the machine around it, and never weaken a safety check to make a test pass.
- **Errors are for users.** Machine error messages are shown in the Studio: say what went
  wrong and what to do.
- **Tests check outcomes in the simulator.** It knows where every part really landed;
  assert on that, not on what the core believes.
- **Keep the docs in step.** A change a user can see updates the spec, the guide it
  touches, and the milestone tables (spec section 11 and `docs/index.md`); a new endpoint
  goes in `docs/api/overview.md`; a new setting in `core/picnicd.example.toml`.
- **Formatting:** `ruff format` for Python. The Studio has no formatter: match the
  surrounding style (two-space indent, double quotes, lines up to 100 characters). Do not
  run Prettier over it.
- **Generated files** (`docs/api/openapi.json`, `studio/src/api/schema.ts`,
  `studio/src/api/fingerprint.ts`) come from `make api`; never edit them by hand.
