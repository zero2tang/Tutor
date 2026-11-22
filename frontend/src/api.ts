import { readEventStream } from "./streaming/sse";

// 前端只处理公开接口，模型密钥和标准答案不进入本地存储。
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "请求未完成，请稍后重试");
  return data as T;
}

export async function streamOperation(
  path: string,
  body: unknown,
  onEvent: (event: string, data: unknown) => void,
): Promise<void> {
  const response = await fetch(`/api${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok || !response.body) {
    const error = await response
      .json()
      .catch(() => ({ detail: "无法连接学习服务" }));
    throw new Error(error.detail);
  }
  await readEventStream(response.body, onEvent);
}
