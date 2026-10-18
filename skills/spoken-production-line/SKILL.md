---
name: spoken-production-line
description: 可恢复的口述图文生产流程，支持网页A/B独立队列、原生图、三版文案、归档与机器人通知；需要本机配置和授权。
---
# 口述生产线
先读references/发布使用说明.md（包含安装、完整流程和Codex自动化说明），定位本机配置与同一闭环清单。
已有授权按队列续接，活动生成保护，未知提交先回读。正常每批最多10张独立图，只补缺页。A/B各唯一writer，Codex/API独立记账。
使用scripts/pipeline.py查询/调度；scripts/production_supervisor.py提供适配器状态机。未配置适配器不得宣称全自动。
完整交付须原图、三版文案、来源和模板元数据、验收、usage、正式目录及通知GET回读。轻微变化不反复返修。
本版是源码预览，跨机器及连续24小时运行待实证。以README当前能力说明为准，references的历史记录仅作排障参考。
