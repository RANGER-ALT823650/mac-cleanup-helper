#!/usr/bin/env python3
"""Interactive macOS cleanup helper with a two-layer, safety-first menu.

The first layer is always the cleanup-category overview.  A category number opens
its second-layer detail page; deletion requires an explicit ``c`` command so a
plain number can never be mistaken for destructive input.
"""

from __future__ import annotations

import argparse
import json
import fnmatch
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence


HOME = Path.home()
NOW = time.time()
DOWNLOAD_STALE_DAYS = 30
XCODE_DERIVED_DATA_ROOT = HOME / "Library" / "Developer" / "Xcode" / "DerivedData"

# Only these rebuildable paths are offered under the DerivedData cleanup item.
# SourcePackages is deliberately absent: it contains Swift Package checkouts such
# as GRDB and should not disappear during an ordinary build-cache cleanup.
XCODE_GLOBAL_CACHE_TARGETS: dict[str, str] = {
    "ModuleCache.noindex": "全局模块缓存",
    "SDKStatCaches.noindex": "SDK 统计缓存",
    "SymbolCache.noindex": "全局符号缓存",
}
XCODE_PROJECT_CACHE_TARGETS: dict[str, str] = {
    "Build": "编译产物",
    "Index.noindex": "代码索引",
    "Logs": "构建日志",
    "SymbolCache": "符号缓存",
    "TextIndex": "文本索引",
    "OpenQuickly-ReferencedFrameworks.index-v1": "快速打开索引",
}

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
    label: str | None = None
    delete_command: Sequence[str] | None = None


@dataclass
class CleanupItem:
    key: str
    risk_level: int
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


def run_command(command: Sequence[str], timeout: int = 120) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError, OSError):
        return None


def command_output(command: Sequence[str], timeout: int = 120) -> str:
    result = run_command(command, timeout=timeout)
    if result is None or result.returncode != 0:
        return ""
    return result.stdout


def candidate_display(candidate: Candidate) -> str:
    return candidate.label or str(candidate.path)


def candidate_name(candidate: Candidate) -> str:
    return candidate.label or candidate.path.name


def collect_code_sign_clones() -> List[Candidate]:
    candidates: List[Candidate] = []
    for cache_root in Path("/private/var/folders").glob("*/*/X/*code_sign_clone*"):
        if cache_root.is_dir():
            candidates.extend(collect_children(cache_root))
    return candidates


def collect_core_simulator_dyld_cache() -> List[Candidate]:
    path = Path("/Library/Developer/CoreSimulator/Caches/dyld")
    size = path_size(path)
    if size <= 0:
        return []
    return [
        Candidate(
            path=path,
            size_bytes=size,
            label="全部 Simulator dyld shared cache",
            delete_command=["xcrun", "simctl", "runtime", "dyld_shared_cache", "remove", "--all"],
        )
    ]


def collect_unavailable_simulator_devices() -> List[Candidate]:
    raw = command_output(["xcrun", "simctl", "list", "devices", "-j"])
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    candidates: List[Candidate] = []
    devices_root = HOME / "Library" / "Developer" / "CoreSimulator" / "Devices"
    for runtime, devices in data.get("devices", {}).items():
        for device in devices:
            if device.get("isAvailable", True):
                continue
            udid = device.get("udid")
            name = device.get("name", "Unknown Simulator")
            if not udid:
                continue
            path = devices_root / udid
            size = path_size(path)
            candidates.append(
                Candidate(
                    path=path,
                    size_bytes=size,
                    label=f"{name} ({runtime})",
                    delete_command=["xcrun", "simctl", "delete", udid],
                )
            )
    return candidates


def collect_old_simulator_runtimes(not_used_days: int = 30) -> List[Candidate]:
    runtime_raw = command_output(["xcrun", "simctl", "runtime", "list", "-j"])
    dry_run_raw = command_output(
        ["xcrun", "simctl", "runtime", "delete", "--notUsedSinceDays", str(not_used_days), "--dry-run"]
    )
    if not runtime_raw or not dry_run_raw:
        return []

    try:
        runtime_data = json.loads(runtime_raw)
    except json.JSONDecodeError:
        return []

    runtime_sizes: dict[str, int] = {}
    runtime_labels: dict[str, str] = {}
    for identifier, runtime in runtime_data.items():
        if not isinstance(runtime, dict):
            continue
        version = runtime.get("version", "?")
        build = runtime.get("build", "?")
        runtime_identifier = runtime.get("runtimeIdentifier", "")
        platform = runtime_identifier.rsplit(".", 1)[-1].split("-", 1)[0] or "Simulator"
        runtime_sizes[identifier] = int(runtime.get("sizeBytes") or 0)
        runtime_labels[identifier] = f"{platform} {version} ({build})"

    candidates: List[Candidate] = []
    for line in dry_run_raw.splitlines():
        match = re.search(r"Would delete \S+:\s+([0-9A-F-]{36})\s+(.+)$", line)
        if not match:
            continue
        identifier = match.group(1)
        label = runtime_labels.get(identifier, match.group(2).strip())
        candidates.append(
            Candidate(
                path=Path(identifier),
                size_bytes=runtime_sizes.get(identifier, 0),
                label=f"{label} runtime",
                delete_command=["xcrun", "simctl", "runtime", "delete", identifier],
            )
        )
    return candidates


