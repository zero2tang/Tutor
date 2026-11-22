import { BookOpen, Lightbulb } from "lucide-react";
import { Markdown } from "./Markdown";
import type { GuidanceAction, GuidanceState } from "../types";

export function GuidancePanel({
  state,
  busy,
  onAction,
  onNewSession,
}: {
  state: GuidanceState;
  busy: boolean;
  onAction: (action: GuidanceAction) => void;
  onNewSession?: () => void;
}) {
  const active = !state.closed;
  // 与后端旧工作区投影一致：有待答问题才等待回答。
  const responseExpectation =
    state.response_expectation ??
    (state.pending_question ? "answer" : "none");
  const requiresAnswer = responseExpectation === "answer";
  const visibleQuestion =
    responseExpectation === "none" ? "" : state.pending_question;
  return (
    <section className="guidance-panel" aria-label="新习题引导讲解">
      <div className="guidance-heading">
        <strong>
          <Lightbulb size={17} /> 引导讲解
        </strong>
        <span>
          {state.closed
            ? "本题已结束"
            : state.status === "completed"
              ? "本轮已完成，可继续追问"
              : !requiresAnswer
                ? "可继续追问或结束"
                : state.status === "awaiting_clarification"
                  ? "请补充题目信息"
                  : "等待你的思路"}
        </span>
      </div>
      <details className="guidance-problem" open={state.turn_count <= 1}>
        <summary>{state.transfer ? "原题" : "当前习题"}</summary>
        <Markdown>{state.problem.stem}</Markdown>
      </details>
      {state.transfer && (
        <section className="guidance-transfer" aria-label="验证新题">
          <div className="guidance-heading">
            <strong>验证新题</strong>
            <span>
              {state.transfer.assisted
                ? "已获帮助"
                : requiresAnswer
                  ? "独立作答中"
                  : "独立练习"}
            </span>
          </div>
          <Markdown>{state.transfer.stem}</Markdown>
          <small>
            {requiresAnswer
              ? "先尝试自己的解法；需要时仍可以请求提示。"
              : "需要时可以继续追问，也可以结束本题。"}
          </small>
        </section>
      )}
      {state.analysis_summary && (
        <details className="guidance-analysis">
          <summary>题目分析</summary>
          <Markdown>{state.analysis_summary}</Markdown>
        </details>
      )}
      {active && (state.focus || visibleQuestion) && (
        <div className="guidance-current">
          {state.focus && (
            <p className="guidance-focus">当前关注：{state.focus}</p>
          )}
          {visibleQuestion && (
            <>
              <small>
                {requiresAnswer ? "接下来，请你想一想" : "如果愿意，可以想一想"}
              </small>
              <Markdown>{visibleQuestion}</Markdown>
            </>
          )}
        </div>
      )}
      {active && (
        <div className="guidance-actions">
          <button
            className="secondary"
            disabled={busy}
            onClick={() => onAction("hint")}
          >
            给一点提示
          </button>
          <button
            className="secondary"
            disabled={busy}
            onClick={() => onAction("explain")}
          >
            完整讲解
          </button>
          <button
            className="secondary"
            disabled={busy}
            onClick={() => onAction("transfer")}
          >
            新题验证
          </button>
          <button
            className="text-button"
            disabled={busy}
            onClick={() => onAction("close")}
          >
            结束本题
          </button>
        </div>
      )}
      {(state.transfer?.assisted || (!state.transfer && state.assisted)) && (
        <p className="muted small">
          本题已经获得帮助；理解后可以用新题检查自己的思路。
        </p>
      )}
      {!!state.sources.length && (
        <details className="citation-list guidance-sources">
          <summary>
            <BookOpen size={14} /> 参考资料（{state.sources.length}）
          </summary>
          {state.sources.map((source, index) => (
            <div key={`${source.id}:${index}`}>
              <span>{source.title}</span>
              {/^https?:\/\//.test(source.source_ref) ? (
                <a href={source.source_ref} target="_blank" rel="noreferrer">
                  查看来源
                </a>
              ) : (
                <small>{source.source_ref}</small>
              )}
            </div>
          ))}
        </details>
      )}
      {!active && onNewSession && (
        <button className="secondary" disabled={busy} onClick={onNewSession}>
          新建会话讲另一题
        </button>
      )}
    </section>
  );
}
