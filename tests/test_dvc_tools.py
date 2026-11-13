"""DVC tools against a real temporary git + DVC repository (skipped if dvc is not installed)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent.errors import InvalidRequest, ToolError
from agent.tools.dvc_tools import DvcTools

pytestmark = pytest.mark.skipif(shutil.which("dvc") is None or shutil.which("git") is None,
                                reason="dvc and git are required")


def sh(cwd: Path, *cmd: str) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, settings):
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    sh(root, "git", "init", "-q", "-b", "main")
    sh(root, "git", "config", "user.email", "test@example.com")
    sh(root, "git", "config", "user.name", "Test")
    sh(root, "dvc", "init", "-q")
    sh(root, "dvc", "remote", "add", "-d", "local", str(tmp_path / "remote"))
    for version, rows in (("v1", "a,b\n1,2\n"), ("v2", "a,b\n1,2\n3,4\n")):
        # bytes, so the file is identical on Windows (no \r\n translation)
        (root / "data" / "d.csv").write_bytes(rows.encode())
        sh(root, "dvc", "add", "-q", "data/d.csv")
        sh(root, "git", "add", "-A")
        sh(root, "git", "commit", "-q", "-m", f"dataset {version}")
        sh(root, "git", "tag", f"data-{version}")
        sh(root, "dvc", "push", "-q")
    return DvcTools(settings.__class__(project_root=root, dvc_reproduce_dir=root / "reproductions"))


def test_history_current_version_and_hash_lookup(repo):
    history = repo.dataset_history()["versions"]
    assert [v["tags"] for v in history] == [["data-v2"], ["data-v1"]]
    current = repo.get_current_version()
    assert current["version_tags"] == ["data-v2"] and current["workspace_matches_dvc"] is True
    found = repo.find_version_by_hash(history[1]["md5"])
    assert found["found"] and found["versions"][0]["tags"] == ["data-v1"]


def test_compare_and_reproduce_previous_version(repo):
    diff = repo.compare_versions("data-v1", "data-v2")
    assert diff["changed"] is True and diff["size_delta_bytes"] == 4
    reproduced = repo.reproduce_version("data-v1")
    assert reproduced["verified"] is True
    assert reproduced["reproduced_md5"] == reproduced["recorded_md5"]


def test_rejects_unsafe_revisions_and_commands(repo):
    with pytest.raises(InvalidRequest):
        repo.compare_versions("--upload-pack=evil")
    with pytest.raises(ToolError):
        repo._run("dvc", "gc", "-f")  # not on the allow-list
