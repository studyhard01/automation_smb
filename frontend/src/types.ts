export type FileSource = "llmops" | "upload";
export type SearchStore = "postgresql" | "minio" | "neo4j";
export type FunctionId = "summary" | "proposal_draft";
export type ConversationStatus = "ready" | "loading" | "success" | "error";

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
}

export interface ProposalDraftGenerated {
  draft_id: string;
  fields: ProposalDraftFields;
  file_name: string;
  download_url: string;
  destination_label: string;
  saved_to_smb: boolean;
  model_used: string;
  elapsed_ms: number;
  timings_ms: Record<string, number>;
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
