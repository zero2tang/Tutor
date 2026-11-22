import assert from "node:assert/strict";
import test from "node:test";
import { createSseParser, readEventStream } from "../src/streaming/sse.ts";
import { updateDrafts } from "../src/streaming/drafts.ts";
import { normalizeMathDelimiters } from "../src/formatting/markdown.ts";
import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkMath from "remark-math";

const encoder = new TextEncoder();
const frame = (event, data, ending = "\n") =>
  `event: ${event}${ending}data: ${JSON.stringify(data)}${ending}${ending}`;
const delta = (id, field, text, agent = "knowledge") => ({
  stream_id: id,
  agent,
  field,
  delta: text,
});

test("SSE 按 CRLF、CR、LF 解析多行数据，忽略心跳注释", () => {
  const received = [];
  const parser = createSseParser((event) => received.push(event));
  for (const character of ": heartbeat\r\nevent: example\r\ndata: first\r\ndata:  second\r\n\r\nevent: done\rdata: {}\r\r") {
    parser.push(character);
  }
  parser.push("", true);
  assert.deepEqual(received, [
    { event: "example", data: "first\n second" },
    { event: "done", data: "{}" },
  ]);
});

test("逐字节网络分包保留中文、公式和 emoji，显式 done 后释放 reader", async () => {
  const text = "中文增量 $x^2$ 🧠";
  const bytes = encoder.encode(
    frame("text_delta", delta("a", "content", text), "\r\n") +
      frame("done", {}),
  );
  const stream = new ReadableStream({
    start(controller) {
      for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
      controller.close();
    },
  });
  const received = [];
  await readEventStream(stream, (event, data) => received.push([event, data]));
  assert.equal(received[0][1].delta, text);
  assert.equal(received.at(-1)[0], "done");
  assert.equal(stream.locked, false);
});

test("正文在流结束前就回调，done 无需等待服务端关闭连接", async () => {
  let controller;
  let cancelled = false;
  const stream = new ReadableStream({
    start(value) {
      controller = value;
    },
    cancel() {
      cancelled = true;
    },
  });
  const received = [];
  let first;
  const delivered = new Promise((resolve) => {
    first = resolve;
  });
  const reading = readEventStream(stream, (event, data) => {
    received.push([event, data]);
    if (event === "text_delta") first();
  });
  controller.enqueue(
    encoder.encode(frame("text_delta", delta("a", "content", "第一段"))),
  );
  await delivered;
  assert.deepEqual(
    received.map(([event]) => event),
    ["text_delta"],
  );
  controller.enqueue(encoder.encode(frame("done", { status: "completed" })));
  await reading;
  assert.equal(cancelled, true);
  assert.equal(stream.locked, false);
});

test("EOF 或残缺 done 不能冒充任务完成", async () => {
  for (const source of [
    frame("text_delta", delta("a", "content", "尚未完成")),
    "event: done\ndata: {}\n",
  ]) {
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(source));
        controller.close();
      },
    });
    await assert.rejects(
      readEventStream(stream, () => {}),
      /尚未确认完成/,
    );
    assert.equal(stream.locked, false);
  }
});

test("服务端 error 和无效 JSON 均拒绝，并释放连接", async () => {
  for (const [source, expected] of [
    [frame("error", { message: "模型暂不可用" }), /模型暂不可用/],
    ["event: text_delta\ndata: {broken}\n\n", /JSON|property/i],
  ]) {
    let cancelled = false;
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(source));
      },
      cancel() {
        cancelled = true;
      },
    });
    await assert.rejects(
      readEventStream(stream, () => {}),
      expected,
    );
    assert.equal(cancelled, true);
    assert.equal(stream.locked, false);
  }
});

test("并行草稿按 stream_id 隔离，重试只撤销指定草稿", () => {
  let drafts = updateDrafts([], "text_delta", delta("a", "title", "标题"));
  drafts = updateDrafts(
    drafts,
    "text_delta",
    delta("b", "content", "另一个轮次", "exercise"),
  );
  drafts = updateDrafts(drafts, "text_delta", delta("a", "content", "正文"));
  drafts = updateDrafts(drafts, "text_delta", delta("a", "content", "增量"));
  drafts = updateDrafts(
    drafts,
    "text_delta",
    delta("a", "follow_up", "继续思考"),
  );
  assert.deepEqual(drafts[0].fields, {
    title: "标题",
    content: "正文增量",
    follow_up: "继续思考",
  });
  assert.equal(drafts[1].fields.content, "另一个轮次");
  drafts = updateDrafts(drafts, "text_reset", { stream_id: "a" });
  assert.deepEqual(
    drafts.map(({ id }) => id),
    ["b"],
  );
});

test("只展示公开文本字段，用户回执不清草稿，正式回复和终止事件清空", () => {
  let drafts = updateDrafts(
    [],
    "text_delta",
    delta("a", "content", "可见文本"),
  );
  const accepted = drafts;
  drafts = updateDrafts(
    drafts,
    "text_delta",
    delta("a", "reasoning", "内部推理"),
  );
  drafts = updateDrafts(
    drafts,
    "text_delta",
    delta("a", "answer_key", "标准答案"),
  );
  drafts = updateDrafts(
    drafts,
    "text_delta",
    delta("a", "content", "无效角色", "unknown"),
  );
  drafts = updateDrafts(drafts, "message", { role: "user", content: "问题" });
  assert.equal(drafts, accepted);
  for (const [event, data] of [
    ["message", { role: "assistant" }],
    ["done", {}],
    ["error", {}],
  ]) {
    assert.deepEqual(updateDrafts(drafts, event, data), []);
  }
});

test("流式和最终消息共用的公式转换区分行内与块级数学", () => {
  const content = normalizeMathDelimiters(
    "行内 \\(x^2\\)，块级 \\[\\frac{1}{2}\\] 然后继续。",
  );
  const tree = unified().use(remarkParse).use(remarkMath).parse(content);
  assert.equal(tree.children[0].children[1].type, "inlineMath");
  assert.equal(tree.children[1].type, "math");
  assert.equal(tree.children[1].value, "\\frac{1}{2}");
});

test("公式转换保留代码示例和尚未结束的流式代码块", () => {
  for (const content of [
    "`\\(x\\)` 与 ``\\[x\\]``",
    "```latex\n\\[x\\]\n```",
    "~~~~latex\n\\(x\\)\n~~~~",
    "```latex\n\\[x\\]",
  ]) {
    assert.equal(normalizeMathDelimiters(content), content);
  }
  assert.equal(normalizeMathDelimiters("公式 \\(x"), "公式 \\(x");
});
