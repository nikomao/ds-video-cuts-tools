# 可选 Harness 视频 Agent preset bundle

主交付已通过项目AGENTS和原生Skill接入，不需要这个可选preset才能使用功能。本目录仅提供可选的“视频智能剪辑”Agent persona选择项，没有前端、服务、监听端口或模型/沙箱覆盖。

## 配置依据与状态

依据当前安装包0.2.1-rc.5核对：

- @deepseek-ai/dsh-agent-preset 的 config 使用 id/name/description/order/plugins。
- @deepseek-ai/dsh-persona 使用 prefix/includeRuntimeContext，不是旧版text。
- package.json 的 dsh.bundle.patch 指向 cordis.patch.yml，补丁使用顶层 insert。
- host默认已经提供bash、fs、skill和项目指令读取，因此此preset只声明persona，不重建这些服务。

已生成配置并做静态校验，**未修改用户profile、未安装bundle、未验证真实挂载**。主视频Skill在当前会话已经被filesystem provider发现并成功加载。

## 可选安装

在现有Harness插件管理界面中，如果当前版本支持本地路径安装，使用本目录的绝对路径；否则使用Desktop自带且支持管理desktop profile的CLI：

    dsh plugin --profile desktop add /绝对路径/vidoes-cuts-tools/harness

注意这只是已核实的命令语法，不表示任意PATH上的dsh可管理Desktop。Desktop文档要求先初始化，再完全退出应用后使用其installed carrier管理profile；不要在本任务里自动关闭用户应用。不能用任意npm CLI绕过Desktop管理限制。

安装后按提示重启/创建新会话，检查Agent选择列表是否出现“视频智能剪辑”，并将会话工作目录设为工具项目根，运行doctor和加载video-editing技能完成验收。preset不捆绑Python代码；必须保留工具项目及其技能目录。

如果安装失败，继续使用项目级Skill即可；不要直接编辑app.asar或用户凭据，也不要启动替代Web服务器。
