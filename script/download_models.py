"""Download AutoCata model assets from Hugging Face Hub."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "model_assets.yml"


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Model asset config not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    assets = data.get("model_assets")
    if not isinstance(assets, dict):
        raise ValueError(f"Missing top-level 'model_assets' mapping in {path}")
    return assets


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    raise TypeError(f"Expected string or list, got {type(value).__name__}")


def resolve_local_dir(raw_path: str | None) -> Path:
    local_dir = Path(raw_path or ".")
    if not local_dir.is_absolute():
        local_dir = PROJECT_ROOT / local_dir
    return local_dir.resolve()


def missing_paths(local_dir: Path, required_paths: list[str]) -> list[str]:
    missing: list[str] = []
    for rel_path in required_paths:
        if not (local_dir / rel_path).exists():
            missing.append(rel_path)
    return missing


def print_plan(
    repo_id: str,
    repo_type: str,
    revision: str,
    local_dir: Path,
    required_paths: list[str],
    allow_patterns: list[str],
) -> None:
    print(f"[assets] repo_id={repo_id}")
    print(f"[assets] repo_type={repo_type}")
    print(f"[assets] revision={revision}")
    print(f"[assets] local_dir={local_dir}")
    print("[assets] required_paths:")
    for path in required_paths:
        print(f"  - {path}")
    if allow_patterns:
        print("[assets] allow_patterns:")
        for pattern in allow_patterns:
            print(f"  - {pattern}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download AutoCata model assets from Hugging Face Hub."
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="Path to model_assets.yml.",
    )
    parser.add_argument("--repo-id", help="Override Hugging Face repo id.")
    parser.add_argument("--repo-type", help="Override Hugging Face repo type.")
    parser.add_argument("--revision", help="Override Hugging Face revision.")
    parser.add_argument(
        "--local-dir",
        help="Destination directory. Defaults to model_assets.local_dir.",
    )
    parser.add_argument(
        "--token",
        help="Optional Hugging Face token. If omitted, the local HF login/cache is used.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the download plan without contacting Hugging Face Hub.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only check whether required local asset paths exist.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Download even when all required paths already exist.",
    )
    parser.add_argument(
        "--local-files-only",
        action="store_true",
        help="Use only the local Hugging Face cache.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    try:
        assets = load_config(config_path)
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    repo_id = args.repo_id or str(assets.get("repo_id", "")).strip()
    if not repo_id:
        print("[error] Missing Hugging Face repo id.", file=sys.stderr)
        return 2

    repo_type = args.repo_type or str(assets.get("repo_type", "model"))
    revision = args.revision or str(assets.get("revision", "main"))
    local_dir = resolve_local_dir(args.local_dir or assets.get("local_dir"))
    required_paths = as_list(assets.get("required_paths"))
    allow_patterns = as_list(assets.get("allow_patterns"))
    ignore_patterns = as_list(assets.get("ignore_patterns"))

    print_plan(repo_id, repo_type, revision, local_dir, required_paths, allow_patterns)

    missing = missing_paths(local_dir, required_paths)
    if missing:
        print("[assets] missing required paths:")
        for path in missing:
            print(f"  - {path}")
    else:
        print("[assets] all required paths are present")

    if args.check_only:
        return 1 if missing else 0

    if args.dry_run:
        print("[dry-run] skipped download")
        return 0

    if not missing and not args.force:
        print("[assets] nothing to download; use --force to refresh")
        return 0

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print(
            "[error] Missing dependency: huggingface_hub. "
            "Install it or recreate the conda environment from envs/autocata.yml.",
            file=sys.stderr,
        )
        return 2

    local_dir.mkdir(parents=True, exist_ok=True)
    print("[assets] downloading from Hugging Face Hub...")
    snapshot_download(
        repo_id=repo_id,
        repo_type=repo_type,
        revision=revision,
        local_dir=str(local_dir),
        allow_patterns=allow_patterns or None,
        ignore_patterns=ignore_patterns or None,
        token=args.token,
        local_files_only=args.local_files_only,
    )

    missing_after = missing_paths(local_dir, required_paths)
    if missing_after:
        print("[error] download completed, but required paths are still missing:", file=sys.stderr)
        for path in missing_after:
            print(f"  - {path}", file=sys.stderr)
        return 1

    print("[assets] model assets are ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
