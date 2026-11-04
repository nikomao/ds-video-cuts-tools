# 视频智能剪辑 Agent：产品与技术架构方案

> **交付形态已调整**：按用户后续要求，实际开发为 DeepSeek Harness 内部 Agent + 原生 Skill + 本地工具，不开发独立 Web/App/后台服务。本文为早期完整产品蓝图，不代表所有阶段已实现；实际功能、验证状态和限制以 README.md 为准。原蓝图中的 Web/PostgreSQL/MinIO/多租户部分不属于当前内部 Agent 实现。

> 版本：v0.1（方案基线） | 更新时间：2026-11-03
> 目标：不从零实现媒体能力，而是用成熟开源组件搭建可编排、可人工复核、可追溯的视频智能剪辑产品。

## 1. 结论先行

建议采用“Agent 负责理解与编排，确定性媒体引擎负责执行，人工工作台负责复核”的三层架构：

1. **Agent 层**：把自然语言需求转为结构化剪辑计划（EditPlan），调用 ASR、翻译、TTS、剪切、合成、烧录等工具；不直接生成不可审计的 ffmpeg 命令。
2. **媒体能力层**：FFmpeg + Python/Node 封装的稳定工具服务，所有步骤产出中间文件、日志和可重试任务。
3. **工作台层**：项目、素材、转写稿、字幕轨、时间轴、预览、版本和导出管理；机器结果必须支持逐句修改和重新渲染。

推荐 MVP 先解决：**视频上传 → ASR → 字幕校正 → 翻译 → 自动去停顿/按语义切片 → 字幕烧录/导出**。配音、说话人分离、素材检索、模板化短视频放在第二阶段。

## 2. 产品定位与能力地图

- **项目与素材**：项目/版本/权限、分片上传、断点续传、去重、病毒和格式校验、媒体探测、代理视频。
- **内容理解**：ASR、词级时间戳、置信度、说话人、镜头/静音/VAD、高光候选、章节、全文及语义检索。
- **文本本地化**：字幕生成、逐句编辑、机器翻译、术语表、双语排版、SRT/VTT/ASS 导入导出。
- **剪辑编排**：按文本删剪、去停顿/口头禅/重复、镜头选择、画幅重构、模板和低清预览。
- **音频与交付**：TTS、原声保留/替换、混音、响度控制、软字幕/硬字幕、片头尾、水印、多规格导出。
- **治理**：置信度标记、逐句复核、版本回退、审计、敏感词/版权风险、成本和用量。

## 3. 端到端用户流程

1. 创建项目，设置源语言、目标语言、画幅、字幕样式、输出平台和隐私策略。
2. 上传素材，执行 probe、音频抽取、响度/降噪可选处理，并生成代理视频。
3. ASR 输出 segment、word timestamp、confidence；低置信词和专名进入复核清单。
4. 生成内部 Cue JSON，再导出 SRT/VTT/ASS；按阅读速度、行宽、最短/最长时长切分。
5. 逐 cue 翻译，保留 cue id 和时间轴，用术语表、数字和长度规则做质检。
6. Agent 生成剪辑计划，用户先看低清预览和变更清单，再接受、修改或拒绝。
7. 可选生成 TTS，检查真实时长，做语速/静音调整和原声 ducking。
8. 人工复核文本、时间轴、画面、声音、版权和敏感内容，形成可回退版本。
9. 执行导出和 QC，提供无字幕、软字幕、硬字幕及多画幅版本。

## 4. Agent 设计与安全边界

### 4.1 Agent 角色

- 项目规划 Agent：理解目标、补齐参数、生成任务 DAG。
- 媒体分析 Agent：读取 probe、转写、摘要和候选剪辑区间。
- 字幕 Agent：断句、纠错、翻译、双语排版和质量检查。
- 剪辑 Agent：把意图转换为 EditPlan，不直接修改原片。
- 配音 Agent：选择音色、生成音频、检查时长与同步。
- 质检 Agent：检查黑帧、静音、字幕越界、漏译、音画同步、响度和编码。

### 4.2 工具契约

建议工具：probe_media、extract_audio、transcribe、align_words、diarize、translate_cues、make_subtitles、find_highlights、build_edit_plan、cut_segments、tts_cues、mix_audio、burn_subtitles、render_preview、qc_export。每个工具必须使用 JSON Schema 校验，带 job_id 幂等执行，返回 artifact_id、日志、模型版本和许可证元数据；长任务异步化，支持取消、重试和断点续跑。

