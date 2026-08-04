import type {
  ApiErrorBody,
  ChatResponse,
  DocumentPreviewResponse,
  DocumentSearchResponse,
  DocumentVersionGraphResponse,
  SelectedFilePayload,
  StoresStatusResponse,
} from "@/types";

export class ApiError extends Error {
  readonly status: number;
  readonly requestId: string;

  constructor(message: string, status = 0, requestId = "") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.requestId = requestId;
  }
}

function normalizeDetail(value: unknown): string {
  if (value == null || value === "") return "";
  if (["string", "number", "boolean"].includes(typeof value)) return String(value);
  if (Array.isArray(value)) return value.map(normalizeDetail).filter(Boolean).join("; ");
  if (typeof value === "object") {
    const detail = value as Record<string, unknown>;
    return normalizeDetail(detail.message ?? detail.detail ?? detail.code ?? detail.error_code);
  }
  return "";
}

async function parseResponse<T>(response: Response): Promise<T> {
  const body = (await response.json().catch(() => ({}))) as ApiErrorBody;
  if (!response.ok) {
    throw new ApiError(
      normalizeDetail(body.detail) || body.message || body.error_code || `HTTP ${response.status}`,
      response.status,
      body.request_id,
    );
  }
  return body as T;
}

export const playgroundApi = {
  async searchFiles(query: string, limit = 10): Promise<DocumentSearchResponse> {
    return parseResponse<DocumentSearchResponse>(
      await fetch("/api/playground/files/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, limit }),
      }),
    );
  },

  async previewFile(file: SelectedFilePayload): Promise<DocumentPreviewResponse> {
    return parseResponse<DocumentPreviewResponse>(
      await fetch(
        `/api/playground/files/${encodeURIComponent(file.doc_id)}/revisions/${encodeURIComponent(file.revision_id)}/artifacts/preview`,
      ),
    );
  },

  async getFileGraph(file: SelectedFilePayload): Promise<DocumentVersionGraphResponse> {
    return parseResponse<DocumentVersionGraphResponse>(
      await fetch(`/api/playground/files/${encodeURIComponent(file.doc_id)}/graph`),
    );
  },

  async getStoresStatus(): Promise<StoresStatusResponse> {
    return parseResponse<StoresStatusResponse>(await fetch("/api/playground/stores/status"));
  },

  async sendChat(payload: Record<string, unknown>): Promise<ChatResponse> {
    return parseResponse<ChatResponse>(
      await fetch("/api/playground/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
    );
  },
};
