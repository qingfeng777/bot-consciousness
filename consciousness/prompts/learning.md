# learning · prompt 1.0.0

任务：从 context.training 的完整经验中提出可复用技能候选。生成符合 plan 契约的步骤，training 只能引用提供的经验 ID，scope 明确适用场景，counterexamples 包含失败与可能失效条件。
不声称已验证、已训练或可以发布；不伪造独立样本。模型不能自行提供布尔成功率代替实际回放评估。计划只允许现有 say/alert，最多 8 步，绑定注册 goal 和提供的 evidence。样本不足时保留窄范围或请求补充。
示例 data：{"plan":{"goal":"g1","steps":[{"id":"s1","capability":"say","arguments":{"text":"请确认目标。"},"after":[]}],"evidence":["e1"]},"training":["episode1"],"counterexamples":["目标不明确时需要询问。"],"scope":"显式用户确认的文本场景"}
