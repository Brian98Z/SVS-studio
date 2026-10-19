# SVS Studio 开发路线图与实施状态

按 SVS 工作站开发规划分 5 阶段交付。

## Phase 1: 乐理与多层参数曲线引擎 ✅
- `packages/core-engine`（Rust）：480 PPQ 坐标系统（tick↔ms 分段积分、MBT 离散坐标、多拍号）、
  8 通道曲线层（Base64 f32le + NaN 稀疏）、五层合成截断、快照式 Undo/Redo。
- `conformance.py`：Rust/Python 曲线与时间坐标对拍。
- 状态：Rust workspace 测试、release 构建、Python 对拍均已通过（2026-10-19）。

## Phase 2: 增量切片与推理调度 ✅
- `inference_service/contract.py`：与 Rust 逐字节一致的切片/哈希/编解码。
- `phrase` 切片：间隙 ≥480 tick 分句；legato 跨句归属。
- `cache_manager.py`：LRU 128 句 / 512MB，命中统计。
- `onnx_pipeline.py`：mel2dur→mel2pitch→acoustic→vocoder，分档桶 128/256/512/1024/2048。
- `dsp_fallback.py`：无模型回退（谐波源+共振峰+气声噪声+余弦窗防爆音）。
- 冒烟：句合成 42ms（2 音符）、缓存命中、400 校验 ✅。

## Phase 3: CLI 与 MCP 协议层 ✅
- `apps/cli`：契约 §7 全命令 + `conformance` 离线对拍子命令。
- `apps/mcp-server`：FastMCP 10 工具（契约 §8），stdio。
- 状态：Rust CLI 待编译；MCP 已可运行（依赖 server）。

## Phase 4: 桌面交互 ✅（浏览器模式验收）
- `apps/desktop`：Vue3 + Canvas。钢琴卷帘（视口剔除/音符拖拽/右键删/双击歌词/Ctrl 缩放）、
  8 通道曲线画笔面板、轨道面板（Mute/Solo）、走带条、WS 实时播放头。
- `vite build` 82KB ✅；Tauri v2 配置齐备（`src-tauri/`），桌面打包需 Rust 工具链。

## Phase 5: 端到端联调
- [x] Rust workspace 测试 22 项 + release server/CLI 编译（2026-10-19）。
- [x] server :8761 + 推理 :8760，五层绘制/读取/擦除/缓存失效/渲染/存取工程真实 HTTP 冒烟。
- [x] 浏览器曲线画布事件：Direct 绘制、擦除恢复空洞、Baseline 只读；截图检查 Effective 视图。
- [x] CLI 与 MCP stdio：指定图层绘制/擦除后读取持久化值。

## 已知边界
- 通用四模型 ONNX 管道缺少模型时使用 DSP；本机绮萱 DiffSinger 通过独立原生桥实际演唱，不能只根据 `/health` 的通用管道模式判断原生声库可用性。
- 浏览器已使用 WebAudio PCM 实时混流与 AudioContext 走带；原生 DiffSinger 仍整轨合成，不是逐句神经流式推理。
- 离线混音目前为 24kHz 单声道；不保留实时监听的立体声声像，外部曲线与轨内变速仍不适用于原生 DiffSinger。

## 2026-10-19：对比报告阶段一 · 五层曲线迭代

- 已实现 Baseline(`base`)/User/Envelope/Direct/Global，旧 `.svsproj` 自动按空新增层读取；Direct 覆盖前三层，Global 最终叠加。
- 编辑器增加图层切换、Effective 预览与擦除；写入捕获轨道/参数/图层，串行提交，避免切换图层后串写及擦除复活。
- 推理接收全部层；修复第二句绝对曲线起点、多速度分段积分、渲染接口丢失曲线。DSP 八通道生效。
- 缓存键包含五层边界与速度表；当前整轨曲线失效粒度，尚未实现逐句窗口失效。
- 实测 Dynamic Direct=0.25：第二句 RMS 比例 0.25，第一句不变；擦除后 PCM 与修改前一致，保存重开保留五层。
- 最终验证：Rust 22 项、DSP/HTTP 10 项与 `conformance.py` 全通过，前端生产构建与 server/CLI release 构建成功；已有 3 条 Rust 警告（WS 多余 mut、导出范围字段未使用、走带 start_ms 未使用）不在本轮范围。
- 模型边界：当前绮萱 DiffSinger 及通用 ONNX 适配器不接收外部曲线，明确拒绝有限曲线输入，不再静默忽略。DiffSinger 轨内变速也明确拒绝。
- G2P 扩容仍未完成；WebAudio 实时混流在下述整曲验收迭代交付。阶段二至四本轮未改。


## 2026-10-19：PCM 实时试听与完整歌曲验收