Agent 可自动执行低风险、可重试任务；批量删剪、字幕锁定、整段 TTS、硬字幕、付费模型、超预算任务必须二次确认；最终发布必须人工复核。禁止删除原片、覆盖唯一工程、无授权克隆声音或人脸、未经确认公开发布、跨项目读取素材和把未验证结果称为已完成。

### 4.3 EditPlan 示例

    {
      "sourceAssetId": "asset_123",
      "segments": [{"sourceIn": 12.4, "sourceOut": 28.9, "reason": "主题命中", "locked": false}],
      "operations": [{"type": "remove_silence", "thresholdDb": -35}, {"type": "burn_subtitle", "trackId": "cue_en"}],
      "outputs": [{"kind": "mp4", "aspect": "9:16", "subtitleMode": "hard"}]
    }

## 5. 推荐技术架构

### 5.1 服务拆分

- Web/API：项目、素材、上传、任务状态、版本和导出接口。
- Orchestrator：MVP 用队列 + 状态机；生产可选 Temporal、LangGraph 或 Argo。
- Media Worker：FFmpeg/ffprobe、代理、剪切、拼接、滤镜、烧录、封装。
- AI Worker：GPU 推理服务，ASR、对齐、说话人、翻译、摘要、TTS。
- Subtitle Service：Cue JSON 与 SRT/VTT/ASS 转换、排版规则和 QA。
- Metadata：PostgreSQL；对象存储 S3/MinIO；Redis 用于队列和缓存。
- Search：PostgreSQL FTS 起步，后续 OpenSearch/向量库。

### 5.2 数据流

Upload -> Probe -> Proxy -> ExtractAudio -> VAD/ASR -> Align/Diarize -> CueJSON -> Translate -> EditPlan -> Preview -> HumanReview -> TTS/Mix -> Render -> QC -> Publish

核心实体：Project、Asset、Job、Transcript、SubtitleTrack、EditPlan、VoiceTrack、Render、Artifact。所有产物通过 Artifact 管理，不把临时路径散落在业务代码中；用内容哈希和版本化命名，设置临时文件 TTL。

## 6. 开源组件选型矩阵

