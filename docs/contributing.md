# Contributing

PicnicPlace welcomes contributors. Most work needs no machine: the core runs against a
built-in simulator, and a fake Marlin firmware exercises the real driver.

## Where to start

- Read the [specification](SPEC.md) for the architecture and the decisions behind it.
- Run it locally: see [Try the simulator](guide/try-the-simulator.md).
- Find your way around the code: the [code map](developers/code-map.md) goes module by
  module. Coding agents should also read
  [AGENTS.md](https://github.com/mfarver/picnicplace/blob/main/AGENTS.md).
- The full development workflow (setup, checks, changing the API, sign-off) is in
  [CONTRIBUTING.md](https://github.com/mfarver/picnicplace/blob/main/CONTRIBUTING.md) on
  GitHub.
- Using the logo? See the [brand assets](brand/README.md).

## The stack

| Part | Built with |
|---|---|
| Core (`picnicd`) | Python 3.12+, asyncio, FastAPI, Pydantic, OpenCV |
| Studio | TypeScript, React, Vite |
| Tooling | uv, ruff, pyright, pytest; ESLint, Vitest |
| Website | Zensical, from the Markdown in `docs/` |

## Ground rules

- **License:** contributions are under the [MPL-2.0](https://github.com/mfarver/picnicplace/blob/main/LICENSE).
- **Sign-off:** commits carry a Developer Certificate of Origin sign-off
  (`git commit -s`). There is no CLA.
- **No OpenPnP code.** OpenPnP is GPL-3.0, so its code must not be copied or ported line
  by line. Reading its file formats and reimplementing published algorithms is fine.
- **American English** in code, docs, and the user interface.

## Improving this website

Every page has an edit button that opens its Markdown file on GitHub. Preview the site
locally with:

```sh
make docs-serve    # then open http://localhost:8000
```
