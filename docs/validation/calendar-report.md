# 0.4.0 周期调度、条件技能与全文检索验证

日期：2026-10-28。环境：Linux / Python 3.10.12，SQLite FTS5。本版在 0.3.0 之后继续关闭可在本机验证的软件缺口；仍不是完整项目或全部研究工作包验收。

## 自动测试

`python3 -m unittest discover -s tests -v`：**98 项测试全部通过**，本次耗时 16.516 秒。新增 15 项验证：

- 每日目标重启去重、各轮独立完成条件、三种补跑策略、墙钟回退。
- 夏令时不存在时刻跳过、重复时刻选择一个 fold；规则禁用和非法时区拒绝。
- 已创建目标但未写入日历检查点时，恢复仍不重复创建。
- 技能未知/争议前置条件阻断、假分支不消耗预算、步骤之间重新检查条件、拒绝任意代码条件。
- 全文排序的有效时间过滤、争议保留、裁决后过滤、知识修订使索引落后、索引重建失败保留事实。
- CLI 整场景、模块状态导出、条件分支、日历状态及全文索引的备份恢复。

## 本机持久化集成

```sh
python3 -m consciousness simulate examples/calendar-skills.jsonl --data-dir local-data/calendar-v04-demo
python3 -m consciousness inspect --data-dir local-data/calendar-v04-demo
python3 -m consciousness.evaluation --scenario examples/calendar-skills.jsonl --runs 100 --output calendar-evaluation.json
```

原始结果：[calendar-evaluation.json](calendar-evaluation.json)。场景含 17 个事件，覆盖一个条件技能、两个每日目标及一次全文检索。两个周期目标的完成证据分别绑定各自 occurrence，初始检查目标也需要独立观察。

| 指标 | 结果 |
| --- | ---: |
| 全新落盘场景 | 100 轮 |
| 场景事件 | 1,700 条 |
| 完成动作 | 100 个 |
| 重启恢复 | 100 次 |
| 重启重复动作 | 0 |
| 最大外部动作并发 | 1 |
| 不同规范化结果哈希 | 1 |
| 本机总运行时间 | 11.781 秒 |

源码 SHA256：`98150a93f5acb7dcfffda009fbaba1c6f827944f8192d9d83366154a895ce7f6`。场景 SHA256：`a573cc7324191a2143dfd6d2ebb5dc4aecbde81b9948d9c15c55df03c3e82ae1`。源码哈希算法同 0.3.0 报告。

规范化结果哈希：`d36879b89f300843cc5c610eea7886c739ab56d215a8b2362d3f8eb77b162c61`。

这是固定逻辑时间场景，不是两天真实时长测试；重复 100 次也不是 100 个独立研究样本。每日规则由显式 tick 驱动，目前不支持任意 cron、周/月规则或无限历史补跑。FTS5 基线限制 1,000 条事实，分词与排序质量尚未进行独立评估；索引回退是子串语义，不等价于全文词项匹配。

0.3.0 的 20 模块集成结果与本轮增强共同构成本机仿真交付；完整剩余项见 [模块实现范围](../cognitive-simulation.md)。