| 能力 | 首选 | 备选/补充 | 许可证与产品化判断 |
|---|---|---|---|
| 媒体处理、编解码、烧录 | [FFmpeg](https://github.com/FFmpeg/FFmpeg) | [GStreamer](https://gitlab.freedesktop.org/gstreamer/gstreamer) | 行业标准；LGPL/GPL 构建差异、专利和命令注入需治理 |
| ASR | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | [Whisper](https://github.com/openai/whisper)、[whisper.cpp](https://github.com/ggml-org/whisper.cpp) | 代码多为 MIT；模型、权重、数据使用需独立核查 |
| 词级对齐/说话人 | [WhisperX](https://github.com/m-bain/whisperX) | [pyannote-audio](https://github.com/pyannote/pyannote-audio) | 依赖模型可能需要 Hugging Face 授权或 token，锁定 GPU/版本 |
| 字幕编辑/转换 | [Subtitle Edit](https://github.com/SubtitleEdit/subtitleedit) | [pysubs2](https://github.com/tkarabela/pysubs2) | Subtitle Edit 常见发行版为 GPL 系列；pysubs2 适合后端转换，需核对版本 |
| 翻译 | [Argos Translate](https://github.com/argosopentech/argos-translate) | [LibreTranslate](https://github.com/LibreTranslate/LibreTranslate) | Argos 代码 MIT；LibreTranslate AGPL；模型语言包要单独审查 |
| 时间轴/剪辑 | [OpenTimelineIO](https://github.com/AcademySoftwareFoundation/OpenTimelineIO) | [auto-editor](https://github.com/WyattBlue/auto-editor)、[MoviePy](https://github.com/Zulko/moviepy) | OTIO Apache-2.0，auto-editor/MoviePy 常用 MIT；复杂渲染交给 FFmpeg |
| Web 模板渲染 | [Remotion](https://github.com/remotion-dev/remotion) | React + FFmpeg | 自定义商业条款，不默认视为宽松 OSS；商用需法务确认 |
| TTS | [Piper](https://github.com/OHF-Voice/piper1-gpl) | [Coqui TTS](https://github.com/coqui-ai/TTS)、[OpenVoice](https://github.com/myshell-ai/OpenVoice)、[MeloTTS](https://github.com/myshell-ai/MeloTTS) | 代码、模型、voice model 许可不同；声音克隆必须有授权证明 |
| Agent/工作流 | [LangGraph](https://github.com/langchain-ai/langgraph) | [Temporal](https://github.com/temporalio/temporal)、[Argo](https://github.com/argoproj/argo-workflows) | LangGraph/Temporal 适合状态、重试和人工介入；媒体任务仍需独立 worker |

调研补充：AutoGen 仓库已提示归档，不建议新生产项目采用；n8n 适合运营编排但为 fair-code/自定义许可，不能默认当作传统 OSS。开源代码许可证不等于模型权重、语言包、声线和素材可商用。上线前应锁定 commit，生成 SBOM，并由法务逐项复核。

## 7. 字幕与配音工程规则

- 内部统一 Cue JSON：start_ms、end_ms、text、words、speaker、confidence、source_segment_id、translation_status；SRT/VTT/ASS 只是导入导出格式。
- 字幕 QA：阅读速度、最短/最长时长、行数、每行字数、标点断行、重叠、越界、低置信词、专名、数字和术语。
- TTS：按 cue 生成并测量真实时长；安全范围内调整语速，超出则重新断句；保留原声时使用 ducking、响度标准化和 limiter。
- 每个音频片段保存 voice_id、模型、参数、授权状态和输入文本哈希。

## 8. MVP 与迭代路线

### Phase 0：技术验证（1–2周）
本地 MP4 完成 probe、音频抽取、faster-whisper 转写、Cue JSON/SRT、FFmpeg 硬字幕烧录；建立 10 条回归素材。

### Phase 1：可用 MVP（3–6周）
项目/素材/任务/字幕工作台；ASR、人工修改、双语翻译、SRT/VTT/ASS、剪切/拼接、去停顿、预览、导出、任务重试和日志。

### Phase 2：智能剪辑（6–10周）
章节和高光、自然语言 EditPlan、9:16/1:1/16:9、多说话人、语义检索、模板字幕和批量任务。

### Phase 3：配音与生产化（10–16周）
TTS、音画同步、混音、术语库、声线管理、企业权限、私有化、审计、计费和质量仪表盘。

## 9. 验收指标

- 质量：按语言、噪声、时长、模型版本分桶统计 WER/CER、词级时间戳误差、翻译人工修改率。
- 时间轴：字幕重叠率和越界率为 0；音画同步偏差目标 P95 ≤ 80ms。
- 性能：上传可断点续传；代理首帧 ≤ 30 秒；短片低清预览 P95 ≤ 2 分钟。
- 可靠性：核心 API 99.9%；失败自动重试至少 2 次；幂等率 100%；导出成功率目标 ≥ 99%。
- 成本：按分钟、模型、导出资源统计，支持缓存复用、预算阈值和超额确认。

## 10. 安全、合规与风险

- 视频和语音可能是个人信息：默认私有、加密存储、短期下载链接、租户隔离、可删除和留痕。
- 语音克隆必须有授权证明；禁止仅凭用户一句话复制第三方声线。
- 上传文件做 MIME/魔数校验、大小/时长限制、病毒扫描和沙箱转码。
- FFmpeg 参数、字幕、字体、URL、压缩包防路径穿越、命令注入和资源耗尽。
- 输出绑定源文件哈希、代码版本、模型版本、提示词版本和许可证清单。

## 11. 第一个可执行技术切片

建议目录边界：

    apps/web                 # 项目、字幕、预览和导出工作台
    services/api              # 项目/素材/任务 API
    services/orchestrator     # 状态机或工作流编排
    workers/media             # probe、extract、cut、concat、mix、burn
    workers/ai                # ASR、align、translate、TTS
    packages/schema           # Cue、Artifact、Job、EditPlan JSON Schema
    packages/ffmpeg           # 安全命令构造器与能力探测
    packages/qc               # 字幕、媒体、音频质量规则
    infra/                    # Postgres、MinIO、Redis、GPU worker

第一条链路：上传 MP4 -> probe -> 生成代理 -> ASR -> Cue JSON -> 人工改一条字幕 -> SRT -> FFmpeg 烧录 -> QC -> 下载。完成后再接翻译、剪辑 Agent 和 TTS。

## 12. 待确认决策

1. 首发形态：本地桌面、私有化部署还是云端 SaaS？
2. 首发语言和平台：中文/英文？抖音、B站、YouTube、课程或会议？
3. 是否允许云端 API，还是必须全本地/内网推理？
4. 最大文件时长、并发数、日处理小时数和 GPU 预算？
5. 配音先用预置音色，还是首期就做声音克隆？
6. 是否排除 GPL/AGPL/非商用模型，还是通过独立服务部署？

这些答案会直接决定工作流引擎、模型部署、存储设计和许可证边界。