export type View =
  "workspace" | "search" | "concepts" | "profile" | "textbooks";
export interface Concept {
  id: string;
  name: string;
  module: string;
  summary: string;
  topic_type?: string;
  question_count: number;
}
export interface Course {
  id: string;
  title: string;
  description: string;
  concept_count: number;
  question_count: number;
  material_count: number;
  index_status?: { es: string; neo4j: string };
}
export interface Citation {
  id: string;
  title: string;
  source_ref: string;
  origin?: string;
}
export interface Question {
  id: string;
  exercise_id?: string;
  course_id?: string;
  source_id: string;
  stem: string;
  stem_format?: string;
  images?: { url: string; alt: string }[];
  options: Record<string, string>;
  question_type: string;
  cognitive_type: string;
  difficulty_value?: number | null;
  difficulty_source?: { value?: number | null; status?: string; method?: string; version?: string; reference_group?: string };
  difficulty_target?: { minimum: number; maximum: number } | null;
  practice_type?: string;
  concept_ids: string[];
  source_ref: string;
  generated: boolean;
  availability?: string;
  availability_note?: string;
  blank_count?: number;
  teaching_pack_id?: string | null;
  textbook_document_id?: string | null;
}
export interface Diagnosis {
  feedback: string;
  misconception: string;
  reflection: string;
  confidence: number;
  level: string;
  answer_key?: unknown;
}
export interface StateChange {
  concept_id: string;
  before: Record<string, number | null>;
  after: Record<string, number | null>;
}
export interface Assessment {
  attempt_id: string;
  question_id: string;
  result: string;
  score: number | null;
  assisted: boolean;
  diagnosis: Diagnosis;
  state_updates: StateChange[];
}
export interface ExerciseBatch {
  id: string;
  title: string;
  questions: Question[];
  assisted_question_ids: string[];
}
export interface BatchAssessment {
  batch_id: string;
  items: Assessment[];
  summary: string;
  next_action: string;
  correct_count: number;
  graded_count: number;
  total: number;
  score: number | null;
}
export interface Message {
  id: string;
  role: string;
  agent: string;
  content: string;
  citations: Citation[];
  metadata: {
    record_only?: boolean;
    kind?: string;
    question?: Question;
    batch_id?: string;
    answers?: Record<string, string>;
    exercise_batch?: ExerciseBatch;
    batch_assessment?: BatchAssessment;
    assessment?: Assessment;
    clarification?: ClarificationDecision;
  };
  created_at: string;
}
export interface ClarificationDecision {
  objective: string;
  question: string;
  missing_information?: string;
  options?: string[];
}
export interface LearningProgress {
  objective: string;
  status: "awaiting_student" | "awaiting_clarification" | "completed";
  current_step: string;
  next_step: string;
  completed_steps: string[];
}
export interface LearningSession {
  id: string;
  course_id?: string;
  title: string;
  goal: string;
  phase: string;
  concept_ids: string[];
  agenda: string[];
  messages: Message[];
  pending_question: Question | null;
  pending_batch?: ExerciseBatch | null;
  pending_assisted: boolean;
  pending_interaction?: {
    wait_id: string;
    question_id?: string | null;
    kind?: string;
  } | null;
  active_turn_id: string | null;
  pending_decision?: ClarificationDecision | null;
  learning_progress?: LearningProgress | null;
  session_goals?: SessionGoals | null;
  archived_problem?: {
    question_id: string | null;
    stem: string;
    status: string;
    notice: string;
  } | null;
  guidance?: GuidanceState | null;
}
export interface SessionGoalNode {
  goal_id: string;
  parent_id: string | null;
  description: string;
  success_criteria: string[];
  status:
    | "planned"
    | "active"
    | "blocked"
    | "awaiting_student"
    | "completed"
    | "cancelled";
  prerequisites: string[];
}
export interface SessionGoals {
  session_id: string;
  root_goal_id: string;
  active_goal_id: string | null;
  nodes: SessionGoalNode[];
  return_stack: {
    goal_id?: string;
    current_step?: string;
    remediation_goal_id?: string;
  }[];
  next_action: string;
  version: number;
}
export type GuidanceAction =
  "start" | "reply" | "hint" | "explain" | "transfer" | "close";
export interface GuidanceProblemInput {
  stem: string;
  source_kind: "pasted";
}
export interface GuidanceState {
  id: string;
  version: number;
  status: "awaiting_student" | "awaiting_clarification" | "ready" | "completed";
  closed?: boolean;
  problem: {
    id: string;
    stem: string;
    source_kind: string;
  };
  pending_question: string;
  pending_question_id?: string | null;
  response_expectation?: "answer" | "optional" | "none";
  focus: string;
  help_level: number;
  assisted: boolean;
  turn_count: number;
  sources: Citation[];
  analysis_summary?: string;
  transfer?: {
    id: string;
    stem: string;
    target: string;
    assisted: boolean;
    response_count: number;
  } | null;
}
export interface SessionSummary {
  id: string;
  course_id?: string;
  title: string;
  phase: string;
  updated_at: string;
}
export interface StudentState {
  concept_id: string;
  name: string;
  mastery: Record<string, number | null>;
  evidence_count: number;
  status: string;
  misconceptions: string[];
  next_review_at: string | null;
}
export interface ScopedMemoryText {
  scope_key?: string;
  scope_kind?: string;
  content: string;
  version: number;
  concept_id?: string;
  concept_name?: string;
  status?: string;
  enabled?: boolean;
  source_refs?: string[];
}
export interface Profile {
  memory_schema?: string;
  global_memory?: ScopedMemoryText | null;
  course_memory?: ScopedMemoryText | null;
  concept_memories?: ScopedMemoryText[];
  student_memory?: { content: string; version: number } | null;
  session_experiences?: {
    session_id: string;
    content: string;
    status: string;
    version: number;
  }[];
  step_learning?: {
    independent_transfer_count: number;
    assisted_completion_count: number;
    observed_steps: number;
    pending_review_count: number;
    note: string;
    evidence: {
      id: string;
      question_id: string;
      independent: boolean;
      assisted: boolean;
      stage: string;
      simulated: boolean;
      scope: string;
    }[];
  };
  states: StudentState[];
  assessed_concepts: number;
  attempt_count: number;
  correct_count: number;
  memories: {
    id: string;
    kind: string;
    text: string;
    concept_id: string | null;
  }[];
}
export interface GraphData {
  nodes: { id: string; name: string }[];
  edges: {
    source: string;
    target: string;
    relation_type: string;
    origin: string;
    verified: boolean;
  }[];
  seed_ids: string[];
}
export interface ToolCall {
  name: string;
  status: string;
  duration_ms?: number;
  channels?: string[];
}
export interface AgentRun {
  id: string;
  agent: string;
  skill: string;
  status: string;
  duration_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  tool_calls: ToolCall[];
  error: string | null;
}
export interface SearchResult {
  items: {
    id: string;
    title: string;
    text: string;
    question: Question;
    score: number;
  }[];
  channels_used: string[];
  timings: Record<string, number>;
  degraded: boolean;
  reason: string;
}
export interface Health {
  status: string;
  services: Record<string, { status: string }>;
  model: string;
  embedding_model: string;
}
