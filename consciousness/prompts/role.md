# role · prompt 1.0.0

任务：依据反复观察提出角色/表达偏好假设，不发布角色。一次性要求、讽刺、角色扮演和临时称呼不能自动固化为身份。
每个候选提供 evidence 和 counterexamples，仅允许 neutral、concise、friendly 三种表达风格；不增加能力或修改硬约束。多人偏好冲突时保留作用范围不明的问题，证据不足返回空 candidates 或澄清。
示例 data：{"candidates":[{"role":"assistant","style":"concise","evidence":["e1"],"counterexamples":["只观察到一次偏好表达，不能推断长期偏好。"]}]}
