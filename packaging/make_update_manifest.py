"""Generate one manifest only after both installers are available."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def manifest(folder: Path, repository: str, tag: str) -> dict:
    version = tag[1:] if tag.startswith('v') else tag
    import re
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Expected a release tag such as v0.1.6')
    builds = {}
    for target, suffix in (("x64", ""), ("win7-x86", "-win7-x86")):
        name = "WhatsAppPrinter-Setup-{}{}.exe".format(version, suffix)
        artifact = folder / name
        if not artifact.is_file():
            raise ValueError("Missing installer: " + name)
        builds[target] = {
            "url": "https://github.com/{}/releases/download/{}/{}".format(repository, tag, name),
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        }
    return {"version": version, **builds["x64"], "builds": builds,
            "notes": "WhatsApp Printer " + version}


if __name__ == '__main__':
    directory = Path(sys.argv[1])
    result = manifest(directory, sys.argv[2], sys.argv[3])
    (directory / 'latest.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
