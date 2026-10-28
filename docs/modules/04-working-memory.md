# 工作记忆与上下文装配

模块编号 M04。为一次决策提供有界、一致的上下文快照，避免简单拼接最近聊天导致目标、约束和证据丢失。

## 数据与内部组件

WorkingItem 包含 item_id、kind、source_ids、scene_id、importance、expires_at、pinned、version 和 token_estimate。kind 区分当前目标、执行状态、观察、检索证据、角色摘要、假设与失败信息。ContextSnapshot 包含 snapshot_id、focus_version、state_version、memory_revision、items 和 budget_usage。

内部组件为 SlotStore、BudgetAllocator、EvidenceSelector、SummaryBuilder 和 SnapshotBuilder。只有执行约束、当前步骤和关键设备状态允许强制保留；普通对话与联想不能自行提升为 pinned。

## 装配和淘汰

确定模型输入总预算 → 扣除系统约束、能力声明和输出预留 → 分配观察、历史和证据配额 → 按相关性选择 → 有证据地摘要 → 原子生成快照。条数上限只用于内存保护，真正装配还要受字节与 token 双预算约束。

淘汰先移除过期和冗余，再压缩低优先级历史；摘要必须保存原始证据 ID、覆盖范围及遗漏标记。预算不足以容纳硬约束时返回 CONTEXT_OVERFLOW，缩小任务或请求澄清，不截断约束后继续执行。

快照创建后不原地修改。状态变化发布新版本；执行前只重检与动作相关的前置条件，避免无关聊天变化使所有计划无休止重做。

## 难点与验证

R04：摘要是否丢失否定、时序、数量和失败条件。建立长会话样本，比较完整上下文、滑动窗口和证据摘要，测量关键事实保留率及计划结果差异；摘要不能作为唯一事实来源。

压力场景含超长消息、多个冲突证据、证据已删除、嵌套引用和持续场景切换。验收要求快照可重复构建、预算不超限、关键约束不被淘汰，来源文本不能改变 pinned 权限。

## 开发拆分

- M04-A：槽位、预算及摘要保真样本。
- M04-B：装配、淘汰、摘要来源和不可变快照实现。
- M04-C：长上下文回归与压缩消融实验；摘要不达标时回退提取式摘要。

研发工作包估算和依赖见 [模块工作包](../work-breakdown.md)，系统验收要求见 [跨模块协议与验证](../system-contracts.md)。
