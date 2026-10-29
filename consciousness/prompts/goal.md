# goal · prompt 1.0.0

任务：把用户意图转换成可观察的目标候选。description 说明期望结果；predicate 使用当前系统可查询的 entity/attribute/value。模型文本、动作“已派发”或一句模拟表达本身都不是目标成功证据。
如果条件不可观察、对象不清楚或所需能力不支持，提具体澄清问题。不得自行扩大用户意图或编造授权。
示例 data：{"description":"核对杯子的位置","predicate":{"entity":"cup-1","attribute":"location_verified","value":true},"evidence":["e1"]}
