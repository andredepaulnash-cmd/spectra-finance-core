import os
import subprocess
import sys
from pathlib import Path


def test_hummingbot_mcp_config_directory_can_be_isolated(tmp_path: Path):
    env = os.environ.copy()
    env["HUMMINGBOT_MCP_CONFIG_DIR"] = str(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from mcp_servers.hummingbot_api.settings import SERVER_CONFIG_PATH; print(SERVER_CONFIG_PATH)",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.stdout.strip() == str(tmp_path / "server.yml")
