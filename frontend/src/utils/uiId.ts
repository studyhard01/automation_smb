type BrowserCrypto = {
  randomUUID?: () => string;
  getRandomValues?: (values: Uint32Array) => Uint32Array;
};

let fallbackSequence = 0;

/** LAN의 비보안 HTTP 환경에서도 사용할 수 있는 UI 전용 고유 ID를 만든다. */
export function createUiId(
  source: BrowserCrypto | null = globalThis.crypto as BrowserCrypto | undefined ?? null,
): string {
  if (typeof source?.randomUUID === "function") {
    try {
      return source.randomUUID.call(source);
    } catch {
      // 일부 브라우저는 비보안 HTTP에서 함수를 노출한 뒤 호출만 차단하므로 아래 경로를 사용한다.
    }
  }

  const entropy = new Uint32Array(4);
  if (typeof source?.getRandomValues === "function") {
    source.getRandomValues.call(source, entropy);
  } else {
    for (let index = 0; index < entropy.length; index += 1) {
      entropy[index] = Math.floor(Math.random() * 0x1_0000_0000);
    }
  }

  fallbackSequence = (fallbackSequence + 1) % Number.MAX_SAFE_INTEGER;
  const randomPart = Array.from(entropy, (value) => value.toString(36)).join("-");
  return `ui-${Date.now().toString(36)}-${fallbackSequence.toString(36)}-${randomPart}`;
}
