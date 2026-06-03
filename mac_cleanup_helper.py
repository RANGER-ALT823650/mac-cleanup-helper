#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Sequence


HOME = Path.home()
NOW = time.time()
DOWNLOAD_STALE_DAYS = 30

TCC_PROTECTED: set[str] = {
    "familycircled",
    "FamilyCircle",
    "com.apple.HomeKit",
    "CloudKit",
    "com.apple.Safari",
    "com.apple.Safari.SafeBrowsing",
    "com.apple.findmy.imagecache",
    "com.apple.findmy.fmfcore",
    "com.apple.findmy.fmipcore",
    "com.apple.containermanagerd",
    "com.apple.homed",
    "com.apple.ap.adprivacyd",
}


@dataclass
class Candidate:
    path: Path
    size_bytes: int


@dataclass
class CleanupItem:
    key: str
    level: int
    title: str
    description: str
    caution: str
    scanner: Callable[[], List[Candidate]]


def human_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{num_bytes} B"


def safe_scandir(path: Path) -> Iterable[os.DirEntry]:
    try:
        with os.scandir(path) as entries:
            yield from list(entries)
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return


def du_size(path: Path) -> int:
    """Get directory size via `du -sk` (much faster than os.walk). Returns bytes, or 0 on failure."""
    try:
        result = subprocess.run(
            ["du", "-sk", str(path)],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if result.returncode == 0:
            m = re.match(r"\s*(\d+)", result.stdout)
            if m:
                return int(m.group(1)) * 1024  # du -sk returns KB blocks
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError, OSError):
        pass
    return 0


def path_size(path: Path) -> int:
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            size = du_size(path)
            if size > 0:
                return size
            total = 0
            for root, dirs, files in os.walk(path, topdown=True, followlinks=False):
                dirs[:] = [d for d in dirs if not Path(root, d).is_symlink()]
                for name in files:
                    file_path = Path(root, name)
                    try:
                        if not file_path.is_symlink():
                            total += file_path.stat().st_size
                    except (FileNotFoundError, PermissionError, OSError):
                        continue
            return total
    except (FileNotFoundError, PermissionError, OSError):
        return 0
    return 0


def collect_children(directory: Path, skip_names: set[str] | None = None) -> List[Candidate]:
    candidates: List[Candidate] = []
    if not directory.exists():
        return candidates
    for entry in safe_scandir(directory):
        child = Path(entry.path)
        if skip_names and child.name in skip_names:
            continue
        size = path_size(child)
        if size <= 0 and not child.exists():
            continue
        candidates.append(Candidate(path=child, size_bytes=size))
    return candidates


def collect_matching_files(directory: Path, patterns: Sequence[str], older_than_days: int) -> List[Candidate]:
    candidates: List[Candidate] = []
    if not directory.exists():
        return candidates
    cutoff = NOW - older_than_days * 86400
    for root, _, files in os.walk(directory):
        root_path = Path(root)
        for file_name in files:
            if not any(fnmatch.fnmatch(file_name.lower(), pattern.lower()) for pattern in patterns):
                continue
            file_path = root_path / file_name
            try:
                stat = file_path.stat()
            except (FileNotFoundError, PermissionError, OSError):
                continue
            if stat.st_mtime > cutoff:
                continue
            candidates.append(Candidate(path=file_path, size_bytes=stat.st_size))
    return candidates


def collect_backup_directories(directory: Path, older_than_days: int) -> List[Candidate]:
    candidates: List[Candidate] = []
    if not directory.exists():
        return candidates
    cutoff = NOW - older_than_days * 86400
    for entry in safe_scandir(directory):
        path = Path(entry.path)
        try:
            modified = path.stat().st_mtime
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if modified > cutoff:
            continue
        size = path_size(path)
        candidates.append(Candidate(path=path, size_bytes=size))
    return candidates


def delete_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.name in TCC_PROTECTED:
        return
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    shutil.rmtree(path)


def top_candidates(candidates: Sequence[Candidate], limit: int = 8) -> List[Candidate]:
    return sorted(candidates, key=lambda item: item.size_bytes, reverse=True)[:limit]


