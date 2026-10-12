"""Dump ``ssc-core``'s live OpenAPI document (including ``x-ssc-ws-frame``) to
``web/openapi.json``, so ``openapi-typescript`` can generate ``src/api/schema.d.ts``
from it.

Types come from the server (frontend-react.md): this script is the only place in
``web/`` that imports ``ssc``, and it does so read-only, purely to ask the FastAPI
app what its own contract is. It never runs against a live device — ``create_app``
is built with the default (unconnected) gateway and a throwaway database, exactly
like ``GET /openapi.json`` would answer for any freshly started server.

Run via ``npm run api:codegen`` (invokes this with ``uv run`` for the ``ssc``
virtualenv), not directly with a bare ``python3``.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = WEB_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from ssc.main import create_app  # noqa: E402
from ssc.settings.config import AppConfig  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(AppConfig(data_dir=Path(tmp), mdns_enabled=False))
        schema = app.openapi()

    out = WEB_DIR / "openapi.json"
    out.write_text(json.dumps(schema, indent=2) + "\n")
    print(
        f"wrote {out} ({len(schema.get('paths', {}))} paths, "
        f"{len(schema.get('components', {}).get('schemas', {}))} schemas)"
    )


if __name__ == "__main__":
    main()
