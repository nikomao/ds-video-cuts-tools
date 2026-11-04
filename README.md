# DeepSeek Harness 视频智能剪辑 Agent

这是 **Harness 内部 Agent + 原生 Skill + 本地媒体工具**，不是 Web、桌面应用、API 服务或常驻后台。用户在 Harness 对话中发出需求，Agent 通过现有 bash、后台任务和文件交付工具执行工作。

## 已接入 Harness

项目根的 AGENTS.md 提供角色与安全边界；.dsh/skills/video-editing/SKILL.md 是实际原生 Skill。

**本次开发会话已收到 Harness 的技能目录刷新，并成功用 skill(name="video-editing") 加载，provider=filesystem。** 不需要修改安装包或启动另一个服务。在这个项目目录新建/继续会话即可使用；其他目录下应先切换到工具项目根。

示例对话：

> 使用视频剪辑 Agent，处理我指定的视频。识别中文语音，生成 SRT；把字幕翻译成英文，去掉长停顿，先给我预览和删剪清单，确认后导出。

Agent 会先问清真实素材路径与目标，再执行。不会自动下载模型、读取 Harness 凭据或上传视频。

## 本轮视听剪辑升级

新增镜头硬切候选检测、逐镜代表帧、黑场/冻结告警、带理由的规则剪辑建议和剪辑风险审阅；当前Harness模型可结合真实画面/转写继续做编辑判断。新增七类非cut转场、原音交叉淡化、逐接点转场（对白硬切与场景淡化可在同一计划混排）、cut 接点 J/L 音画错位、片头尾音画淡化、warm/cool/monochrome基础视觉预设，以及rights-confirmed本地背景音乐、循环、能量侧链压低和音量控制。

所有参数进入不可变EditPlan，预览和最终导出沿相同时间轴；字幕在转场中点切换归属，避免交叠。效果变化后必须重新确认。详细示例见 [视听工作流](docs/CREATIVE_EDITING.md)。这不等于电影级自动导演：J/L 只在 cut 接点生效且重叠音频不做自动 ducking；没有音乐踩点、多机位动作匹配、主体跟踪或专业镜头色彩匹配。

## 功能范围

| 能力 | 实现方式 | 状态/条件 |
|---|---|---|
| 视频探测、音频抽取、代理 | FFmpeg/ffprobe | 真实集成测试覆盖 |
| 语音识别 | faster-whisper，本地 CPU INT8 | 已实现；真实识别需本地权重，未下载权重 |
| SRT/VTT/ASS 导入导出 | 标准库字幕模块 | 单元测试 + SRT 真实 FFmpeg 解析回归 |
| 字幕修订、翻译回填 | 当前 Harness 模型 + apply_translations | 不要求额外 API key；严格校验 cue ID、时间轴 |
| 离线/外部翻译 | Argos / OpenAI-compatible | 可选适配器；需模型/明确联网授权，测试使用 mock |
| 按文本/语义剪辑 | Harness 判断片段 → 显式 EditPlan | 不另外运行一个 LLM Agent 进程 |
| 裁剪、拼接、静音检测 | FFmpeg | 真实媒体测试；切点帧格归一，字幕同步重映射 |
| 镜头分析与候选剪辑 | analyze_shots / suggest_edit | 真实硬切与代表帧验证；不宣称人物/情绪理解 |
| 多模态高光精华推荐 | suggest_highlights | 综合语音密度、强调关键词及画面质量打分，自动提炼黄金切片 |
| 剪辑风险审阅 | review_plan | 词/句内切点、对白交叠、重复镜头、质量告警；仍需人工审阅 |
| 口播停顿与语气词清洗 | clean_speech | 基于词级时间戳识别语气词(嗯/啊/like等)与长停顿，生成紧凑剪辑区间建议 |
| 剪辑计划方案比对 | diff_plans | 对比两版计划的时长、删减/新增范围、转场与字幕模式差异，生成人机易读差分 |
| 字幕排版与断句美化 | refine_subtitles | 长句自动断句拆分，防竖屏溢出，词级时间戳严格无损重映射 |
| 转场与音画淡化 | xfade / concat / afade / amix | 七类非cut转场与逐接点cut混排真实渲染；共享时间轴 |
| 音画错位（J/L-cut） | 独立音频范围 + 采样级 adelay + amix | cut 接点音频提前/延后，真实音调验证；不作自动 ducking |
| 视觉预设与配乐 | 固定滤镜 / 本地音乐 / sidechaincompress | 支持 film/vintage/cyberpunk/vibrant 调色预设、zoom_in/pan 运镜动画、white_fade 闪白入场 |
| 横/竖/方画幅 | 等比缩放+补边 | 不承诺人脸跟踪、智能主体裁切 |
| 配音 | 本地 Piper / macOS say | 已实现逐句 WAV；声线需预装，不支持克隆 |
| 配音时长适配、混音 | FFmpeg atempo、delay、mix、loudnorm | 真实合成音频集成测试；0.5x..2x以外明确失败 |
| 软字幕 | MP4 mov_text | 真实媒体测试，低清预览保留字幕流 |
| 硬字幕 | FFmpeg libass | 已用 ffmpeg-full 9.0.2 真实烧录；默认 ffmpeg 8.1 仍缺 libass，需设置 VIDEO_AGENT_FFMPEG |
| 审批与版本 | 计划摘要SHA256 + 独立artifact | 修改后必须重新确认，原片不覆盖 |
| 任务、重试、取消、缓存 | 项目SQLite账本 + Harness后台jobs | 真实/单元回归；强杀限制见下 |
| 输出质检 | 完整A/V解码、时长、字幕结构/可读性 | 不等于语义/视觉质量或版权审核 |

