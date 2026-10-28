# 需求与动机生成

模块编号 M11。把需求缺口、角色承诺、环境机会及长期目标转成可比较的任务候选，处理动机冲突、重复和资源约束。动机不直接执行动作。

## 数据及组件

Need 定义 type、target_range、current_gap、urgency_class、evidence 和 expires_at。Motive 包含 origin、goal_template、expected_benefit、estimated_cost、confidence、cooldown_key、status 和 explanation。状态为 proposed、eligible、suppressed、dispatched、resolved、expired；与任务状态保持分离。

组件为 NeedDetector、CandidateGenerator、UtilityEstimator、ConflictResolver、CooldownManager 和 IdlePolicy。输入为 SelfState、GoalProgress、SceneDelta、RoleSnapshot 及 AffectState；输出为候选目标和动机解释。

## 决策流程

采样需求 → 与进行中的同类动机合并 → 检查能力及硬约束 → 分优先级类 → 在类内比较预期收益、需求缺口、时间紧迫和成本 → 按预算选择候选 → 提交目标中心 → 依据结果更新需求。类别优先级不可被效用分数抵消。

例：低电量产生返回充电需求，正在充电时同一需求只更新证据；紧急危险到达则先按 P0 处理，充电目标保留并在状态稳定后重新评估。目标未完成不能仅因为任务曾被派发就标记需求已满足。

冷却以来源、需求类型和场景为键；危险升级及关键状态变化可解除冷却。动机失败不能无限生成相同任务，重试额度归属目标预算，超过阈值进入 blocked 并记录解除条件。

## 难点与实验

R11：不同需求量纲、短期收益与长期目标的平衡。先用词典序类别加类内线性效用，比较规则排序和效用方案的任务完成率、能量余量、饥饿率及无效切换次数。所有权重有版本，不能在线无约束自调。

空闲策略只允许预算内的记忆整理、日历检查和低成本联想，随机选择固定种子便于回放；随机字符本身不成为外部行动目标。自我复制等需求在初期没有注册执行能力，仅记录待研究需求。

## 验收与拆分

低电量抖动、相反目标、长期饥饿、冷却解除、持续失败和无输入运行分别建轨迹。验收要求同一需求不产生多个活动目标，目标完成证据能解除需求，预算耗尽时停止生成动作候选。

- M11-A：需求本体、冲突规则、效用基线及评估场景。
- M11-B：候选生命周期、合并、冷却、预算和目标提交。
- M11-C：多需求竞争及空闲长跑实验；学习效用无收益时保留规则基线。

研发工作包估算和依赖见 [模块工作包](../work-breakdown.md)，系统验收要求见 [跨模块协议与验证](../system-contracts.md)。