def collect_simulator_device_sizes() -> List[Candidate]:
    path = HOME / "Library" / "Developer" / "CoreSimulator" / "Devices"
    if not path.exists():
        return []
    size = path_size(path)
    if size <= 0:
        return []
    return [
        Candidate(
            path=path,
            size_bytes=size,
            label="全部 iOS Simulator 模拟器设备数据与设置",
            delete_command=["xcrun", "simctl", "erase", "all"],
        )
    ]


def collect_arduino_caches() -> List[Candidate]:
    staging_path = HOME / "Library" / "Arduino15" / "staging"
    if staging_path.exists():
        size = path_size(staging_path)
        if size > 0:
            return [Candidate(path=staging_path, size_bytes=size, label="Arduino IDE 下载缓存目录")]
    return []


def collect_xdg_caches() -> List[Candidate]:
    return collect_children(HOME / ".cache")


def xcode_project_display_name(directory_name: str) -> str:
    """Remove Xcode's trailing DerivedData hash from a project directory name."""
    return re.sub(r"-[a-z]{20,32}$", "", directory_name) or directory_name


def collect_xcode_derived_data_caches() -> List[Candidate]:
    """Collect rebuildable DerivedData caches without touching SourcePackages.

    Older versions of this script returned each project directory as one candidate.
    Deleting that directory also deleted Swift Package source checkouts.  Keeping the
    allow-list here narrow makes the scanner and the delete guard share one policy.
    """
    candidates: List[Candidate] = []
    if not XCODE_DERIVED_DATA_ROOT.exists():
        return candidates

    for entry in safe_scandir(XCODE_DERIVED_DATA_ROOT):
        path = Path(entry.path)

        global_label = XCODE_GLOBAL_CACHE_TARGETS.get(path.name)
        if global_label is not None:
            candidates.append(
                Candidate(
                    path=path,
                    size_bytes=path_size(path),
                    label=f"{global_label} — {path}",
                )
            )
            continue

        if not path.is_dir() or path.is_symlink():
            continue

        project_name = xcode_project_display_name(path.name)
        for target_name, target_label in XCODE_PROJECT_CACHE_TARGETS.items():
            target = path / target_name
            if not target.exists() and not target.is_symlink():
                continue
            candidates.append(
                Candidate(
                    path=target,
                    size_bytes=path_size(target),
                    label=f"{project_name} · {target_label} — {target}",
                )
            )

    return candidates


def collect_container_caches(root_dir: Path) -> List[Candidate]:
    candidates: List[Candidate] = []
    if not root_dir.exists():
        return candidates
    for entry in safe_scandir(root_dir):
        container_path = Path(entry.path)
        data_dir = container_path / "Data"
        if data_dir.exists():
            caches_dir = data_dir / "Library" / "Caches"
            if caches_dir.exists():
                size = path_size(caches_dir)
                if size > 0:
                    candidates.append(Candidate(
                        path=caches_dir, 
                        size_bytes=size, 
                        label=f"沙盒应用缓存 ({container_path.name})"
                    ))
            tmp_dir = data_dir / "tmp"
            if tmp_dir.exists():
                size = path_size(tmp_dir)
                if size > 0:
                    candidates.append(Candidate(
                        path=tmp_dir, 
                        size_bytes=size, 
                        label=f"沙盒临时文件 ({container_path.name})"
                    ))
    return candidates


def collect_group_container_caches() -> List[Candidate]:
    candidates: List[Candidate] = []
    gc_root = HOME / "Library" / "Group Containers"
    if not gc_root.exists():
        return candidates
    for entry in safe_scandir(gc_root):
        path = Path(entry.path)
        library_dir = path / "Library"
        if library_dir.exists():
            caches_dir = library_dir / "Caches"
            if caches_dir.exists():
                size = path_size(caches_dir)
                if size > 0:
                    candidates.append(Candidate(path=caches_dir, size_bytes=size, label=f"应用组缓存 ({path.name})"))
            cache_dir = library_dir / "Cache"
            if cache_dir.exists():
                size = path_size(cache_dir)
                if size > 0:
                    candidates.append(Candidate(path=cache_dir, size_bytes=size, label=f"应用组缓存 ({path.name})"))
        
        # Specifically target Telegram media cache
        if "Telegram" in path.name:
            for acct_dir in path.glob("stable/account-*/postbox/media"):
                if acct_dir.exists():
                    size = path_size(acct_dir)
                    if size > 0:
                        candidates.append(Candidate(path=acct_dir, size_bytes=size, label=f"Telegram 媒体缓存 ({path.name})"))
    return candidates


def collect_dictionary_sources() -> List[Candidate]:
    candidates: List[Candidate] = []
    containers_root = HOME / "Library" / "Containers"
    if not containers_root.exists():
        return candidates
    for entry in safe_scandir(containers_root):
        container_path = Path(entry.path)
        dicts_dir = container_path / "Data" / "Library" / "Application Support" / "dictionaries"
        if dicts_dir.exists():
            for imported_entry in safe_scandir(dicts_dir):
                imported_path = Path(imported_entry.path)
                source_dir = imported_path / "source"
                if source_dir.exists():
                    size = path_size(source_dir)
                    if size > 0:
                        candidates.append(Candidate(path=source_dir, size_bytes=size, label=f"词典源码备份 ({container_path.name})"))
    return candidates