## 验证状态（v0.4.0）

166项自动化测试全部通过；真实CLI合成示例完成镜头分析、转场、配乐、视觉预设和软字幕导出；逐接点cut/fade混排包含真实渲染、完整解码和硬切颜色边界验证；J/L-cut 用 440/880Hz 音调在真实渲染和真实CLI全流程中确认音频先于/晚于画面切点。详细证据和未验证能力见 [升级验收记录](docs/UPGRADE_VALIDATION.md)。合成示例用于检验效果/时序，不代表电影级成片审美。

## 零服务启动

需要 Python >= 3.10、FFmpeg、ffprobe。从项目根直接运行，无需 pip 安装基础包：

    python3 -m video_agent doctor
    python3 -m video_agent --help
    python3 -m unittest discover -s tests -v

可选依赖必须用户允许后安装：

    python3 -m pip install -e '.[asr]'
    python3 -m pip install -e '.[translation]'
    python3 -m pip install -e '.[tts]'

这些 extras 不代表模型或声线已安装。faster-whisper 默认禁止下载；Argos 只使用已安装语言包；Piper 要求本地 ONNX 模型与配置。许可证分别核对代码、权重、语言包、字体和声线。

需要硬字幕时（macOS/Homebrew），安装含 libass 的 ffmpeg-full 并显式指向它，默认 ffmpeg 不会被替换：

    brew install ffmpeg-full
    export VIDEO_AGENT_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg
    export VIDEO_AGENT_FFPROBE=/opt/homebrew/opt/ffmpeg-full/bin/ffprobe

设置后 `python3 -m video_agent doctor` 的 hard_subtitles 应为 true；未设置时仍使用默认 FFmpeg，硬字幕会被依赖门控拒绝。

本机探测结果：Python 3.12.12；默认 FFmpeg/ffprobe 8.1（缺 libass）；已额外安装 Homebrew ffmpeg-full 9.0.2（含 libass，/opt/homebrew/opt/ffmpeg-full）；faster-whisper 可导入，medium 模型已下载到项目内缓存；Argos/Piper 未安装；macOS say 存在。以上为开发时检查，不是跨机器保证。

## 工具调用约定

1. init --project NEW_DIR --input LOCAL_MEDIA：拷贝素材，计算 SHA256，创建项目；拒绝覆盖非空目录。
2. run --project DIR --task TASK --params PARAMS.json：一次任务，JSON 参数、JSON 结果。
3. jobs/status：本地账本状态；retry：失败/取消任务的新attempt；approve：记录已取得的人工计划确认。

完整参数示例见 docs/WORKFLOWS.md。CLI 的 job_id 是本地任务ID，不是 Harness 后台任务ID。

每个项目包含：

    project.json                  # 原片绑定、项目元数据
    assets/<sha256>.<suffix>       # 原片副本
    jobs.sqlite3                  # 事务任务、artifact、审批、执行指纹
    jobs/<job_id>/attempt-N/       # 每次执行独立目录
      request.json                # 参数、非敏感执行配置
      commands.jsonl              # FFmpeg/ffprobe argv、返回码、耗时、stderr尾部
      result.json / error.json
      track.json / *.mp4 / *.wav  # 实际产物

已注册文件不允许手改。修订字幕用新import_subtitles任务，变更剪辑用新plan。成功任务缓存验证产物及其依赖；代码/显式模型配置/本地模型内容变化会改变缓存键。

## 可选“视频智能剪辑”专用 Agent 选择项

harness/ 提供按已安装 Harness 0.2.1-rc.5 源码接口编写的本地 Cordis bundle，只注册persona preset，复用现有host工具。主路径无需安装该bundle，Skill已经可用。

此可选bundle **尚未安装到用户全局profile，也未做真实preset挂载验收**；不要把“配置已生成”当成“下拉选择项已生效”。详见 harness/README.md。不会照搬旧版 .agent-presets/crew 配置。

## 明确限制

- 当前实现是单机/单项目隔离、无服务器形态，不包含此前SaaS方案的多租户、RBAC、计费、MinIO、Postgres或Web时间轴。它们不适用于本次交付形态。
- 说话人分离、WhisperX强制对齐、自动B-roll/复杂模板、自动口型同步、OTIO交换未实现；Harness可分析转写与画面，但没有伪造这些模型能力。
- 配音目前要求全源时间轴。剪后配音先导出，基于剪后视频创建新工程并导入重映射字幕。字幕/配音可以不同语言，但cue ID和时间必须匹配。
- SIGTERM/键盘中断记录cancelled；SIGKILL、断电可能留下running，当前不自动抢占或恢复活跃任务。确认旧进程退出后再建新任务（--fresh）或人工恢复；不要盲目重复运行。
- 审批是对话工作流约束和摘要记录，不是防恶意本地用户的认证/签名系统。
- API key不进入参数/账本；重试前校验记录的非敏感配置、代码和本地模型指纹，变更后要求提交新任务。模型名对应远端版本变化、隐藏依赖升级无法仅靠名称完全复现；关键交付需固定权重/版本并审查日志。
- SRT数字序号按标准重建，真实cue ID保存在JSON；VTT/ASS可保ID。ASS为10ms精度；为防覆盖标签注入，字面反斜杠/花括号以全角字形显示，原文放Effect元数据以供恢复。
- 字幕复核、配音授权、翻译语义、音乐素材版权、最终视觉听感必须人工验收。绝不自动对外发布。
