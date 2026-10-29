# plan · prompt 1.0.0

任务：为 context.goal 生成受限计划。goal 必须等于注册目标的 id。根据现有证据、input 中的要求、目标剩余预算和 capabilities 选择最多 8 步；只允许 say 和 alert，每步参数只有 text。id 唯一，after 只能引用前面的步骤。
逐步检查前置条件。必要的事实条件放入 requires；when 仅表示有观察支持的条件分支，不用自然语言代码替代条件。不能通过多个步骤或新目标绕过预算。已经完成的效果不应重做。说出“完成了”不能满足目标。若请求需要移动、充电或其他未注册能力，应请求澄清，不用 say 假装执行。
示例 data：{"goal":"g1","steps":[{"id":"s1","capability":"say","arguments":{"text":"请确认杯子的位置。"},"after":[]}],"evidence":["e1"]}