def collect_app_support_caches() -> List[Candidate]:
    candidates: List[Candidate] = []
    app_support = HOME / "Library" / "Application Support"
    if not app_support.exists():
        return candidates
    
    cache_names = {
        "cache", "caches", "cacheddata", "cachedextensionvsixs", 
        "dawncache", "dawngraphitecache", "dawnwebgpucache", 
        "gpucache", "code cache", "gpupersistentcache", "crx_cache",
        "shadercache", "grshadercache", "graphitedawncache"
    }
    
    for app_entry in safe_scandir(app_support):
        app_path = Path(app_entry.path)
        if not app_path.is_dir():
            continue
        
        # Check level 1 subdirs (e.g. Application Support/AppName/Cache)
        for entry_l1 in safe_scandir(app_path):
            path_l1 = Path(entry_l1.path)
            if path_l1.is_dir():
                if path_l1.name.lower() in cache_names:
                    size = path_size(path_l1)
                    if size > 0:
                        candidates.append(Candidate(path=path_l1, size_bytes=size, label=f"应用支持缓存 ({app_path.name}/{path_l1.name})"))
                else:
                    # Check level 2 subdirs (e.g. Application Support/AppName/ProfileName/Cache)
                    for entry_l2 in safe_scandir(path_l1):
                        path_l2 = Path(entry_l2.path)
                        if path_l2.is_dir() and path_l2.name.lower() in cache_names:
                            size = path_size(path_l2)
                            if size > 0:
                                candidates.append(Candidate(path=path_l2, size_bytes=size, label=f"应用支持缓存 ({app_path.name}/{path_l1.name}/{path_l2.name})"))
    return candidates


def collect_homebrew_cleanup() -> List[Candidate]:
    candidates: List[Candidate] = []
    if shutil.which("brew"):
        raw = command_output(["brew", "cleanup", "-n"])
        size_bytes = 0
        lines = raw.splitlines()
        for line in lines:
            if "This operation would free approximately" in line:
                match = re.search(r"approximately\s+([\d.]+)\s*([A-Za-z]+)", line)
                if match:
                    val = float(match.group(1))
                    unit = match.group(2).upper()
                    if "G" in unit:
                        size_bytes = int(val * 1024 * 1024 * 1024)
                    elif "M" in unit:
                        size_bytes = int(val * 1024 * 1024)
                    elif "K" in unit:
                        size_bytes = int(val * 1024)
        
        if size_bytes > 0 or "Would remove" in raw:
            candidates.append(Candidate(
                path=Path("/opt/homebrew"),
                size_bytes=max(size_bytes, 1024 * 1024),
                label="Homebrew 缓存与旧版本软件",
                delete_command=["brew", "cleanup", "-s"]
            ))
            
        autoremove_raw = command_output(["brew", "autoremove", "-n"])
        if "Would autoremove" in autoremove_raw:
            formulas = []
            for line in autoremove_raw.splitlines():
                if line.startswith("  ") or (line.strip() and not line.startswith("Would") and not line.startswith("==")):
                    formulas.extend(line.strip().split())
            
            autoremove_size = 0
            cellar_root = Path("/opt/homebrew/Cellar")
            if not cellar_root.exists():
                cellar_root = Path("/usr/local/Cellar")
            
            for formula in formulas:
                formula_path = cellar_root / formula
                if formula_path.exists():
                    autoremove_size += path_size(formula_path)
            
            if autoremove_size > 0:
                candidates.append(Candidate(
                    path=Path("/opt/homebrew/autoremove"),
                    size_bytes=autoremove_size,
                    label="Homebrew 孤立未使用的依赖软件包",
                    delete_command=["brew", "autoremove"]
                ))
                
    return candidates


