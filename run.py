#!/usr/bin/env python3
"""Cross-platform launcher for Civic Air Quality Briefing Studio (Windows, macOS, Linux).

Needs only Python 3.11+ (no Nix, no Docker). First run creates a local virtual environment in
./.venv, installs requirements.txt, then starts Streamlit and opens the browser.

    python run.py                  # start (default http://localhost:8501)
    python run.py --port 8700      # choose a port (auto-picks the next free one if busy)
    python run.py --no-browser     # do not open a browser tab
    python run.py --host 0.0.0.0   # let other devices on the network connect
    python run.py --reinstall      # force a dependency reinstall
    python run.py --check          # environment self-check, then exit
    python run.py --test           # install dev deps and run the test suite

Configuration: put GEMINI_API_KEY=... in a .env file next to this script (see .env.example) or
set it in the environment. Without a key the app runs in offline fallback mode.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import socket
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
IS_WINDOWS = os.name == "nt"
MIN_PY = (3, 11)


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def say(msg: str) -> None:
    print(f"[run] {msg}", flush=True)


def load_dotenv(path: Path) -> list[str]:
    """Minimal .env reader (KEY=VALUE, # comments, optional quotes). Does not override real env vars."""
    loaded: list[str] = []
    if not path.is_file():
        return loaded
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def free_port(host: str, wanted: int) -> int:
    bind = "127.0.0.1" if host in ("0.0.0.0", "") else host
    for port in range(wanted, wanted + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            if s.connect_ex((bind, port)) != 0:  # nothing is listening
                return port
    raise SystemExit(f"No free port found in {wanted}-{wanted + 49}. Use --port.")


def venv_works() -> bool:
    py = venv_python()
    if not py.exists():
        return False
    try:
        subprocess.run([str(py), "-c", "import sys"], check=True, capture_output=True, timeout=30)
        return True
    except Exception:
        return False


def ensure_venv() -> None:
    if venv_works():
        return
    if VENV.exists() or VENV.is_symlink():
        say(".venv exists but its Python does not run; recreating it")
        if VENV.is_symlink():
            VENV.unlink()
        else:
            import shutil

            shutil.rmtree(VENV, ignore_errors=True)
    say(f"creating virtual environment in {VENV} (one time)")
    venv.EnvBuilder(with_pip=True, clear=False, symlinks=not IS_WINDOWS).create(VENV)


def deps_hash(files: list[Path]) -> str:
    h = hashlib.sha256()
    for f in files:
        h.update(f.read_bytes())
    return h.hexdigest()


def imports_ok(dev: bool) -> bool:
    mods = "streamlit, pandas, numpy, pydantic, google.genai, openpyxl, yaml, plotly" + (", pytest" if dev else "")
    r = subprocess.run([str(venv_python()), "-c", f"import {mods}"], capture_output=True, env=child_env())
    return r.returncode == 0


def ensure_deps(dev: bool = False, force: bool = False) -> None:
    req = [ROOT / ("requirements-dev.txt" if dev else "requirements.txt"), ROOT / "requirements.txt"]
    stamp = VENV / (".deps-dev.sha" if dev else ".deps.sha")
    digest = deps_hash(sorted(set(req)))
    if not force and stamp.exists() and stamp.read_text().strip() == digest:
        return
    if not force and imports_ok(dev):
        stamp.write_text(digest)  # already satisfied (e.g. a uv/devenv-managed environment)
        return
    say("installing dependencies (first run takes a minute or two)...")
    cmd = [str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(req[0])]
    if subprocess.run(cmd, cwd=ROOT).returncode != 0:
        raise SystemExit(
            "pip install failed. Check your internet connection or proxy "
            "(pip install --proxy http://user:pass@host:port ...) and run again."
        )
    stamp.write_text(digest)


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"  # Windows: default to UTF-8 file I/O (Hindi/µ text in prompts and data)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    return env


def check() -> int:
    ok = True
    say(f"Python {sys.version.split()[0]} on {sys.platform}")
    if sys.version_info < MIN_PY:
        say(f"FAIL: Python {MIN_PY[0]}.{MIN_PY[1]}+ required")
        ok = False
    for rel in ("app.py", "data/sample_week1.csv", "data/sample_week2.csv", "data/action_library.yaml", "requirements.txt"):
        exists = (ROOT / rel).exists()
        say(f"{'ok  ' if exists else 'FAIL'} {rel}")
        ok &= exists
    loaded = load_dotenv(ROOT / ".env")
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    say("Gemini key: " + ("found" if key else "not set (offline fallback mode)") + (f" (.env: {', '.join(loaded)})" if loaded else ""))
    if venv_works():
        r = subprocess.run(
            [str(venv_python()), "-c", "import streamlit, pandas, pydantic, yaml, plotly, openpyxl; print('imports ok')"],
            env=child_env(), capture_output=True, text=True,
        )
        say(r.stdout.strip() or ("FAIL imports: " + r.stderr.strip()[-300:]))
        ok &= r.returncode == 0
    else:
        say("virtual environment not created yet (it will be on first start)")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8501)))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reinstall", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    if sys.version_info < MIN_PY:
        raise SystemExit(f"Python {MIN_PY[0]}.{MIN_PY[1]} or newer is required (this is {sys.version.split()[0]}). "
                         "Install it from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'.")
    if args.check:
        return check()

    os.chdir(ROOT)
    loaded = load_dotenv(ROOT / ".env")
    ensure_venv()
    ensure_deps(dev=args.test, force=args.reinstall)

    if args.test:
        return subprocess.run([str(venv_python()), "-m", "pytest", "-q"], cwd=ROOT, env=child_env()).returncode

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    say("Gemini: " + ("API key found" if key else "no key set -> offline fallback mode (see .env.example)")
        + (f" [loaded {', '.join(loaded)} from .env]" if loaded else ""))
    port = free_port(args.host, args.port)
    if port != args.port:
        say(f"port {args.port} is busy, using {port}")
    url = f"http://localhost:{port}"
    say(f"starting {url}   (Ctrl+C to stop)")
    cmd = [
        str(venv_python()), "-m", "streamlit", "run", "app.py",
        "--server.port", str(port), "--server.address", args.host,
        "--server.headless", "true" if args.no_browser else "false",
        "--browser.gatherUsageStats", "false",
    ]
    try:
        return subprocess.run(cmd, cwd=ROOT, env=child_env()).returncode
    except KeyboardInterrupt:
        say("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
