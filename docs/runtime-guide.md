# 确定性内核运行说明

当前版本为 0.5.0，面向 Python 3.10 以上的 Linux 或其他支持 POSIX 文件锁的环境。运行代码只使用 Python 标准库。基础 run/replay 和离线 fixture 不需要外部服务；cloud 及云端 model 场景需要云服务配置，见 [云端模型说明](cloud-models.md)。所有动作均为模拟，包括文本表达；程序在标准输出打印 JSON 报告，不控制机器人。

多模块本机仿真已提供 `simulate` 入口，见 [认知仿真说明](cognitive-simulation.md)。下文主要介绍基础运行时。

## 快速运行

在仓库根目录执行：

```sh
python3 -m consciousness run '你好' '小智，介绍一下自己' '火警'
python3 -m consciousness replay examples/basic.jsonl
python3 -m consciousness replay examples/preemption.jsonl --timed --stop-delay 0.1
python3 -m unittest discover -s tests -v
```

run 按一批输入受理再排序。预置紧急短语优先，点名输入在普通输入中优先，同级按入队顺序。示例中的“火警”是精确匹配的演示规则，不是可以部署到现实环境的危险识别器。“这里没有火警”不会命中该精确规则。

报告包含 simulation、任务状态数量和任务记录。正常完成退出码为 0；输入非法、背压或存在 outcome_unknown 时退出码为 2。CLI 是单次批处理入口，持续接收输入和执行中抢占通过 Runtime 的异步接口实现，相关例子见测试。

## 保存数据与恢复

```sh
python3 -m consciousness run '小智，你好' --data-dir local-data/demo
python3 -m consciousness inspect --data-dir local-data/demo
python3 -m consciousness resume --data-dir local-data/demo
```

目录内 journal.sqlite3 保存事件、任务、动作意图及消费检查点；simulator.sqlite3 独立保存模拟动作回执。必须一起保存这两个数据库才能验证跨重启的动作结果。持久化路径已被 Git 忽略，不应提交运行数据。

inspect 导出事件、任务及存在的认知模块状态，不触发恢复或执行，但当前仍获取独占文件锁，因此先关闭运行实例。resume 会查询未终结动作，再执行已经排队的任务。不能确认是否执行的动作进入 outcome_unknown 并冻结后续动作，不提供强制把未知状态改成成功的命令。

恢复规则如下：

| 崩溃位置 | 恢复行为 |
| --- | --- |
| 已受理、尚未占用动作槽位 | 保留 queued，启动后正常调度 |
| 动作意图为 prepared，尚未记录 sending | 可确认未发送，取消原任务并记录证据 |
| 已记录 sending，但没有可信结果 | 查询模拟器；查不到时保持未知，禁止重发 |
| 模拟器已完成，结果未写回任务 | 从模拟器最终回执恢复终态，不再次执行 |
| 已写入终态及反馈 | 不重新执行 |

close 会停止当前动作并保留未执行队列。被中断任务在本版终结为 cancelled，不自动重新排队；检查点暂停与技能恢复属于后续工作。动作执行协程抛异常并不能证明没有外部效果，因此记为未知而非普通 failed。

## 数据升级身份与容量

0.2.0 使用数据库 schema 2，启动时在一个事务中升级 schema 1，保留原事件 ID、任务和幂等指纹。旧记录无法还原的来源标为 legacy，发生时间为 null，接收时间沿用原 created。未知版本仍拒绝打开，旧版程序不能写入 schema 2。

```sh
python3 -m consciousness run '你好' --data-dir local-data/robot-a --robot-id robot-a --max-tasks 100000 --max-events 2000000 --aging-seconds 30
python3 -m consciousness backup --data-dir local-data/robot-a local-data/backup-001
```

每个数据库绑定一个 robot_id，重启可自动读取；显式传入其他 ID 或提交其他机器人的 Signal 会被拒绝。不同机器人使用不同目录，不支持在同一个文件内混合控制多台设备。

容量及老化配置保存在数据库元数据中，未指定参数时继承已有配置。max_tasks 限制累计任务数，max_events 是新任务受理预算；受理时为未终结任务预留每项 16 条事件额度。到限返回 CAPACITY_EXCEEDED，但既有输入重试仍可查询原任务，已受理动作仍允许完成及写入回执。

这是受理背压，不是物理磁盘硬配额：恢复、生命周期与关键回执仍可写入，WAL 及投影也占空间。当前不自动删除历史和去重记录；容量到限后先备份，再显式扩容或设计后续保留窗口。不能通过随意删除输入历史恢复容量，否则会破坏幂等及证据链。

backup 持有独占实例锁，使用 SQLite 备份接口复制日志及存在的模拟器数据库，执行完整性检查并生成 SHA256 清单 manifest.json。目标目录必须不存在；中断或失败留下 INCOMPLETE 标记，不应恢复不完整快照。备份副本应保持不变，复制到新的运行目录后再 resume；模拟器回执与日志必须一起恢复。

inspect、resume、backup 遇到不存在的数据库会报错，不会因目录拼写错误创建一个看似正常的空实例。

## 公平性与截止时间

Signal 可携带 deadline_at，单位为 UTC Unix 秒，表示最晚开始执行时间。占用槽位前取消已经到期的排队任务；不取消已经启动的动作，已启动动作由 action_timeout 和停止协议管理。

