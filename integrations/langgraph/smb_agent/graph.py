"""LangGraph Studio용 SMB 공유폴더 에이전트 그래프.

    Chat 입력(messages) → [에이전트 LLM] ⇄ [도구: find_folder/search_content/refresh_content] → Chat 응답

`langgraph.prebuilt.create_react_agent`로 ReAct 루프를 만든다. 도구는 모두 사내 smb-finder(HTTP)를
호출하므로 검색 로직은 재구현하지 않는다(tools.py 참고). LangGraph Studio는 이 그래프의 messages
상태를 채팅으로 렌더링하고, 노드 단위로 호출을 시각화·디버깅하게 해준다.

개발 프로필은 단일 로컬 테스트 흐름을 사용하며, 도구는 smb-finder의 읽기 API를 호출한다.
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from smb_agent.config import load_settings
from smb_agent.tools import TOOLS

_settings = load_settings()

# 온프레미스 OpenAI 호환 LLM (llama.cpp 등). base_url을 외부로 바꾸지 말 것.
_llm = ChatOpenAI(
    base_url=_settings.llm_base_url,
    api_key=_settings.llm_api_key or "local-no-key",
    model=_settings.llm_model,
    temperature=_settings.llm_temperature,
    timeout=_settings.llm_timeout_ms / 1000,
)

SYSTEM_PROMPT = """SMB 파일에서 사용자 질문에 대한 정확한 파일 경로와 내용을 제공해 주세요.
당신은 진단검사실(분자유전팀)의 **사내 SMB 공유폴더 찾기 비서**다.
사용자의 한국어 요청을 듣고 알맞은 도구를 골라 폴더/파일을 찾아 간결히 답한다.

도구 사용 규칙:
- 폴더 **이름/위치**를 찾으면 → find_folder
- 파일 **본문 내용**(키워드)을 찾으면 → search_content
  (검색했는데 '인덱스가 비어 있다'고 나오면, 사용자에게 어떤 폴더를 DB화할지 물어보고
   refresh_content로 그 폴더를 먼저 DB화한 뒤 다시 search_content 한다.)
- "이 폴더 DB화/인덱싱해줘" 같은 요청 → refresh_content (경로를 받는다)

원칙:
- 도구 결과(경로·파일명)를 지어내지 말고 그대로 전달한다. 결과가 없으면 없다고 말한다.
- 답변은 짧고 명확하게. 경로는 그대로 보여준다."""

# Studio가 불러갈 그래프 객체. langgraph.json에서 `graph`로 참조한다.
graph = create_react_agent(_llm, TOOLS, prompt=SYSTEM_PROMPT)