def build_items() -> List[CleanupItem]:
    return [
        CleanupItem(
            key="user_caches",
            level=1,
            title="用户应用缓存",
            description="清理 ~/Library/Caches 下的大部分 App 缓存，通常可自动重建。",
            caution="安全性高，但首次重新打开某些 App 可能会稍慢。",
            scanner=lambda: collect_children(HOME / "Library" / "Caches", TCC_PROTECTED),
        ),
        CleanupItem(
            key="user_logs",
            level=1,
            title="用户日志",
            description="清理 ~/Library/Logs 下的日志和崩溃报告。",
            caution="删除后不影响 App 使用，但会失去部分排错线索。",
            scanner=lambda: collect_children(HOME / "Library" / "Logs"),
        ),
        CleanupItem(
            key="quark_caches",
            level=2,
            title="夸克缓存",
            description="清理夸克浏览器/网盘缓存，包括视频缓存和通用 Cache 目录。",
            caution="只清理缓存内容，不删除账号、持久化数据库或下载文件；建议先退出夸克。",
            scanner=lambda: sum(
                [
                    collect_children(HOME / "Library" / "Application Support" / "Quark" / "Cache"),
                    collect_children(HOME / "Library" / "Application Support" / "Quark" / "Quark" / "Cache"),
                ],
                [],
            ),
        ),
        CleanupItem(
            key="trash",
            level=1,
            title="废纸篓",
            description="清空 ~/.Trash 里的内容。",
            caution="会永久删除废纸篓中的文件。",
            scanner=lambda: collect_children(HOME / ".Trash"),
        ),
        CleanupItem(
            key="xcode_derived_data",
            level=2,
            title="Xcode DerivedData",
            description="清理 Xcode 编译缓存，常见且很占空间。",
            caution="安全性较高，但下次编译会变慢。",
            scanner=lambda: collect_children(HOME / "Library" / "Developer" / "Xcode" / "DerivedData"),
        ),
        CleanupItem(
            key="xcode_archives",
            level=2,
            title="Xcode Archives",
            description="清理旧归档包，适合不再需要的历史构建产物。",
            caution="如果你需要回滚某个旧归档，请别删对应版本。",
            scanner=lambda: collect_children(HOME / "Library" / "Developer" / "Xcode" / "Archives"),
        ),
        CleanupItem(
            key="ios_simulator_caches",
            level=2,
            title="iOS Simulator 缓存",
            description="清理 CoreSimulator 的缓存目录。",
            caution="不会删掉模拟器设备本身，但部分缓存会重建。",
            scanner=lambda: collect_children(HOME / "Library" / "Developer" / "CoreSimulator" / "Caches"),
        ),
        CleanupItem(
            key="package_manager_caches",
            level=2,
            title="开发工具缓存",
            description="清理 pip / npm / pnpm / Homebrew / CocoaPods 等常见包管理缓存。",
            caution="会让下次安装依赖时重新下载一部分文件。",
            scanner=lambda: sum(
                [
                    collect_children(HOME / ".cache" / "pip"),
                    collect_children(HOME / ".npm"),
                    collect_children(HOME / ".cargo"),
                    collect_children(HOME / ".gradle"),
                    collect_children(HOME / ".m2"),
                    collect_children(HOME / "go" / "pkg"),
                    collect_children(HOME / ".docker"),
                    collect_children(HOME / ".pnpm-store"),
                    collect_children(HOME / "Library" / "Caches" / "pnpm"),
                    collect_children(HOME / "Library" / "Caches" / "Homebrew"),
                    collect_children(HOME / "Library" / "Caches" / "CocoaPods"),
                    collect_children(HOME / "Library" / "Caches" / "pip"),
                    collect_children(HOME / "Library" / "Caches" / "uv"),
                ],
                [],
            ),
        ),
        CleanupItem(
            key="xcodebuildmcp_workspaces",
            level=2,
            title="XcodeBuildMCP 工作区缓存",
            description="清理 Codex / XcodeBuildMCP 的派生构建缓存。",
            caution="只影响后续重新构建速度。",
            scanner=lambda: collect_children(HOME / "Library" / "Developer" / "XcodeBuildMCP" / "workspaces"),
        ),
        CleanupItem(
            key="downloads_installers",
            level=3,
            title="下载目录中的旧安装包",
            description=f"筛出 Downloads 中超过 {DOWNLOAD_STALE_DAYS} 天的 .dmg / .pkg / .zip / .xip / .iso。",
            caution="请确认这些安装包以后不再需要。",
            scanner=lambda: collect_matching_files(
                HOME / "Downloads",
                patterns=["*.dmg", "*.pkg", "*.zip", "*.xip", "*.iso"],
                older_than_days=DOWNLOAD_STALE_DAYS,
            ),
        ),
        CleanupItem(
            key="ios_backups",
            level=3,
            title="iPhone / iPad 本地备份",
            description="清理 MobileSync 里的旧设备备份。",
            caution="删除后无法用本地备份恢复旧设备数据，请谨慎。",
            scanner=lambda: collect_backup_directories(
                HOME / "Library" / "Application Support" / "MobileSync" / "Backup",
                older_than_days=30,
            ),
        ),
        CleanupItem(
            key="device_support",
            level=3,
            title="Xcode 旧设备支持文件",
            description="清理 iOS DeviceSupport 目录中的旧版本支持文件。",
            caution="如果你还要连旧系统真机调试，请保留对应版本。",
            scanner=lambda: collect_children(HOME / "Library" / "Developer" / "Xcode" / "iOS DeviceSupport"),
        ),
        CleanupItem(
            key="containers",
            level=3,
            title="应用沙盒容器数据",
            description="清理 ~/Library/Containers 下的沙盒应用数据。UUID目录常见离线视频、聊天文件等大体积内容。",
            caution="包含应用的用户数据（如B站离线视频、微信文件），建议先进入目录确认内容再操作。",
            scanner=lambda: collect_children(HOME / "Library" / "Containers"),
        ),
        CleanupItem(
            key="group_containers",
            level=3,
            title="应用组共享容器",
            description="清理 ~/Library/Group Containers 下的应用与扩展共享数据。",
            caution="应用主体与Widget/Extension共享的数据，清理可能影响扩展功能。",
            scanner=lambda: collect_children(HOME / "Library" / "Group Containers"),
        ),
        CleanupItem(
            key="app_support",
            level=3,
            title="应用支持文件",
            description="清理 ~/Library/Application Support 下的应用持久化数据。Chrome配置、Claude Desktop VM、飞书缓存等常很占空间。",
            caution="包含浏览器配置、聊天记录等重要用户数据，强烈建议先确认内容。",
            scanner=lambda: collect_children(HOME / "Library" / "Application Support"),
        ),
    ]