队列先按有效优先级、截止时间、点名分数和入队序号排序。每等待 aging_seconds 秒，非紧急任务的有效级别提高一级，最高到 P1；P0 始终优先。这样老普通任务能越过新点名任务，但在持续紧急或更早截止任务占满容量时不承诺绝对等待上限。

同一次进程内使用单调时钟推进排队年龄，避免系统墙钟校时改变等待时长；跨重启从持久接收时间重建。时间异常或跨设备时钟校准仍需上层设备适配。

## 应用层接口

```python
import asyncio
from consciousness.domain import Signal
from consciousness.driver import SimulatedDriver
from consciousness.journal import Journal
from consciousness.runtime import Runtime

async def example():
    journal = Journal()
    driver = SimulatedDriver()
    runtime = Runtime(journal, driver)
    try:
        await runtime.start()
        signal = Signal("小智，你好", event_id="request-001")
        task_id = await runtime.submit(signal)
        await runtime.submit(signal)  # 同一事件不会新增任务
        result = await runtime.drain()
        print(task_id, result["counts"])
    finally:
        await runtime.close()
        driver.close()
        journal.close()

asyncio.run(example())
```

接口在同一事件循环内使用，不支持从多个线程直接调用同一个 Journal。start 必须完成后才能 submit。drain 等待到当前无可运行工作；这可能表示队列清空，也可能表示未知动作阻塞，调用者必须检查 blocked。

cancel(task_id) 取消排队任务；执行中的任务走停止协议。reconcile() 在调度器没有当前动作时查询未知结果，获得可信回执后可以解除冻结。迟到回执不会默默启动新任务，需要显式核对或重启恢复。

## 事件与回放

每个输入必须有稳定 event_id。相同 ID 与相同内容返回原任务；相同 ID 换内容返回 DUPLICATE_CONFLICT。内部反馈不经过外部输入接口，source=output 等来源被拒绝，避免自我循环。

事件导出包含 seq、event_id、robot_id、trace_id、causation_id、type、source、occurred_at、received_at、payload 和 schema_version。时间使用 UTC Unix 秒；occurred_at 与 received_at 分开保存。兼容旧读取器的 cause_id、kind 和 created 别名仍保留。SQL 日志本身是持久化投递源，消费者以 seq 检查点读取，不额外复制一份 outbox 表。

Journal.project 仅接受无外部副作用的 JSON 转换函数，投影结果和检查点在同一事务提交。消费者失败可重试；它不是可用于直接驱动机器人或网络调用的恰好一次投递机制。

默认 replay 接收 JSONL，每行包含 id、text 和可选 source，所有输入先入队再执行。它始终新建内存模拟器，不复用生产状态；结果 SHA256 排除随机 ID 与墙钟时间。

--timed 模式每行额外包含 at（从虚拟零时刻起的到达秒数）及可选 deadline_at；时间必须有限、非负、单调排列。同刻输入先一批受理，再让模拟器处理已就绪回调。回放推进虚拟时间，无需等待文件中的真实秒数；结果哈希包含虚拟完成时刻。同一个事件 ID 重新送达不等于新的观察，不能用相同 ID 更换正文或截止时间。

--duration 指定模拟动作耗时，--stop-delay 指定停止确认延迟。停止超时即使后来得到回执也不会在回放中自动解除冻结，报告 blocked=true，CLI 返回 2。该模式仅支持内置模拟器和有界回调链，不用于真实设备或外部模型调用。

## 性能复现

```sh
python3 -m consciousness.benchmark --trials 100 --rate 20 --output benchmark.json
```

基准采用真实时钟、临时目录中的 SQLite WAL/FULL 和模拟器，交替发送普通输入与紧急输入。报告分别记录受理后到驱动 stop 调用、包含受理存储耗时的入口到 stop、p50/p95/p99/最大值、实际输入速率、环境和源码哈希。默认 100 次抢占、200 条输入、目标 20 条/秒，约运行 10 秒。

该指标不包含物理制动或模型推理，也不等于长期稳定性测试。进程 maxrss 是操作系统高水位，不是稳态内存或内存增长率。实测记录见 [G1 验证报告](validation/g1-report.md)。

## 当前限制

- 已有结构事实、目标、情绪、动机、技能与学习候选的仿真基线；已提供云模型及多模态请求接口，真实服务质量待配置后验收。各模块完整缺口见认知仿真说明。
- 工作记忆目前按条数和 UTF-8 字节预算管理，不包含模型 token 装配和自动摘要。
- 首版以精确紧急词、点名和 FIFO 作为确定性注意力基线，未实现多模态信息组与迟滞。
- 仅支持单机单进程写入，使用 POSIX 文件锁；不具备多节点租约或驱动端 fencing。
- 自动归档删除、日志轮转、72 小时长跑及物理磁盘/内存硬配额尚待验收；已有受理预算、无损备份及规则中断微基准。
- 驱动必须明确声明 simulation=True；真实硬件接入在后续设备阶段开放。

模块完成度和下一批工作见 [研发进度](progress.md)。

## 本地管理台

0.6.0 起无参数启动 `python3 -m consciousness` 会启动本地 Web 前后端，配置就绪时同时启动语音；其余子命令保持原行为。详见 [管理台说明](dashboard.md)。
