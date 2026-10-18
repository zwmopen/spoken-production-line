# 团建口述生产线
2026.10.18-preview.1 · 可分享源码预览版 · Windows优先

将“补原料”“出成品”“继续”等要求转成可追溯的图文生产流程。包含网页GPT-A/B独立队列、Codex/Cockpit API入口、提交去重、下载验收、三版文案、归档、台账/面板和飞书机器人卡片。

## 当前能力
这是AI协作技能和脚本系统，不是安装完即无人值守的软件。本机已有A/B及Cockpit完整归档和通知回读；跨机器安装、全部适配器自动闭环、连续24小时运行尚未验证。pipeline明确返回fullyAutomated=false和缺失适配器。20–35套/天是目标，尚非保证。
语义页映射、视觉判断、异常分析、新素材筛选仍需AI参与；计数、哈希、去重、断点、锁、台账与通知由代码承担。

## 安装
需要Python 3.10+、自己的素材/真实模板、已登录且可CDP连接的浏览器。平台与模型资格自行配置。包内没有素材图片、模板图片、密钥、登录态或原作者生产数据。
Windows双击install.cmd，或依次运行：

    python -m venv .venv
    .venv/Scripts/python -m pip install -r requirements.txt
    .venv/Scripts/python configure.py
    .venv/Scripts/python doctor.py

修改config/config.json为自己的路径，模板注册表填写真实localPath和conversationLinks。configure只创建空目录和空队列，不联系平台。
查看和准备：

    python skills/spoken-production-line/scripts/pipeline.py status --task-root ./data/tasks
    python skills/spoken-production-line/scripts/prepare.py --config config/config.json --out data/tasks/prepared-queue.json
    python skills/spoken-production-line/scripts/pipeline.py auto --task-root ./data/tasks --account A

prepare当前筛选“精·方案”模板和秋季路线，其他业务需调整策略；生成待分配队列后须核绑定并分入queue-A/B。auto默认输出决定，配置及授权完成后再加--execute。正常批量走batch_launch_queue.py，start_ready.py为单页补页入口。
浏览器须自行提供CDP，本包不含原作者内容生产App、Library Bridge和浏览器启动器。安装自检不等于真实生产验收。

## 包内结构
- skills/spoken-production-line/scripts：素材准备、上传、计划回读、批量生成、原图下载、文案保存、验收、元数据、台账、面板、锁和调度。
- skills/spoken-production-line/references：提示词、元数据、归档、恢复/去重、额度、快捷命令及通知SOP。历史条款以本版SKILL和docs当前流程为准。
- docs/OPERATION.md：完整流程与故障恢复。
- docs/CODEX_AUTOMATION.md：Goal、15分钟巡检、日报与唤醒的职责及配置。
- config/config.example.json：机器配置示例。
- configure.py、doctor.py、install.cmd：安全初始化和依赖检查。
- RELEASE-MANIFEST.json、VALIDATION.json：文件SHA256和实际发布检查。

## 持久调度器
production_supervisor.py读取TASK_ROOT/production-supervisor.json，每个账号需配置discover/reconcile/submit/download/verify/copy/archive/notify适配器命令；stdin/stdout JSON协议见脚本顶部。空配置返回UNCONFIGURED。已有脚本不是所有适配器自动串好，需由使用端接线。
运行一次检查：

    python skills/spoken-production-line/scripts/production_supervisor.py --task-root ./data/tasks --once

适配器必须尊重相同账号锁及提交意图，未知发送只能reconcile。后续步骤成功才推进，整套完成再领取下一套。

## Cockpit API（可选）
设置COCKPIT_API_KEY、COCKPIT_BASE_URL（默认http://localhost:51612/v1）、COCKPIT_IMAGE_MODEL、SPL_TASK_ROOT。
任务根目录建立自己的Cockpit-生产断点.json，含workId、stage=READY、references、prompt和activePage，再运行scripts/cockpit_line.py。
这是图片调用和回收入口；文案/验收/归档仍需后续闭环。模型列表成功不证明生图，聚合对话数不代表独立额度。HTTP响应及平台恢复时间优先。

## 飞书（可选）
环境变量FEISHU_APP_ID、FEISHU_APP_SECRET，机器人加入自己的群并授予消息写入/读取权限。统一用production_cards.py的completed/daily/deliver，凭据不落包。
未配置通知仍可本地检查与准备，但不要执行notify阶段。发布自检不发送测试消息。

## 分享与维护
先用两套自有作品完整闭环，再观察24小时。每段无进展保存原因、证据与下一触发。不能用定时器在线证明产出。
保留同一闭环清单，增量更新。不要把后来填入的config/config.json、data、.env重新打包。所有个人配置与账号信息由使用者自己提供。
