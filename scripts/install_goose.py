"""Install the pinned release into ignored .tools only; no system installation."""

import hashlib
import io
import os
import platform
import tarfile
import urllib.request
from pathlib import Path

VERSION = "1.49.0"
URL = (
    "https://github.com/aaif-goose/goose/releases/download/v1.49.0/"
    "goose-x86_64-unknown-linux-gnu.tar.gz"
)
SHA256 = "38d5035e4a786f6b62abe0cd0f2bef7e6ac8041e3e006e2000561dd8df6aead3"


def main():
    if (platform.system(), platform.machine()) != ("Linux", "x86_64"):
        raise RuntimeError("NOT_RUN: this pinned asset targets Linux x86_64 GNU")
    folder = Path(".tools")
    folder.mkdir(exist_ok=True)
    archive = folder / f"goose-v{VERSION}.tar.gz"
    if not archive.exists():
        with urllib.request.urlopen(URL, timeout=120) as response:
            content = response.read()
    else:
        content = archive.read_bytes()
    if hashlib.sha256(content).hexdigest() != SHA256:
        raise ValueError("Goose release SHA-256 mismatch")
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
        member = tar.getmember("./goose")
        if not member.isfile():
            raise ValueError("Expected a regular binary")
        binary = tar.extractfile(member).read()
    target = folder / "goose"
    if target.exists() and target.read_bytes() != binary:
        raise FileExistsError("Preserving existing different Goose binary")
    if not archive.exists():
        archive.write_bytes(content)
    if not target.exists():
        target.write_bytes(binary)
        target.chmod(0o755)
    print(
        f"Goose {VERSION}: release archive SHA-256 verified; "
        f"executable={os.access(target, os.X_OK)}"
    )


if __name__ == "__main__":
    main()
