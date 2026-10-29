# memory_extract · prompt 1.0.0

任务：从证据提取主体—属性—值候选，最多 16 项。只选明确、有用、可回溯的内容；用 uncertainty 区分 observed、reported、hypothesis。用户声称的事实通常为 reported，推断为 hypothesis。
保留否定语义，不将“昨天在 A”写为“现在在 A”。当前 Schema 不包含有效时间字段；遇到关键时间、单位或条件无法用属性和值准确表达时请求澄清，不悄悄丢弃。不要用模型先验补充输入里没有的属性。
示例 data：{"claims":[{"entity":"cup-1","attribute":"reported_location","value":"desk","evidence":["e1"],"uncertainty":"reported"}]}
