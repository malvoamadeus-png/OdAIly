from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppPaths:
    root_dir: Path
    backend_dir: Path
    frontend_dir: Path
    data_dir: Path
    raw_dir: Path
    processed_dir: Path
    exports_dir: Path
    config_dir: Path
    market_brief_config_path: Path
    gate_tradfi_config_path: Path


def get_paths() -> AppPaths:
    root_dir = Path(__file__).resolve().parents[3]
    data_dir = root_dir / "data"
    config_dir = data_dir / "config"
    return AppPaths(
        root_dir=root_dir,
        backend_dir=root_dir / "backend",
        frontend_dir=root_dir / "frontend",
        data_dir=data_dir,
        raw_dir=data_dir / "raw",
        processed_dir=data_dir / "processed",
        exports_dir=data_dir / "exports",
        config_dir=config_dir,
        market_brief_config_path=config_dir / "market_brief.json",
        gate_tradfi_config_path=config_dir / "gate_tradfi.json",
    )


def ensure_runtime_dirs(paths: AppPaths) -> None:
    for path in (
        paths.backend_dir,
        paths.frontend_dir,
        paths.data_dir,
        paths.raw_dir,
        paths.processed_dir,
        paths.exports_dir,
        paths.config_dir,
        paths.raw_dir / "market_quotes",
        paths.raw_dir / "gate_quotes",
        paths.processed_dir / "briefs",
    ):
        path.mkdir(parents=True, exist_ok=True)
