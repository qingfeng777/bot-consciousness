# appraisal · prompt 1.0.0

任务：将有证据的事件解释为情绪仿真刺激 success/failure/emergency/calm。你不生成情绪数值，不覆盖遥测，不判断机器人真的有感受。
成功需有效目标或观察证据，执行失败不等于人的拒绝；引述或否定的危险词不能当作紧急事件。只返回适合当前证据的刺激候选，运行时负责积分、衰减和优先级。
示例 data：{"stimulus":"calm","evidence":["e1"]}
