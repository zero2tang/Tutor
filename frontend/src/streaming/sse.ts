export interface SseFrame {
  event: string;
  data: string;
}

// 按行处理 SSE，保留跨网络分片的 CRLF 和尚未结束的事件。
export function createSseParser(onFrame: (frame: SseFrame) => void) {
  let buffer = "";
  let event = "";
  let data: string[] = [];
  const line = (value: string) => {
    if (!value) {
      if (data.length)
        onFrame({ event: event || "message", data: data.join("\n") });
      event = "";
      data = [];
      return;
    }
    if (value.startsWith(":")) return;
    const colon = value.indexOf(":");
    const field = colon < 0 ? value : value.slice(0, colon);
    let content = colon < 0 ? "" : value.slice(colon + 1);
    if (content.startsWith(" ")) content = content.slice(1);
    if (field === "event") event = content;
    if (field === "data") data.push(content);
  };
  return {
    push(text: string, final = false) {
      buffer += text;
      let offset = 0;
      for (let index = 0; index < buffer.length; index++) {
        const character = buffer[index];
        if (character !== "\r" && character !== "\n") continue;
        if (character === "\r" && index === buffer.length - 1 && !final) break;
        line(buffer.slice(offset, index));
        if (character === "\r" && buffer[index + 1] === "\n") index++;
        offset = index + 1;
      }
      buffer = buffer.slice(offset);
    },
  };
}

export async function readEventStream(
  stream: ReadableStream<Uint8Array>,
  onEvent: (event: string, data: unknown) => void,
): Promise<void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let completed = false;
  let ended = false;
  const parser = createSseParser(({ event, data }) => {
    if (completed) return;
    const payload: unknown = JSON.parse(data);
    onEvent(event, payload);
    if (event === "error") {
      const message = (payload as { message?: string } | null)?.message;
      throw new Error(message || "本轮任务失败");
    }
    if (event === "done") completed = true;
  });
  try {
    while (!completed) {
      const { value, done } = await reader.read();
      ended = done;
      parser.push(decoder.decode(value, { stream: !done }), done);
      if (done) break;
    }
    // 网络 EOF 不能替代服务端的完成确认，否则中途断线会被误报为成功。
    if (!completed)
      throw new Error("连接中断，本轮输出尚未确认完成，正在同步学习记录。");
  } finally {
    if (!ended) await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
