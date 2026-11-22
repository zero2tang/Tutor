import type { ScopedMemoryText } from "../types";
import { Markdown } from "./Markdown";

export interface ScopedMemorySnapshot {
  global_memory?: ScopedMemoryText | null;
  course_memory?: ScopedMemoryText | null;
  concept_memories?: ScopedMemoryText[];
}

export function ScopedMemoryView({ memory }: { memory: ScopedMemorySnapshot }) {
  const block = (title: string, item: ScopedMemoryText | null | undefined, key: string) => (
    <div className="memory-card" key={key}>
      <div>
        <strong>{title}</strong>
        {item?.content ? <Markdown>{item.content}</Markdown> : <p className="context-note">尚无可用的整理记录。</p>}
        {item?.enabled === false && <p className="context-note">此范围已暂停记录。</p>}
      </div>
    </div>
  );
  return <>
    {block("学生总体刻画", memory.global_memory, "student")}
    {block("本课程学习记忆", memory.course_memory, "course")}
    {memory.concept_memories?.map(item => block(item.concept_name || "知识点学习记忆", item, item.scope_key || item.concept_id || "concept"))}
  </>;
}
