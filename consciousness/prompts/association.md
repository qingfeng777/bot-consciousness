# association · prompt 1.0.0

任务：从给定证据或检索缺口生成最多 5 个可验证的问题型假设。每项 question 应说明要验证什么，evidence 只引用真实种子。
假设不是事实，不得输出肯定结论或行动命令；不要循环改写同一个问题来填满数量。选择与当前目标有关、低成本能验证的方向，没有有用方向可返回空 hypotheses。
示例 data：{"hypotheses":[{"question":"是否需要新的观察来确认杯子仍在桌上？","evidence":["e1"]}]}
