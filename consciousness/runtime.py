"""Serialized external actions with durable stop and recovery semantics."""
import asyncio
from collections import deque
from dataclasses import asdict

from .attention import Attention
from .clock import RealClock
from .domain import Decision, InvalidInput, Result, Signal, Status, StopReceipt, TERMINAL, canonical, new_id, positive_number
from .memory import Item, WorkingMemory


class Runtime:
    def __init__(self, journal, driver, *, clock=None, name="小智", max_pending=64,
                 action_timeout=30.0, stop_timeout=5.0, query_timeout=5.0):
        if type(max_pending) is not int or max_pending < 1:
            raise ValueError("max_pending must be a positive integer")
        for key, value in (("action_timeout", action_timeout), ("stop_timeout", stop_timeout),
                           ("query_timeout", query_timeout)):
            positive_number(value, key)
        if driver.simulation is not True:
            raise ValueError("v0 runtime only permits explicitly simulated drivers")
        self.journal, self.driver = journal, driver
        self.clock = clock or RealClock()
        self.attention = Attention(name)
        self.memory = WorkingMemory()
        self.max_pending = max_pending
        self.action_timeout, self.stop_timeout, self.query_timeout = action_timeout, stop_timeout, query_timeout
        self.epoch = new_id()
        self._worker = None
        self._action = None
        self._interrupt = asyncio.Event()
        self._background = set()
        self._started = False
        self._closed = False
        self._fatal = None
        self._interrupt_received_at = None
        self.stop_request_latencies_ms = deque(maxlen=10000)

    async def start(self):
        if self._started:
            return
        if self._closed:
            raise RuntimeError("runtime closed")
        # Rebuild bounded context in order; accepted inputs remain durable.
        cursor = 0
        while True:
            events = self.journal.history(cursor)
            if not events:
                break
            for event in events:
                if event["kind"] == "input.accepted":
                    self.memory.add(Item(event["event_id"], "observation", event["payload"]["text"]))
            cursor = events[-1]["seq"]
        await self.reconcile()
        self._started = True
        self.journal.record("runtime.started", {"epoch": self.epoch, "simulation": True})
        self._kick()

    def _track(self, task):
        self._background.add(task)

        def finished(item):
            self._background.discard(item)
            if not item.cancelled():
                item.exception()  # Retrieve late errors; unresolved state stays frozen.
        task.add_done_callback(finished)
        return task

    async def _bounded(self, awaitable, seconds):
        task = self._track(asyncio.create_task(awaitable))
        timer = asyncio.create_task(self.clock.sleep(seconds))
        try:
            done, _ = await asyncio.wait([task, timer], return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return await task
            # Timeout does not cancel device communication or imply physical stop.
            raise TimeoutError("driver response deadline exceeded")
        finally:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)

    async def reconcile(self):
        if self._action is not None:
            raise RuntimeError("cannot reconcile while scheduler is executing")
        for action, phase in self.journal.unresolved():
            if phase == "prepared":
                self.journal.finish(action, "cancelled", "journal:never_marked_sending", "recovered_unsent")
                continue
            try:
                result = await self._bounded(self.driver.query(action), self.query_timeout)
            except Exception as exc:
                result = None
                self.journal.record("recovery.query_failed", {"action_id": action.id,
                                                            "error_type": type(exc).__name__}, action.trace_id)
            if isinstance(result, Result) and result.matches(action):
                self.journal.finish(action, result.status, result.evidence, "recovered:" + result.detail)
            elif self.journal.task(action.task_id)["status"] != "outcome_unknown":
                self.journal.transition(action.task_id, "outcome_unknown", "recovery_requires_evidence")
        if self._started:
            self._kick()

    async def submit(self, signal: Signal):
        return await self._submit(signal, self.attention.select(signal))

    async def submit_action(self, capability, arguments, *, event_id):
        """Accept a validated simulation command through the same serial slot.

        Intended for configured goals/skills, not raw model output. The full
        command is fingerprinted so retrying an ID cannot change its effect.
        """
        if capability not in {"say", "alert"} or not isinstance(arguments, dict) or set(arguments) != {"text"}:
            raise InvalidInput("unsupported simulated command")
        text = arguments["text"]
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 12000:
            raise InvalidInput("invalid command text")
        signal = Signal(canonical({"capability": capability, "arguments": arguments}),
                        event_id=event_id, robot_id=self.journal.robot_id)
        return await self._submit(signal, Decision(0 if capability == "alert" else 2, 50,
                                                   capability, dict(arguments), "validated_plan"))

    async def _submit(self, signal, decision):
        if not self._started or self._closed:
            raise RuntimeError("start runtime before submitting; closed runtime rejects new work")
        if self._fatal is not None:
            raise RuntimeError("runtime is faulted") from self._fatal
        task, accepted = self.journal.accept(signal, decision, self.max_pending)
        if accepted:
            self.memory.add(Item(signal.event_id, "observation", signal.text))
            if self._action is not None:
                current = self.journal.task(self._action.task_id)
                # Only higher priority class preempts; named inputs reorder the queue.
                if decision.priority < current["priority"]:
                    if not self._interrupt.is_set():
                        self._interrupt_received_at = self.clock.monotonic()
                    self._interrupt.set()
            self._kick()
        return task["id"]

    async def cancel(self, task_id):
        task = self.journal.task(task_id)
        if task["status"] == "queued":
            self.journal.transition(task_id, "cancelled", "user_cancelled")
        elif self._action and self._action.task_id == task_id:
            self._interrupt.set()
        return self.journal.task(task_id)

    def _kick(self):
        if self._started and not self._closed and (self._worker is None or self._worker.done()):
            self._worker = asyncio.create_task(self._pump())
            self._worker.add_done_callback(lambda task: None if task.cancelled() else task.exception())

    async def _pump(self):
        try:
            while not self._closed:
                action = self.journal.claim(self.epoch)
                if action is None:
                    return
                self._action = action
                self._interrupt.clear()
                self._interrupt_received_at = None
                try:
                    await self._execute(action)
                finally:
                    self._action = None
        except BaseException as exc:
            # A storage failure must not silently schedule the next action.
            self._fatal = exc
            raise

    async def _execute(self, action):
        self.journal.phase(action.id, "sending")
        execution = self._track(asyncio.create_task(self.driver.execute(action)))
        interrupt = asyncio.create_task(self._interrupt.wait())
        deadline = asyncio.create_task(self.clock.sleep(self.action_timeout))
        try:
            ready, _ = await asyncio.wait([execution, interrupt, deadline], return_when=asyncio.FIRST_COMPLETED)
            if execution in ready:
                self._complete(action, execution)
                return
            reason = "action_timeout" if deadline in ready else "interrupted"
            self.journal.transition(action.task_id, "waiting_stop", reason)
            if self._interrupt_received_at is not None:
                self.stop_request_latencies_ms.append(
                    1000 * (self.clock.monotonic() - self._interrupt_received_at))
            try:
                receipt = await self._bounded(self.driver.stop(action), self.stop_timeout)
            except Exception as exc:
                self.journal.transition(action.task_id, "outcome_unknown", "stop:" + type(exc).__name__)
                return
            if not isinstance(receipt, StopReceipt) or not receipt.matches(action):
                self.journal.transition(action.task_id, "outcome_unknown", "invalid_stop_receipt")
                return
            self.journal.record("stop.confirmed", asdict(receipt), action.trace_id)
            # A completed action can win a concurrent cancellation race.
            if execution.done() and not execution.cancelled():
                try:
                    result = execution.result()
                except Exception:
                    result = None
                if isinstance(result, Result) and result.matches(action):
                    self.journal.finish(action, result.status, result.evidence, result.detail)
                    return
            self.journal.finish(action, "cancelled", receipt.evidence, reason)
        except BaseException:
            # Even if journal persistence fails, make a bounded best-effort stop.
            # No further task is scheduled; recovery must still query the driver.
            try:
                await self._bounded(self.driver.stop(action), self.stop_timeout)
            except BaseException:
                pass
            raise
        finally:
            interrupt.cancel()
            deadline.cancel()
            await asyncio.gather(interrupt, deadline, return_exceptions=True)

    def _complete(self, action, execution):
        if execution.cancelled():
            self.journal.transition(action.task_id, "outcome_unknown", "driver_coroutine_cancelled")
            return
        try:
            result = execution.result()
        except Exception as exc:
            self.journal.transition(action.task_id, "outcome_unknown", "driver:" + type(exc).__name__)
            return
        if not isinstance(result, Result) or not result.matches(action):
            self.journal.transition(action.task_id, "outcome_unknown", "invalid_action_result")
            return
        self.journal.finish(action, result.status, result.evidence, result.detail)

    async def drain(self):
        """Wait until no task is runnable (empty OR blocked by unknown outcome)."""
        if self._worker is not None:
            await asyncio.shield(self._worker)
        return self.summary()

    def summary(self):
        tasks = self.journal.tasks()
        counts = {state.value: 0 for state in Status}
        for task in tasks:
            counts[task["status"]] += 1
        return {"simulation": True, "counts": counts,
                "blocked": counts["outcome_unknown"] > 0, "tasks": tasks,
                "storage": self.journal.usage()}

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self._interrupt.set()
        await self.drain()
        # Pending tasks intentionally remain durable for the next start.
        self.journal.record("runtime.closed", {"epoch": self.epoch,
                                              "pending_driver_calls": len(self._background)})
