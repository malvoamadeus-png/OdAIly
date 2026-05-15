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
    searcher_dir: Path | None = None
    searcher_cache_path: Path | None = None
    writer3_dir: Path | None = None
    writer3_index_path: Path | None = None

    def __post_init__(self) -> None:
        searcher_dir = self.searcher_dir or self.processed_dir / "searcher"
        searcher_cache_path = self.searcher_cache_path or searcher_dir / "searcher.sqlite"
        writer3_dir = self.writer3_dir or self.processed_dir / "writer3"
        writer3_index_path = self.writer3_index_path or writer3_dir / "writer3.sqlite"
        object.__setattr__(self, "searcher_dir", searcher_dir)
        object.__setattr__(self, "searcher_cache_path", searcher_cache_path)
        object.__setattr__(self, "writer3_dir", writer3_dir)
        object.__setattr__(self, "writer3_index_path", writer3_index_path)


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
        searcher_dir=data_dir / "processed" / "searcher",
        searcher_cache_path=data_dir / "processed" / "searcher" / "searcher.sqlite",
        writer3_dir=data_dir / "processed" / "writer3",
        writer3_index_path=data_dir / "processed" / "writer3" / "writer3.sqlite",
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
        paths.searcher_dir,
        paths.writer3_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
