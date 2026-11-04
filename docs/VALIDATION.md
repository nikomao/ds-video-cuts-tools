# 开发验收记录（初版）

> 本文记录初版100项测试。当前v0.4.0的166项测试、视听升级和最新限制见 [升级验收记录](UPGRADE_VALIDATION.md)。

日期：2026-11-03。交付形态：DeepSeek Harness 内部 Agent / 原生 Skill / 本地工具；没有新增 Web、App、API 服务或常驻后台。

## 已执行验证

1. 当前 Harness 技能目录自动出现 video-editing；调用 skill(name="video-editing") 成功，provider=filesystem，resourceBase 指向项目 .dsh/skills/video-editing。
2. 执行 python3 -m unittest discover -s tests -v：**100 tests，OK，退出码0**。包含真实 FFmpeg 合成素材测试、字幕格式回归、任务账本/重试/审批/缓存和模型适配器mock。
3. python3 -m video_agent --help 成功；python3 -m compileall -q video_agent scripts 成功。
4. 解析并静态核验可选 Harness bundle YAML 成功；没有安装到用户profile，没有声称专用preset已挂载。
5. 最终真实CLI冒烟：python3 -m scripts.smoke_test --output .video-agent/smoke-final-20261103 --say，退出码0。
   - 14次实际CLI调用全部退出码0。
   - 使用4秒FFmpeg合成测试画面与正弦音频，字幕明确标注“示例数据”。
   - 实际执行探测、音频抽取、字幕导入、固定译文回填、字幕QC、SRT导出、macOS say预置音色合成、配音混流、计划、预览、测试素材审批、软字幕导出、完整解码与时长QC、成功缓存复用。
   - 最终render.status=completed，QC passed=true，cache_hit=true。
   - 固定译文回填用于验证接口，不代表调用了ASR或机器翻译模型。

实际JSON报告：[smoke report](../.video-agent/smoke-final-20261103/report.json)。报告中的输出路径相对于该工程的project目录。

## 已复现并修复

- SRT导出字符串cue ID导致FFmpeg拒绝解析：改为标准1..N数字序号，内部JSON保留真实ID。
- soft字幕被代理预览丢弃：代理显式保留软字幕流。
- 多段非帧对齐剪辑累计时长偏移：计划/渲染共享帧格，字幕沿统一时间表映射；VFR规范到平均帧率CFR。
- 初始化mkdir失败后job卡running：初始化纳入异常入账。
- 缓存遗漏执行模型配置：记录非敏感执行配置、实现代码和显式本地模型内容指纹；重试变更配置时拒绝静默执行。
- 配音缓存只验manifest：递归验证注册音频依赖。
- 剪后时间轴字幕被再次当源字幕映射：明确拒绝，要求新工程显式导入。
- 配音/字幕时间表不一致：比对cue ID与时序，允许不同语言文本。
- ASR缺词级时间戳：启用真实word_timestamps，输出词级时间与概率，微小抖动裁剪，严重异常失败。

## 没有执行/尚需配置

- **真实ASR推理未执行**：faster-whisper可导入，默认缓存未找到模型权重；没有下载模型。对应适配器和词级时间戳使用mock测试。
- **Argos、外部LLM翻译和Piper推理未执行**：本机Argos/Piper未安装，没有联网或读取Harness凭据。适配器用mock测试；字幕翻译可由当前Harness对话模型生成后严格回填。
- **真实硬字幕烧录未执行**：本机默认FFmpeg8.1未带libass。已验证缺依赖时明确失败，软字幕正常。需用户提供带libass的FFmpeg或授权安装后再验收。
- **专用可选preset未安装**：项目Skill已可用；bundle只完成接口核实与静态校验，安装/重启需用户决定。
- 说话人分离、强制对齐模型、智能主体跟踪、B-roll、口型同步、OTIO和多租户平台未实现，不以名称/空接口冒充。
- 没有处理用户真实素材，也没有对翻译语义、配音授权、视觉听感或版权作自动通过断言。

## 仍有的边界

强制SIGKILL或断电可能保留running，当前不自动抢占；文件哈希和审批是防误改/工作流记录，不是防恶意本地管理员的认证系统。实际商用应固定依赖、模型权重、声线/字体许可并使用用户素材进行逐场景人工验收。
