import { useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  FlaskConical,
  GitBranch,
  Lightbulb,
  LoaderCircle,
  MessageCircle,
  Plus,
  Send,
  Sparkles,
  Target,
} from "lucide-react";
import { Markdown } from "../components/Markdown";
import { KnowledgeGraph } from "../components/KnowledgeGraph";
import { QuestionCard } from "../components/QuestionCard";
import { GuidancePanel } from "../components/GuidancePanel";
import { NewProblemEntry } from "../components/NewProblemEntry";
import { SessionGoals } from "../components/SessionGoals";
import { LearningMemory } from "../components/LearningMemory";
import {
  ExerciseBatchCard,
  type BatchSubmission,
} from "../components/ExerciseBatchCard";
import { BatchAssessmentMessage } from "../components/BatchAssessmentMessage";
import { GoalClarification } from "../components/GoalClarification";
import { StreamingMessage } from "../components/StreamingMessage";
import type { StreamingDraft } from "../streaming/drafts";
import {
  AssessmentMessage,
  assessmentReceipt,
} from "../components/AssessmentMessage";
import type {
  AgentRun,
  Concept,
  GraphData,
  LearningSession,
  Profile,
  GuidanceAction,
  GuidanceProblemInput,
} from "../types";

export interface SendOptions {
  intent?: string;
  cognitive_type?: string;
  generate?: boolean;
  exercise_count?: number;
  concept_ids?: string[];
}
interface Props {
  courseId: string;
  courseTitle: string;
  session: LearningSession | null;
  concepts: Concept[];
  profile: Profile | null;
  graph: GraphData | null;
  runs: AgentRun[];
  busy: boolean;
  activity: string;
  liveTools: string[];
  drafts: StreamingDraft[];
  onStart: (goal: string, ids: string[]) => void;
  onSend: (content: string, options?: SendOptions) => void;
  onAnswer: (answer: string) => void;
  onGuidance: (
    action: GuidanceAction,
    text?: string,
    problem?: GuidanceProblemInput,
  ) => void;
  onNewSession?: () => void;
  onBatchAnswer: (submission: BatchSubmission) => void;
  onSkip: () => void;
  onSelectConcept: (id: string) => void;
}
const agentNames: Record<string, string> = {
  knowledge: "知识点学习",
  exercise: "习题讲解",
  assessment: "学习评估",
  supervisor: "学习决策",
};
const progressStatusNames: Record<string, string> = {
  awaiting_student: "等待你的作答或反馈",
  awaiting_clarification: "等待确认学习目标",
  completed: "本次计划已完成",
};

