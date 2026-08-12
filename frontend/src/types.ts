export type FileSource = "llmops" | "upload";
export type SearchStore = "postgresql" | "minio" | "neo4j";
export type FunctionId = "summary" | "proposal_draft";
export type ConversationStatus = "ready" | "loading" | "success" | "error";
export type ProposalTypeRequest = "auto" | "purchase" | "event_attendance" | "general";
export type ProposalType = Exclude<ProposalTypeRequest, "auto">;
export type ProposalTypeSource = "user" | "rule" | "fallback";
export const PROPOSAL_TEXT_MAX_LENGTH = 2000;
export const PROPOSAL_CLARIFICATION_ANSWER_MAX_LENGTH = 500;

export interface DocumentSearchHit {
  source: FileSource;
  doc_id: string;
  revision_id: string;
  file_name: string;
  title: string;
  extension: string;
  size_bytes: number | null;
  modified_at?: string | null;
  score: number;
  match_source: "metadata" | "content";
  matched_stores?: SearchStore[];
}

export interface DocumentSearchResponse {
  query: string;
  normalized_query: string;
  hits: DocumentSearchHit[];
  result_count: number;
  elapsed_ms: number;
  over_budget: boolean;
  source: FileSource;
  search_mode?: "postgresql" | "multistore";
  queried_stores?: SearchStore[];
  llm_expanded?: boolean;
  timings_ms?: Record<string, number>;
  warnings?: string[];
}

export interface SelectedFilePayload {
  source: FileSource;
  file_name: string;
  title: string;
  doc_id: string;
  revision_id: string;
}

export interface DocumentPreviewResponse {
  doc_id: string;
  revision_id: string;
  artifact_type: "preview" | "canonical";
  media_type: string;
  content: string;
  size_bytes: number;
  elapsed_ms: number;
}

export interface DocumentGraphNode {
  id: string;
  type: "document" | "revision";
  label: string;
  status: string;
}

export interface DocumentGraphEdge {
  source: string;
  target: string;
  type: "HAS_REVISION" | "LATEST" | "SUPERSEDES";
}

export interface DocumentVersionGraphResponse {
  doc_id: string;
  nodes: DocumentGraphNode[];
  edges: DocumentGraphEdge[];
  degraded: boolean;
  warnings: string[];
  elapsed_ms: number;
}

export interface StoreConnectionState {
  configured: boolean;
  connected: boolean;
  degraded: boolean;
  message: string;
  latency_ms: number;
  metadata: Record<string, unknown>;
}

export interface StoresStatusResponse {
  overall: "ok" | "degraded" | "unavailable";
  postgresql: StoreConnectionState;
  minio: StoreConnectionState;
  neo4j: StoreConnectionState;
}

export interface UploadSettings {
  enabled: boolean;
  configured: boolean;
  relative_directory: string;
  destination_label: string;
  max_size_bytes: number;
  allowed_extensions: string[];
}

export interface PlaygroundSettingsResponse {
  upload: UploadSettings;
  proposal_draft: {
    relative_directory: string;
    destination_label: string;
  };
  local_llm_configured: boolean;
}

export interface ProposalDraftFields {
  title: string;
  approval_request: string;
  body: string;
}

export interface ProposalDraftRequest {
  instruction: string;
  selected_files: SelectedFilePayload[];
  proposal_type: ProposalTypeRequest;
}

export interface ProposalDraftRevisionRequest {
  feedback: string;
  selected_files: SelectedFilePayload[];
}

export interface ProposalClarificationAnswer {
  question_id: string;
  answer: string;
}

export interface ProposalDraftClarificationRequest {
  answers: ProposalClarificationAnswer[];
  selected_files: SelectedFilePayload[];
}

export interface ProposalClarificationQuestion {
  question_id: string;
  field_key: string;
  prompt: string;
  reason: "missing" | "conflict";
}

export interface ProposalCompletionSummary {
  schema_version: "proposal-completion-v1";
  status: "completed" | "needs_clarification" | "completed_with_omissions";
  questions: ProposalClarificationQuestion[];
  supported_claim_count: number;
  derived_claim_count: number;
  omitted_claim_count: number;
  conflicting_claim_count: number;
  clarification_round: number;
}

export interface ProposalEvidenceFilter {
  schema_version: "proposal-evidence-filter-v1";
  input_document_count: number;
  included_document_count: number;
  excluded_document_count: number;
  input_citation_count: number;
  included_citation_count: number;
  excluded_citation_count: number;
  excluded_reason_counts: Record<string, number>;
  fallback_used: boolean;
}

export interface ProposalParagraphBlock {
  type: "paragraph";
  text: string;
}

export interface ProposalListBlock {
  type: "list";
  ordered: boolean;
  items: string[];
}

