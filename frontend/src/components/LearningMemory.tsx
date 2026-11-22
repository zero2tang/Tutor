import { useEffect, useRef, useState } from "react";
import { BookOpen, RefreshCw } from "lucide-react";
import { api } from "../api";
import { Markdown } from "./Markdown";
import { ScopedMemoryView, type ScopedMemorySnapshot } from "./ScopedMemoryView";

interface MemoryText { content: string; version: number; status?: string; covered_seq?: number }
interface Snapshot extends ScopedMemorySnapshot {
  student_memory: MemoryText | null;
  session_experience: MemoryText | null;
  jobs: { id: string; status: string; error: string | null }[];
  scope_jobs?: { id: string; status: string; error: string | null }[];
  presentation_restricted?: boolean;
}

export function LearningMemory({ sessionId, revision }: { sessionId?: string; revision: number }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [correction, setCorrection] = useState("");
  const [scope, setScope] = useState("student");
  const [refreshTick, setRefreshTick] = useState(0);
  const current = useRef(sessionId);
  current.current = sessionId;
  useEffect(() => { setCorrection(""); setScope("student"); setSnapshot(null); setBusy(false); }, [sessionId]);
  useEffect(() => {
    if (!sessionId) { setSnapshot(null); return; }
    const controller = new AbortController();
    setError("");
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const data = await api<Snapshot>(`/sessions/${sessionId}/memory`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setSnapshot(data);
        if ([...data.jobs, ...(data.scope_jobs || [])].some(job => ["pending", "running", "retry"].includes(job.status))) timer = setTimeout(load, 2500);
      } catch (failure) {
        if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "读取记忆失败");
      }
    };
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [sessionId, revision, refreshTick]);

  const update = async () => {
    if (!sessionId || busy) return;
    const target = sessionId;
    setBusy(true); setError("");
    try {
      await api(`/sessions/${target}/memory/refresh?wait=true`, {
        method: "POST", body: JSON.stringify({ close: false }),
      });
      // 刷新接口也可能仅返回排队状态，读取正式快照后再展示。
      const data = await api<Snapshot>(`/sessions/${target}/memory`);
      if (current.current === target) { setSnapshot(data); setRefreshTick(value => value + 1); }
    } catch (failure) {
      if (current.current === target) setError(failure instanceof Error ? failure.message : "更新记忆失败");
    } finally { if (current.current === target) setBusy(false); }
  };
  const maintain = async (kind: string, text?: string) => {
    if (!sessionId || busy) return;
    const target = sessionId;
    const selectedKind = kind.split(":", 1)[0];
    // concept_id本身可以含冒号，按首个分隔位置保留完整目录ID。
    const concept = selectedKind === "concept" ? kind.slice("concept:".length) : undefined;
    setBusy(true); setError("");
    try {
      await api(`/sessions/${target}/memory/${text ? "correct?wait=true" : selectedKind + (concept ? "?concept_id=" + encodeURIComponent(concept) : "")}`, {
        method: text ? "POST" : "DELETE",
        ...(text ? { body: JSON.stringify({ kind: selectedKind, text, ...(concept ? { concept_id: concept } : {}) }) } : {}),
      });
      const data = await api<Snapshot>(`/sessions/${target}/memory`);
      if (current.current === target) { setSnapshot(data); setCorrection(""); setRefreshTick(value => value + 1); }
    } catch (failure) {
      if (current.current === target) setError(failure instanceof Error ? failure.message : "记忆维护未完成");
    } finally { if (current.current === target) setBusy(false); }
  };
  if (!sessionId) return null;
  const jobs = [...(snapshot?.jobs || []), ...(snapshot?.scope_jobs || [])];
  const pending = jobs.some(job => ["pending", "running", "retry"].includes(job.status));
  const failed = jobs.find(job => job.status === "failed");
  const scoped = snapshot?.global_memory !== undefined || snapshot?.course_memory !== undefined;
  return (
    <section className="context-card" aria-label="学习记忆">
      <div className="card-heading"><BookOpen size={16} /><h3>学习记忆</h3></div>
      {snapshot?.presentation_restricted ? <p className="context-note">当前等待独立作答，学习记忆在本次提交后展示。</p> : <>
      {scoped ? <ScopedMemoryView memory={snapshot || {}} /> : <>
      <strong>长期学生记忆</strong>
      {snapshot?.student_memory?.content ? <Markdown>{snapshot.student_memory.content}</Markdown> : <p className="context-note">尚未提取稳定的学习偏好或特征。</p>}
      </>}
      <details>
      <summary>本次学习经历</summary>
      <strong>本次学习经历</strong>
      {snapshot?.session_experience?.content ? <Markdown>{snapshot.session_experience.content}</Markdown> : <p className="context-note">学习过程中会按阶段自动整理。</p>}
      </details>
      </>}
      <p className="muted small">{pending ? "正在整理已保存的学习记录。" : "模型根据真实记录总结；经历描述不代表已经掌握。"}</p>
      {(error || failed) && <p role="status" className="context-note">{error || "记忆更新未完成，原始学习记录已保留。"}</p>}
      <button className="secondary" disabled={busy || pending} onClick={() => void update()}><RefreshCw size={14} />{busy ? "正在更新" : "整理当前记忆"}</button>
      <details>
        <summary>纠正或忘记记忆</summary>
        {scoped && <select aria-label="记忆维护范围" value={scope} onChange={event => setScope(event.target.value)}>
          <option value="student">学生总体刻画</option><option value="course">本课程记忆</option>
          {snapshot?.concept_memories?.map(item => <option key={item.concept_id} value={`concept:${item.concept_id}`}>{item.concept_name || "知识点记忆"}</option>)}
        </select>}
        <textarea aria-label="纠正长期学生记忆" placeholder="例如：只有这道题想看完整解答，平时仍先给提示。" value={correction} onChange={event => setCorrection(event.target.value)} maxLength={4000} />
        <button className="secondary" disabled={busy || !correction.trim()} onClick={() => void maintain(scope, correction.trim())}>纠正所选记忆</button>
        <button className="secondary" disabled={busy} onClick={() => void maintain(scope)}>忘记所选记忆</button>
        <button className="secondary" disabled={busy} onClick={() => void maintain("experience")}>忘记本次经历</button>
      </details>
    </section>
  );
}
