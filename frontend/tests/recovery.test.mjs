import assert from "node:assert/strict";
import test from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { observeSessionRecovery } from "../src/streaming/recovery.ts";

const snapshot = (active_turn_id, content = "正式消息") => ({
  id: "session-a",
  active_turn_id,
  messages: [{ id: "message-a", content }],
});

function options(overrides = {}) {
  return {
    signal: new AbortController().signal,
    isCurrent: () => true,
    retryDelayMs: 1,
    requestTimeoutMs: 1000,
    onSnapshot: () => {},
    ...overrides,
  };
}

test("本地旧快照没有 active_turn_id，断流后仍反复读取直到正式结束", async () => {
  let calls = 0;
  let busy = true;
  let local = snapshot(null, "流断开前的旧消息");
  const observed = [];
  const completed = await observeSessionRecovery(
    options({
      read: async () => {
        calls++;
        if (calls <= 2) throw new TypeError("网络暂不可达");
        return calls === 3 ? snapshot("turn-a", "中间记录") : snapshot(null);
      },
      onSnapshot: (current) => {
        assert.equal(busy, true);
        local = current;
        observed.push(current.active_turn_id);
        if (!current.active_turn_id) busy = false;
      },
    }),
  );
  assert.equal(calls, 4);
  assert.deepEqual(observed, ["turn-a", null]);
  assert.equal(local.messages[0].content, "正式消息");
  assert.equal(completed, local);
  assert.equal(busy, false);
});

test("慢读取始终串行执行，旧请求不会与下一次观察重叠", async () => {
  let calls = 0;
  let concurrent = 0;
  let maximum = 0;
  await observeSessionRecovery(
    options({
      read: async () => {
        calls++;
        concurrent++;
        maximum = Math.max(maximum, concurrent);
        await delay(5);
        concurrent--;
        return snapshot(calls < 3 ? "turn-a" : null);
      },
    }),
  );
  assert.equal(calls, 3);
  assert.equal(maximum, 1);
});

test("单次读取超时会取消该请求并重试，而不会永久卡住恢复", async () => {
  let calls = 0;
  let cancelled = 0;
  const completed = await observeSessionRecovery(
    options({
      requestTimeoutMs: 10,
      read: async (signal) => {
        calls++;
        if (calls > 1) return snapshot(null);
        return new Promise((resolve, reject) => {
          signal.addEventListener(
            "abort",
            () => {
              cancelled++;
              reject(new DOMException("请求超时", "AbortError"));
            },
            { once: true },
          );
        });
      },
    }),
  );
  assert.equal(calls, 2);
  assert.equal(cancelled, 1);
  assert.equal(completed.active_turn_id, null);
});

test("权威快照先解锁，辅助刷新挂起或失败都不阻断正式消息恢复", async () => {
  for (const failure of ["pending", "async", "sync"]) {
    let busy = true;
    let detailsStarted = false;
    let rejectDetails;
    const pending = new Promise((resolve, reject) => {
      rejectDetails = reject;
    });
    const completed = await observeSessionRecovery(
      options({
        read: async () => snapshot(null),
        onSnapshot: (current) => {
          assert.equal(current.messages[0].content, "正式消息");
          busy = false;
        },
        refreshDetails: () => {
          assert.equal(busy, false);
          detailsStarted = true;
          if (failure === "sync") throw new Error("辅助数据同步失败");
          return failure === "async"
            ? Promise.reject(new Error("profile 接口不可用"))
            : pending;
        },
      }),
    );
    assert.equal(completed.active_turn_id, null);
    assert.equal(busy, false);
    assert.equal(detailsStarted, true);
    if (failure === "pending") rejectDetails(new Error("辅助刷新晚到的失败"));
    await delay(0);
  }
});

test("切换课程或会话后，晚到的旧快照不再更新界面或刷新辅助资料", async () => {
  let current = true;
  let deliver;
  let applied = 0;
  let details = 0;
  const observing = observeSessionRecovery(
    options({
      isCurrent: () => current,
      read: () => new Promise((resolve) => (deliver = resolve)),
      onSnapshot: () => applied++,
      refreshDetails: async () => details++,
    }),
  );
  current = false;
  deliver(snapshot(null));
  assert.equal(await observing, undefined);
  assert.equal(applied, 0);
  assert.equal(details, 0);
});

test("离开恢复作用域会取消正在读取的请求和后续重试计时器", async () => {
  const readingController = new AbortController();
  let requestCancelled = false;
  const reading = observeSessionRecovery(
    options({
      signal: readingController.signal,
      read: (signal) =>
        new Promise((resolve, reject) => {
          signal.addEventListener("abort", () => {
            requestCancelled = true;
            reject(new DOMException("会话已切换", "AbortError"));
          });
        }),
    }),
  );
  readingController.abort();
  assert.equal(await reading, undefined);
  assert.equal(requestCancelled, true);

  const waitingController = new AbortController();
  let calls = 0;
  const waiting = observeSessionRecovery(
    options({
      signal: waitingController.signal,
      retryDelayMs: 60000,
      read: async () => {
        calls++;
        return snapshot("turn-a");
      },
      onSnapshot: () => waitingController.abort(),
    }),
  );
  assert.equal(await waiting, undefined);
  assert.equal(calls, 1);
});
