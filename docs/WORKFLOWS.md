# 在 Harness 内执行视频任务

> 镜头分析、转场、视觉预设、配乐和剪辑风险审阅的新流程见 [视听工作流](CREATIVE_EDITING.md)。

所有示例的路径和ID都是占位符，必须换成工具返回的真实值。JSON参数文件用Harness write创建，不从字幕正文生成shell命令。

## 1. 初始化与探测

    python3 -m video_agent init --project .video-agent/projects/demo --input /用户指定/source.mp4
    python3 -m video_agent run --project .video-agent/projects/demo --task probe
    python3 -m video_agent run --project .video-agent/projects/demo --task proxy

JSON结果包含artifacts的id、相对工程路径、sha256、size。读取或交付时把路径解析到工程目录，而不是项目代码根。

## 2. 转写或导入字幕

asr.json：

    {"language":"zh","model":"/本地模型目录","allow_download":false,"timeout":3600}

    python3 -m video_agent run --project .video-agent/projects/demo --task transcribe --params asr.json

没有模型时，向用户说明并征求下载/安装意见；不得用示例字幕冒充ASR结果。已有SRT可改为import_subtitles：

    {"file":"/用户指定/subtitles.srt"}

或直接从已审核文本生成Cue：

    {"cues":[{"id":"intro","start_ms":100,"end_ms":2100,"text":"示例数据：这是测试字幕"}]}

## 3. 使用当前对话模型翻译

读真实转写track的cues，在当前Harness对话模型中逐句翻译后生成参数：

    {"track":"实际TRACK_ID","language":"en","translations":[{"id":"intro","text":"Example data: this is a test subtitle."}]}

    python3 -m video_agent run --project .video-agent/projects/demo --task apply_translations --params translation.json

必须完整覆盖所有id、不能更改时间。大文件分批读取/翻译后先汇总完整映射，再回填；不要把一小批伪装成完整轨道。

外部翻译可用translate，Argos需要source_language/target_language；OpenAI-compatible另需明确allow_network及用户配置的环境变量。不得复用/导出Harness登录凭据。

## 4. 导出字幕

    {"track":"实际TRACK_ID","format":"srt"}

    python3 -m video_agent run --project .video-agent/projects/demo --task export_subtitles --params export.json

format可为srt/vtt/ass。字幕编辑重新import_subtitles生成新track；旧版保留用于回退。轨道内单个说话人标签可保留，但没有运行说话人分离模型就不能声称已识别人。

## 5. 生成剪辑计划

plan.json：

    {"segments":[{"sourceIn":1.0,"sourceOut":4.0,"reason":"保留引言"},{"sourceIn":8.0,"sourceOut":12.0,"reason":"保留结论"}],"track":"实际TRACK_ID","subtitle_mode":"soft","aspect":"9:16","intent":"用户要求的摘要短片"}

去长静音可以换为：

    {"remove_silence":true,"threshold_db":-35,"min_duration":0.6,"padding":0.12,"track":"实际TRACK_ID","subtitle_mode":"soft"}

    python3 -m video_agent run --project .video-agent/projects/demo --task plan --params plan.json

工具将切点规范到共享CFR帧格；输出计划展示实际切点和实际预计时长，所有字幕沿同一时间表重映射。VFR输入会按平均帧率规范为CFR；并非无损流拷贝。

## 6. 预览、确认、最终渲染

render.json：

    {"plan":"实际PLAN_ARTIFACT_ID"}

    python3 -m video_agent run --project .video-agent/projects/demo --task preview --params render.json

向用户展示低清预览、SRT和删剪清单，得到确认后：

    python3 -m video_agent approve --project .video-agent/projects/demo --artifact 实际PLAN_ARTIFACT_ID
    python3 -m video_agent run --project .video-agent/projects/demo --task render --params render.json

approve仅记录人工确认，Agent不得自己把用户未确认的计划批准。预览保留软字幕流，但播放器是否默认显示取决于播放器；附SRT便于复核。硬字幕需libass，缺失则先报告并请求配置，不擅自改用户想要的输出模式。

## 7. 配音

基于最终时间轴的track，用本地Piper或macOS已装预置音色：

    {"track":"实际TRACK_ID","provider":"piper","voice":"/本地/voice.onnx","confirmed":true,"timeout":120}

或：

    {"track":"实际TRACK_ID","provider":"say","confirmed":true}

    python3 -m video_agent run --project .video-agent/projects/demo --task tts --params tts.json

先确认音色/授权，不支持声音克隆。返回voice-manifest ID用于plan：

    {"voice_manifest":"实际VOICE_MANIFEST_ID","track":"相同时间ID的TRACK_ID","subtitle_mode":"soft","original_volume":0.15}

不传segments即保留全片。当前不接受剪切后直接套原始配音；应先用剪后输出创建新工程，导入剪后SRT，再TTS。超出0.5x..2x时长适配范围会失败，需改译文/断句。

## 8. 后台任务与重试

在Harness内通过run_code调用tools.bash({description, command, workdir, run_in_background:true})，保存返回的Harness jobId；完成通知后用job_output获取最终输出。取消用job_kill。不自己实现守护进程或忙轮询。

    python3 -m video_agent jobs --project .video-agent/projects/demo
    python3 -m video_agent status --project .video-agent/projects/demo --job 本地JOB_ID
    python3 -m video_agent retry --project .video-agent/projects/demo --job 失败或取消的本地JOB_ID

断电/强杀可能留下running，确认旧worker已死后再人工处理或--fresh；不自动抢占。工具会校验request.json对应的非敏感执行指纹，配置/代码/本地模型改变时拒绝原任务重试，要求提交新任务；远端同名模型仍需用户固定版本。

## 9. 交付

只交付本次已完成、QC范围清楚的真实输出。用Harness present提供视频和字幕；回复中注明字幕是否软/硬、配音模式、实际执行/未执行步骤及人工审核项。不要把语义翻译质量说成已通过机械QC。