def progress_bar(current: int, total: int, label: str = "", width: int = 30) -> str:
    """Return a single-line progress bar string (no newline)."""
    filled = int(width * current / total) if total > 0 else width
    bar = "█" * filled + "░" * (width - filled)
    pct = current * 100 // total if total > 0 else 100
    return f"\r  扫描中 [{bar}] {current}/{total} ({pct:3d}%)  {label}"


def summarize(items: Sequence[CleanupItem]) -> list[tuple[CleanupItem, List[Candidate], int]]:
    summary = []
    total = len(items)
    for i, item in enumerate(items):
        print(progress_bar(i, total, item.title), end="", flush=True)
        candidates = item.scanner()
        item_total = sum(candidate.size_bytes for candidate in candidates)
        summary.append((item, candidates, item_total))
    print(progress_bar(total, total, "完成"))
    print()  # final newline
    return summary


def print_summary(summary: Sequence[tuple[CleanupItem, List[Candidate], int]]) -> None:
    print("\n可清理项目总览")
    print("-" * 90)
    for index, (item, candidates, total) in enumerate(summary, start=1):
        count = len(candidates)
        print(
            f"[{index:02d}] L{item.level} | {item.title:<28} | "
            f"{human_size(total):>9} | {count:>4} 项 | {item.key}"
        )
        print(f"     {item.description}")
        print(f"     注意: {item.caution}")
    print("-" * 90)


