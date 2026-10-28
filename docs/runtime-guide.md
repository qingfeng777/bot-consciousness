# 确定性内核运行说明

当前版本为 0.1.0，面向 Python 3.10 以上的 Linux 或其他支持 POSIX 文件锁的环境。运行代码只使用 Python 标准库，无需模型密钥和外部服务。所有动作均为模拟，包括文本表达；程序在标准输出打印 JSON 报告，不控制机器人。

## 快速运行

在仓库根目录执行：

```sh
python3 -m consciousness run '你好' '小智，介绍一下自己' '火警'
python3 -m consciousness replay examples/basic.jsonl
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

inspect 只导出事件与任务，不触发恢复或执行，但当前仍获取独占文件锁，因此先关闭运行实例。resume 会查询未终结动作，再执行已经排队的任务。不能确认是否执行的动作进入 outcome_unknown 并冻结后续动作，不提供强制把未知状态改成成功的命令。

恢复规则如下：

| 崩溃位置 | 恢复行为 |
| --- | --- |
| 已受理、尚未占用动作槽位 | 保留 queued，启动后正常调度 |
| 动作意图为 prepared，尚未记录 sending | 可确认未发送，取消原任务并记录证据 |
| 已记录 sending，但没有可信结果 | 查询模拟器；查不到时保持未知，禁止重发 |
| 模拟器已完成，结果未写回任务 | 从模拟器最终回执恢复终态，不再次执行 |
| 已写入终态及反馈 | 不重新执行 |

close 会停止当前动作并保留未执行队列。被中断任务在本版终结为 cancelled，不自动重新排队；检查点暂停与技能恢复属于后续工作。动作执行协程抛异常并不能证明没有外部效果，因此记为未知而非普通 failed。

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

事件导出包含 seq、event_id、trace_id、cause_id、kind、payload、created 和 schema_version。created 是 UTC Unix 秒；当前单机器人实例用独立数据库隔离，尚未实现多机器人 EventEnvelope 的全部字段。SQL 日志本身是持久化投递源，消费者以 seq 检查点读取，不额外复制一份 outbox 表。

Journal.project 仅接受无外部副作用的 JSON 转换函数，投影结果和检查点在同一事务提交。消费者失败可重试；它不是可用于直接驱动机器人或网络调用的恰好一次投递机制。

replay 接收 JSONL，每行包含 id、text 和可选 source。它始终新建内存模拟器，所有输入先入队再执行，不复用生产状态。结果 SHA256 排除随机 ID 与墙钟时间，用于比较同一批输入的行为结果。带时间的流式回放尚未提供 CLI；中断时序由可注入 ManualClock 和故障测试覆盖。

## 当前限制

- 尚无模型调用、多模态知觉、长期事实检索、目标中心、情绪动机或技能学习。
- 工作记忆目前按条数和 UTF-8 字节预算管理，不包含模型 token 装配和自动摘要。
- 首版以精确紧急词、点名和 FIFO 作为确定性注意力基线，未实现多模态信息组与迟滞。
- 仅支持单机单进程写入，使用 POSIX 文件锁；不具备多节点租约或驱动端 fencing。
- 事件保留与日志轮转、72 小时长跑、完整性能基准和持续运行资源上限尚待验收。
- 驱动必须明确声明 simulation=True；真实硬件接入在后续设备阶段开放。

模块完成度和下一批工作见 [研发进度](progress.md)。