export function Workspace(props: Props) {
  const {
    courseId,
    courseTitle,
    session,
    concepts,
    profile,
    graph,
    runs,
    busy,
    activity,
    liveTools,
    drafts,
    onStart,
    onSend,
    onAnswer,
    onGuidance,
    onNewSession,
    onBatchAnswer,
    onSkip,
    onSelectConcept,
  } = props;
  const [input, setInput] = useState(""),
    [goal, setGoal] = useState(""),
    [practiceType, setPracticeType] = useState("auto"),
    [exerciseCount, setExerciseCount] = useState(0),
    [traceOpen, setTraceOpen] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const conversation = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const [showLatest, setShowLatest] = useState(false);
  useEffect(() => {
    // 会话切换时清理尚未发送的输入，避免把上一段学习内容发给新会话。
    setInput("");
    following.current = true;
    setShowLatest(false);
    if (conversation.current)
      conversation.current.scrollTop = conversation.current.scrollHeight;
  }, [session?.id]);
  useEffect(() => {
    const element = conversation.current;
    if (!element) return;
    if (following.current) element.scrollTop = element.scrollHeight;
    else setShowLatest(true);
  }, [session?.messages.length, busy, drafts]);
  const guidanceActive = Boolean(session?.guidance && !session.guidance.closed);
  const independentResponse = Boolean(
    (session?.pending_question && !session.pending_assisted)
      || session?.pending_batch?.questions.some(question => !session.pending_batch?.assisted_question_ids.includes(question.id))
    || (session?.guidance?.transfer && !session.guidance.transfer.assisted && !session.guidance.closed),
  );
  const isMath = courseId === "math" || courseId === "junior_algebra";
  const mathQuickStarts = concepts
    .filter(
      (concept) =>
        concept.topic_type !== "category" && concept.question_count > 0,
    )
    .sort((a, b) => b.question_count - a.question_count)
    .slice(0, 3);
  const target =
    session?.concept_ids ||
    (isMath
      ? mathQuickStarts.map((concept) => concept.id)
      : ["mechanics:c19", "mechanics:c160"]);
  const quickTopics = isMath
    ? mathQuickStarts.map((concept, index) => ({
        title: concept.name.trim(),
        description: `${concept.question_count} 道关联题目 · 从概念开始理解`,
        goal: `学习${concept.name.trim()}，先讲解基本方法，再给出一个例子`,
        ids: [concept.id],
        icon: [GitBranch, Lightbulb, FlaskConical][index],
        color: ["mint", "sand", "blue"][index],
      }))
    : [
        {
          title: "应力与应变",
          description: "从单位面积的力，到相对变形",
          goal: "理解应力与应变的区别，以及二者如何联系",
          ids: ["mechanics:c19", "mechanics:c160"],
          icon: GitBranch,
          color: "mint",
        },
        {
          title: "虚位移与虚功",
          description: "用一个假想位移理解平衡",
          goal: "理解虚位移与虚功原理",
          ids: ["mechanics:c2", "mechanics:c48"],
          icon: Lightbulb,
          color: "sand",
        },
        {
          title: "有限元方法",
          description: "从连续问题走向可计算模型",
          goal: "学习有限元方法的基本思路与步骤",
          ids: ["mechanics:c167"],
          icon: FlaskConical,
          color: "blue",
        },
      ];
  const targetConcepts = concepts.filter((c) => target.includes(c.id));
  const selectedStates =
    profile?.states.filter((state) => target.includes(state.concept_id)) || [];
  const send = () => {
    if (!input.trim() || busy) return;
    const content = input.trim();
    setInput("");
    if (session) onSend(content);
    else onStart(content, []);
  };
  const quickStart = (text: string, ids: string[]) => onStart(text, ids);
  return (
    <div className="workspace-layout">
      <section className="learning-area" aria-label={`${courseTitle}学习空间`}>
        {!session ? (
          <div className="welcome-content">
            <div className="welcome-label">
              <span className="status-dot" />
              {isMath ? "MATHEMATICS LEARNING LAB" : "MECHANICS LEARNING LAB"}
            </div>
            <h1>
              从一个问题开始，
              <br />
              <span>{isMath ? "把数学学明白。" : "把力学学明白。"}</span>
            </h1>
            <p className="welcome-description">
              沿着知识之间的联系，理解公式背后的逻辑。
              <br />
              讲解、练习与评估，在同一个学习空间里完成。
            </p>
            <NewProblemEntry
              busy={busy}
              defaultExpanded
              onStart={(stem) =>
                onGuidance("start", "", { stem, source_kind: "pasted" })
              }
            />
            <div className="goal-input-card">
              <div>
                <Target size={19} />
                <span>今天，你想学会什么？</span>
              </div>
              <textarea
                aria-label="学习目标"
                placeholder={
                  isMath
                    ? "例如：如何理解分数？遇到应用题时怎样找到数量之间的关系？"
                    : "例如：应力和应变有什么区别？如何用它们分析拉杆变形？"
                }
                value={goal}
                onChange={(event) => setGoal(event.target.value)}
              />
              <div className="goal-input-bottom">
                <span>支持一个或多个知识点</span>
                <button
                  className="primary"
                  disabled={busy || !goal.trim()}
                  onClick={() => onStart(goal.trim(), [])}
                >
                  开始学习
                  <ArrowRight size={17} />
                </button>
              </div>
            </div>
            <div className="section-label">也可以从这里开始</div>
            <div className="quick-topics">
              {quickTopics.map((topic) => (
                <button
                  key={topic.ids.join(",")}
                  disabled={busy}
                  onClick={() => quickStart(topic.goal, topic.ids)}
                >
                  <div className={`topic-icon ${topic.color}`}>
                    <topic.icon size={21} />
                  </div>
                  <strong>{topic.title}</strong>
                  <p>{topic.description}</p>
                  <ArrowRight size={17} />
                </button>
              ))}
            </div>
            <div className="learning-principle">
              <BookOpen size={17} />
              <span>
                知识点不会因“看过”就标记为掌握，学习状态来自你的真实作答。
              </span>
            </div>
          </div>
        ) : (
          <>
            <div className="goal-banner">
              <div className="goal-banner-icon">
                <Target size={20} />
              </div>
              <div>
                <span className="eyebrow">当前学习目标</span>
                <h2>{session.goal}</h2>
                <div className="concept-tags">
                  {targetConcepts.map((c) => (
                    <span key={c.id}>{c.name}</span>
                  ))}
                </div>
              </div>
              <span className="session-state">
                {session.phase === "awaiting_answer"
                  ? "等待作答"
                  : session.phase === "assessing"
                    ? "正在评估"
                    : session.phase === "awaiting_clarification"
                      ? "待确认学习目标"
                      : "学习中"}
              </span>
            </div>
            {!guidanceActive && (
              <NewProblemEntry
                busy={busy}
                blocked={Boolean(
                  session.pending_question ||
                  session.pending_batch,
                )}
                onNewSession={onNewSession}
                onStart={(stem) =>
                  onGuidance("start", "", { stem, source_kind: "pasted" })
                }
              />
            )}
            {!guidanceActive && (
              <div className="learning-toolbar">
                <button
                  disabled={busy}
                  onClick={() =>
                    onSend("请继续讲解当前知识点，并给出一个具体例子", {
                      intent: "teach",
                    })
                  }
                >
                  <BookOpen size={15} />
                  知识讲解
                </button>
                <button
                  disabled={
                    busy ||
                    !!session.pending_question ||
                    !!session.pending_batch
                  }
                  onClick={() =>
                    onSend("请给我一道与当前目标相关的练习", {
                      intent: "practice",
                      ...(practiceType !== "auto"
                        ? { cognitive_type: practiceType }
                        : {}),
                    })
                  }
                >
                  <Plus size={15} />
                  检索练习
                </button>
                <button
                  disabled={
                    busy ||
                    !!session.pending_question ||
                    !!session.pending_batch
                  }
                  onClick={() =>
                    onSend(
                      exerciseCount
                        ? `请生成 ${exerciseCount} 道当前知识点的新题，作答后统一评估`
                        : "请根据当前学习需要安排新题，自主决定题数、题型与难度范围",
                      {
                        intent: "practice",
                        generate: true,
                        ...(practiceType !== "auto"
                          ? { cognitive_type: practiceType }
                          : {}),
                        ...(exerciseCount
                          ? { exercise_count: exerciseCount }
                          : {}),
                      },
                    )
                  }
                >
                  <Sparkles size={15} />
                  安排新练习
                </button>
                <select
                  aria-label="练习认知类型"
                  value={practiceType}
                  onChange={(e) => setPracticeType(e.target.value)}
                >
                  <option value="auto">类型由助手决定</option>
                  <option value="recall">记忆</option>
                  <option value="understanding">理解</option>
                  <option value="application">应用</option>
                </select>
                <select
                  aria-label="练习组题数"
                  value={exerciseCount}
                  onChange={(event) =>
                    setExerciseCount(Number(event.target.value))
                  }
                >
                  <option value={0}>题数由助手决定</option>
                  {[1, 2, 3, 4, 5, 6].map((count) => (
                    <option key={count} value={count}>
                      {count} 题
                    </option>
                  ))}
                </select>
              </div>
            )}
            <div
              className="conversation"
              ref={conversation}
              onScroll={(event) => {
                const element = event.currentTarget;
                following.current =
                  element.scrollHeight -
                    element.scrollTop -
                    element.clientHeight <
                  80;
                if (following.current) setShowLatest(false);
              }}
            >
              {session.messages
                .filter(
                  (message) =>
                    !(
                      message.metadata.kind === "question" &&
                      (message.metadata.batch_id ||
                        message.metadata.record_only)
                    ),
                )
                .map((message) => (
                  <article
                    key={message.id}
                    className={`message ${message.role === "user" ? "user-message" : "assistant-message"}`}
                  >
                    {message.role !== "user" && (
                      <div className="message-identity">
                        <div className="agent-avatar">理</div>
                        <strong>理序</strong>
                        <span>{agentNames[message.agent] || "学习助手"}</span>
                      </div>
                    )}
                    <div className="message-body">
                      {message.metadata.batch_assessment ? (
                        <BatchAssessmentMessage
                          assessment={message.metadata.batch_assessment}
                          concepts={concepts}
                        />
                      ) : message.metadata.assessment ? (
                        <AssessmentMessage
                          assessment={message.metadata.assessment}
                          content={message.content}
                          concepts={concepts}
                        />
                      ) : (
                        <Markdown>{message.content}</Markdown>
                      )}
                      {!guidanceActive && message.metadata.exercise_batch && (
                        <ExerciseBatchCard
                          key={message.metadata.exercise_batch.id}
                          batch={
                            session.pending_batch?.id ===
                            message.metadata.exercise_batch.id
                              ? session.pending_batch
                              : message.metadata.exercise_batch
                          }
                          pending={
                            session.pending_batch?.id ===
                            message.metadata.exercise_batch.id
                          }
                          busy={busy}
                          onSubmit={onBatchAnswer}
                          onSkip={onSkip}
                          submittedAnswers={
                            session.messages.find(
                              (item) =>
                                item.metadata.kind === "batch_answer" &&
                                item.metadata.batch_id ===
                                  message.metadata.exercise_batch?.id,
                            )?.metadata.answers
                          }
                          onExplain={(index) =>
                            onSend(`请讲解这组练习的第 ${index + 1} 题`, {
                              intent: "explain",
                            })
                          }
                        />
                      )}
                      {!guidanceActive && message.metadata.clarification && (
                        <GoalClarification
                          decision={message.metadata.clarification}
                          active={
                            session.pending_decision?.question ===
                              message.metadata.clarification.question &&
                            session.messages[session.messages.length - 1]
                              ?.id === message.id
                          }
                          busy={busy}
                          onChoose={(option) => onSend(option)}
                        />
                      )}
                      {!guidanceActive && message.metadata.question && (
                        <QuestionCard
                          key={message.metadata.question.id}
                          question={message.metadata.question}
                          pending={
                            session.pending_question?.id ===
                            message.metadata.question.id
                          }
                          busy={busy}
                          assisted={session.pending_assisted}
                          onAnswer={onAnswer}
                          onExplain={() =>
                            onSend("请讲解当前题目的解题过程", {
                              intent: "explain",
                            })
                          }
                          onSkip={onSkip}
                        />
                      )}
                      {!!message.citations?.length && (
                        <details className="citation-list">
                          <summary>
                            <BookOpen size={13} />
                            参考了 {message.citations.length} 条课程材料
                            <ChevronDown size={13} />
                          </summary>
                          {message.citations.map((citation) => (
                            <div key={citation.id}>
                              <span>{citation.title}</span>
                              {citation.source_ref.startsWith("http") ? (
                                <a
                                  href={citation.source_ref}
                                  target="_blank"
                                  rel="noreferrer"
                                >
                                  查看来源
                                </a>
                              ) : (
                                <small>{citation.source_ref}</small>
                              )}
                            </div>
                          ))}
                        </details>
                      )}
                      {message.metadata.assessment && (
                        <div
                          className={`assessment-receipt ${message.metadata.assessment.result}`}
                        >
                          <CheckCircle2 size={16} />
                          <span>
                            {assessmentReceipt(message.metadata.assessment)}
                          </span>
                        </div>
                      )}
                    </div>
                  </article>
                ))}
              {!guidanceActive &&
                session.pending_batch &&
                !session.messages.some(
                  (message) =>
                    message.metadata.exercise_batch?.id ===
                    session.pending_batch?.id,
                ) && (
                  <ExerciseBatchCard
                    key={session.pending_batch.id}
                    batch={session.pending_batch}
                    pending
                    busy={busy}
                    onSubmit={onBatchAnswer}
                    onSkip={onSkip}
                    onExplain={(index) =>
                      onSend(`请讲解这组练习的第 ${index + 1} 题`, {
                        intent: "explain",
                      })
                    }
                  />
                )}
              {!guidanceActive &&
                session.pending_question &&
                !session.messages.some(
                  (message) =>
                    !message.metadata.record_only &&
                    message.metadata.question?.id ===
                      session.pending_question?.id,
                ) && (
                  <QuestionCard
                    key={session.pending_question.id}
                    question={session.pending_question}
                    pending
                    busy={busy}
                    assisted={session.pending_assisted}
                    onAnswer={onAnswer}
                    onExplain={() =>
                      onSend("请讲解当前题目", { intent: "explain" })
                    }
                    onSkip={onSkip}
                  />
                )}
              {drafts.map((draft) => (
                <StreamingMessage key={draft.id} draft={draft} />
              ))}
              {session.guidance && (
                <GuidancePanel
                  state={session.guidance}
                  busy={busy}
                  onAction={(action) => onGuidance(action)}
                  onNewSession={onNewSession}
                />
              )}
              {!guidanceActive && session.archived_problem && (
                <aside className="legacy-teaching-notice" role="status">
                  <strong>原有习题可以继续学习</strong>
                  <Markdown>{session.archived_problem.stem}</Markdown>
                  <p>{session.archived_problem.notice}</p>
                  <button className="secondary" disabled={busy || !!session.pending_question || !!session.pending_batch}
                    onClick={() => onGuidance("start", "", { stem: session.archived_problem!.stem, source_kind: "pasted" })}>
                    讲解这道题
                  </button>
                </aside>
              )}
              {busy && (
                <div className="working-indicator">
                  <LoaderCircle size={17} />
                  <div>
                    <strong>{activity || "正在组织本轮学习…"}</strong>
                    {liveTools.length > 0 && (
                      <span>{liveTools[liveTools.length - 1]}</span>
                    )}
                  </div>
                </div>
              )}
              <div ref={bottom} />
            </div>
            {showLatest && (
              <button
                className="latest-output"
                onClick={() => {
                  following.current = true;
                  setShowLatest(false);
                  bottom.current?.scrollIntoView({
                    behavior: "smooth",
                    block: "end",
                  });
                }}
              >
                <ChevronDown size={14} />
                查看最新输出
              </button>
            )}
          </>
        )}
        {session && (
          <div className="composer-wrap">
            <div className="composer">
              <textarea
                aria-label="向学习助手提问"
                placeholder={
                  guidanceActive
                    ? "写出你的思路、尝试的步骤，或直接追问为什么…"
                    : "继续追问，或说说你还不理解的地方…"
                }
                rows={2}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send();
                  }
                }}
              />
              <button
                aria-label="发送消息"
                className="send-button"
                disabled={busy || !input.trim()}
                onClick={send}
              >
                {busy ? (
                  <LoaderCircle size={19} className="spin" />
                ) : (
                  <Send size={19} />
                )}
              </button>
            </div>
            <div className="composer-hint">
              Enter 发送 · Shift + Enter 换行
              <span>学习状态为诊断估计，结合多次独立练习观察</span>
            </div>
          </div>
        )}
      </section>
      <aside className="context-panel">
        <div className="context-heading">
          <span>学习上下文</span>
          <span className="tag">{isMath ? "数学" : "力学"}</span>
        </div>
        <section className="context-card">
          <div className="card-heading">
            <GitBranch size={16} />
            <h3>知识点的联系</h3>
          </div>
          <KnowledgeGraph data={graph} onSelect={onSelectConcept} />
          <p className="context-note">
            {isMath
              ? "关系来自课程资料与题目关联；先修依赖需要单独确认。"
              : "从关联题目理解联系，先修关系单独标注。"}
          </p>
        </section>
        <section className="context-card">
          <div className="card-heading">
            <Target size={16} />
            <h3>{session ? "本次学习路径" : "一个完整的学习循环"}</h3>
          </div>
          <ol className="learning-steps">
            {(session?.agenda.length
              ? session.agenda
              : [
                  "明确目标与已有基础",
                  "理解概念和成立条件",
                  "独立练习与状态评估",
                ]
            ).map((step, index) => (
              <li key={index}>
                <span>{String(index + 1).padStart(2, "0")}</span>
                <p>{step}</p>
              </li>
            ))}
          </ol>
          {session?.learning_progress && (
            <div
              className="mini-mastery"
              aria-label="学习流程进度"
              aria-live="polite"
            >
              <div>
                <strong>当前进度</strong>
                <span>
                  {progressStatusNames[session.learning_progress.status]}
                </span>
              </div>
              <p className="context-note">
                {session.learning_progress.current_step}
              </p>
              <div>
                <strong>接下来</strong>
              </div>
              <p className="context-note">
                {session.learning_progress.next_step}
              </p>
              <p className="context-note">
                {session.learning_progress.completed_steps.length > 0
                  ? `已推进 ${session.learning_progress.completed_steps.length} 个流程步骤。`
                  : ""}
                流程推进不代表知识点已掌握，掌握情况以独立作答证据为准。
              </p>
            </div>
          )}
        </section>
        {!independentResponse && <SessionGoals state={session?.session_goals} />}
        <LearningMemory
          sessionId={session?.id}
          revision={session?.messages.length || 0}
        />
        <section className="context-card">
          <div className="card-heading">
            <CheckCircle2 size={16} />
            <h3>当前掌握情况</h3>
          </div>
          {selectedStates.slice(0, 3).map((state) => (
            <div className="mini-mastery" key={state.concept_id}>
              <div>
                <strong>{state.name}</strong>
                <span>
                  {state.evidence_count
                    ? `${state.evidence_count} 道证据题`
                    : "待评估"}
                </span>
              </div>
              <div className="mini-levels">
                {["beginner", "advanced", "proficient"].map((level, index) => (
                  <span
                    key={level}
                    className={state.mastery[level] !== null ? "observed" : ""}
                  >
                    {["基础", "进阶", "熟练"][index]}
                    <b>
                      {state.mastery[level] === null
                        ? "—"
                        : `${Math.round(state.mastery[level]! * 100)}%`}
                    </b>
                  </span>
                ))}
              </div>
            </div>
          ))}
          {!selectedStates.length && (
            <p className="context-note">
              完成练习后，这里会显示有证据的学习状态。
            </p>
          )}
        </section>
        <button
          className="trace-toggle"
          onClick={() => setTraceOpen(!traceOpen)}
        >
          <MessageCircle size={15} />
          <span>查看 Agent 活动记录</span>
          <ChevronRight size={15} className={traceOpen ? "rotated" : ""} />
        </button>
        {traceOpen && (
          <div className="trace-panel">
            {!runs.length && <p>完成一轮学习后可查看真实调用记录。</p>}
            {runs.slice(0, 12).map((run) => (
              <div className="trace-run" key={run.id}>
                <div>
                  <strong>{agentNames[run.agent] || run.agent}</strong>
                  <span>
                    {run.status === "completed"
                      ? "完成"
                      : run.status === "failed"
                        ? "失败"
                        : "执行中"}
                  </span>
                </div>
                <small>
                  {run.skill} · {(run.duration_ms / 1000).toFixed(1)}s ·{" "}
                  {run.prompt_tokens + run.completion_tokens} Tokens
                </small>
                {run.tool_calls.map((tool, index) => (
                  <p key={index}>
                    {tool.status === "failed" ? "×" : "✓"} {tool.name}
                  </p>
                ))}
                {run.error && <p className="error-text">{run.error}</p>}
              </div>
            ))}
          </div>
        )}
      </aside>
    </div>
  );
}
