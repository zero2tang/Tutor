import { useEffect, useState } from "react";
import { ArrowUpRight, Search, SlidersHorizontal, Timer } from "lucide-react";
import { api } from "../api";
import type { Concept, Question, SearchResult } from "../types";
import { Markdown } from "../components/Markdown";
import { DifficultyBadge } from "../components/DifficultyBadge";

export function SearchPage({
  courseId,
  concepts,
  onPractice,
}: {
  courseId: string;
  concepts: Concept[];
  onPractice: (question: Question) => void;
}) {
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState("keyword");
  const [concept, setConcept] = useState("");
  const [kind, setKind] = useState("");
  const [result, setResult] = useState<SearchResult | null>(null);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      setBusy(true);
      setError("");
      try {
        const params = new URLSearchParams({
          q: query,
          mode,
          limit: "12",
          course_id: courseId,
        });
        if (concept) params.set("concept_ids", concept);
        if (kind) params.set("question_type", kind);
        setResult(
          await api<SearchResult>(`/search/questions?${params}`, {
            signal: controller.signal,
          }),
        );
      } catch (reason) {
        if (!controller.signal.aborted)
          setError(reason instanceof Error ? reason.message : "搜索失败");
      } finally {
        if (!controller.signal.aborted) setBusy(false);
      }
    }, 300);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query, mode, concept, kind, courseId]);
  return (
    <div className="page-content">
      <div className="page-title">
        <span className="eyebrow">EXERCISE LIBRARY</span>
        <h1>找到你需要的那道题</h1>
        <p>按原题关键词查找，或描述问题，发现相似练习。</p>
      </div>
      <div className="search-toolbar">
        <div className="search-field">
          <Search size={20} />
          <input
            aria-label="搜索题目"
            placeholder="输入题干、题号，或描述你想练习的问题"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          {busy && <span className="small-spinner" />}
        </div>
        <div className="filter-row">
          <SlidersHorizontal size={16} />
          <select
            aria-label="搜索方式"
            value={mode}
            onChange={(e) => setMode(e.target.value)}
          >
            <option value="keyword">关键词搜索</option>
            <option value="hybrid">语义混合搜索</option>
          </select>
          <select
            aria-label="筛选知识点"
            value={concept}
            onChange={(e) => setConcept(e.target.value)}
          >
            <option value="">全部知识点</option>
            {concepts.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
          <select
            aria-label="筛选题型"
            value={kind}
            onChange={(e) => setKind(e.target.value)}
          >
            <option value="">全部题型</option>
            <option value="choice">单选题</option>
            {courseId !== "math" && (
              <option value="multi_choice">多选题</option>
            )}
            <option value="fill">填空题</option>
          </select>
        </div>
      </div>
      {error && <div className="error-banner">{error}</div>}
      <div className="results-meta">
        <span>本次返回 {result?.items.length || 0} 道题</span>
        {result && (
          <span>
            <Timer size={13} />
            {Math.round(result.timings.total_ms)} ms ·{" "}
            {result.channels_used
              .map((v) =>
                v === "keyword" ? "关键词" : v === "vector" ? "向量" : v,
              )
              .join(" + ")}
          </span>
        )}
      </div>
      <div className="search-results">
        {result?.items.map((item) => (
          <article className="result-card" key={item.id}>
            <div className="result-card-top">
              <span className="tag">
                {{ choice: "单选题", multi_choice: "多选题", fill: "填空题" }[
                  item.question.question_type
                ] || "练习"}
              </span>
              <span className="muted small">{item.question.source_id}</span>
              <DifficultyBadge question={item.question} />
            </div>
            <Markdown>{item.question.stem}</Markdown>
            <div className="concept-tags">
              {item.question.concept_ids.map((id) => (
                <span key={id}>
                  {concepts.find((c) => c.id === id)?.name || id}
                </span>
              ))}
            </div>
            {item.question.availability !== "ready" && (
              <p className="question-availability-note">
                {item.question.availability_note ||
                  "原题内容待核验，暂不开放独立练习。"}
              </p>
            )}
            <div className="result-footer">
              <span>
                {item.question.generated
                  ? "生成变式"
                  : courseId === "math"
                    ? "XES3G5M · 数学课程"
                    : "MOOCRadar · 力学课程"}
              </span>
              <button
                className="text-button accent"
                disabled={item.question.availability !== "ready"}
                title={item.question.availability_note}
                onClick={() => onPractice(item.question)}
              >
                {item.question.availability !== "ready"
                  ? "暂不可独立练习"
                  : "开始练习"}
                <ArrowUpRight size={15} />
              </button>
            </div>
          </article>
        ))}
      </div>
      {result?.items.length === 0 && !busy && (
        <div className="empty-state">
          <Search size={32} />
          <h3>没有找到匹配题目</h3>
          <p>试试减少筛选条件，或切换到语义混合搜索。</p>
        </div>
      )}
    </div>
  );
}
