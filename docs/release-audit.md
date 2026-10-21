# 2026-10-21 代码审计与 Windows 发布审核

## 当前结论 / Release decision

源码可以同步；Windows 安装包仅进入维护者可见的 GitHub Release 草稿，不公开发布、不设为 Latest。包包含 Pedalboard 0.9.26（GPLv3），现有上游 tag 与五个 gitlink 源码归档是候选材料，尚未确认与所用 Windows wheel 的确切对应源码、构建脚本及相关分发义务完整匹配。草稿和 prerelease 标签不能替代该门槛。安装器未签名。

Source changes may be pushed. The Windows installer remains in a maintainer-only review draft, not a public release or Latest. Exact corresponding-source completeness and redistribution obligations for the bundled Pedalboard Windows wheel are not established. The installer is unsigned.

## 范围

- 两个回环 HTTP 服务、WebSocket、WebDAV 与声库分发信任边界。
- 工程恢复的 VST 引用、声库 YAML 模型引用、原生编辑器生命周期。
- WASAPI 设备所有权与关闭路径、冻结解释器布局、原始 wheel 校验及发布输入。
- 本轮是有界代码审计，不是“整个应用无漏洞”认证；未做独立渗透测试、物理声学延迟或跨机器验收。

## 已修复发现

| 优先级 | 问题与依据 | 处理 |
|---|---|---|
| P1 | 外站简单 POST 可重置本机工程；实测恶意 Origin 返回 200，工程轨道由 1 变 0。外部 Host 亦被接受 | Rust／Flask 全局 Host、Origin、Fetch Metadata 检查，覆盖业务入口和 WebSocket；恶意请求返回 403，状态不变；保留 Tauri、开发源和无 Origin CLI |
| P1 | 工程／同步恢复的任意绝对 VST 路径可进入原生加载器；worker 不是安全沙箱 | 相对／绝对路径统一约束到本机可信插件根；网络、设备、驱动器相对、解析后越界拒绝；根内绝对路径仍可用 |
| P2 | 分发声库 YAML 内模型引用可越出声库并在检查时访问 UNC | 共享校验 acoustic、各组件模型引用与 vocoder 引用；加载预测器前重复使用该边界，合法声库内部相对引用保留 |
| P1 | 异步 embed 持有编辑器互斥锁后调用可能等待 UI 线程的 hwnd getter，与 stale 同步 IPC 锁序相反 | HWND 查询移到取锁之前；本轮实际 stale IPC 批量调用完成，不声称确定性复现了原死锁 |
| P1 | 打包把 inference_service 内容摊平，干净外部 Python 的模块启动失败 | 保留 inference-service/inference_service 包层级；失败前 ModuleNotFoundError，安装后无源码污染环境可导入 |
| P2 | 完整 wheel／RECORD 未变时，额外包目录文件仍可被 collect-all 打包 | 发布预检对照原始 wheel 文件集合，拒绝未登记包目录新增文件；真实新增 canary 文件被拒绝，随后删除 |
| P2 | release/ 不进源码仓库，但打包强依赖其中输入，源码克隆无法自行满足预检 | 发布 docs/release-inputs.json 明确材料包、逐项哈希和步骤；材料包附审核草稿，不将缺输入伪装成可直接构建 |

## 实际验证

- Python：62 passed，1 skipped。跳过项需要 Windows symlink 权限；另创建真实目录 junction 验证可信根逃逸被拒绝。
- Rust server：16 passed；Node 音频回归：13 passed。
- npm production audit：0 已报告漏洞；仅覆盖该次 npm advisory 数据，不代表 Rust／原生依赖无漏洞。
- 真实 HTTP：修复前后跨 Origin 工程重置对照；伪造 Host、无 Origin cross-site、恶意 WebSocket 拒绝；允许的 Tauri／开发 WebSocket 与 CLI 继续可用。
- 完整 Windows 构建、隔离安装、可信 Nova 两轮 76 参数回读、外部解释器包导入通过；主窗 344 ms 退出，卸载返回 0，测试快捷方式恢复。
- 当前安装包：`SVS_Studio_0.1.0_windows-x64_setup.exe`，133841710 字节，SHA-256 `1eafe32e2a9561266172acfe5623bb6e29fc98b83cb5d12280f5b48b7fff901c`。
- 本次只验证上述安装版受影响路径；历史六插件／压力矩阵保持其原包身份，不冒称本包全部重测。

## 构建与材料

1. 从维护者取得 `docs/release-inputs.json` 指定的审核材料包，校验 SHA-256，解出 `release/python/`。
2. 用 CPython 3.11 创建 `.venv-release-lock`，按 `scripts/release-windows-py311.lock` 与 `--require-hashes` 安装；将原始 wheel 下载到 `release/python/wheelhouse`。
3. 运行 `scripts/package_desktop.ps1 -PrepareOnly`；缺材料应失败，不删除现有 runtime。
4. 完整打包命令构建前端、server、冻结 Python、Tauri／NSIS。外部 GPU 环境仍由使用者配置。
5. 原始 wheel、材料和源码候选不等于 GPL 对应源码闭环；确认许可前保持草稿。

详细本机证据留在被忽略的 `acceptance/2026-10-21_代码审计与发布/`，不把截图中的其他用户窗口、测试配置、日志或凭据推到源码仓库。
