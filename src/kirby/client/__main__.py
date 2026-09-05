"""Run the local E2E browser client without database or model credentials."""

import argparse
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn

from kirby.client.a2a import A2AClient
from kirby.client.app import create_app
from kirby.roles import load_samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--credentials-file",
        type=Path,
        default=os.getenv("KIRBY_CREDENTIALS_FILE"),
        help="KIRBY credential JSON file; defaults to KIRBY_CREDENTIALS_FILE",
    )
    parser.add_argument("--credential-index", type=int, default=0)
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument(
        "--agents-dir",
        "--examples-dir",
        dest="agents_dir",
        type=Path,
        default=Path("agents"),
    )
    args = parser.parse_args()
    if args.credentials_file is None:
        parser.error("Set --credentials-file or KIRBY_CREDENTIALS_FILE")
    if args.credential_index < 0:
        parser.error("--credential-index must be nonnegative")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        credentials = json.loads(args.credentials_file.read_text())
        token = credentials[args.credential_index]["token"]
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Empty token")
        client = A2AClient(args.url, token)
    except (OSError, ValueError, IndexError, KeyError, TypeError, httpx.InvalidURL):
        parser.error("Check the API URL, credential file and credential index")
    samples = load_samples(args.agents_dir)
    app = create_app(client, samples=samples)

    @asynccontextmanager
    async def lifespan(app):
        async with client:
            yield

    app.router.lifespan_context = lifespan
    print(f"KIRBY E2E Client: http://127.0.0.1:{args.port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