export interface ProposalTableBlock {
  type: "table";
  headers: string[];
  rows: string[][];
}

export type ProposalBlock = ProposalParagraphBlock | ProposalListBlock | ProposalTableBlock;
export type ProposalSemanticRole =
  | "purpose"
  | "background"
  | "request"
  | "details"
  | "budget"
  | "schedule"
  | "expected_effect"
  | "attachments"
  | "notes"
  | "other";

export interface ProposalSectionV2 {
  heading: string;
  semantic_role: ProposalSemanticRole;
  citations: string[];
  blocks: ProposalBlock[];
  missing_information: string[];
}

export interface ProposalDocumentV2 {
  schema_version: "proposal-document-v2";
  proposal_type: ProposalType | null;
  title: string;
  approval_request: string;
  sections: ProposalSectionV2[];
  missing_information: string[];
}

export interface ProposalContextUsage {
  schema_version: "proposal-context-usage-v1";
  source_citation_count: number;
  packed_citation_count: number;
  deduplicated_citation_count: number;
  source_document_count: number;
  packed_document_count: number;
  context_budget_chars: number;
  context_chars: number;
  estimated_input_tokens: number;
  truncated: boolean;
  retry_count: number;
  first_attempt_context_chars: number | null;
  prompt_eval_count: number | null;
}

export interface ProposalDraftGenerated {
  draft_id: string;
  title: string;
  fields: ProposalDraftFields;
  document: ProposalDocumentV2;
  proposal_type: ProposalType;
  proposal_type_source: ProposalTypeSource;
  evidence_filter: ProposalEvidenceFilter;
  context_usage: ProposalContextUsage;
  completion: ProposalCompletionSummary;
  file_name: string | null;
  download_url: string | null;
  destination_label: string | null;
  saved_to_smb: boolean;
  model_used: string;
  elapsed_ms: number;
  timings_ms: Record<string, number>;
  clarification_of_draft_id?: string;
  answered_question_count?: number;
  revision_of_draft_id?: string;
  revision_number?: string;
  revision_summary?: string;
}

export interface FileUploadResponse {
  file_name: string;
  size_bytes: number;
  uploaded_at: string;
  destination_label: string;
  indexed: boolean;
  conversation_ready: boolean;
  selected_file: DocumentSearchHit | null;
}

export type UploadStatus = "idle" | "pending" | "success" | "error";

export interface Citation {
  index: number;
  doc_id: string;
  revision_id: string;
  chunk_id: string;
  title: string;
  section_path: string[];
  location: Record<string, unknown>;
  excerpt: string;
  scores: { vector?: number; lexical?: number; trigram?: number; rrf: number };
}

export interface RetrievalMetadata {
  trace_id: string;
  scope: Array<{ doc_id: string; revision_id: string }>;
  candidate_count: number;
  result_count: number;
  grounded: boolean;
  decision: "answerable" | "insufficient_evidence";
  degraded_dependencies: string[];
  timings_ms: Record<string, number>;
  elapsed_ms: number;
  over_budget: boolean;
}

export interface ConversationArtifact {
  doc_id: string;
  revision_id: string;
  preview_url: string;
  canonical_url: string;
  graph_url: string;
}

export interface ChatResponse {
  request_id: string;
  session_id: string;
  provider_used: "local";
  model_used: string;
  assistant_message: string;
  tool_calls: Array<{
    tool_id: "search_selected_llmops";
    tool_name: string;
    status: "ok" | "error";
    elapsed_ms: number;
    result_count: number;
    error_code: string;
  }>;
  elapsed_ms: number;
  warnings: string[];
  error_code: string;
  over_budget: boolean;
  rag_grounding?: Record<string, unknown> | null;
  citations: Citation[];
  retrieval?: RetrievalMetadata | null;
  artifacts: ConversationArtifact[];
  token_usage?: {
    provider: "local";
    model: string;
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    calls: number;
  } | null;
}

export interface ChatUiMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  selectedFiles?: string[];
  response?: ChatResponse;
  error?: boolean;
  pending?: boolean;
  searchResponse?: DocumentSearchResponse;
  proposalDraft?: ProposalDraftGenerated;
  proposalDraftSelectedFiles?: SelectedFilePayload[];
  proposalClarificationCompleted?: boolean;
}

export interface ConversationDefinition {
  label: string;
  title: string;
  subtitle: string;
  placeholder: string;
}

export interface FunctionDefinition extends ConversationDefinition {
  id: FunctionId;
  description: string;
  icon: string;
  requiresFiles: boolean;
  resultDescription: string;
}

export interface ApiErrorBody {
  detail?: unknown;
  message?: string;
  error_code?: string;
  request_id?: string;
}
