"""DVC data-management tools.

Only an allow-list of read-only git/dvc sub-commands can run (no shell, fixed argument
lists, timeouts). Reproducing an old dataset version uses ``dvc get``, which writes a
copy into ``reproductions/`` and never touches the working tree.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from agent.config import Settings
from agent.errors import InvalidRequest, ServiceUnavailable, ToolError
from agent.schemas import ToolSpec, integer, obj, string

ALLOWED_COMMANDS = (
    ("git", "log"), ("git", "show"), ("git", "rev-parse"), ("git", "tag"), ("git", "ls-files"),
    ("git", "status"), ("dvc", "status"), ("dvc", "diff"), ("dvc", "get"), ("dvc", "remote", "list"),
    ("dvc", "version"),
)
_SAFE_REV = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/~^")


class DvcTools:
    def __init__(self, settings: Settings) -> None:
        self.root = Path(settings.project_root)
        self.reproduce_dir = Path(settings.dvc_reproduce_dir)

    # ------------------------------------------------------------------ helpers
    def _run(self, *cmd: str, timeout: float = 30) -> str:
        if not any(cmd[: len(prefix)] == prefix for prefix in ALLOWED_COMMANDS):
            raise ToolError(f"command not allowed: {' '.join(cmd[:3])}")
        if shutil.which(cmd[0]) is None:
            raise ServiceUnavailable(f"'{cmd[0]}' is not installed or not on PATH")
        try:
            proc = subprocess.run(list(cmd), cwd=self.root, capture_output=True, text=True,
                                  timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"{cmd[0]} {cmd[1]} timed out after {timeout}s") from exc
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout).strip()[-600:]
            if "not a git repository" in msg or "not inside of a DVC repository" in msg.lower():
                raise ServiceUnavailable(msg)
            raise InvalidRequest(f"{' '.join(cmd[:3])} failed: {msg}")
        return proc.stdout

    @staticmethod
    def _rev(rev: str) -> str:
        if not rev or rev.startswith("-") or not set(rev) <= _SAFE_REV:
            raise InvalidRequest(f"invalid git revision {rev!r}")
        return rev

    def _dvc_files(self) -> list[str]:
        out = self._run("git", "ls-files", "*.dvc")
        return [line for line in out.splitlines() if line.strip()]

    def _resolve(self, dataset: str | None) -> str:
        files = self._dvc_files()
        if not files:
            raise InvalidRequest("no DVC-tracked datasets (.dvc files) found in this repository")
        if not dataset:
            return files[0]
        for f in files:
            if dataset in (f, f[: -len(".dvc")], Path(f).name, Path(f[: -len(".dvc")]).name):
                return f
        raise InvalidRequest(f"dataset {dataset!r} is not tracked by DVC; tracked: {[f[:-4] for f in files]}")

    @staticmethod
    def _parse_dvc(text: str) -> dict[str, Any]:
        data = yaml.safe_load(text) or {}
        out = (data.get("outs") or [{}])[0]
        return {"md5": out.get("md5"), "size": out.get("size"), "path": out.get("path")}

    def _tags(self, commit: str) -> list[str]:
        return [t for t in self._run("git", "tag", "--points-at", commit).split() if t]

    def _history(self, dvc_file: str, limit: int = 20) -> list[dict[str, Any]]:
        log = self._run("git", "log", f"-n{limit}", "--format=%H|%h|%ad|%s", "--date=iso-strict", "--", dvc_file)
        versions = []
        for line in log.splitlines():
            full, short, date, subject = line.split("|", 3)
            try:
                info = self._parse_dvc(self._run("git", "show", f"{full}:{dvc_file}"))
            except InvalidRequest:  # file deleted in this commit
                continue
            versions.append({"commit": short, "commit_full": full, "date": date, "message": subject,
                             "tags": self._tags(full), **info})
        return versions

    # ------------------------------------------------------------------ tools
    def list_tracked_datasets(self) -> dict[str, Any]:
        datasets = []
        for f in self._dvc_files():
            info = self._parse_dvc((self.root / f).read_text(encoding="utf-8"))
            datasets.append({"dataset": f[: -len(".dvc")], "dvc_file": f, **info})
        remotes = self._run("dvc", "remote", "list").strip().splitlines()
        pipeline = (self.root / "dvc.yaml").exists()
        return {"datasets": datasets, "remotes": remotes, "has_dvc_pipeline": pipeline}

    def get_current_version(self, dataset: str | None = None) -> dict[str, Any]:
        dvc_file = self._resolve(dataset)
        info = self._parse_dvc((self.root / dvc_file).read_text(encoding="utf-8"))
        history = self._history(dvc_file, limit=1)
        last = history[0] if history else {}
        data_path = self.root / dvc_file[: -len(".dvc")]
        workspace_md5 = _md5(data_path) if data_path.is_file() else None
        head = self._run("git", "rev-parse", "--short", "HEAD").strip()
        return {
            "dataset": dvc_file[: -len(".dvc")],
            "md5": info["md5"], "size": info["size"],
            "version_tags": last.get("tags", []),
            "committed_in": last.get("commit"), "commit_date": last.get("date"), "commit_message": last.get("message"),
            "repo_head": head,
            "workspace_file_present": data_path.exists(),
            "workspace_matches_dvc": workspace_md5 == info["md5"] if workspace_md5 else None,
        }

    def dataset_history(self, dataset: str | None = None, limit: int = 10) -> dict[str, Any]:
        dvc_file = self._resolve(dataset)
        versions = self._history(dvc_file, limit=max(1, min(int(limit), 50)))
        for v in versions:
            v.pop("commit_full", None)
        return {"dataset": dvc_file[: -len(".dvc")], "versions": versions}

    def find_version_by_hash(self, md5: str, dataset: str | None = None) -> dict[str, Any]:
        md5 = (md5 or "").strip().lower()
        if len(md5) < 6 or not all(c in "0123456789abcdef" for c in md5):
            raise InvalidRequest("md5 must be a hexadecimal DVC hash (at least 6 characters)")
        dvc_file = self._resolve(dataset)
        matches = [v for v in self._history(dvc_file, limit=200) if (v.get("md5") or "").startswith(md5)]
        for v in matches:
            v.pop("commit_full", None)
        return {"dataset": dvc_file[: -len(".dvc")], "md5": md5, "found": bool(matches), "versions": matches,
                "note": None if matches else "no commit in this repository recorded this hash for the dataset"}

    def compare_versions(self, rev_a: str, rev_b: str = "HEAD", dataset: str | None = None) -> dict[str, Any]:
        dvc_file = self._resolve(dataset)
        a, b = self._rev(rev_a), self._rev(rev_b)
        info_a = self._parse_dvc(self._run("git", "show", f"{a}:{dvc_file}"))
        info_b = self._parse_dvc(self._run("git", "show", f"{b}:{dvc_file}"))
        diff = json.loads(self._run("dvc", "diff", "--json", a, b) or "{}")
        size_delta = (info_b["size"] or 0) - (info_a["size"] or 0)
        return {"dataset": dvc_file[: -len(".dvc")],
                "rev_a": {"rev": a, **info_a}, "rev_b": {"rev": b, **info_b},
                "changed": info_a["md5"] != info_b["md5"], "size_delta_bytes": size_delta,
                "dvc_diff": diff}

    def check_status(self) -> dict[str, Any]:
        local = json.loads(self._run("dvc", "status", "--json", timeout=60) or "{}")
        try:
            remote = json.loads(self._run("dvc", "status", "--cloud", "--json", timeout=60) or "{}")
            remote_error = None
        except ToolError as exc:
            remote, remote_error = None, str(exc)
        return {"workspace_changes": local, "workspace_clean": local == {},
                "remote_status": remote, "remote_in_sync": remote == {} if remote is not None else None,
                "remote_error": remote_error}

    def reproduce_version(self, rev: str, dataset: str | None = None) -> dict[str, Any]:
        """Materialise a past dataset version into reproductions/ and verify its hash."""
        dvc_file = self._resolve(dataset)
        rev = self._rev(rev)
        recorded = self._parse_dvc(self._run("git", "show", f"{rev}:{dvc_file}"))
        data_path = dvc_file[: -len(".dvc")]
        target = self.reproduce_dir / rev.replace("/", "_") / Path(data_path).name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        self._run("dvc", "get", ".", data_path, "--rev", rev, "-o", str(target), "--force", timeout=120)
        actual = _md5(target)
        return {
            "dataset": data_path, "rev": rev, "output": str(target.relative_to(self.root)),
            "recorded_md5": recorded["md5"], "reproduced_md5": actual,
            "verified": actual == recorded["md5"],
            "commands": [f"git checkout {rev} -- {dvc_file}", f"dvc checkout {dvc_file}",
                         f"# or, without touching the workspace: dvc get . {data_path} --rev {rev} -o <file>"],
        }

    # ------------------------------------------------------------------ registry
    def specs(self) -> list[ToolSpec]:
        ds = {"dataset": string("Dataset path, e.g. data/raw/breast_cancer.csv (optional: defaults to the "
                                "only tracked dataset)")}
        return [
            ToolSpec("dvc_list_tracked_datasets", "dvc",
                     "List datasets tracked by DVC with their md5 hash and size, the DVC remotes and whether a "
                     "dvc.yaml pipeline exists.", obj(), self.list_tracked_datasets),
            ToolSpec("dvc_get_current_version", "dvc",
                     "Current version of a dataset: DVC md5, size, version tags, the git commit that recorded it, "
                     "and whether the file in the workspace matches the recorded hash.",
                     obj(ds), self.get_current_version),
            ToolSpec("dvc_dataset_history", "dvc",
                     "All recorded versions of a dataset from git history: commit, date, message, version tags, md5 "
                     "and size per version.", obj({**ds, "limit": integer("Maximum versions (default 10)")}),
                     self.dataset_history),
            ToolSpec("dvc_find_version_by_hash", "dvc",
                     "Find which dataset version (git commit and tag) has a given DVC md5 hash. Use it to map the "
                     "dataset hash recorded on an MLflow run to a dataset version.",
                     obj({"md5": string("DVC md5 hash (full or prefix)"), **ds}, ["md5"]),
                     self.find_version_by_hash),
            ToolSpec("dvc_compare_versions", "dvc",
                     "Compare a dataset between two git revisions or tags (e.g. data-v1 and data-v2): hashes, sizes "
                     "and the `dvc diff` result.",
                     obj({"rev_a": string("First revision or tag"), "rev_b": string("Second revision (default HEAD)"),
                          **ds}, ["rev_a"]), self.compare_versions),
            ToolSpec("dvc_check_status", "dvc",
                     "Detect whether tracked data changed: `dvc status` for the workspace and against the remote.",
                     obj(), self.check_status, timeout_s=90),
            ToolSpec("dvc_reproduce_version", "dvc",
                     "Reproduce a previous dataset version: download it with `dvc get` into reproductions/ (the "
                     "workspace is not modified), verify its md5 against the recorded hash, and return the commands "
                     "to check it out.", obj({"rev": string("Git revision or tag, e.g. data-v1"), **ds}, ["rev"]),
                     self.reproduce_version, timeout_s=150),
        ]


def _md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - DVC content hash, not used for security
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
