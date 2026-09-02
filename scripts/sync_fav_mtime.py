#!/usr/bin/env python3
import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "ttracker" / "fav.json"


@dataclass
class MtimeSyncReport:
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    missing_targets: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class FavMtimeConfig:
    recordings_fav_path: Path


def _load_config(path: Path) -> FavMtimeConfig:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    return FavMtimeConfig(recordings_fav_path=Path(data["recordings_fav_path"]).expanduser())


def sync_link_mtimes(link_dir: os.PathLike | str, dry_run: bool = False) -> MtimeSyncReport:
    report = MtimeSyncReport()
    root = Path(link_dir).expanduser()

    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if not entry.is_symlink():
            report.skipped.append(entry.name)
            continue

        try:
            target = entry.resolve(strict=True)
        except FileNotFoundError:
            report.missing_targets.append(entry.name)
            continue
        except OSError as exc:
            report.errors.append(f"{entry.name}: {exc}")
            continue

        if not target.is_dir():
            report.skipped.append(entry.name)
            continue

        try:
            target_stat = target.stat()
            link_stat = entry.lstat()
            if link_stat.st_mtime_ns == target_stat.st_mtime_ns:
                report.unchanged.append(entry.name)
                continue

            report.updated.append(entry.name)
            if not dry_run:
                os.utime(
                    entry,
                    ns=(link_stat.st_atime_ns, target_stat.st_mtime_ns),
                    follow_symlinks=False,
                )
        except OSError as exc:
            report.errors.append(f"{entry.name}: {exc}")

    return report


def format_report(report: MtimeSyncReport, dry_run: bool = False) -> str:
    lines: list[str] = []
    update_label = "would update" if dry_run else "updated"

    if report.updated:
        lines.append(f"{update_label}: {', '.join(report.updated)}")
    if report.unchanged:
        lines.append(f"unchanged: {len(report.unchanged)}")
    if report.missing_targets:
        lines.append(f"missing targets: {', '.join(report.missing_targets)}")
    if report.errors:
        lines.append(f"errors: {'; '.join(report.errors)}")

    if not lines:
        return "no symlink mtimes to update"

    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync favorite symlink mtimes from their target directories.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to installed tools config JSON")
    parser.add_argument("--path", help="Favorite symlink directory. Defaults to RECORDINGS_FAV_PATH from config")
    parser.add_argument("--dry-run", action="store_true", help="Print what would change without touching symlinks")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    config_path = Path(args.config).expanduser()

    if not config_path.exists():
        print(f"tools config not found: {config_path}")
        print("Run the TkLiveTracker tools installer first.")
        return 2

    config = _load_config(config_path)
    link_dir = Path(args.path).expanduser() if args.path else config.recordings_fav_path

    if not link_dir.is_dir():
        print(f"favorite symlink directory not found: {link_dir}")
        return 1

    report = sync_link_mtimes(link_dir, dry_run=args.dry_run)
    print(format_report(report, dry_run=args.dry_run))
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
