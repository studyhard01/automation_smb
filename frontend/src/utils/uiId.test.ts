import { describe, expect, it, vi } from "vitest";

import { createUiId } from "@/utils/uiId";

describe("createUiId", () => {
  it("보안 컨텍스트에서는 browser randomUUID를 사용한다", () => {
    const randomUUID = vi.fn(() => "11111111-1111-4111-8111-111111111111");

    expect(createUiId({ randomUUID })).toBe("11111111-1111-4111-8111-111111111111");
    expect(randomUUID).toHaveBeenCalledOnce();
  });

  it("LAN HTTP처럼 randomUUID가 없어도 고유 ID를 만든다", () => {
    const getRandomValues = vi.fn((values: Uint32Array) => {
      values.set([1, 2, 3, 4]);
      return values;
    });

    const first = createUiId({ getRandomValues });
    const second = createUiId({ getRandomValues });

    expect(first).toMatch(/^ui-/);
    expect(second).toMatch(/^ui-/);
    expect(second).not.toBe(first);
    expect(getRandomValues).toHaveBeenCalledTimes(2);
  });

  it("Web Crypto 자체가 없어도 UI ID 생성을 중단하지 않는다", () => {
    expect(createUiId(null)).toMatch(/^ui-/);
  });
});
