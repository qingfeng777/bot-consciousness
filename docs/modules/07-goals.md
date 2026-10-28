# 目标与长期任务中心

模块编号 M07。管理“需要达成的结果”、依赖及周期事件；运行任务何时执行由 M08 决定。

## 数据和状态

Goal 包含 owner、description、success_predicate、deadline、priority_class、dependencies、budget、status 和 recurrence。状态为 proposed、active、blocked、satisfied、failed、cancelled、expired；完成必须有 GoalEvidence，不因计划执行完就自动 satisfied。

内部组件包括 GoalRegistry、DependencyGraph、RecurrenceEngine、ProgressEvaluator 和 BudgetLedger。目标依赖图必须无环；目标可拆为子目标，但父目标的成功条件要明确 all、any 或加权条件，不能默认所有子任务结束即成功。

## 处理流程

受理目标 → 检查可观测完成条件与能力 → 分解或澄清 → 解析依赖 → 发布可执行目标 → 接收执行及观察证据 → 更新进度 → 满足条件后完成。blocked 必须记录阻塞对象及解除事件；失败子目标按显式策略触发重规划或父目标失败。

周期任务使用 rule_id 与 occurrence_time 作为唯一键。时区属于规则的一部分，重启后补跑策略为 skip、run_once 或 bounded_catchup；默认不把所有错过的周期同时补跑。夏令时重复时刻及不存在时刻需要确定规则，即使首个设备运行在单一时区。

预算包含总动作步数、模型成本、截止时间及重试次数，子目标从父预算分配，不能通过无限分解获得无限预算。

## 难点与验收

R07：自然语言目标到可检测完成条件。基线为显式模板与澄清问句；模型生成的条件必须绑定可用观察，无法观测的目标标记需人工确认。

验收覆盖环依赖、互相冲突的目标、截止时间已过、连续失败、跨重启定时器及成功证据撤销。目标状态更新有版本，重复反馈不会重复增加进度或预算。

## 开发拆分

- M07-A：目标类型、成功条件表达和定时规则语义。
- M07-B：依赖图、持久化定时器、预算及进度评估。
- M07-C：重启补跑、时区、失败传播和自然语言目标澄清验证。

研发工作包估算和依赖见 [模块工作包](../work-breakdown.md)，系统验收要求见 [跨模块协议与验证](../system-contracts.md)。
