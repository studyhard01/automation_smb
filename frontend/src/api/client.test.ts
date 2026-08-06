import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { playgroundApi } from "./client";
import type { FileUploadResponse, PlaygroundSettingsResponse } from "@/types";

const settingsResponse: PlaygroundSettingsResponse = {
  upload: {
    enabled: true,
    configured: true,
    relative_directory: "playground/uploads",
    destination_label: "공유폴더 업로드 영역",
    max_size_bytes: 1024,
    allowed_extensions: [".md"],
  },
  local_llm_configured: true,
};

const uploadResponse: FileUploadResponse = {
  file_name: "[업로드] synthetic_20260805_v1.0.md",
  size_bytes: 9,
  uploaded_at: "2026-08-05T00:00:00Z",
  destination_label: "공유폴더 업로드 영역",
  indexed: false,
  conversation_ready: true,
  selected_file: {
    source: "upload",
    doc_id: "33333333-3333-3333-3333-333333333333",
    revision_id: "44444444-4444-4444-4444-444444444444",
    file_name: "[업로드] synthetic-note_20260805_v1.0.md",
    title: "synthetic-note",
    extension: ".md",
    size_bytes: 9,
    score: 1,
    match_source: "content",
  },
};

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: vi.fn().mockResolvedValue(body),
  } as unknown as Response;
}

describe("playgroundApi settings and upload", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => vi.unstubAllGlobals());

  it("비밀정보 없는 Playground 설정을 조회하고 상대 경로만 PATCH한다", async () => {
    fetchMock.mockResolvedValueOnce(response(settingsResponse));
    await expect(playgroundApi.getSettings()).resolves.toEqual(settingsResponse);
    expect(fetchMock).toHaveBeenLastCalledWith("/api/playground/settings");

    fetchMock.mockResolvedValueOnce(response(settingsResponse));
    await playgroundApi.updateUploadDirectory("playground/reviewed");
    expect(fetchMock).toHaveBeenLastCalledWith("/api/playground/settings/upload", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ relative_directory: "playground/reviewed" }),
    });
  });

  it("파일을 file 필드의 multipart body로 전송하고 indexed=false 응답을 보존한다", async () => {
    fetchMock.mockResolvedValueOnce(response(uploadResponse));
    const file = new File(["synthetic"], "synthetic.md", { type: "text/markdown" });

    await expect(playgroundApi.uploadFile(file)).resolves.toEqual(uploadResponse);
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/playground/files/upload");
    expect(options.method).toBe("POST");
    expect(options.headers).toBeUndefined();
    expect(options.body).toBeInstanceOf(FormData);
    expect((options.body as FormData).get("file")).toBe(file);
  });
});
