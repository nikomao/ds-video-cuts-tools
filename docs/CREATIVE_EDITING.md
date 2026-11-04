# 视听剪辑工作流（Harness 内部 Agent）

这次升级目标是“有证据的剪辑助理”，不是一键电影导演。所有示例ID和路径均为占位符，必须使用工具实际返回值。

## 一、先看素材，再定节奏

    python3 -m video_agent run --project 工程目录 --task analyze_shots

可选参数：

    {"threshold":0.3,"min_shot_seconds":0.5,"max_shots":120}

输出分析JSON和真实逐镜JPEG。报告绑定源SHA256，frame_artifact_id引用注册图像，frame_path相对于工程目录。先用Harness read_image看真实画面；必要时通过现有video_storyboard/video_zoom观察动作。像素硬切可能漏掉同亮度切镜和渐变，代表帧不能证明人物身份、运动、情绪或故事意义。

黑场/冻结是技术信号，可能是正常片头、照片或叙事留白。工具不会删除原片。

## 二、候选建议不是最终叙事

    {"analysis":"实际ANALYSIS_ID","target_duration":30,"max_segment_seconds":6,"min_segment_seconds":1}

    python3 -m video_agent run --project 工程目录 --task suggest_edit --params suggest.json

建议按源顺序、不重复、尽量避开黑场并考虑静帧比例；不足目标长度就返回shortfall_seconds，不复制画面凑时长。它没有做语义/美学评分。当前Harness模型应结合用户目标、转写和实际帧，修订segments并写具体reason。

工作手册位于项目技能的references/EDITING_PLAYBOOK.md，包含访谈、纪实、宣传片三类思路和自审清单。

## 三、完整视听计划

plan.json示例：

    {
      "segments":[
        {"sourceIn":0,"sourceOut":4,"reason":"实际镜头证据支持的开场"},
        {"sourceIn":8,"sourceOut":12,"reason":"实际镜头证据支持的收束"}
      ],
      "analysis":"实际ANALYSIS_ID",
      "track":"实际TRACK_ID",
      "subtitle_mode":"soft",
      "transition":{"type":"fade","duration":0.4},
      "visual":{"look":"warm","fade_in":0.3,"fade_out":0.6,"letterbox":0},
      "music":{"artifact_id":"已授权导入的音乐ID","volume":0.15,"ducking":true,"loop":true,"offset":0,"fade_in":0.3,"fade_out":0.8},
      "intent":"用户的目标与风格要求"
    }

    python3 -m video_agent run --project 工程目录 --task plan --params plan.json

### 转场

- cut：硬切，duration必须0，默认且对白优先。
- fade：平滑交叉淡化，适合审慎的时间/场景过渡。
- dissolve：FFmpeg像素溶解，视觉上会有颗粒式交替，不等同平滑fade。
- fadeblack：经黑色过渡。
- wipeleft/wiperight：左右擦除；slideleft/slideright：左右滑动。

统一转场仍可用 transition；需要逐接点控制时用 transitions，数组长度必须等于接点数（镜头数-1），每项为 {type,duration}。例如两个接点先淡化再硬切：

    {"transitions":[{"type":"fade","duration":0.4},{"type":"cut","duration":0}]}

这适合“对白处硬切、场景之间淡化”。transition 与 transitions 不能同时给；省略 transitions 时 transition 对整个计划统一生效。

最多50个非cut接点/51镜，cut最多500镜。非cut时长不能大于其相邻任一镜头长度的一半，也不能超过FFmpeg xfade的60秒上限。切点和转场时长对齐帧格，输出片长=sum(镜头长度)-sum(各接点重叠)，以实际计划duration为准。单镜非cut不会产生转场，计划会告警。

非cut接点用 afade 交叉淡化；cut 接点默认原音与画面一起硬切，指定 J/L 时按音画错位处理。对白内容优先硬切，或先剪后在新工程配音。转场字幕按交叠中点分配所有权，保证时间不重叠；被裁短的对白和字幕仍须试听复核。

### J/L 音画错位

cut 接点可加 jcut/lcut（秒），对白仍优先硬切：

    {"transitions":[{"type":"cut","jcut":0.4},{"type":"cut","lcut":0.5}]}

- jcut：下一镜音频提前进入，画面仍在上一个镜头；扩展的是下一镜源片段之前的音频。
- lcut：上一镜音频在画面切走后继续；扩展的是上一镜源片段之后的音频。
- 两者可同时使用；只允许 cut 接点，扩展必须在源片范围内，超出则计划失败。
- 重叠音频直接求和，不做自动 ducking、限幅或电平自动化；必须试听对白清晰度与削波风险。

### 视觉预设与片头尾

look为natural/warm/cool/monochrome，都是固定基础滤镜，不分析肤色/曝光，不匹配镜头色彩，不进行HDR流程或专业LUT校准。fade_in/fade_out作用于画面及现有音轨，不改变片长；各不超过片长一半且最多5秒。

letterbox为上下各遮盖的画面比例(0..0.2)，默认0。它不是自动宽银幕重新构图，可能遮主体/已有字幕；黑边本身不等于电影感。新字幕在视觉处理之后封装/烧录。

## 四、导入有使用权的本地音乐

    {"file":"/用户指定/音乐.wav","rights_confirmed":true,"license_note":"用户提供的许可说明"}

    python3 -m video_agent run --project 工程目录 --task import_music --params music.json

rights_confirmed只记录用户确认，不是法律鉴定。禁止擅自联网下载音乐或假定可商用。导入会复制并校验哈希；视频文件不能当音乐导入，允许音频文件中的附带封面图。plan.music只接受成功授权导入任务产生的音频artifact，不能随便引用未确认音轨。

volume为0..1；短音乐loop=true循环，false则补静音；offset必须在音乐长度内。输出裁齐实际片长，限幅避免溢出。ducking由源音轨能量控制，不是语音检测/人声分离；音乐和噪声也能触发。无源音轨时仅播放音乐。必须试听音乐是否掩盖对白。

## 五、机械审阅→预览→用户确认

    {"plan":"实际PLAN_ID"}

    python3 -m video_agent run --project 工程目录 --task review_plan --params render.json
    python3 -m video_agent run --project 工程目录 --task preview --params render.json

审阅包括词/句内切点、明显短/长镜头、源片段重复、黑场/冻结交集、非cut对白交叠、黑边遮盖风险和能量压低局限。告警不是审美分数；无词级时间戳就只提示句内切点。

用户确认后才approve和render。转场、音乐、视觉参数全部写入计划；改变任何一项都生成新计划并需重新确认，不能沿用旧审批。最终质检包含完整解码与时长检查，不代替主观审片。

## 六、真实可重现演示

    python3 -m scripts.creative_demo --output .video-agent/一个全新示例目录

只生成测试图、色条、纯色和正弦音频，执行真实CLI镜头分析、候选建议、音乐导入、转场、视觉预设、软字幕、审阅、预览、测试素材审批与渲染。它是功能验证，不是实拍电影效果案例。示例对比源片9秒，两个0.6秒转场后计划7.8秒；AAC封装可产生毫秒级容器尾差。

## 七、当前水平与下一步

已具备有素材证据、可解释建议、受控视听工具和人工门禁的剪辑助理基础。J/L 已在 cut 接点支持，但没有自动对白 ducking、自动故事导演、多机位动作匹配、主体跟踪、口型同步、自动音乐选取/节拍踩点、专业调色或复杂合成。专业宣传片能否用，必须拿实际素材做审美验收；合成测试通过只证明工具和时序正确。
