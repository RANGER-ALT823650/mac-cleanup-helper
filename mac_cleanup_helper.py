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
            title="🧹 用户应用缓存",
            description="清理 ~/Library/Caches 下的大部分 App 缓存，通常可自动重建。",
            caution="安全性高，但首次重新打开某些 App 可能会稍慢。",
            scanner=lambda: collect_children(HOME / "Library" / "Caches", TCC_PROTECTED),
        ),
        CleanupItem(
            key="xdg_caches",
            level=1,
            title="⚙️ XDG 用户缓存",
            description="清理 ~/.cache 下的各类命令行工具与开发框架缓存（如 HuggingFace 模型缓存等）。",
            caution="安全性较高，但部分 CLI 工具或 AI 框架下次运行会重新下载模型或依赖环境。",
            scanner=collect_xdg_caches,
        ),
        CleanupItem(
            key="user_logs",
            level=1,
            title="📝 用户日志",
            description="清理 ~/Library/Logs 下的日志 and 崩溃报告。",
            caution="删除后不影响 App 使用，但会失去部分排错线索。",
            scanner=lambda: collect_children(HOME / "Library" / "Logs"),
        ),
        CleanupItem(
            key="quark_caches",
            level=2,
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
            level=1,
            title="🗑️ 废纸篓",
            description="清空 ~/.Trash 里的内容。",
            caution="会永久删除废纸篓中的文件。",
            scanner=lambda: collect_children(HOME / ".Trash"),
        ),
        CleanupItem(
            key="xcode_derived_data",
            level=2,
            title="🛠️ Xcode DerivedData",
            description="清理 Xcode 编译缓存，常见且很占空间。",
            caution="安全性较高，但下次编译会变慢。",
            scanner=lambda: collect_children(HOME / "Library" / "Developer" / "Xcode" / "DerivedData"),
        ),
        CleanupItem(
            key="xcode_archives",
            level=2,
            title="📦 Xcode Archives",
            description="清理旧归档包，适合不再需要的历史构建产物。",
            caution="如果你需要回滚某个旧归档，请别删对应版本。",
            scanner=lambda: collect_children(HOME / "Library" / "Developer" / "Xcode" / "Archives"),
        ),
        CleanupItem(
            key="ios_simulator_caches",
            level=2,
            title="📱 iOS Simulator 缓存",
            description="清理 CoreSimulator 的缓存目录。",
            caution="不会删掉模拟器设备本身，但部分缓存会重建。",
            scanner=lambda: collect_children(HOME / "Library" / "Developer" / "CoreSimulator" / "Caches"),
        ),
        CleanupItem(
            key="core_simulator_dyld_cache",
            level=2,
            title="⚡ Simulator dyld 缓存",
            description="通过 simctl 清理所有 Simulator runtime 的 dyld shared cache。",
            caution="安全性较高，但下次启动/运行模拟器时会按需重建；可能需要 Xcode 命令行工具可用。",
            scanner=collect_core_simulator_dyld_cache,
        ),
        CleanupItem(
            key="unavailable_simulator_devices",
            level=2,
            title="🚫 不可用模拟器设备",
            description="清理 simctl 标记为不可用的模拟器设备数据。",
            caution="只删除不可用设备；如果以后需要旧 runtime，可重新下载 runtime 后再创建设备。",
            scanner=collect_unavailable_simulator_devices,
        ),
        CleanupItem(
            key="package_manager_caches",
            level=2,
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
            level=2,
            title="🤖 Arduino IDE 下载缓存",
            description="清理 ~/Library/Arduino15/staging/ 下的开发板与库下载缓存。",
            caution="安全性高，不影响已安装的开发板和库，下次升级板卡或安装库时会重新下载包。",
            scanner=collect_arduino_caches,
        ),
        CleanupItem(
            key="homebrew_cleanup",
            level=2,
            title="🍺 Homebrew 官方深度清理",
            description="运行 brew cleanup 和 brew autoremove 清理旧版本包、下载缓存及孤立无用依赖。",
            caution="会清理不再需要的旧版本软件 and 未被使用的底层依赖包，安全性高，建议运行。",
            scanner=collect_homebrew_cleanup,
        ),
        CleanupItem(
            key="app_support_caches",
            level=2,
            title="🔍 应用支持目录隐藏缓存",
            description="清理 ~/Library/Application Support/ 各应用子目录下的 Cache/GPUCache/Code Cache 等隐藏缓存文件夹。",
            caution="很多应用将缓存保存在此处而非标准 Caches 目录，清理此项安全性高且能释放较多空间。",
            scanner=collect_app_support_caches,
        ),
        CleanupItem(
            key="xcodebuildmcp_workspaces",
            level=2,
            title="💻 XcodeBuildMCP 工作区缓存",
            description="清理 Codex / XcodeBuildMCP 的派生构建缓存。",
            caution="只影响后续重新构建速度。",
            scanner=lambda: collect_children(HOME / "Library" / "Developer" / "XcodeBuildMCP" / "workspaces"),
        ),
        CleanupItem(
            key="xctest_devices",
            level=2,
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
            level=3,
            title="🔑 临时代码签名副本",
            description="清理 Codex / Edge 等 Electron 软件在 /private/var/folders 下留下的 code_sign_clone 临时 App 副本。",
            caution="建议先关闭相应 App 再清理；如果 App 正在运行，跳过这一项更稳妥。",
            scanner=collect_code_sign_clones,
        ),
        CleanupItem(
            key="old_simulator_runtimes",
            level=3,
            title="⏳ 30天未使用的 Simulator runtime",
            description="通过 simctl 删除 30 天未使用的可删除 Simulator runtime。",
            caution="会删除旧 iOS/watchOS runtime；如果需要测试旧系统兼容，请逐项选择而不是全选。",
            scanner=lambda: collect_old_simulator_runtimes(not_used_days=30),
        ),
        CleanupItem(
            key="ios_simulator_erase",
            level=3,
            title="📱 iOS Simulator 设备数据重置",
            description="通过 simctl erase all 重置所有 iOS 模拟器设备的内容和设置（相当于恢复出厂设置）。",
            caution="【中危】会删除模拟器内安装的所有 App 和测试数据，但不会删除模拟器本身。下次测试时需要重新跑/编译 App。",
            scanner=collect_simulator_device_sizes,
        ),
        CleanupItem(
            key="downloads_installers",
            level=3,
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
            level=3,
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
            level=3,
            title="📂 Xcode 旧设备 support 文件",
            description="清理 iOS DeviceSupport 目录中的旧版本支持文件。",
            caution="如果你还要连旧系统真机调试，请保留对应版本。",
            scanner=lambda: collect_children(HOME / "Library" / "Developer" / "Xcode" / "iOS DeviceSupport"),
        ),
        CleanupItem(
            key="dictionary_sources",
            level=3,
            title="📖 翻译/词典软件离线源文件",
            description="清理翻译软件（如 Bob/Easydict）导入词典后残留在 source 目录下的原始 mdx/mdd 安装包副本。",
            caution="仅清理源安装文件副本，不影响已经成功导入并使用的词典数据库。",
            scanner=collect_dictionary_sources,
        ),
        CleanupItem(
            key="containers",
            level=3,
            title="📦 应用沙盒容器缓存",
            description="清理 ~/Library/Containers 下各沙盒应用的 Caches 和 tmp 缓存文件夹。",
            caution="仅清理各沙盒应用的临时缓存与临时文件，不影响应用配置和数据库等核心数据，安全性较高。",
            scanner=lambda: collect_container_caches(HOME / "Library/Containers"),
        ),
        CleanupItem(
            key="group_containers",
            level=3,
            title="👥 应用组共享容器缓存",
            description="清理 ~/Library/Group Containers 下各共享组 of Caches 缓存与 Telegram 媒体缓存。",
            caution="仅清理缓存文件（如 Telegram 离线图片/视频缓存），不删除账号配置与本地数据库，安全性较高。",
            scanner=collect_group_container_caches,
        ),
        CleanupItem(
            key="app_support",
            level=3,
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


def print_level_1_summary(summary: Sequence[tuple[CleanupItem, List[Candidate], int]]) -> None:
    print("\n==================== [层级 1] 可清理项目总览 ====================")
    print("-" * 90)
    total_all_bytes = 0
    total_all_items = 0
    for index, (item, candidates, total) in enumerate(summary, start=1):
        count = len(candidates)
        total_all_bytes += total
        total_all_items += count
        print(
            f"{index:02d} 风险{item.level} | {item.title:<28} | "
            f"{human_size(total):>9} | {count:>4} 项 | {item.key}"
        )
        print(f"     {item.description}")
        print(f"     注意: {item.caution}")
        print()
    print("-" * 90)
    print(f"📊 总计可清理: {human_size(total_all_bytes)} | 共 {total_all_items} 个子项")
    print("================================================================")


def print_level_2_summary(summary: Sequence[tuple[CleanupItem, List[Candidate], int]]) -> None:
    print("\n==================== [层级 2] 总览每个项目的全部子项 ====================")
    has_content = False
    for index, (item, candidates, total) in enumerate(summary, start=1):
        if not candidates:
            continue
        has_content = True
        print(f"\n{index:02d} {item.title} - {human_size(total)} (共 {len(candidates)} 项)")
        sorted_candidates = sorted(candidates, key=lambda c: c.size_bytes, reverse=True)
        for sub_idx, candidate in enumerate(sorted_candidates, start=1):
            print(f"  - {index:02d}.{sub_idx:02d} {human_size(candidate.size_bytes):>9}  {candidate_display(candidate)}")
    if not has_content:
        print("\n✨ 所有项目均已清理干净，无任何子项！")
    print("\n=======================================================================")


def print_level_3_summary(summary: Sequence[tuple[CleanupItem, List[Candidate], int]], parent_idx: int) -> None:
    print("\n==================== [层级 3] 单独父项下所有的子项 ====================")
    item, candidates, total = summary[parent_idx]
    print(f"📂 父项: {parent_idx + 1:02d} {item.title}")
    print(f"   说明: {item.description}")
    print(f"   注意: {item.caution}")
    print(f"   总计: {human_size(total)} (共 {len(candidates)} 个子项)")
    print("-" * 90)
    if not candidates:
        print("   (当前项目下无任何可清理的子项)")
    else:
        sorted_candidates = sorted(candidates, key=lambda c: c.size_bytes, reverse=True)
        for sub_idx, candidate in enumerate(sorted_candidates, start=1):
            print(f"  - {sub_idx:02d} {human_size(candidate.size_bytes):>9}  {candidate_display(candidate)}")
    print("=======================================================================")


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


def parse_mixed_selection(
    raw: str, limit_categories: int, summary: Sequence[tuple[CleanupItem, List[Candidate], int]]
) -> dict[int, list[int] | None]:
    """
    Parses selection string.
    Returns a dict mapping 1-based category index to either:
      - None (meaning delete the whole category)
      - list[int] (1-based indices of sorted candidates to delete)
    Raises ValueError on invalid formats.
    """
    selection: dict[int, list[int] | None] = {}
    normalized = raw.replace("[", "").replace("]", "")
    for chunk in normalized.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "." in chunk:
            # Sub-item selection or range, e.g., "17.1", "17.1-3", "17.1-17.3"
            if "-" in chunk:
                start_part, end_part = chunk.split("-", 1)
                start_part = start_part.strip()
                end_part = end_part.strip()

                if "." not in start_part:
                    raise ValueError("Invalid format")

                start_cat_str, start_sub_str = start_part.split(".", 1)
                start_cat = int(start_cat_str)
                start_sub = int(start_sub_str)

                if "." in end_part:
                    end_cat_str, end_sub_str = end_part.split(".", 1)
                    end_cat = int(end_cat_str)
                    end_sub = int(end_sub_str)
                else:
                    end_cat = start_cat
                    end_sub = int(end_part)

                if start_cat != end_cat:
                    raise ValueError("Cross-category ranges not supported")
                if not (1 <= start_cat <= limit_categories):
                    continue

                _, candidates, _ = summary[start_cat - 1]
                sorted_candidates = sorted(candidates, key=lambda c: c.size_bytes, reverse=True)
                limit_sub = len(sorted_candidates)

                if selection.get(start_cat) is not None:
                    current_subs = selection[start_cat]
                    for sub in range(start_sub, end_sub + 1):
                        if 1 <= sub <= limit_sub:
                            current_subs.append(sub)
                elif start_cat not in selection:
                    current_subs = []
                    for sub in range(start_sub, end_sub + 1):
                        if 1 <= sub <= limit_sub:
                            current_subs.append(sub)
                    selection[start_cat] = current_subs
            else:
                cat_str, sub_str = chunk.split(".", 1)
                cat_idx = int(cat_str)
                sub_idx = int(sub_str)
                if not (1 <= cat_idx <= limit_categories):
                    continue

                _, candidates, _ = summary[cat_idx - 1]
                sorted_candidates = sorted(candidates, key=lambda c: c.size_bytes, reverse=True)
                limit_sub = len(sorted_candidates)

                if 1 <= sub_idx <= limit_sub:
                    if selection.get(cat_idx) is not None:
                        selection[cat_idx].append(sub_idx)
                    elif cat_idx not in selection:
                        selection[cat_idx] = [sub_idx]
        else:
            # Whole category or category range, e.g., "17", "17-19"
            if "-" in chunk:
                start_text, end_text = chunk.split("-", 1)
                start = int(start_text)
                end = int(end_text)
                for cat_idx in range(start, end + 1):
                    if 1 <= cat_idx <= limit_categories:
                        selection[cat_idx] = None
            else:
                cat_idx = int(chunk)
                if 1 <= cat_idx <= limit_categories:
                    selection[cat_idx] = None

    for cat_idx, subs in selection.items():
        if subs is not None:
            selection[cat_idx] = sorted(list(set(subs)))

    return selection


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


def perform_deletion(
    summary: Sequence[tuple[CleanupItem, List[Candidate], int]],
    current_level: int,
    selected_parent_idx: int
) -> set[Path]:
    deleted_paths: set[Path] = set()

    if current_level == 1:
        print("\n--- 执行层级 1 (项目总览) 删除操作 ---")
        choice = input("请输入要清理的父项序号 (例如: 1 或 1,3 或 2-5，输入 'all' 清理全部，直接回车取消): ").strip().lower()
        if not choice:
            print("已取消删除操作。")
            return deleted_paths

        if choice == "all":
            indexes = list(range(1, len(summary) + 1))
        else:
            try:
                indexes = parse_selection(choice, len(summary))
            except ValueError:
                print("⚠️ 序号格式不正确。")
                return deleted_paths

        if not indexes:
            print("⚠️ 未选择任何项目。")
            return deleted_paths

        selected_items_info = []
        total_bytes_to_delete = 0
        candidates_to_delete = []

        for idx in indexes:
            item, candidates, total = summary[idx - 1]
            if candidates:
                selected_items_info.append(f"  - {idx:02d} {item.title} ({human_size(total)})")
                total_bytes_to_delete += total
                candidates_to_delete.extend((item, c) for c in candidates)

        if not candidates_to_delete:
            print("⚠️ 选择的项目中没有可清理的内容。")
            return deleted_paths

        candidates_to_delete = deduplicate_candidates(candidates_to_delete)
        total_bytes_to_delete = sum(candidate.size_bytes for _, candidate in candidates_to_delete)

        print(f"\n🚀 准备清理以下项目:")
        for info in selected_items_info:
            print(info)
        print(f"预计可释放空间: {human_size(total_bytes_to_delete)}")

        if not ask_yes_no("❓ 确认删除？", default=False):
            print("已取消删除操作。")
            return deleted_paths

        removed_count = 0
        removed_bytes = 0
        for item, candidate in candidates_to_delete:
            try:
                size = candidate.size_bytes
                delete_candidate(candidate)
                removed_count += 1
                removed_bytes += size
                deleted_paths.add(candidate.path)
            except Exception as exc:
                print(f"  ❌ 删除失败: {candidate_display(candidate)} -> {exc}")

        print(f"\n✨ 清理完成！已成功删除 {removed_count} 项，释放空间 {human_size(removed_bytes)}。")
        input("\n按回车键继续...")
        return deleted_paths

    elif current_level == 2:
        print("\n--- 执行层级 2 (全部子项总览) 删除操作 ---")
        choice = input("请输入要删除的子项目序号 (例如: 1.1 或 1.1-3, 2.3，或者输入整数如 1 清空第1项的全部子项，直接回车取消): ").strip().lower()
        if not choice:
            print("已取消删除操作。")
            return deleted_paths

        try:
            selection = parse_mixed_selection(choice, len(summary), summary)
        except ValueError:
            print("⚠️ 序号格式不正确。")
            return deleted_paths

        candidates_to_delete = []
        total_bytes_to_delete = 0
        selected_details = []

        for cat_idx, subs in selection.items():
            item, candidates, total = summary[cat_idx - 1]
            if not candidates:
                continue
            sorted_candidates = sorted(candidates, key=lambda c: c.size_bytes, reverse=True)
            if subs is None:
                selected_details.append(f"  - {cat_idx:02d} 整项: {item.title} ({human_size(total)})")
                total_bytes_to_delete += total
                candidates_to_delete.extend((item, c) for c in candidates)
            else:
                cat_subs_bytes = 0
                sub_details = []
                for sub_idx in subs:
                    if 1 <= sub_idx <= len(sorted_candidates):
                        c = sorted_candidates[sub_idx - 1]
                        candidates_to_delete.append((item, c))
                        cat_subs_bytes += c.size_bytes
                        sub_details.append(f"    * {cat_idx:02d}.{sub_idx:02d} {candidate_name(c)} ({human_size(c.size_bytes)})")
                if sub_details:
                    selected_details.append(f"  - {cat_idx:02d} 部分子项 ({human_size(cat_subs_bytes)}):")
                    selected_details.extend(sub_details)
                    total_bytes_to_delete += cat_subs_bytes

        if not candidates_to_delete:
            print("⚠️ 未匹配任何可清理的内容。")
            return deleted_paths

        candidates_to_delete = deduplicate_candidates(candidates_to_delete)
        total_bytes_to_delete = sum(candidate.size_bytes for _, candidate in candidates_to_delete)

        print("\n🚀 准备清理以下内容:")
        for detail in selected_details:
            print(detail)
        print(f"预计可释放空间: {human_size(total_bytes_to_delete)}")

        if not ask_yes_no("❓ 确认删除？", default=False):
            print("已取消删除操作。")
            return deleted_paths

        removed_count = 0
        removed_bytes = 0
        for item, candidate in candidates_to_delete:
            try:
                size = candidate.size_bytes
                delete_candidate(candidate)
                removed_count += 1
                removed_bytes += size
                deleted_paths.add(candidate.path)
            except Exception as exc:
                print(f"  ❌ 删除失败: {candidate_display(candidate)} -> {exc}")

        print(f"\n✨ 清理完成！已成功删除 {removed_count} 项，释放空间 {human_size(removed_bytes)}。")
        input("\n按回车键继续...")
        return deleted_paths

    elif current_level == 3:
        print("\n--- 执行层级 3 (单独父项子项列表) 删除操作 ---")
        item, candidates, total = summary[selected_parent_idx]
        if not candidates:
            print("⚠️ 该项目下没有可清理的子项。")
            return deleted_paths

        choice = input(f"请输入要删除的子项序号 (如: 1 或 1,3 或 2-5，输入 'all' 清理该项下所有子项，直接回车取消): ").strip().lower()
        if not choice:
            print("已取消删除操作。")
            return deleted_paths

        sorted_candidates = sorted(candidates, key=lambda c: c.size_bytes, reverse=True)

        if choice == "all":
            indexes = list(range(1, len(sorted_candidates) + 1))
        else:
            try:
                indexes = parse_selection(choice, len(sorted_candidates))
            except ValueError:
                print("⚠️ 序号格式不正确。")
                return deleted_paths

        if not indexes:
            print("⚠️ 未选择任何子项。")
            return deleted_paths

        selected_candidates = [sorted_candidates[idx - 1] for idx in indexes]
        total_bytes_to_delete = sum(c.size_bytes for c in selected_candidates)

        print(f"\n🚀 准备清理 [{item.title}] 的以下子项目:")
        for idx in indexes:
            c = sorted_candidates[idx - 1]
            print(f"  - {idx:02d} {candidate_name(c)} ({human_size(c.size_bytes)})")
        print(f"预计可释放空间: {human_size(total_bytes_to_delete)}")

        if not ask_yes_no("❓ 确认删除这些子项目？", default=False):
            print("已取消删除操作。")
            return deleted_paths

        removed_count = 0
        removed_bytes = 0
        for candidate in selected_candidates:
            try:
                size = candidate.size_bytes
                delete_candidate(candidate)
                removed_count += 1
                removed_bytes += size
                deleted_paths.add(candidate.path)
            except Exception as exc:
                print(f"  ❌ 删除失败: {candidate_display(candidate)} -> {exc}")

        print(f"\n✨ 清理完成！已成功删除 {removed_count} 项，释放空间 {human_size(removed_bytes)}。")
        input("\n按回车键继续...")
        return deleted_paths

    return deleted_paths


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


def choose_parent(
    summary: Sequence[tuple[CleanupItem, List[Candidate], int]],
) -> int | None:
    """Ask which parent project should be shown in level 3."""
    print("\n请选择要查看的父项：")
    for idx, (item, candidates, total) in enumerate(summary, start=1):
        print(f"  {idx:02d} {item.title:<28} {human_size(total):>9} | {len(candidates):>4} 项")

    while True:
        raw = input(f"请输入父项序号 (1-{len(summary)})，直接回车返回: ").strip().lower()
        if not raw or raw in {"b", "back"}:
            return None
        if raw in {"q", "quit", "exit"}:
            raise SystemExit(0)
        try:
            parent_idx = int(raw) - 1
        except ValueError:
            print("⚠️ 请输入有效的父项序号。")
            continue
        if 0 <= parent_idx < len(summary):
            return parent_idx
        print(f"⚠️ 请输入 1 到 {len(summary)} 之间的数字。")


def print_action_menu(current_level: int) -> None:
    print(f"\n🛠️ 当前位于层级 {current_level}，可用指令:")
    print("  1  可清理项目总览")
    print("  2  每个项目的全部子项总览")
    print("  3  查看一个父项下的全部子项")
    print("  d  删除当前层级中选定的内容")
    print("  q  退出脚本")


def main() -> None:
    args = parse_args()
    ensure_macos()

    print("✨ macOS 手动清理助手 ✨")
    print("💡 特点: 先扫描、分等级、分层级交互、逐项确认。")

    items = build_items()
    summary = summarize(items)

    if args.scan_only:
        print_level_1_summary(summary)
        if args.details:
            print_level_2_summary(summary)
        print("\n当前是 --scan-only 模式，未执行任何删除。")
        return

    current_level = 1
    selected_parent_idx: int | None = None

    while True:
        if current_level == 1:
            print_level_1_summary(summary)
        elif current_level == 2:
            print_level_2_summary(summary)
        elif current_level == 3 and selected_parent_idx is not None:
            print_level_3_summary(summary, selected_parent_idx)

        print_action_menu(current_level)
        choice = input("\n请选择指令 [1/2/3/d/q]: ").strip().lower()

        if choice == "1":
            current_level = 1
        elif choice == "2":
            current_level = 2
        elif choice == "3":
            parent_idx = choose_parent(summary)
            if parent_idx is not None:
                selected_parent_idx = parent_idx
                current_level = 3
        elif choice == "d":
            deleted_paths = perform_deletion(
                summary,
                current_level,
                selected_parent_idx if selected_parent_idx is not None else 0,
            )
            if deleted_paths:
                print("\n🔄 正在重新扫描，以磁盘上的实际结果更新总览...")
                summary = summarize(items)
            current_level = 1
            selected_parent_idx = None
        elif choice in {"q", "quit", "exit"}:
            print("已退出。")
            break
        else:
            print("⚠️ 未知指令，请重试。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n用户取消。")
        sys.exit(130)
