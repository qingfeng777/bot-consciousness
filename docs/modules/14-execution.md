# 执行器输出与反馈核对

模块编号 M14。唯一可以向文本、语音或机器人能力发送外部动作的模块。把“提交请求”“执行开始”“效果完成”和“无法判断”分开记录。

## 能力契约

CapabilityDescriptor 包含 name、version、argument_schema、preconditions、expected_effect、timeout、cancellation_mode、query_support 和 idempotency_support。适配器提供 prepare、start、cancel、query 四类操作；不能停止的动作必须声明其最小不可中断区间，若不满足设备验收则不接入。

ActionAttempt 包含 action_id、task_id、step_id、generation、idempotency_key、driver_receipt、status 和 outcome_evidence。停止请求必须指向具体动作，StopReceipt 含 action_id、原 generation、已停止标志及采样时间。仅“收到取消请求”不能当作已停止。

## 执行协议

验证能力和当前前置条件 → 持久化 prepared 意图 → 占用槽位并记录 sending → 向驱动发送 → 保存 accepted 回执 → 等待 running 或最终结果 → 用反馈观察核对效果 → 写终态并释放槽位。prepare 不产生外部效果。

数据库与硬件之间不能假设原子事务。发送后、回执落库前崩溃时，恢复流程按幂等键或驱动状态查询核对。驱动无幂等且无查询时进入 outcome_unknown；不能为追求可用性自动重试。

重复或晚到结果先匹配动作、代次和驱动序列号，再做幂等状态归约；已记录成功不能被旧 running 回执回退。外部结果可能先于 start 响应，需保存并关联，不按 HTTP 请求完成先后假定真实顺序。

## 输出与反馈

文本输出与语音输出也占用统一普通动作槽位；第一版不能边说话边移动，这是对原架构约束的明确实现。若未来允许多资源并发，需要独立设计资源冲突矩阵，不能直接放开线程数。

停止和硬件急停属于控制当前动作的协议消息，不作为第二个普通动作占槽位。反馈区分 transport_success、action_complete 和 goal_satisfied；例如收到充电命令并不等于电量恢复。自身语音经麦克风回流时标记输出关联，不能盲目全部丢弃同时间的人声。

## 验收与拆分

模拟器应支持启动延迟、分步效果、不能立即停止、重复回执和状态查询。注入发送前后崩溃、丢回执、错误成功、连续 cancel 和断连；验证串行不变量和未知状态冻结。硬件停止时限由设备规格给出，不能沿用仿真毫秒数。

- M14-A：能力注册、动作与停止回执协议、故障模拟器规范。
- M14-B：串行动作槽位、日志、幂等核对、文本语音模拟输出。
- M14-C：发送窗口崩溃、停止竞态和反馈真实性验收；真实驱动另列硬件任务。

研发工作包估算和依赖见 [模块工作包](../work-breakdown.md)，系统验收要求见 [跨模块协议与验证](../system-contracts.md)。
