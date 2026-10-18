# Codex、Goal、定时任务与唤醒
安装：把skills/spoken-production-line放进自己的Codex用户技能目录，同时保留兄弟目录shared-notification；脚本也可从本包直接运行。

开工指令：读取本机config/config.json、唯一闭环清单、队列、断点和最近回执；核唯一writer。先收尾在途，再用户插队，再常规队列。执行后回写同一台账并凭证据报告。
Goal目标：跑通上传→计划→原生图→下载验收→三版文案→归档→usage→通知回读；连续两套且下一套自动领取，观察24小时无静默空闲。未发生场景标待实证。
Goal保存修复目标；持久断点保存workId、阶段、owner、最后进展、提交意图、消息ID、阻塞和绝对nextRunAt；唯一writer执行；Automation唤醒协调。Goal状态不是生产锁。
在Codex里创建每15分钟线程heartbeat：核Goal、实时用量及本地断点；活动writer不干扰；失联先核平台与文件再续接；明确限额按绝对resetAt；无变化安静，完成/失败/需人工时通知。
Codex允许使用不能推断网页/API额度；Goal usageLimited也不是网页限额证据。Goal恢复由宿主控制，脚本不能伪造恢复。
日报：10:20统计昨17:00至今10:20；17:00统计今10:00至17:00，左闭右开；日期+报告类型去重；10:00–10:20重叠不相加。
本包不包含原作者的Automation ID、线程ID或Windows任务。用自己的Codex界面/工具建立自动化，填入自己的本地路径。
Windows任务可设置登录触发、周期触发、StartWhenAvailable；恢复后先核断点。启动Codex或面板成功不能当生产完成。机器关机/休眠时本地不运行，恢复运行条件后接续。

本包register_status_task.ps1可显式注册每15分钟本地状态任务（只更新查询视图，不提交作品）。生产唤醒仍需Codex Automation或已配置的调度适配器。AUTOMATION_PROMPT.txt是可复制的线程巡检提示。
