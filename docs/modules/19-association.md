# 联想想象与离线探索

模块编号 M19。从已有语义、记忆和未解决目标提出有价值的假设，服务检索和规划，不把生成内容直接提升为事实。

## 输入输出与数据

AssociationRequest 包含 seed_ids、purpose、context_snapshot、novelty_budget 和 deadline。Hypothesis 包含 statement、seed_refs、relation_type、novelty_score、plausibility、verification_method、expires_at 和 status。状态为 proposed、selected、verified、rejected、expired。

组件包括 SeedSelector、CandidateGenerator、DuplicateFilter、ValueScorer 和 VerificationRouter。关系类型区分类比、补全问题、原因假设与行动候选，不能混在同一事实索引中。

## 处理流程

选择种子 → 生成有限候选 → 去重与查证是否已有结论 → 检查逻辑与资源约束 → 按潜在信息增益、任务相关性和验证成本排序 → 进入工作记忆假设槽位或提交验证目标。第一版每次最多 5 个候选、最多两层展开，避免自我生成内容不断再作种子。

联想仅可建议调查，调查动作仍需目标与调度模块批准。验证通过后由 M18 和 M05 将证据写为事实；Hypothesis 自身不直接改类型绕过证据校验。

## 难点与实验

R19：有用新颖性与随机噪声的区分。以检索失败问题及未解决目标作为种子，比较不联想、随机记忆和受约束联想，记录新增可验证假设比例、带来的任务成功改善和每个有效假设成本。

如果没有稳定收益则保留手动触发或关闭自动空闲联想，不能用生成数量作为成功指标。生成模型给出的 plausibility 只是排序特征，非事实可靠度。

## 验收与拆分

验证预算耗尽、循环种子、过时上下文、中断和伪造证据。联想必须可抢占、假设检索与事实检索默认隔离、所有候选可追溯种子及版本。

- M19-A：假设契约、有效性定义及对照实验集。
- M19-B：种子选择、预算、去重、假设存储和验证路由。
- M19-C：效用消融、循环抑制及空闲资源成本评估。

研发工作包估算和依赖见 [模块工作包](../work-breakdown.md)，系统验收要求见 [跨模块协议与验证](../system-contracts.md)。
