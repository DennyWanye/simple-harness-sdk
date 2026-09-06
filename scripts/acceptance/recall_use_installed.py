# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Small target-installed public consumer; dependencies borrowed unchanged.

Run with Python -I, target directory, wheel, copied consumer area, output manifest.
No SDK source path, private Memory/SQL, model or native application is used.
"""

from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import sys
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse


def main() -> int:
    target, wheel, consumer, output = [Path(p).resolve() for p in sys.argv[1:]]
    assert sys.flags.isolated and not any(p.endswith("/src") for p in sys.path)
    sys.path.insert(0, str(target))
    sys.path.insert(1, str(consumer))
    import simple_harness as harness
    import simple_harness_memory as memory
    import pytest

    assert harness.__version__ == "0.7.4"
    assert Path(harness.__file__).resolve().is_relative_to(target)
    assert Path(memory.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    owner = metadata.distribution("simple-harness-sdk")
    assert Path(owner.locate_file("simple_harness")).resolve().is_relative_to(target)
    origin = json.loads(owner.read_text("direct_url.json"))
    parsed = urlparse(origin["url"])
    assert parsed.scheme == "file" and Path(unquote(parsed.path)).resolve() == wheel
    package_bytes = {}
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith("simple_harness/") and not name.endswith("/"):
                payload = archive.read(name)
                assert (target / name).read_bytes() == payload, name
                package_bytes[name] = hashlib.sha256(payload).hexdigest()
    result = pytest.main(
        [
            "-q",
            "-c",
            "/dev/null",
            "--rootdir",
            str(consumer),
            "-p",
            "no:cacheprovider",
            str(consumer / "public_cases/test_context_use_public_memory.py"),
        ]
    )
    origins = {}
    for name, module in tuple(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if path is None:
            continue
        if name == "simple_harness" or name.startswith("simple_harness."):
            assert Path(path).resolve().is_relative_to(target), (name, path)
            origins[name] = str(Path(path).resolve())
        if name == "simple_harness_memory" or name.startswith("simple_harness_memory."):
            assert Path(path).resolve().is_relative_to(Path(sys.prefix).resolve()), (name, path)
    manifest = dict(
        version=harness.__version__,
        wheel=str(wheel),
        wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
        target=str(target),
        interpreter=sys.executable,
        memory_version=metadata.version("simple-harness-memory-sdk"),
        borrowed_dependency_prefix=sys.prefix,
        direct_url=origin,
        package_bytes=package_bytes,
        loaded_harness_origins=origins,
        pytest_exit_code=int(result),
        source_overlay=False,
        boundary="Target-installed public consumer; no Host/default/native or formal401 claim",
    )
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            dict(
                result=int(result),
                package_files=len(package_bytes),
                loaded_harness_modules=len(origins),
            )
        )
    )
    return int(result)


if __name__ == "__main__":
    raise SystemExit(main())
