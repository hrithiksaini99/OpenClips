import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _resolved_compose() -> dict[str, object]:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI is not available to resolve the Compose file")
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_root = Path(temporary_directory)
        compose_path = temporary_root / "docker-compose.yml"
        shutil.copy2(PROJECT_ROOT / "docker-compose.yml", compose_path)
        shutil.copy2(PROJECT_ROOT / ".env.example", temporary_root / ".env")
        result = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=temporary_root,
            check=True,
            capture_output=True,
            text=True,
        )
    return json.loads(result.stdout)


def test_compose_shares_media_and_model_cache() -> None:
    compose = _resolved_compose()
    services = compose["services"]
    volumes = compose["volumes"]

    assert {"media-data", "model-cache"} <= volumes.keys()

    api_volumes = services["api"]["volumes"]
    worker_volumes = services["worker"]["volumes"]
    assert {
        ("media-data", "/data/media", False),
        ("model-cache", "/root/.cache/huggingface", True),
    } <= {
        (volume["source"], volume["target"], volume.get("read_only", False))
        for volume in api_volumes
    }
    assert {
        ("media-data", "/data/media", False),
        ("model-cache", "/root/.cache/huggingface", False),
    } <= {
        (volume["source"], volume["target"], volume.get("read_only", False))
        for volume in worker_volumes
    }

    for service in (services["api"], services["worker"]):
        environment = service["environment"]
        assert environment["OPENCLIPS_MEDIA_ROOT"] == "/data/media"
        assert environment["OPENCLIPS_DATABASE_URL"].endswith("@db:5432/openclips")
        assert environment["OPENCLIPS_REDIS_URL"] == "redis://redis:6379/0"
