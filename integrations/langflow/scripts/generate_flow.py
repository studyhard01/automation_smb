"""자연어 설명으로 Langflow 워크플로우 JSON을 생성하는 CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.langflow.flow_builder import (  # noqa: E402
    DEFAULT_SERVICE_URL,
    WorkflowPlanner,
    install_flow,
    render_langflow_flow,
)


def generate_workflow_file(
    *,
    instruction: str,
    out: Path,
    service_url: str = DEFAULT_SERVICE_URL,
    limit: int = 5,
    timeout_ms: int = 1500,
    llm_provider: str = "auto",
    local_base_url: str | None = None,
    local_model: str | None = None,
    openai_model: str | None = None,
    install: bool = False,
    langflow_url: str = "http://127.0.0.1:7860",
    langflow_api_key_env: str = "LANGFLOW_API_KEY",
    langflow_folder_id: str = "",
    update_existing: bool = True,
) -> dict:
    """워크플로우 JSON을 파일로 저장하고 생성 결과를 반환한다."""
    spec = WorkflowPlanner().plan(
        instruction,
        service_url=service_url,
        limit=limit,
        timeout_ms=timeout_ms,
        llm_provider=llm_provider,  # type: ignore[arg-type]
        local_base_url=local_base_url,
        local_model=local_model,
        openai_model=openai_model,
    )
    result = render_langflow_flow(spec)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.flow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    meta = result.model_dump(exclude={"flow"})
    if install:
        installed = install_flow(
            result.flow,
            langflow_url=langflow_url,
            api_key_env=langflow_api_key_env,
            folder_id=langflow_folder_id,
            update_existing=update_existing,
        )
        meta["installed_flow"] = installed.model_dump()
    return meta


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate an automation_smb Langflow workflow JSON.")
    parser.add_argument("--instruction", required=True, help="워크플로우로 만들 사용자의 자연어 요구사항.")
    parser.add_argument("--out", required=True, type=Path, help="저장할 JSON 파일 경로.")
    parser.add_argument("--service-url", default=DEFAULT_SERVICE_URL, help="smb-finder base URL.")
    parser.add_argument("--limit", default=5, type=int, help="SMBFolderFinder 최대 결과 수.")
    parser.add_argument("--timeout-ms", default=1500, type=int, help="SMBFolderFinder 요청 timeout(ms).")
    parser.add_argument(
        "--llm-provider",
        default="auto",
        choices=["auto", "rule", "local", "openai"],
        help="템플릿 판별 provider. auto는 로컬 LLM 설정이 없으면 규칙 기반으로 동작.",
    )
    parser.add_argument("--local-base-url", default=None, help="로컬 OpenAI 호환 LLM base URL.")
    parser.add_argument("--local-model", default=None, help="로컬 OpenAI 호환 LLM model.")
    parser.add_argument("--openai-model", default=None, help="OpenAI 사용 시 model. API key는 환경변수만 사용.")
    parser.add_argument("--install", action="store_true", help="생성한 flow를 Langflow API에 바로 등록.")
    parser.add_argument("--langflow-url", default="http://127.0.0.1:7860", help="Langflow base URL.")
    parser.add_argument("--langflow-api-key-env", default="LANGFLOW_API_KEY", help="Langflow API key 환경변수 이름.")
    parser.add_argument("--langflow-folder-id", default="", help="등록할 Langflow folder id.")
    parser.add_argument("--no-update-existing", action="store_true", help="같은 이름의 flow가 있어도 새로 생성 시도.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    meta = generate_workflow_file(
        instruction=args.instruction,
        out=args.out,
        service_url=args.service_url,
        limit=args.limit,
        timeout_ms=args.timeout_ms,
        llm_provider=args.llm_provider,
        local_base_url=args.local_base_url,
        local_model=args.local_model,
        openai_model=args.openai_model,
        install=args.install,
        langflow_url=args.langflow_url,
        langflow_api_key_env=args.langflow_api_key_env,
        langflow_folder_id=args.langflow_folder_id,
        update_existing=not args.no_update_existing,
    )
    print(json.dumps({"out": str(args.out), **meta}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
