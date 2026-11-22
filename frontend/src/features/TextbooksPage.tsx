import { useEffect, useState } from "react";
import { api } from "../api";
import { Markdown } from "../components/Markdown";
import type { Question } from "../types";

interface SourceRef {
  block_id: string;
  source_uri?: string;
  locator?: string;
  page?: number | null;
}
interface Document {
  id: string;
  title: string;
  edition: string;
  source_kind: string;
  source_uri: string;
  exercise_count: number;
  ready_pack_count: number;
  draft_pack_count: number;
  block_count: number;
  provenance: { license?: string; vision_calls?: number };
  stats?: { fallback_region_count?: number };
}
interface Pack {
  id: string;
  course_id?: string;
  question_id: string;
  status: string;
  document_id: string;
  problem: { stem: string; equation?: string; number?: string };
  concepts: { id: string; name: string }[];
  techniques: { id: string; name: string; conditions: string[] }[];
  source_refs: SourceRef[];
  provenance?: { origin?: string; analysis_version?: string };
  verification: { scope: string };
  solution_paths: {
    id: string;
    label: string;
    requirements: { foundations: string[]; techniques: string[] };
  }[];
  issues: { code: string; message: string }[];
}

export function TextbooksPage({
  courseId,
  busy,
  onPractice,
}: {
  courseId: string;
  busy: boolean;
  onPractice: (question: Question) => void;
}) {
  const [documents, setDocuments] = useState<Document[]>([]);
  const [selected, setSelected] = useState("");
  const [packs, setPacks] = useState<Pack[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    let active = true;
    api<{ items: Document[] }>(
      `/textbooks?course_id=${encodeURIComponent(courseId)}`,
    )
      .then((result) => {
        if (active) {
          setDocuments(result.items);
          setSelected(result.items[0]?.id || "");
        }
      })
      .catch((reason) => {
        if (active) setError(String(reason));
      });
    return () => {
      active = false;
    };
  }, [courseId]);
  useEffect(() => {
    let active = true;
    setPacks([]);
    if (selected) {
      setLoading(true);
      api<{ items: Pack[] }>(
        `/teaching-packs?course_id=${encodeURIComponent(courseId)}&document_id=${selected}&limit=100`,
      )
        .then((result) => {
          if (active) setPacks(result.items);
        })
        .catch((reason) => {
          if (active) setError(String(reason));
        })
        .finally(() => {
          if (active) setLoading(false);
        });
    }
    return () => {
      active = false;
    };
  }, [selected, courseId]);
  function practice(pack: Pack) {
    // 使用教材公开题干建立会话；具体题目、资料和来源仍由后端按 ID 读取。
    onPractice({
      id: pack.question_id,
      course_id: pack.course_id || courseId,
      source_id: pack.problem.number || pack.question_id,
      stem: pack.problem.stem,
      options: {},
      question_type: "unknown",
      cognitive_type: "understanding",
      concept_ids: [],
      source_ref: pack.source_refs[0]?.source_uri || "",
      generated: false,
      textbook_document_id: pack.document_id,
    });
  }
  const document = documents.find((item) => item.id === selected);
  return (
    <div className="page-content textbook-page">
      <div className="page-title">
        <span className="eyebrow">TEXTBOOK & METHODS</span>
        <h1>从教材题目开始学习</h1>
        <p>阅读题干、知识资料与来源，在对话中逐步分析思路。</p>
      </div>
      {error && <p role="alert">{error}</p>}
      {!documents.length && (
        <div className="empty-state">
          <h3>这门课程尚未导入教材</h3>
          <p>导入后会在这里展示章节、例题和教学分析。</p>
        </div>
      )}
      {documents.length > 0 && (
        <label>
          教材选段
          <select
            aria-label="选择教材"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
          >
            {documents.map((item) => (
              <option key={item.id} value={item.id}>
                {item.title}
              </option>
            ))}
          </select>
        </label>
      )}
      {document && (
        <section className="textbook-card">
          <strong>{document.title}</strong>
          <p>
            {document.edition} · {document.exercise_count} 道原文题 ·{" "}
            {document.block_count} 个资料块
          </p>
          <p className="muted">
            这是{" "}
            {document.source_kind === "curated_excerpt"
              ? "教材原文选段"
              : "已导入教材材料"}
            ，保留原书版次与定位。可以从题干开始引导讲解，已有分析与原文来源分别展示。
          </p>
          <small className="muted">
            许可：{document.provenance.license} · 视觉识别调用{" "}
            {document.provenance.vision_calls ?? 0} 次
          </small>
          {!!document.stats?.fallback_region_count && (
            <p className="muted">
              另有 {document.stats.fallback_region_count}{" "}
              处图像或版式区域待复核，尚未作为数学依据。
            </p>
          )}
          {/^https?:\/\//.test(document.source_uri) && (
            <p>
              <a href={document.source_uri} target="_blank" rel="noreferrer">
                教材原文与来源
              </a>
            </p>
          )}
        </section>
      )}
      {loading && <p>正在读取教材例题…</p>}
      <div className="textbook-pack-list">
        {packs.map((pack) => (
          <section className="textbook-card" key={pack.id}>
            <div className="textbook-heading">
              <strong>习题 {pack.problem.number}</strong>
              <span>
                {pack.status !== "ready"
                  ? "可从题干开始讲解"
                  : pack.provenance?.origin === "source_projection" ||
                      pack.provenance?.analysis_version ===
                        "textbook-catalog-v1"
                    ? "原文参考资料"
                    : "历史教学分析"}
              </span>
            </div>
            <Markdown>{pack.problem.stem}</Markdown>
            {pack.solution_paths.map((path) => (
              <div className="teaching-requirements" key={path.id}>
                <strong>{path.label}</strong>
                <p>
                  基础：
                  {path.requirements.foundations
                    .map(
                      (id) =>
                        pack.concepts.find((c) => c.id === id)?.name || id,
                    )
                    .join("、") || "理解题目条件与目标"}
                </p>
                <p>
                  可选技巧：
                  {path.requirements.techniques
                    .map(
                      (id) =>
                        pack.techniques.find((t) => t.id === id)?.name || id,
                    )
                    .join("、") || "这条路径不要求额外技巧"}
                </p>
              </div>
            ))}
            {pack.verification.scope && (
              <details className="textbook-analysis-scope">
                <summary>已有分析的覆盖说明</summary>
                <p className="muted">{pack.verification.scope}</p>
              </details>
            )}
            {pack.issues.map((issue, index) => (
              <p key={index}>{issue.message}</p>
            ))}
            <details>
              <summary>教材原文定位（{pack.source_refs.length} 处）</summary>
              {pack.source_refs.map((ref, index) => (
                <p className="muted small" key={index}>
                  {ref.page ? `第 ${ref.page} 页 · ` : ""}
                  {ref.locator || ref.block_id}
                </p>
              ))}
            </details>
            <button
              className="primary"
              disabled={busy || !pack.question_id || !pack.problem.stem.trim()}
              onClick={() => practice(pack)}
            >
              开始引导讲解
            </button>
          </section>
        ))}
      </div>
    </div>
  );
}
