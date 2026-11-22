export type DraftAgent = "supervisor" | "knowledge" | "exercise" | "assessment";
export type DraftField =
  "message" | "title" | "content" | "follow_up" | "feedback" | "summary";

export interface StreamingDraft {
  id: string;
  agent: DraftAgent;
  fields: Partial<Record<DraftField, string>>;
}

const agents = new Set(["supervisor", "knowledge", "exercise", "assessment"]);
const fields = new Set([
  "message",
  "title",
  "content",
  "follow_up",
  "feedback",
  "summary",
]);

// 草稿独立于持久消息；工具调用、重试或正式回复都可以撤销临时内容。
export function updateDrafts(
  drafts: StreamingDraft[],
  event: string,
  data: unknown,
): StreamingDraft[] {
  const value = data as Record<string, unknown> | null;
  if (!value || typeof value !== "object") return drafts;
  if (event === "message" && value.role === "assistant") return [];
  if (event === "done" || event === "error") return [];
  if (typeof value.stream_id !== "string") return drafts;
  if (event === "text_reset")
    return drafts.filter((draft) => draft.id !== value.stream_id);
  if (
    event !== "text_delta" ||
    typeof value.agent !== "string" ||
    !agents.has(value.agent) ||
    typeof value.field !== "string" ||
    !fields.has(value.field) ||
    typeof value.delta !== "string" ||
    !value.delta
  )
    return drafts;
  const field = value.field as DraftField;
  const existing = drafts.find((draft) => draft.id === value.stream_id);
  const next: StreamingDraft = {
    id: value.stream_id,
    agent: value.agent as DraftAgent,
    fields: {
      ...existing?.fields,
      [field]: (existing?.fields[field] || "") + value.delta,
    },
  };
  return existing
    ? drafts.map((draft) => (draft.id === next.id ? next : draft))
    : [...drafts, next];
}
