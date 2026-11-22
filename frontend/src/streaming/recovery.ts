interface RecoverySnapshot {
  active_turn_id: string | null;
}

interface RecoveryOptions<T extends RecoverySnapshot> {
  read: (signal: AbortSignal) => Promise<T>;
  onSnapshot: (snapshot: T) => void;
  refreshDetails?: (snapshot: T) => Promise<unknown>;
  isCurrent: () => boolean;
  signal: AbortSignal;
  retryDelayMs?: number;
  requestTimeoutMs?: number;
}

function pause(delay: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    };
    const timer = setTimeout(finish, delay);
    signal.addEventListener("abort", finish, { once: true });
  });
}

// 恢复只读取权威会话快照，不重新提交学习请求；辅助资料失败不影响任务确权。
export async function observeSessionRecovery<T extends RecoverySnapshot>({
  read,
  onSnapshot,
  refreshDetails,
  isCurrent,
  signal,
  retryDelayMs = 2000,
  requestTimeoutMs = 10000,
}: RecoveryOptions<T>): Promise<T | undefined> {
  const current = () => !signal.aborted && isCurrent();
  while (current()) {
    const request = new AbortController();
    const cancel = () => request.abort();
    signal.addEventListener("abort", cancel, { once: true });
    const timeout = setTimeout(cancel, requestTimeoutMs);
    let snapshot: T | undefined;
    try {
      snapshot = await read(request.signal);
    } catch {
      // 网络错误或单次请求超时都保持恢复状态，稍后再次读取。
    } finally {
      clearTimeout(timeout);
      signal.removeEventListener("abort", cancel);
    }
    if (!current()) return;
    if (snapshot) {
      onSnapshot(snapshot);
      if (!snapshot.active_turn_id) {
        // 正式会话已确认结束，资料刷新不占住提交入口，也不传播辅助错误。
        if (current() && refreshDetails) {
          try {
            void refreshDetails(snapshot).catch(() => {});
          } catch {
            /* 同步的辅助刷新异常同样不影响权威会话。 */
          }
        }
        return snapshot;
      }
    }
    // 串行等待上一次读取结束后再重试，避免慢请求并发覆盖较新的快照。
    await pause(retryDelayMs, signal);
  }
}
