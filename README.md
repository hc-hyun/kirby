# KIRBY

**Knowledge & Instruction Runtime for Building Your-agent**

역할·스킬을 조합해 Goose 에이전트를 A2A로 제공하는 런타임입니다. API가 요청을
PostgreSQL에 저장하고, 별도 Worker가 Goose를 실행합니다. 현재 VOC 분석과 로그 분석을
제공하며, 같은 Context의 후속 대화는 저장된 Goose 세션으로 이어집니다.

## 구조

```text
A2A Client → API → PostgreSQL → Worker → Goose → OpenRouter
                                 ↕
                           영속 세션 디렉터리
```

주요 모듈은 `src/kirby/api`, `worker`, `runtime`, `roles`, `storage`로 분리했습니다.
공유 데이터 구조는 `contracts.py`에 있습니다. 새 역할은 기존 역할 설정·스킬·결과 스키마와
같은 방식으로 등록합니다. 역할마다 Python 에이전트를 새로 만들 필요는 없습니다.

## 로컬 실행

Python 3.12, uv, PostgreSQL이 필요합니다. Goose는 지정 버전과 SHA-256으로 설치합니다.

```bash
uv sync --locked
uv run python scripts/install_goose.py
mkdir -p .local/sessions
cp config/credentials.example.json .local/credentials.json
```

`.local/credentials.json`의 token을 임의의 긴 비밀값으로 바꾸고, tenant·subject·roles를
사용자별로 설정하세요. OpenRouter 키 파일은 저장소 밖에 둡니다.

```bash
export KIRBY_DATABASE_URL='postgresql://kirby:password@127.0.0.1:5432/kirby'
export KIRBY_CREDENTIALS_FILE="$PWD/.local/credentials.json"
export KIRBY_OPENROUTER_KEY_FILE='/absolute/path/to/open_router.key'

uv run python -m kirby.storage migrate
uv run python -m kirby.api
```

같은 환경변수를 지정한 다른 터미널에서 Worker를 실행합니다.

```bash
uv run python -m kirby.worker
```

기본 주소는 `http://127.0.0.1:8000`입니다. 모델 기본값은 무료
`cohere/north-mini-code:free`이며, 유료 모델로 자동 전환하지 않습니다.

## A2A 주소

| 기능 | 경로 |
|---|---|
| 허용된 역할 목록 | `/agents` |
| VOC 요청 | `/agents/voc-analyst/rpc` |
| 로그 요청 | `/agents/log-analyst/rpc` |
| 역할 설명 | `/agents/{role_id}/.well-known/agent-card.json` |
| 상태 확인 | `/healthz`, `/readyz` |

상태 확인 외의 요청에는 `Authorization: Bearer <token>`이 필요합니다.
JSON-RPC 요청에는 `A2A-Version: 1.0`을 사용합니다. Send/Get/List/Stream/Subscribe/Cancel을
제공합니다. `returnImmediately`가 없으면 완료까지 기다립니다. 후속 요청은 기존
`contextId`와 새 `messageId`로 보냅니다. 역할 변경은 새 Context로 시작합니다.

실행 중인 API와 Worker에 합성 입력을 보내는 예제:

```bash
uv run python scripts/smoke.py --credentials-file .local/credentials.json
```

## Docker Compose

```bash
export KIRBY_POSTGRES_PASSWORD='choose-a-url-safe-password'
export KIRBY_UID="$(id -u)" KIRBY_GID="$(id -g)"
# 위의 KIRBY_CREDENTIALS_FILE / KIRBY_OPENROUTER_KEY_FILE도 지정합니다.
docker compose up --build -d
```

PostgreSQL, migration, API, Worker를 실행합니다. 세션은 `.local/sessions`에 유지됩니다.
기존 외부 PostgreSQL을 쓰거나 Kubernetes에 배포하는 방법은
[운영 문서](docs/OPERATIONS.md)를 참고하세요. Helm chart는 `deploy/helm/kirby`에 있습니다.

## 검증과 범위

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

기본 테스트는 외부 LLM을 호출하지 않습니다. 실제 Goose 테스트와 PostgreSQL 테스트는
별도 opt-in입니다. 작업·결과·역할 고정은 PostgreSQL에, Goose 세션은 영속 디렉터리에
보관합니다. 실행이 실패하거나 취소되면 해당 Context를 닫고 새 Context로 시작합니다.

첫 운영은 Worker 하나와 읽기 전용 스킬 도구를 기준으로 합니다. 외부 MCP, OIDC,
Pod 간 자동 복구, 자동 확장, 상세 이벤트 재생은 [후속 계획](docs/FUTURE.md)으로 남겼습니다.
원래 상세 계획서는 ZIP 원본으로 보존했습니다.

현재 검증 결과와 다음 작업은 [STATUS.md](STATUS.md), 모듈 간 계약은
[구조 문서](docs/ARCHITECTURE.md)를 참고하세요.