def delete_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.name in TCC_PROTECTED:
        raise RuntimeError("受 macOS 隐私保护，已跳过")
    if path.is_symlink() or path.is_file():
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            path.unlink(missing_ok=True)
        return
        
    def on_error(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    shutil.rmtree(path, onerror=on_error)
    if path.exists() or path.is_symlink():
        raise RuntimeError("删除后目标仍然存在，可能是权限不足或文件正在使用")


def validate_xcode_derived_data_target(path: Path) -> None:
    """Reject broad or dependency-bearing DerivedData deletion targets."""
    try:
        relative = path.absolute().relative_to(XCODE_DERIVED_DATA_ROOT.absolute())
    except ValueError as exc:
        raise RuntimeError("Xcode 清理目标不在 DerivedData 目录内，已拒绝") from exc

    parts = relative.parts
    if not parts or "SourcePackages" in parts:
        raise RuntimeError("SourcePackages/Swift Package 源码受保护，已拒绝删除")

    if len(parts) == 1 and parts[0] in XCODE_GLOBAL_CACHE_TARGETS:
        return
    if len(parts) == 2 and parts[1] in XCODE_PROJECT_CACHE_TARGETS:
        return
    raise RuntimeError("该路径不是允许清理的 Xcode 编译缓存，已拒绝删除")


def delete_candidate(candidate: Candidate, item: CleanupItem | None = None) -> None:
    if candidate.delete_command:
        result = subprocess.run(list(candidate.delete_command), text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"命令退出码 {result.returncode}: {' '.join(candidate.delete_command)}")
        return
    if item is not None and item.key == "xcode_derived_data":
        validate_xcode_derived_data_target(candidate.path)
    delete_path(candidate.path)


def top_candidates(candidates: Sequence[Candidate], limit: int = 8) -> List[Candidate]:
    return sorted(candidates, key=lambda item: item.size_bytes, reverse=True)[:limit]


def build_items() -> List[CleanupItem]:
    return [
        CleanupItem(
            key="user_caches",
            risk_level=1,
            title="🧹 用户应用缓存",
            description="清理 ~/Library/Caches 下的大部分 App 缓存，通常可自动重建。",
            caution="安全性高，但首次重新打开某些 App 可能会稍慢。",
            scanner=lambda: collect_children(HOME / "Library" / "Caches", TCC_PROTECTED),
        ),
        CleanupItem(
            key="xdg_caches",
            risk_level=1,
            title="⚙️ XDG 用户缓存",
            description="清理 ~/.cache 下的各类命令行工具与开发框架缓存（如 HuggingFace 模型缓存等）。",
            caution="安全性较高，但部分 CLI 工具或 AI 框架下次运行会重新下载模型或依赖环境。",
            scanner=collect_xdg_caches,
        ),
        CleanupItem(
            key="user_logs",
            risk_level=1,
            title="📝 用户日志",
            description="清理 ~/Library/Logs 下的日志 and 崩溃报告。",
            caution="删除后不影响 App 使用，但会失去部分排错线索。",
            scanner=lambda: collect_children(HOME / "Library" / "Logs"),
        ),
        CleanupItem(
            key="quark_caches",
            risk_level=2,
            title="🌐 夸克缓存及残留",
            description="清理夸克浏览器/网盘缓存（包括视频缓存、常规 Cache）、下载的更新包及 AI 组件包。",
            caution="建议先退出夸克；除常规缓存外，还将清理 updates 目录下的旧安装包和 QianwenInstaller 临时文件副本。",
            scanner=lambda: sum(
                [
                    collect_children(HOME / "Library" / "Application Support" / "Quark" / "Cache"),
                    collect_children(HOME / "Library" / "Application Support" / "Quark" / "Quark" / "Cache"),
                    collect_children(HOME / "Library" / "Application Support" / "Quark" / "updates"),
                    collect_children(HOME / "Library" / "Application Support" / "Quark" / "QianwenInstaller"),
                ],
                [],
            ),
        ),
        CleanupItem(
            key="trash",
            risk_level=1,
            title="🗑️ 废纸篓",
            description="清空 ~/.Trash 里的内容。",
            caution="会永久删除废纸篓中的文件。",
            scanner=lambda: collect_children(HOME / ".Trash"),
        ),
        CleanupItem(
            key="xcode_derived_data",
            risk_level=2,
            title="🛠️ Xcode DerivedData",
            description="按项目清理 Build、索引、日志等可重建内容，不删除 Swift Package 源码。",
            caution="下次编译和建立索引会变慢；SourcePackages（包括 GRDB checkout）始终保留。",
            scanner=collect_xcode_derived_data_caches,
        ),
        CleanupItem(
            key="xcode_archives",
            risk_level=2,
            title="📦 Xcode Archives",
            description="清理旧归档包，适合不再需要的历史构建产物。",
            caution="如果你需要回滚某个旧归档，请别删对应版本。",
            scanner=lambda: collect_children(HOME / "Library" / "Developer" / "Xcode" / "Archives"),
        ),
        CleanupItem(
            key="ios_simulator_caches",
            risk_level=2,
            title="📱 iOS Simulator 缓存",
            description="清理 CoreSimulator 的缓存目录。",
            caution="不会删掉模拟器设备本身，但部分缓存会重建。",
            scanner=lambda: collect_children(HOME / "Library" / "Developer" / "CoreSimulator" / "Caches"),
        ),
        CleanupItem(
            key="core_simulator_dyld_cache",
            risk_level=2,
            title="⚡ Simulator dyld 缓存",
            description="通过 simctl 清理所有 Simulator runtime 的 dyld shared cache。",
            caution="安全性较高，但下次启动/运行模拟器时会按需重建；可能需要 Xcode 命令行工具可用。",
            scanner=collect_core_simulator_dyld_cache,
        ),
        CleanupItem(
            key="unavailable_simulator_devices",
            risk_level=2,
            title="🚫 不可用模拟器设备",
            description="清理 simctl 标记为不可用的模拟器设备数据。",
            caution="只删除不可用设备；如果以后需要旧 runtime，可重新下载 runtime 后再创建设备。",
            scanner=collect_unavailable_simulator_devices,
        ),
        CleanupItem(
            key="package_manager_caches",
            risk_level=2,
            title="👩‍💻 开发工具缓存",
            description="清理 pip / npm / pnpm / CocoaPods / Yarn / Bun 等开发包管理缓存。",
            caution="会让下次安装依赖时重新下载一部分文件。注意：仅清理包下载缓存，不影响全局安装的 CLI 工具或认证配置。",
            scanner=lambda: sum(
                [
                    collect_children(HOME / ".cache" / "pip"),
                    collect_children(HOME / ".npm"),
                    collect_children(HOME / ".cargo" / "registry" / "cache"),
                    collect_children(HOME / ".cargo" / "registry" / "src"),
                    collect_children(HOME / ".cargo" / "git" / "db"),
                    collect_children(HOME / ".gradle" / "caches"),
                    collect_children(HOME / ".m2" / "repository"),
                    collect_children(HOME / "go" / "pkg" / "mod"),
                    collect_children(HOME / ".pnpm-store"),
                    collect_children(HOME / "Library" / "Caches" / "pnpm"),
                    collect_children(HOME / "Library" / "Caches" / "CocoaPods"),
                    collect_children(HOME / "Library" / "Caches" / "pip"),
                    collect_children(HOME / "Library" / "Caches" / "uv"),
                    collect_children(HOME / "Library" / "Caches" / "Yarn"),
                    collect_children(HOME / "Library" / "Caches" / "bun"),
                    collect_children(HOME / "Library" / "Caches" / "deno"),
                    collect_children(HOME / ".cache" / "uv"),
                ],
                [],
            ),
        ),
        CleanupItem(
            key="arduino_caches",
            risk_level=2,
            title="🤖 Arduino IDE 下载缓存",
            description="清理 ~/Library/Arduino15/staging/ 下的开发板与库下载缓存。",
            caution="安全性高，不影响已安装的开发板和库，下次升级板卡或安装库时会重新下载包。",
            scanner=collect_arduino_caches,
        ),
        CleanupItem(
            key="homebrew_cleanup",
            risk_level=2,
            title="🍺 Homebrew 官方深度清理",
            description="运行 brew cleanup 和 brew autoremove 清理旧版本包、下载缓存及孤立无用依赖。",
            caution="会清理不再需要的旧版本软件 and 未被使用的底层依赖包，安全性高，建议运行。",
            scanner=collect_homebrew_cleanup,
        ),
        CleanupItem(
            key="app_support_caches",
            risk_level=2,
            title="🔍 应用支持目录隐藏缓存",
            description="清理 ~/Library/Application Support/ 各应用子目录下的 Cache/GPUCache/Code Cache 等隐藏缓存文件夹。",
            caution="很多应用将缓存保存在此处而非标准 Caches 目录，清理此项安全性高且能释放较多空间。",
            scanner=collect_app_support_caches,
        ),
        CleanupItem(
            key="xcodebuildmcp_workspaces",
            risk_level=2,
            title="💻 XcodeBuildMCP 工作区缓存",
            description="清理 Codex / XcodeBuildMCP 的派生构建缓存。",
            caution="只影响后续重新构建速度。",
            scanner=lambda: collect_children(HOME / "Library" / "Developer" / "XcodeBuildMCP" / "workspaces"),
        ),
        CleanupItem(
            key="xctest_devices",
            risk_level=2,
            title="🧪 XCTest 临时设备",
            description="清理 Xcode/XCTest 生成的临时测试设备数据。",
            caution="会删除旧 UI 测试设备状态；需要时 Xcode 会重新创建。",
            scanner=lambda: [
                candidate
                for candidate in collect_children(HOME / "Library" / "Developer" / "XCTestDevices")
                if candidate.path.is_dir()
            ],
        ),
        CleanupItem(
            key="code_sign_clones",
            risk_level=3,
            title="🔑 临时代码签名副本",
            description="清理 Codex / Edge 等 Electron 软件在 /private/var/folders 下留下的 code_sign_clone 临时 App 副本。",
            caution="建议先关闭相应 App 再清理；如果 App 正在运行，跳过这一项更稳妥。",
            scanner=collect_code_sign_clones,
        ),
        CleanupItem(
            key="old_simulator_runtimes",
            risk_level=3,
            title="⏳ 30天未使用的 Simulator runtime",
            description="通过 simctl 删除 30 天未使用的可删除 Simulator runtime。",
            caution="会删除旧 iOS/watchOS runtime；如果需要测试旧系统兼容，请逐项选择而不是全选。",
            scanner=lambda: collect_old_simulator_runtimes(not_used_days=30),
        ),
        CleanupItem(
            key="ios_simulator_erase",
            risk_level=3,
            title="📱 iOS Simulator 设备数据重置",
            description="通过 simctl erase all 重置所有 iOS 模拟器设备的内容和设置（相当于恢复出厂设置）。",
            caution="【中危】会删除模拟器内安装的所有 App 和测试数据，但不会删除模拟器本身。下次测试时需要重新跑/编译 App。",
            scanner=collect_simulator_device_sizes,
        ),
        CleanupItem(
            key="downloads_installers",
            risk_level=3,
            title="📥 下载目录中的旧安装包",
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
            risk_level=3,
            title="📲 iPhone / iPad 本地备份",
            description="清理 MobileSync 里的旧设备备份。",
            caution="删除后无法用本地备份恢复旧设备数据，请谨慎。",
            scanner=lambda: collect_backup_directories(
                HOME / "Library" / "Application Support" / "MobileSync" / "Backup",
                older_than_days=30,
            ),
        ),
        CleanupItem(
            key="device_support",
            risk_level=3,
            title="📂 Xcode 旧设备 support 文件",
            description="清理 iOS DeviceSupport 目录中的旧版本支持文件。",
            caution="如果你还要连旧系统真机调试，请保留对应版本。",
            scanner=lambda: collect_children(HOME / "Library" / "Developer" / "Xcode" / "iOS DeviceSupport"),
        ),
        CleanupItem(
            key="dictionary_sources",
            risk_level=3,
            title="📖 翻译/词典软件离线源文件",
            description="清理翻译软件（如 Bob/Easydict）导入词典后残留在 source 目录下的原始 mdx/mdd 安装包副本。",
            caution="仅清理源安装文件副本，不影响已经成功导入并使用的词典数据库。",
            scanner=collect_dictionary_sources,
        ),
        CleanupItem(
            key="containers",
            risk_level=3,
            title="📦 应用沙盒容器缓存",
            description="清理 ~/Library/Containers 下各沙盒应用的 Caches 和 tmp 缓存文件夹。",
            caution="仅清理各沙盒应用的临时缓存与临时文件，不影响应用配置和数据库等核心数据，安全性较高。",
            scanner=lambda: collect_container_caches(HOME / "Library/Containers"),
        ),
        CleanupItem(
            key="group_containers",
            risk_level=3,
            title="👥 应用组共享容器缓存",
            description="清理 ~/Library/Group Containers 下各共享组 of Caches 缓存与 Telegram 媒体缓存。",
            caution="仅清理缓存文件（如 Telegram 离线图片/视频缓存），不删除账号配置与本地数据库，安全性较高。",
            scanner=collect_group_container_caches,
        ),
        CleanupItem(
            key="app_support",
            risk_level=3,
            title="⚠️ 应用支持文件 (警告)",
            description="清理 ~/Library/Application Support 下的应用持久化数据。包含 Chrome配置、飞书文件等。",
            caution="【高危】这会直接删除整个应用的配置和数据文件夹（如浏览器配置、聊天记录等），强烈建议在进入目录确认后手动操作！",
            scanner=lambda: collect_children(HOME / "Library" / "Application Support"),
        ),
    ]


def progress_bar(current: int, total: int, label: str = "", width: int = 30) -> str:
    """Return a single-line progress bar string (no newline)."""
    filled = int(width * current / total) if total > 0 else width
    bar = "█" * filled + "░" * (width - filled)
    pct = current * 100 // total if total > 0 else 100
    return f"\r  🔍 扫描中 [{bar}] {current}/{total} ({pct:3d}%)  {label}"


def summarize(items: Sequence[CleanupItem]) -> list[tuple[CleanupItem, List[Candidate], int]]:
    summary = []
    total = len(items)
    for i, item in enumerate(items):
        print(progress_bar(i, total, item.title), end="", flush=True)
        candidates = item.scanner()
        item_total = sum(candidate.size_bytes for candidate in candidates)
        summary.append((item, candidates, item_total))
    print(progress_bar(total, total, "✅ 完成"))
    print()  # final newline
    return summary


def print_overview(summary: Sequence[tuple[CleanupItem, List[Candidate], int]]) -> None:
    """Print only the cleanup categories, matching the requested first layer."""
    print("\n==================== [第一层] 可清理大项总览 ====================")
    print("-" * 76)
    total_all_bytes = 0
    total_all_items = 0
    for index, (item, candidates, total) in enumerate(summary, start=1):
        total_all_bytes += total
        total_all_items += len(candidates)
        print(
            f"{index:02d} {item.title:<30} "
            f"{human_size(total):>10} | {len(candidates):>4} 项"
        )
    print("-" * 76)
    print(f"📊 总计可清理: {human_size(total_all_bytes)} | 共 {total_all_items} 个子项")
    print("================================================================")


def print_item_detail(
    summary: Sequence[tuple[CleanupItem, List[Candidate], int]],
    item_index: int,
    *,
    show_commands: bool = True,
) -> None:
    """Print one category's introduction and every child as the second layer."""
    item, candidates, total = summary[item_index]
    print("\n==================== [第二层] 大项详情 ====================")
    print(f"📂 {item_index + 1:02d} {item.title}")
    print(f"说明: {item.description}")
    print(f"风险: {item.risk_level} / 3")
    print(f"注意: {item.caution}")
    print(f"合计: {human_size(total)} | {len(candidates)} 个子项")
    print("-" * 90)
    if not candidates:
        print("  （当前大项没有可清理的子项）")
    else:
        for sub_index, candidate in enumerate(
            sorted(candidates, key=lambda value: value.size_bytes, reverse=True),
            start=1,
        ):
            print(
                f"{sub_index:02d} {human_size(candidate.size_bytes):>10} | "
                f"{candidate_display(candidate)}"
            )
    print("==========================================================")
    if show_commands:
        print_detail_commands()


def print_all_item_details(
    summary: Sequence[tuple[CleanupItem, List[Candidate], int]],
) -> None:
    """Batch report used only by --scan-only --details."""
    for item_index in range(len(summary)):
        print_item_detail(summary, item_index, show_commands=False)


def print_overview_commands(item_count: int) -> None:
    print("\n可用指令:")
    print(f"  01-{item_count:02d}       输入大项序号，进入该大项的第二层详情")
    print("  c 1,3,6-8   清理一个或多个完整大项（执行前仍会确认）")
    print("  r           重新扫描")
    print("  q           退出脚本")


def print_detail_commands() -> None:
    print("\n可用指令:")
    print("  c 1,3,5-7   清理选中的子项")
    print("  c all       清理当前大项的全部子项")
    print("  b           返回第一层大项总览")
    print("  r           重新扫描并刷新当前大项")
    print("  q           退出脚本")


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
    """Parse comma/range input and reject any out-of-range or reversed value."""
    result: set[int] = set()
    normalized = raw.replace("[", "").replace("]", "")
    for chunk in normalized.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start_text, end_text = chunk.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if start > end:
                raise ValueError("范围起点不能大于终点")
            for number in range(start, end + 1):
                if not 1 <= number <= limit:
                    raise ValueError(f"序号必须在 1-{limit} 之间")
                result.add(number)
        else:
            number = int(chunk)
            if not 1 <= number <= limit:
                raise ValueError(f"序号必须在 1-{limit} 之间")
            result.add(number)
    return sorted(result)


def delete_command_argument(raw: str) -> str | None:
    """Return a delete command's argument while keeping plain numbers navigational."""
    for command in ("c", "clean", "d", "delete", "清理"):
        if raw == command:
            return ""
        prefix = f"{command} "
        if raw.startswith(prefix):
            return raw[len(prefix):].strip()
    return None


def candidate_identity(candidate: Candidate) -> tuple[str, ...]:
    """Return a stable identity so overlapping cleanup projects run only once."""
    if candidate.delete_command:
        return ("command", *candidate.delete_command)
    return ("path", str(candidate.path.absolute()))


def deduplicate_candidates(
    candidates: Sequence[tuple[CleanupItem, Candidate]],
) -> list[tuple[CleanupItem, Candidate]]:
    path_candidates = [
        candidate.path.absolute()
        for _, candidate in candidates
        if not candidate.delete_command
    ]
    unique: list[tuple[CleanupItem, Candidate]] = []
    seen: set[tuple[str, ...]] = set()
    for item, candidate in candidates:
        identity = candidate_identity(candidate)
        if identity in seen:
            continue
        if not candidate.delete_command:
            path = candidate.path.absolute()
            if any(parent != path and parent in path.parents for parent in path_candidates):
                continue
        seen.add(identity)
        unique.append((item, candidate))
    return unique


def execute_deletion(
    candidates: Sequence[tuple[CleanupItem, Candidate]],
    selected_lines: Sequence[str],
) -> list[Candidate]:
    """Confirm and execute one normalized deletion batch."""
    unique_candidates = deduplicate_candidates(candidates)
    if not unique_candidates:
        print("⚠️ 选择的内容目前没有可清理子项。")
        return []

    total_bytes = sum(candidate.size_bytes for _, candidate in unique_candidates)
    selected_items = {item.key: item for item, _ in unique_candidates}
    highest_risk = max(item.risk_level for item in selected_items.values())

    print("\n🚀 准备清理:")
    for line in selected_lines:
        print(f"  - {line}")
    print(f"预计可释放空间: {human_size(total_bytes)}")
    for item in selected_items.values():
        print(f"  注意 [{item.title}]: {item.caution}")

    if highest_risk >= 3:
        confirmation = input("⚠️ 包含高风险内容，请输入 DELETE 确认，其他输入取消: ").strip()
        if confirmation != "DELETE":
            print("已取消清理。")
            return []
    elif not ask_yes_no("❓ 确认清理？", default=False):
        print("已取消清理。")
        return []

    removed_count = 0
    removed_bytes = 0
    removed_candidates: list[Candidate] = []
    for item, candidate in unique_candidates:
        try:
            delete_candidate(candidate, item)
            removed_count += 1
            removed_bytes += candidate.size_bytes
            removed_candidates.append(candidate)
        except Exception as exc:
            print(f"  ❌ 清理失败: {candidate_display(candidate)} -> {exc}")

    print(f"\n✨ 清理完成：成功 {removed_count} 项，释放 {human_size(removed_bytes)}。")
    input("按回车键返回第一层总览...")
    return removed_candidates


def delete_overview_items(
    summary: Sequence[tuple[CleanupItem, List[Candidate], int]],
    selection_text: str,
) -> list[Candidate]:
    indexes = parse_selection(selection_text, len(summary))
    candidates: list[tuple[CleanupItem, Candidate]] = []
    selected_lines: list[str] = []
    for index in indexes:
        item, item_candidates, total = summary[index - 1]
        selected_lines.append(f"{index:02d} {item.title}（{human_size(total)}）")
        candidates.extend((item, candidate) for candidate in item_candidates)
    return execute_deletion(candidates, selected_lines)


def delete_detail_items(
    summary: Sequence[tuple[CleanupItem, List[Candidate], int]],
    item_index: int,
    selection_text: str,
) -> list[Candidate]:
    item, candidates, _ = summary[item_index]
    sorted_candidates = sorted(candidates, key=lambda value: value.size_bytes, reverse=True)
    if not sorted_candidates:
        print("⚠️ 当前大项没有可清理的子项。")
        return []

    indexes = (
        list(range(1, len(sorted_candidates) + 1))
        if selection_text == "all"
        else parse_selection(selection_text, len(sorted_candidates))
    )
    selected_candidates = [(item, sorted_candidates[index - 1]) for index in indexes]
    selected_lines = [
        f"{index:02d} {candidate_name(sorted_candidates[index - 1])}"
        f"（{human_size(sorted_candidates[index - 1].size_bytes)}）"
        for index in indexes
    ]
    return execute_deletion(selected_candidates, selected_lines)


def update_summary_after_deletion(
    summary: Sequence[tuple[CleanupItem, List[Candidate], int]],
    removed_candidates: Sequence[Candidate],
) -> list[tuple[CleanupItem, List[Candidate], int]]:
    """Update displayed totals from successful deletions without a full rescan.

    Exact targets and descendants disappear from the summary.  If another cleanup
    item contains one of the deleted paths, its cached size is reduced by the known
    deleted size.  The explicit ``r`` command remains available for a fresh scan.
    """
    removed_identities = {candidate_identity(candidate) for candidate in removed_candidates}
    removed_paths = [
        (candidate.path.absolute(), candidate.size_bytes)
        for candidate in removed_candidates
        if not candidate.delete_command
    ]
    updated_summary: list[tuple[CleanupItem, List[Candidate], int]] = []

    for item, candidates, _ in summary:
        updated_candidates: list[Candidate] = []
        for candidate in candidates:
            if candidate_identity(candidate) in removed_identities:
                continue
            if candidate.delete_command:
                updated_candidates.append(candidate)
                continue

            path = candidate.path.absolute()
            if any(removed_path in path.parents for removed_path, _ in removed_paths):
                continue

            nested_removed_size = sum(
                removed_size
                for removed_path, removed_size in removed_paths
                if path in removed_path.parents
            )
            if nested_removed_size:
                candidate = Candidate(
                    path=candidate.path,
                    size_bytes=max(0, candidate.size_bytes - nested_removed_size),
                    label=candidate.label,
                    delete_command=candidate.delete_command,
                )
            updated_candidates.append(candidate)

        updated_total = sum(candidate.size_bytes for candidate in updated_candidates)
        updated_summary.append((item, updated_candidates, updated_total))

    return updated_summary


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
        help="扫描后显示每个项目的全部子项",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ensure_macos()

    print("✨ macOS 手动清理助手 ✨")
    print("💡 流程: 扫描 → 第一层大项总览 → 第二层子项详情 → 确认清理。")

    items = build_items()
    summary = summarize(items)

    if args.scan_only:
        print_overview(summary)
        if args.details:
            print_all_item_details(summary)
        print("\n当前是 --scan-only 模式，未执行任何删除。")
        return

    # None means first layer; an index means that category's second layer.
    selected_item_index: int | None = None

    while True:
        if selected_item_index is None:
            print_overview(summary)
            print_overview_commands(len(summary))
            choice = input("\n第一层请输入指令: ").strip().lower()

            if choice in {"q", "quit", "exit"}:
                print("已退出。")
                return
            if choice in {"r", "rescan", "刷新", "重新扫描"}:
                print("\n🔄 正在重新扫描...")
                summary = summarize(items)
                continue

            delete_argument = delete_command_argument(choice)
            if delete_argument is not None:
                if not delete_argument:
                    print("⚠️ 请在 c 后写大项序号，例如：c 1,3,6-8")
                    continue
                try:
                    removed_candidates = delete_overview_items(summary, delete_argument)
                except ValueError as exc:
                    print(f"⚠️ 清理序号无效：{exc}")
                    continue
                if removed_candidates:
                    summary = update_summary_after_deletion(summary, removed_candidates)
                continue

            try:
                item_number = int(choice)
            except ValueError:
                print("⚠️ 请输入大项序号，或使用 c / r / q 指令。")
                continue
            if not 1 <= item_number <= len(summary):
                print(f"⚠️ 大项序号必须在 1-{len(summary)} 之间。")
                continue
            selected_item_index = item_number - 1
            continue

        print_item_detail(summary, selected_item_index)
        choice = input("\n第二层请输入指令: ").strip().lower()

        if choice in {"q", "quit", "exit"}:
            print("已退出。")
            return
        if choice in {"b", "back", "返回"}:
            selected_item_index = None
            continue
        if choice in {"r", "rescan", "刷新", "重新扫描"}:
            print("\n🔄 正在重新扫描...")
            summary = summarize(items)
            continue

        delete_argument = delete_command_argument(choice)
        if delete_argument is None:
            print("⚠️ 第二层请使用 c 清理、b 返回、r 刷新或 q 退出。")
            continue
        if not delete_argument:
            print("⚠️ 请在 c 后写子项序号，例如：c 1,3,5-7；或输入 c all。")
            continue
        try:
            removed_candidates = delete_detail_items(summary, selected_item_index, delete_argument)
        except ValueError as exc:
            print(f"⚠️ 清理序号无效：{exc}")
            continue
        if removed_candidates:
            summary = update_summary_after_deletion(summary, removed_candidates)
            selected_item_index = None


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n用户取消。")
        sys.exit(130)