- 播放清单与短句 PCM 接口、音频内容版本和混音版本分离；浏览器按 AudioContext 时钟调度音源，支持暂停、seek、音量与声像实时更新及旧请求取消。
- 修复播放启动未设置 playing 的实际故障；专用 Chrome 实测 5 音源同步起播、非零主输出、暂停全部停止、延迟响应不复活、音量归零和自然曲尾。
- 修复 ABC 导入尾奏被最后人声音符截断；完整谱长 total_ticks 进入伴奏生成。
- 修复 DiffSinger 分段累计时间与全局 0.15 秒前导静音错位，缓存标签升级为 diffsinger-aligned-3。
- 完成《把晨光唱给远方》60 小节整曲：168 字歌词由应用 AI 生成并逐音符对齐，绮萱真实主唱与副歌和声，6 处旋律/时值调优后重合成。
- 应用原始混音 129.107 秒；后处理交付 WAV 复测 −16.01 LUFS、−1.50 dBTP。48kHz/24bit 来自 24kHz 源重采样，不是音频超分。
- 最终验证：Rust 27 项、Python 音频回归 11 项，前端及 server/CLI release 构建通过。人工听审、原生外部曲线、离线立体声与独立安装发行未验收。
- 资产、截图和 JSON 证据位于 `acceptance/2026-10-19_把晨光唱给远方/`；正式 Markdown/Word/PDF 验收报告与全部成品交付桌面同名目录。


## 2026-10-19：原生控制、宿主互通与本地 GPU 推理

- 原生 DiffSinger 接入五层可支持条件、逐句缓存、呼吸音、颤音与轨内速度图；不支持的声库输入仍显式拒绝。G2P 已扩容并接入发音工具。
- 立体声离线混音、EQ/混响/压缩及总线路由已接入；浏览器实际创建总线、启用混响、保存轨道发送并完成两句 PCM 播放，导出回读双声道 24kHz WAV。实时图路由的最终一致性验收单独记录，不把方程 fixture 等同于完整试听。
- 本地 GPU：RTX 5070 Ti 16GB，PyTorch 2.11.0+cu130、ONNX Runtime 1.31.0。显式 CUDA 原生未缓存合成成功，8 个会话均加载 CUDA；Basic Pitch 实际 HTTP 在 CUDA 上转录 440Hz 为 MIDI 69；Demucs 实际 HTTP 模型/输入位于 cuda:0，返回四路有限值音轨。
- 配置 `SVS_GPU_PYTHON` 选择兼容环境，GPU 启动默认 `SVS_INFERENCE_DEVICE=cuda`；显式设备不可用时报错。`/api/inference/hardware` 和声库页面显示运行会话，不用安装清单冒充设备证明。
- AI 主题作曲及伴奏沿用配置的聊天服务，不是本地 GPU 大模型；VITS、多说话人/情绪声库、可运行 VST3 效果器、市场/云服务仍有明确外部验收前提，未宣称全功能完成。
- 本轮集成回归：Rust workspace 33 项、Python 行为回归 50 项通过。真实 GPU 音频与设备数据位于 `acceptance/2026-10-19_GPU本地推理/`。安装包、界面音符回写及下载按实际运行结果另记，不能仅凭构建成功验收。
- 浏览器端实际上传 440Hz WAV，Basic Pitch 在 CUDA 上运行并导入可编辑 MIDI 69；Demucs 在 CUDA 上运行并生成 bass/drums/other/vocals 四个 WAV 下载链接。主题作曲生成并导入 26 个带歌词音符，伴奏生成并导入独立乐器轨 8 个音符，工程与界面截图已保存至上述 GPU 验收目录。首次伴奏长度不符时明确拒绝，增加解析后精确拍数约束后成功。
- 实际 AudioContext 图验证：发送电平 0.5 后输出 RMS 为直达的约 1.51 倍；改路由到静音总线输出为零，恢复总线音量后输出恢复。该结果证明动态发送/路由运行，不等于全部采样率、混合通道及效果尾音逐样本一致。
- 混合通道已改为每个音源独立声像处理，避免同轨单声道/立体声相互覆盖。实际 OfflineAudioContext 并发渲染与离线公式最大误差 9.47e-9；seek 进入淡出后持续下降至尾端。最终 Vite 构建通过。
- Windows NSIS 包已完成隔离安装并显示 Tauri 窗口；通过外部 GPU Python 与显式声库路径运行中文双音符 `/render_ds`，HTTP 200、未命中缓存，随后独立回读严格 CUDA 与 8 个 CUDA 模型会话。修复了安装环境中文声库 GBK 解码问题，启动统一设置 `PYTHONUTF8=1`。安装器不包含 CUDA Python、模型或声库。
- 最终安装器 `release/windows/SVS_Studio_0.1.0_windows-x64_setup.exe`，109,966,666 字节，SHA-256 `6f9c64900f98d28b718f1adbe73a5617a3e00282a3ceb81f7ff0379a5b61b5a2`；隔离安装含 3,569 个文件。关闭安装后的应用窗口，自有后端与 GPU 推理进程树退出，8760/8761 均拒绝连接。完整证据、外部验收条件与边界见 GPU 验收目录的 `迭代验收报告.md` 和 `release-manifest.json`。

