# 🧹 Mac 清理助手

一个面向普通用户的 macOS 磁盘清理脚本。**先扫描、分等级、逐项确认，不会默认直接删除任何文件。**

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

运行后会看到三个步骤：

### 第一步：扫描

脚本自动扫描你的 Mac，显示进度条和总览表：

```
可清理项目总览
------------------------------------------------------------------------------------------
[01] L1 | 用户应用缓存                       |  304.3 MB |   22 项 | user_caches
     清理 ~/Library/Caches 下的大部分 App 缓存，通常可自动重建。
     注意: 安全性高，但首次重新打开某些 App 可能会稍慢。
[02] L1 | 用户日志                         |       0 B |    0 项 | user_logs
     ...
[12] L3 | 应用沙盒容器数据                     |    8.5 GB |  735 项 | containers
     清理 ~/Library/Containers 下的沙盒应用数据...
------------------------------------------------------------------------------------------
```

每个项目的 `L1`/`L2`/`L3` 代表安全等级：

| 等级 | 含义 | 举例 |
|------|------|------|
| 🟢 **L1** | 安全，纯缓存/临时文件，删了不影响使用 | 浏览器缓存、日志、废纸篓 |
| 🟡 **L2** | 较安全，开发工具缓存，删了下次会慢但可再生 | Xcode 编译缓存、npm/pip 缓存 |
| 🔴 **L3** | 需人工判断，可能包含用户数据 | 应用容器数据、下载的安装包、旧备份 |

脚本也会识别常见开发工具大头，例如 Xcode `DerivedData`、Simulator dyld 缓存、不可用模拟器设备、XCTest 临时设备、30 天未使用的 Simulator runtime、iOS DeviceSupport，以及 Codex 的 `code_sign_clone` 临时签名副本。涉及 runtime 或 dyld 缓存的项目会通过 `xcrun simctl` 执行，而不是直接删除已挂载的 Simulator 卷。

### 第二步：选择清理范围

```
选择清理方式:
  1. 选择 L1 项
  2. 选择 L2 项
  3. 选择 L3 项
  输入总览序号可选择特定项，例如 [01]、01、01,05、01-03
  q. 退出
```

- **新手推荐选 1**，只选择最安全的 L1 项
- 如果只想处理某个总览项目，可以直接输入它的序号，例如 `[05]` 或 `05`

### 第三步：确认删除

对每个选中的项目，脚本会先展示里面有哪些大文件，然后问你：

```
请选择操作：直接回车=全部清理；输入编号=清理指定项；l=列出全部；n/q=跳过
  >
```

- 直接回车 — 清理当前项目下的全部候选项
- 输入编号 — 只清理指定候选项，例如 `[01]`、`1,3`、`5-8`
- `l` — 列出当前项目的全部候选项
- `n` / `q` — 跳过当前项目

清理完成后，脚本会重新扫描并回到“选择清理方式”这一步。只有在选择清理方式时输入 `q`，脚本才会退出。

---

## 💡 常见问题

### 我需要 Python 吗？

不需要安装。macOS 自带 Python 3，开箱即用。在终端输入 `python3 --version` 可以确认。

### 会误删重要文件吗？

不会。脚本有三重保护：
1. **先扫描后操作** — 告诉你每个项目是什么、占了多大、有什么风险
2. **逐项确认** — 删之前会再问一次，不会默默执行
3. **系统保护目录自动跳过** — Safari、iCloud、HomeKit 等系统保护的缓存目录不会出现在候选列表中

### 还可以用 `--scan-only` 只看看

```bash
python3 mac_cleanup_helper.py --scan-only
```

只扫描不删除，适合先了解一下磁盘上什么东西占空间。

加 `--details` 可以看到每个项目里最大的文件/目录：

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

1. **用系统命令 `du` 扫描** 常见的大文件聚集地（`~/Library/Caches`、`~/Library/Containers`、`~/Library/Application Support`、Xcode 缓存目录等）
2. **按安全等级分类**，告诉你哪些是纯缓存（可放心删），哪些可能含用户数据（需确认）
3. **交互式清理**，每步都要你点头才执行

对 Xcode Simulator runtime 这类不能直接 `rm -rf` 的内容，脚本会使用 `xcrun simctl runtime delete`、`xcrun simctl runtime dyld_shared_cache remove` 等官方命令。这样不会误删 `/Library/Developer/CoreSimulator/Volumes` 下的已挂载 APFS runtime 卷。

没有后门、不上传数据、不需要网络。脚本是单文件实现，你可以自己审查。

---

## 📄 许可

MIT License — 随便用，随便改。
