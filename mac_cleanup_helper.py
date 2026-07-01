#!/usr/bin/env python3
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
        return
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


def delete_candidate(candidate: Candidate) -> None:
    if candidate.delete_command:
        result = subprocess.run(list(candidate.delete_command), text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"命令退出码 {result.returncode}: {' '.join(candidate.delete_command)}")
        return
    delete_path(candidate.path)


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
            title="夸克缓存及残留",
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
            key="core_simulator_dyld_cache",
            level=2,
            title="Simulator dyld 缓存",
            description="通过 simctl 清理所有 Simulator runtime 的 dyld shared cache。",
            caution="安全性较高，但下次启动/运行模拟器时会按需重建；可能需要 Xcode 命令行工具可用。",
            scanner=collect_core_simulator_dyld_cache,
        ),
        CleanupItem(
            key="unavailable_simulator_devices",
            level=2,
            title="不可用模拟器设备",
            description="清理 simctl 标记为不可用的模拟器设备数据。",
            caution="只删除不可用设备；如果以后需要旧 runtime，可重新下载 runtime 后再创建设备。",
            scanner=collect_unavailable_simulator_devices,
        ),
        CleanupItem(
            key="package_manager_caches",
            level=2,
            title="开发工具缓存",
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
                ],
                [],
            ),
        ),
        CleanupItem(
            key="homebrew_cleanup",
            level=2,
            title="Homebrew 官方深度清理",
            description="运行 brew cleanup 和 brew autoremove 清理旧版本包、下载缓存及孤立无用依赖。",
            caution="会清理不再需要的旧版本软件 and 未被使用的底层依赖包，安全性高，建议运行。",
            scanner=collect_homebrew_cleanup,
        ),
        CleanupItem(
            key="app_support_caches",
            level=2,
            title="应用支持目录隐藏缓存",
            description="清理 ~/Library/Application Support/ 各应用子目录下的 Cache/GPUCache/Code Cache 等隐藏缓存文件夹。",
            caution="很多应用将缓存保存在此处而非标准 Caches 目录，清理此项安全性高且能释放较多空间。",
            scanner=collect_app_support_caches,
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
            key="xctest_devices",
            level=2,
            title="XCTest 临时设备",
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
            level=3,
            title="临时代码签名副本",
            description="清理 Codex / Edge 等 Electron 软件在 /private/var/folders 下留下的 code_sign_clone 临时 App 副本。",
            caution="建议先关闭相应 App 再清理；如果 App 正在运行，跳过这一项更稳妥。",
            scanner=collect_code_sign_clones,
        ),
        CleanupItem(
            key="old_simulator_runtimes",
            level=3,
            title="30天未使用的 Simulator runtime",
            description="通过 simctl 删除 30 天未使用的可删除 Simulator runtime。",
            caution="会删除旧 iOS/watchOS runtime；如果需要测试旧系统兼容，请逐项选择而不是全选。",
            scanner=lambda: collect_old_simulator_runtimes(not_used_days=30),
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
            title="Xcode 旧设备 support 文件",
            description="清理 iOS DeviceSupport 目录中的旧版本支持文件。",
            caution="如果你还要连旧系统真机调试，请保留对应版本。",
            scanner=lambda: collect_children(HOME / "Library" / "Developer" / "Xcode" / "iOS DeviceSupport"),
        ),
        CleanupItem(
            key="dictionary_sources",
            level=3,
            title="翻译/词典软件离线源文件",
            description="清理翻译软件（如 Bob/Easydict）导入词典后残留在 source 目录下的原始 mdx/mdd 安装包副本。",
            caution="仅清理源安装文件副本，不影响已经成功导入并使用的词典数据库。",
            scanner=collect_dictionary_sources,
        ),
        CleanupItem(
            key="containers",
            level=3,
            title="应用沙盒容器缓存",
            description="清理 ~/Library/Containers 下各沙盒应用的 Caches 和 tmp 缓存文件夹。",
            caution="仅清理各沙盒应用的临时缓存与临时文件，不影响应用配置和数据库等核心数据，安全性较高。",
            scanner=lambda: collect_container_caches(HOME / "Library" / "Containers"),
        ),
        CleanupItem(
            key="group_containers",
            level=3,
            title="应用组共享容器缓存",
            description="清理 ~/Library/Group Containers 下各共享组 of Caches 缓存与 Telegram 媒体缓存。",
            caution="仅清理缓存文件（如 Telegram 离线图片/视频缓存），不删除账号配置与本地数据库，安全性较高。",
            scanner=collect_group_container_caches,
        ),
        CleanupItem(
            key="app_support",
            level=3,
            title="应用支持文件 (警告)",
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
        print()
    print("-" * 90)


def show_item_details(summary: Sequence[tuple[CleanupItem, List[Candidate], int]]) -> None:
    for index, (item, candidates, total) in enumerate(summary, start=1):
        if not candidates:
            continue
        print(f"\n[{index:02d}] {item.title} - {human_size(total)}")
        for candidate in top_candidates(candidates):
            print(f"  - {human_size(candidate.size_bytes):>9}  {candidate_display(candidate)}")


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
    normalized = raw.replace("[", "").replace("]", "")
    for chunk in normalized.split(","):
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


def print_candidate_choices(candidates: Sequence[Candidate], limit: int | None = None) -> None:
    visible_candidates = list(candidates if limit is None else candidates[:limit])
    for i, candidate in enumerate(visible_candidates, start=1):
        print(f"    [{i:02d}]  {human_size(candidate.size_bytes):>9}  {candidate_name(candidate)}")
    if limit is not None and len(candidates) > limit:
        print(f"    ... 还有 {len(candidates) - limit} 项，输入 l 查看全部")


def delete_specific_candidates(candidates: Sequence[Candidate], indexes: Sequence[int]) -> tuple[int, int]:
    selected = [candidates[i - 1] for i in indexes]
    total_selected = sum(c.size_bytes for c in selected)
    print(f"  已选择 {len(selected)} 项，预计释放 {human_size(total_selected)}")

    if not ask_yes_no("  确认删除？", default=False):
        return 0, 0

    removed_count = 0
    removed_bytes = 0
    for candidate in selected:
        try:
            size = candidate.size_bytes
            delete_candidate(candidate)
            removed_count += 1
            removed_bytes += size
        except Exception as exc:
            print(f"  ! 删除失败: {candidate_display(candidate)} -> {exc}")

    return removed_count, removed_bytes


def selective_delete(candidates: Sequence[Candidate]) -> tuple[int, int]:
    """Let user pick specific items to delete from a candidate list."""
    sorted_candidates = sorted(candidates, key=lambda c: c.size_bytes, reverse=True)

    print("\n  全部候选项：")
    print_candidate_choices(sorted_candidates)

    while True:
        raw = input("  输入编号（如 [01]、1,3、5-7），q=返回: ").strip().lower()
        if raw in ("q", ""):
            return 0, 0
        try:
            indexes = parse_selection(raw, len(sorted_candidates))
            if indexes:
                break
            print("  未匹配任何编号，请重试。")
        except ValueError:
            print("  格式不正确，请重试。")

    return delete_specific_candidates(sorted_candidates, indexes)


def delete_candidates(item: CleanupItem, candidates: Sequence[Candidate]) -> tuple[int, int]:
    removed_count = 0
    removed_bytes = 0
    for candidate in candidates:
        try:
            size = candidate.size_bytes
            delete_candidate(candidate)
            removed_count += 1
            removed_bytes += size
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 删除失败: {candidate_display(candidate)} -> {exc}")
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
        sorted_candidates = sorted(candidates, key=lambda c: c.size_bytes, reverse=True)
        print_candidate_choices(sorted_candidates, limit=10)

        print("\n请选择操作：直接回车=全部清理；输入编号=清理指定项；l=列出全部；n/q=跳过")
        skip_item = False
        while True:
            choice = input("  > ").strip().lower()
            if choice in ("n", "q"):
                print("  已跳过。")
                skip_item = True
                break
            if choice in ("", "a", "all", "y"):
                removed_count, removed_bytes = delete_candidates(item, sorted_candidates)
                break
            if choice == "l":
                print_candidate_choices(sorted_candidates)
                continue
            try:
                indexes = parse_selection(choice, len(sorted_candidates))
            except ValueError:
                indexes = []
            if indexes:
                removed_count, removed_bytes = delete_specific_candidates(sorted_candidates, indexes)
                break
            print("  请输编号、l、n，或直接回车。")
        if skip_item:
            continue
        total_removed_count += removed_count
        total_removed_bytes += removed_bytes
        print(f"已清理 {removed_count} 项，估算释放 {human_size(removed_bytes)}。")

    print("\n清理完成")
    print(f"总计删除: {total_removed_count} 项")
    print(f"估算释放: {human_size(total_removed_bytes)}")


def choose_items(summary: Sequence[tuple[CleanupItem, List[Candidate], int]]) -> Optional[List[int]]:
    print(
        "\n选择清理方式:\n"
        "  1. 选择 L1 项\n"
        "  2. 选择 L2 项\n"
        "  3. 选择 L3 项\n"
        "  输入总览序号可选择特定项，例如 [01]、01、01,05、01-03\n"
        "  q. 退出"
    )
    while True:
        choice = input("请输入选项编号或总览序号: ").strip().lower()
        if choice == "1":
            return [i for i, (item, _, total) in enumerate(summary, start=1) if item.level == 1 and total > 0]
        if choice == "2":
            return [i for i, (item, _, total) in enumerate(summary, start=1) if item.level == 2 and total > 0]
        if choice == "3":
            return [i for i, (item, _, total) in enumerate(summary, start=1) if item.level == 3 and total > 0]
        if choice in {"q", "quit", "exit"}:
            return None
        if choice:
            try:
                indexes = parse_selection(choice, len(summary))
            except ValueError:
                print("编号格式不正确，请重试。")
                continue
            indexes = [i for i in indexes if summary[i - 1][2] > 0]
            if indexes:
                return indexes
            print("未匹配任何有内容的项目，请重试。")
            continue
        print("请输入 1 / 2 / 3、总览序号，或 q。")


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

    while True:
        selected_indexes = choose_items(summary)
        if selected_indexes is None:
            print("已退出。")
            return
        if not selected_indexes:
            print("当前选择没有可清理内容，请重新选择。")
            continue

        clean_selected(summary, selected_indexes)
        print("\n重新扫描，更新可清理项目总览...")
        summary = summarize(items)
        print_summary(summary)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n用户取消。")
        sys.exit(130)
