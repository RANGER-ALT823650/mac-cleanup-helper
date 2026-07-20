# 🧹 Mac 清理助手

一个面向普通用户与开发者的 macOS 磁盘清理脚本。**先扫描、按两层菜单查看、逐项确认，不会默认直接删除任何文件。**

无需安装任何第三方软件，macOS 自带的 Python 3 就能跑。

---

## ⚡ 快速开始

打开终端（在启动台搜“终端”或 Terminal），复制粘贴下面一行，回车：

```bash
python3 <(curl -fsSL https://raw.githubusercontent.com/RANGER-ALT823650/mac-cleanup-helper/main/mac_cleanup_helper.py)
```

如果你想下载到本地以后再用：

```bash
curl -O https://raw.githubusercontent.com/RANGER-ALT823650/mac-cleanup-helper/main/mac_cleanup_helper.py
python3 mac_cleanup_helper.py
```

---

## 🖥️ 怎么用

运行后会进入清晰的两层交互模式：

### 🔄 分层交互模式

1. **[第一层] 可清理大项总览**
   - 扫描完成后首先显示 26 个清理大项，只展示名称、总大小和子项数量。
   - 直接输入大项序号（例如 `6`），进入该大项的第二层详情。

2. **[第二层] 当前大项详情**
   - 显示该大项的用途说明、风险等级、注意事项、总大小和全部具体子项。
   - 裸数字只用于第一层导航；清理操作必须显式使用 `c`，避免数字被误解为删除指令。

### 🛠️ 交互指令

第一层指令：

- `1` 到 `26`：进入对应大项的第二层详情。
- `c 1,3,6-8`：清理一个或多个完整大项。
- `r`：重新扫描磁盘。
- `q`：退出脚本。

第二层指令：

- `c 1,3,5-7`：清理当前大项中选定的子项。
- `c all`：清理当前大项的全部子项。
- `b`：返回第一层总览。
- `r`：重新扫描磁盘并刷新当前大项。
- `q`：退出脚本。

> [!IMPORTANT]
> - 执行清理前，脚本会**自动去重路径**、展示预计释放空间和风险提示；低/中风险内容需要 `y` 确认，高风险内容必须输入完整的 `DELETE`。
> - 清理完成后不会等待完整重扫，而是根据成功删除结果即时更新统计并直接返回第一层。需要读取磁盘上的全部最新变化时，请手动输入 `r`。

---

## 🟢🟡🔴 安全等级说明

每个项目的 `L1`/`L2`/`L3` 代表安全等级：

| 等级 | 含义 | 举例 |
|------|------|------|
| 🟢 **L1** | 安全，纯缓存/临时文件，删了不影响使用 | 浏览器缓存、日志、废纸篓、**XDG 用户缓存** |
| 🟡 **L2** | 较安全，开发工具/应用缓存，删了下次会慢但可再生 | Xcode 编译缓存、npm/pip 依赖包缓存、**Arduino 下载缓存** |
| 🔴 **L3** | 需人工判断，可能包含用户数据或环境状态 | 下载目录安装包、本地手机备份、**iOS Simulator 数据重置** |

### 🛠️ 特殊开发工具/系统工具支持
- **Simulator 官方命令操作**：对涉及 Xcode Simulator runtime 卸载、模拟器 dyld 共享缓存清理，或 Simulator 设备数据重置 (`ios_simulator_erase`) 等操作，脚本调用官方的 `xcrun simctl` 工具执行，而不是直接 `rm -rf` 挂载目录，更加安全合规。
- **XDG 缓存支持**：新增对 `~/.cache` 下 `xdg_caches`（如 HuggingFace 等 AI 工具编译/模型缓存）的支持。
- **Arduino IDE 支持**：新增对 `~/Library/Arduino15/staging/` 下的开发板与库下载缓存清理。
- **Codex 临时签名**：清理 Electron 软件（如 Codex/Edge 等）产生的 `code_sign_clone` 临时 App 副本。
- **Xcode DerivedData 保护**：只清理 Build、索引、日志、符号缓存等可重建内容；明确保留 `SourcePackages`，不会在普通缓存清理中删除 GRDB 等 Swift Package checkout。

---

## 💡 常见问题

### 我需要 Python 吗？

不需要单独安装。macOS 自带 Python 3，开箱即用。在终端输入 `python3 --version` 可以确认。

### 会误删重要文件吗？

不会。脚本有三重保护：
1. **先扫描后操作** — 告诉你每个项目是什么、占了多大、有什么风险。
2. **两层交互与去重** — 第一层看大项、第二层看全部子项，并自动去重路径，防止重复删除。
3. **安全确认与系统过滤** — 删除前必须二次确认，且 Safari、iCloud、HomeKit 等系统保护目录已被脚本自动过滤，不会报错干扰你。

### 还可以用 `--scan-only` 只看看吗？

可以：
```bash
python3 mac_cleanup_helper.py --scan-only
```
只扫描不删除，适合先了解一下磁盘上什么东西占空间。

如果想要同时输出第一层总览和所有大项的第二层详情，可以加 `--details`：
```bash
python3 mac_cleanup_helper.py --scan-only --details
```

### 清理完空间怎么没变化？

如果删了东西但可用空间没增加，可能是 macOS 的“可清除空间”还在等系统回收。可以试试：
- 重启 Mac
- 打开“系统设置 → 通用 → 储存空间”，等它刷新

### 为什么有些目录删不掉？

macOS 对 Safari、iCloud、家庭共享等系统目录有保护，普通权限删不了。脚本已自动过滤这些目录，不会报错干扰你。

---

## 🔧 脚本原理

简单来说，脚本做了三件事：

1. **用系统命令 `du` 扫描** 常见的大文件聚集地（`~/Library/Caches`、`~/Library/Containers`、`~/Library/Application Support`、Xcode 缓存目录等）。
2. **按安全等级分类**，告诉你哪些是纯缓存（可放心删），哪些可能含用户数据（需确认）。
3. **交互式清理**，每步都要你点头才执行。

对 Xcode Simulator runtime 这类不能直接 `rm -rf` 的内容，脚本会使用 `xcrun simctl runtime delete`、`xcrun simctl runtime dyld_shared_cache remove` 等官方命令。这样不会误删 `/Library/Developer/CoreSimulator/Volumes` 下的已挂载 APFS runtime 卷。

没有后门、不上传数据、不需要网络。脚本是单文件实现，你可以自己审查。

---

## 📄 许可

MIT License — 随便用，随便改。