def show_item_details(summary: Sequence[tuple[CleanupItem, List[Candidate], int]]) -> None:
    for index, (item, candidates, total) in enumerate(summary, start=1):
        if not candidates:
            continue
        print(f"\n[{index:02d}] {item.title} - {human_size(total)}")
        for candidate in top_candidates(candidates):
            print(f"  - {human_size(candidate.size_bytes):>9}  {candidate.path}")


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input(f"{prompt} {suffix} ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("请输入 y 或 n。")


def parse_selection(raw: str, limit: int) -> List[int]:
    result = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start_text, end_text = chunk.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            for number in range(start, end + 1):
                if 1 <= number <= limit:
                    result.add(number)
        else:
            number = int(chunk)
            if 1 <= number <= limit:
                result.add(number)
    return sorted(result)


def selective_delete(candidates: Sequence[Candidate]) -> tuple[int, int]:
    """Let user pick specific items to delete from a candidate list."""
    sorted_candidates = sorted(candidates, key=lambda c: c.size_bytes, reverse=True)

    print(f"\n  共 {len(sorted_candidates)} 项。输入编号选择（如 1,3,5-7），a=全选，q=返回：")
    for i, c in enumerate(sorted_candidates, start=1):
        print(f"    [{i:3d}]  {human_size(c.size_bytes):>9}  {c.path.name}")

    while True:
        raw = input("  > ").strip().lower()
        if raw in ("q", ""):
            return 0, 0
        if raw == "a":
            indexes = list(range(1, len(sorted_candidates) + 1))
            break
        try:
            indexes = parse_selection(raw, len(sorted_candidates))
            if indexes:
                break
            print("  未匹配任何编号，请重试。")
        except ValueError:
            print("  格式不正确，请重试。")

    selected = [sorted_candidates[i - 1] for i in indexes]
    total_selected = sum(c.size_bytes for c in selected)
    print(f"  已选择 {len(selected)} 项，预计释放 {human_size(total_selected)}")

    if not ask_yes_no("  确认删除？", default=False):
        return 0, 0

    removed_count = 0
    removed_bytes = 0
    for candidate in selected:
        try:
            size = candidate.size_bytes
            delete_path(candidate.path)
            removed_count += 1
            removed_bytes += size
        except Exception as exc:
            print(f"  ! 删除失败: {candidate.path} -> {exc}")

    return removed_count, removed_bytes


def delete_candidates(item: CleanupItem, candidates: Sequence[Candidate]) -> tuple[int, int]:
    removed_count = 0
    removed_bytes = 0
    for candidate in candidates:
        try:
            size = candidate.size_bytes
            delete_path(candidate.path)
            removed_count += 1
            removed_bytes += size
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 删除失败: {candidate.path} -> {exc}")
    return removed_count, removed_bytes


def clean_selected(summary: Sequence[tuple[CleanupItem, List[Candidate], int]], indexes: Sequence[int]) -> None:
    total_removed_count = 0
    total_removed_bytes = 0
    for index in indexes:
        item, candidates, total = summary[index - 1]
        if not candidates:
            print(f"\n跳过 {item.title}，没有可清理内容。")
            continue

        print(f"\n准备处理: {item.title}")
        print(f"预计可释放: {human_size(total)}，共 {len(candidates)} 项")
        for candidate in top_candidates(candidates, limit=5):
            print(f"  - {human_size(candidate.size_bytes):>9}  {candidate.path}")

        print(f"\n请选择操作: [y=全部清理 / s=逐项选择 / n=跳过]")
        skip_item = False
        while True:
            choice = input("  > ").strip().lower()
            if choice in ("n", ""):
                print("  已跳过。")
                skip_item = True
                break
            if choice == "y":
                removed_count, removed_bytes = delete_candidates(item, candidates)
                break
            if choice == "s":
                removed_count, removed_bytes = selective_delete(candidates)
                break
            print("  请输入 y / s / n。")
        if skip_item:
            continue
        total_removed_count += removed_count
        total_removed_bytes += removed_bytes
        print(f"已清理 {removed_count} 项，估算释放 {human_size(removed_bytes)}。")

    print("\n清理完成")
    print(f"总计删除: {total_removed_count} 项")
    print(f"估算释放: {human_size(total_removed_bytes)}")


def choose_items(summary: Sequence[tuple[CleanupItem, List[Candidate], int]]) -> List[int]:
    print(
        "\n选择清理方式:\n"
        "  1. 只清理 L1 安全项\n"
        "  2. 清理 L1 + L2\n"
        "  3. 清理全部 L1 + L2 + L3\n"
        "  4. 自定义选择编号\n"
        "  5. 退出"
    )
    while True:
        choice = input("请输入选项编号: ").strip()
        if choice == "1":
            return [i for i, (item, _, total) in enumerate(summary, start=1) if item.level <= 1 and total > 0]
        if choice == "2":
            return [i for i, (item, _, total) in enumerate(summary, start=1) if item.level <= 2 and total > 0]
        if choice == "3":
            return [i for i, (_, _, total) in enumerate(summary, start=1) if total > 0]
        if choice == "4":
            raw = input("输入编号，支持 1,3,5-7 这种格式: ").strip()
            try:
                return parse_selection(raw, len(summary))
            except ValueError:
                print("编号格式不正确，请重试。")
                continue
        if choice == "5":
            return []
        print("请输入 1-5。")


def ensure_macos() -> None:
    if sys.platform != "darwin":
        print("这个脚本主要面向 macOS 目录结构设计。")
        if not ask_yes_no("仍然继续吗？", default=False):
            sys.exit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="手动清理 macOS 缓存和不必要文件的交互脚本")
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="只扫描并显示可清理内容，不执行删除",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="扫描后显示每个项目里最大的若干文件/目录",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ensure_macos()

    print("macOS 手动清理助手")
    print("特点: 先扫描、分等级、逐项确认，不会默认直接删除。")

    items = build_items()
    summary = summarize(items)
    print_summary(summary)

    if args.details or ask_yes_no("要展开看每个项目里最大的文件/目录吗？", default=False):
        show_item_details(summary)

    if args.scan_only:
        print("\n当前是 --scan-only 模式，未执行任何删除。")
        return

    selected_indexes = choose_items(summary)
    if not selected_indexes:
        print("未选择任何项目，已退出。")
        return

    clean_selected(summary, selected_indexes)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n用户取消。")
        sys.exit(130)
