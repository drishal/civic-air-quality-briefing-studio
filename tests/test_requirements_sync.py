"""requirements.txt (used by run.py / pip on Windows) must match pyproject.toml runtime deps."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _norm(spec: str) -> str:
    return re.sub(r"\s+", "", spec.split("#")[0]).lower()


def test_requirements_match_pyproject():
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    runtime = {_norm(d) for d in deps if not _norm(d).startswith("pytest")}
    reqs = {
        _norm(line)
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if _norm(line)
    }
    assert reqs == runtime, f"out of sync: only in requirements={reqs - runtime}, only in pyproject={runtime - reqs}"
