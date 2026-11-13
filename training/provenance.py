"""Dataset and code provenance recorded with every training run.

Works without the git or dvc binaries (the Airflow image has neither): the DVC hash
is read from the ``.dvc`` file and the git commit straight from ``.git``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml


def dvc_info(dvc_file: Path) -> dict[str, str | int | None]:
    """md5, size and path recorded by ``dvc add`` for a tracked file."""
    if not dvc_file.exists():
        return {"md5": None, "size": None, "path": None}
    data = yaml.safe_load(dvc_file.read_text(encoding="utf-8")) or {}
    out = (data.get("outs") or [{}])[0]
    return {"md5": out.get("md5"), "size": out.get("size"), "path": out.get("path")}


def file_md5(path: Path) -> str:
    """md5 of the file's bytes, the same hash DVC uses for a single file."""
    digest = hashlib.md5()  # noqa: S324 - matches DVC's content hash, not used for security
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit(repo_root: Path) -> str | None:
    """Current commit hash, read directly from the .git directory."""
    git_dir = repo_root / ".git"
    if git_dir.is_file():  # worktree: "gitdir: <path>"
        target = git_dir.read_text(encoding="utf-8").split(":", 1)[1].strip()
        git_dir = (repo_root / target).resolve()
    head_file = git_dir / "HEAD"
    if not head_file.exists():
        return None
    head = head_file.read_text(encoding="utf-8").strip()
    if not head.startswith("ref:"):
        return head or None
    ref = head.split(" ", 1)[1].strip()
    ref_file = git_dir / ref
    if ref_file.exists():
        return ref_file.read_text(encoding="utf-8").strip() or None
    packed = git_dir / "packed-refs"
    if packed.exists():
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0]
    return None


def collect(data_file: Path, dvc_file: Path, repo_root: Path) -> dict[str, str | int | None]:
    """Everything needed to trace a run back to the exact dataset version."""
    info = dvc_info(dvc_file)
    actual = file_md5(data_file) if data_file.exists() else None
    return {
        "dataset.path": str(data_file.relative_to(repo_root) if data_file.is_relative_to(repo_root) else data_file),
        "dataset.dvc_md5": info["md5"],
        "dataset.actual_md5": actual,
        "dataset.matches_dvc": str(bool(actual and info["md5"] == actual)).lower(),
        "git.commit": git_commit(repo_root),
    }
