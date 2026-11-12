"""Build a reproducible temp-tree and launch the same backend from several working dirs.

Run with the repo venv:

    .venv/bin/python examples/startup_config_demo/run_demo.py

Everything is created under ``$TMPDIR`` (a fresh, uniquely named directory) and printed at
the end, so a reviewer can ``cd`` into it and re-run any single scenario by hand.
No real secrets are used; every value is an obvious placeholder.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "app.py"

# A minimal, scrubbed environment so host env vars cannot leak into the demo.
SAFE_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "PYTHONPATH": str(HERE.parent.parent),
    "LANG": "en_US.UTF-8",
}

PYPROJECT_TOML = """\
[tool.pydantic-settings]
app_name = "release-pyproject-app"
service_region = "pyproject-region"
retries = 7
feature_flag = true

[tool.pydantic-settings.service]
name = "release-svc"
region = "pyproject-svc-region"
"""

DOTENV = """\
APP_NAME=release-dotenv-app
SERVICE_REGION=dotenv-region
RETRIES=3
FEATURE_FLAG=false
SERVICE__NAME=dotenv-svc
"""


def build_tree(root: Path) -> dict[str, Path]:
    # The task workdir nests *under* the release root, so the release files are real
    # ancestors (the only relationship upward search can traverse). A separate
    # ``sibling_task`` sits on a different branch and can never reach the release by
    # walking parents, no matter how large the depth.
    release = root / "opt" / "release"
    task = release / "var" / "jobs" / "task-a1b2"
    sibling_task = root / "tmp" / "jobs" / "task-side-branch"
    release.mkdir(parents=True)
    task.mkdir(parents=True)
    sibling_task.mkdir(parents=True)

    (release / "pyproject.toml").write_text(PYPROJECT_TOML)
    (release / ".env").write_text(DOTENV)
    # Decoys higher up the tree: upward search can reach these when the release files
    # are not ancestors, demonstrating why an unbounded parent walk is a trust risk.
    (root / "pyproject.toml").write_text('[tool.pydantic-settings]\napp_name = "root-decoy-app"\nretries = 99\n')
    (root / ".env").write_text("APP_NAME=root-decoy-dotenv-app\nRETRIES=88\n")
    return {"root": root, "release": release, "task": task, "sibling_task": sibling_task}


def run(label: str, cwd: Path, extra_env: dict[str, str] | None = None) -> dict:
    env = dict(SAFE_ENV)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, str(APP)],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return {"label": label, "cwd": str(cwd), "extra_env": extra_env or {}, "report": json.loads(proc.stdout)}


def print_tree(root: Path) -> None:
    print("Temporary layout (all paths under a fresh $TMPDIR dir):")
    for path in sorted(root.rglob("*")):
        depth = len(path.relative_to(root).parts)
        marker = "/" if path.is_dir() else ""
        print(f'  {"    " * (depth - 1)}{path.name}{marker}')


def main() -> None:
    # mkdtemp keeps the tree on disk after the run so each scenario can be re-created by hand.
    root = Path(tempfile.mkdtemp(prefix="config-source-demo-", dir=os.environ.get("TMPDIR")))
    paths = build_tree(root)
    print_tree(root)
    print()

    scenarios = [
        run(
            "1. release dir, depth=0 -> outcome A: the files in the cwd are used",
            paths["release"],
            {"DEMO_ENV_FILE_DEPTH": "0", "DEMO_PYPROJECT_DEPTH": "0"},
        ),
        run(
            "2. nested task dir, depth=0 -> outcome B: nothing found, defaults survive",
            paths["task"],
            {"DEMO_ENV_FILE_DEPTH": "0", "DEMO_PYPROJECT_DEPTH": "0"},
        ),
        run(
            "3. nested task dir, depth=3 -> outcome C: upward search finds the release ancestors",
            paths["task"],
            {"DEMO_ENV_FILE_DEPTH": "3", "DEMO_PYPROJECT_DEPTH": "3"},
        ),
        run(
            "4. same as 3, plus a real env var -> env beats dotenv beats pyproject",
            paths["task"],
            {
                "DEMO_ENV_FILE_DEPTH": "3",
                "DEMO_PYPROJECT_DEPTH": "3",
                "APP_NAME": "real-env-app",
            },
        ),
        run(
            "5. side-branch task dir, depth=99 -> misses release, silently picks up root decoys",
            paths["sibling_task"],
            {"DEMO_ENV_FILE_DEPTH": "99", "DEMO_PYPROJECT_DEPTH": "99"},
        ),
        run(
            "6. side-branch task dir with explicit absolute paths -> pins cross-branch, depth ignored",
            paths["sibling_task"],
            {
                "DEMO_ENV_FILE_DEPTH": "0",
                "DEMO_PYPROJECT_DEPTH": "0",
                "DEMO_EXPLICIT_PYPROJECT": str(paths["release"] / "pyproject.toml"),
                "DEMO_EXPLICIT_DOTENV": str(paths["release"] / ".env"),
            },
        ),
    ]

    for scenario in scenarios:
        report = scenario["report"]
        print("=" * 88)
        print(f"SCENARIO {scenario['label']}")
        print(f"  cwd                 : {report['cwd']}")
        if scenario["extra_env"]:
            print(f"  extra env           : {scenario['extra_env']}")
        pyproject = report["pyproject_source"]
        dotenv = report["dotenv_source"]
        print(f"  pyproject picked    : {pyproject['picked_path']} (exists={pyproject['path_exists']})")
        print(f"  dotenv picked       : {dotenv['picked_path']} (exists={dotenv['path_exists']})")
        print(f"  pyproject contributed: {pyproject['contributed']}")
        print(f"  dotenv contributed  : {dotenv['contributed']}")
        print(f"  merged settings     : {report['merged_settings']}")
        print()

    print(f"Raw JSON for each scenario was printed by {APP.name}; temp root retained at:")
    print(f"  {root}")


if __name__ == "__main__":
    main()
