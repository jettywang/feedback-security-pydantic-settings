"""Demo backend: one settings model backed by BOTH pyproject.toml and dotenv sources.

The point of this file is to make every provenance fact observable:

* which pyproject.toml path the ``PyprojectTomlConfigSettingsSource`` actually picked,
* which ``.env`` path the ``DotEnvSettingsSource`` actually read (after upward search),
* what each source contributed, and what the merged, validated settings are.

All sample values are non-sensitive placeholders.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    PyprojectTomlConfigSettingsSource,
    SettingsConfigDict,
)
from pydantic_settings.sources.utils import _resolve_config_file

EXPLICIT_PYPROJECT = (
    Path(os.environ.get("DEMO_EXPLICIT_PYPROJECT", "")).expanduser()
    if os.environ.get("DEMO_EXPLICIT_PYPROJECT")
    else None
)
EXPLICIT_DOTENV = (
    Path(os.environ.get("DEMO_EXPLICIT_DOTENV", "")).expanduser() if os.environ.get("DEMO_EXPLICIT_DOTENV") else None
)
DOTENV_DEPTH = int(os.environ.get("DEMO_ENV_FILE_DEPTH", "0"))
PYPROJECT_DEPTH = int(os.environ.get("DEMO_PYPROJECT_DEPTH", "2"))


class Service(BaseModel):
    name: str
    region: str


class AppSettings(BaseSettings):
    """Settings with a deliberately fixed, non-sensitive shape."""

    app_name: str = "fallback-app"
    service_region: str = "local-default"
    retries: int = 0
    feature_flag: bool = False
    service: Service = Service(name="fallback-svc", region="local-default")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_depth=DOTENV_DEPTH,
        pyproject_toml_depth=PYPROJECT_DEPTH,
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        pyproject_source = PyprojectTomlConfigSettingsSource(settings_cls, EXPLICIT_PYPROJECT)
        # Priority (highest first): explicit init kwargs > real environment > dotenv > pyproject > defaults.
        return init_settings, env_settings, dotenv_settings, pyproject_source


def _describe_pyproject() -> dict[str, Any]:
    source = PyprojectTomlConfigSettingsSource(AppSettings, EXPLICIT_PYPROJECT)
    return {
        "picked_path": str(source.toml_file_path),
        "path_exists": source.toml_file_path.is_file(),
        "resolved_from": "explicit_argument" if EXPLICIT_PYPROJECT is not None else "cwd_or_parents",
        "contributed": source(),
    }


def _describe_dotenv() -> dict[str, Any]:
    source = DotEnvSettingsSource(
        AppSettings,
        env_file=EXPLICIT_DOTENV if EXPLICIT_DOTENV is not None else ".env",
        env_file_depth=DOTENV_DEPTH,
    )
    requested = EXPLICIT_DOTENV if EXPLICIT_DOTENV is not None else Path(".env")
    # Mirror the exact internal resolution so the report shows what upward search decided.
    resolved = _resolve_config_file(requested, DOTENV_DEPTH, allow_fifo=True)
    return {
        "requested_path": str(requested),
        "picked_path": str(resolved) if resolved is not None else None,
        "path_exists": resolved is not None and resolved.is_file(),
        "resolved_from": "explicit_argument" if EXPLICIT_DOTENV is not None else "cwd_or_parents",
        "contributed": source(),
    }


def main() -> None:
    settings = AppSettings()
    report = {
        "cwd": os.getcwd(),
        "config_knobs": {
            "pyproject_toml_depth": PYPROJECT_DEPTH,
            "env_file_depth": DOTENV_DEPTH,
            "explicit_pyproject": str(EXPLICIT_PYPROJECT) if EXPLICIT_PYPROJECT else None,
            "explicit_dotenv": str(EXPLICIT_DOTENV) if EXPLICIT_DOTENV else None,
        },
        "pyproject_source": _describe_pyproject(),
        "dotenv_source": _describe_dotenv(),
        "merged_settings": settings.model_dump(),
    }
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    sys.exit(main())
