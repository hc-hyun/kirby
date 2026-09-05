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

주요 모듈은 `src/kirby/api`, `worker`, `runtime`, `roles`, `storage`, `files`로 분리했습니다.
공유 데이터 구조는 `contracts.py`에 있습니다. 새 역할은 기존 역할 설정·스킬·결과 스키마와
같은 방식으로 등록합니다. 역할마다 Python 에이전트를 새로 만들 필요는 없습니다.

## 에이전트별 관리

에이전트 하나의 정의 파일을 `agents/<에이전트 ID>/`에 모았습니다.

```text
agents/log-analyst/
  agent.yaml                 # 역할·스킬·실행 제한과 파일 경로
  README.md
  skills/                    # 이 에이전트의 스킬
  schemas/result.schema.json # 결과 형식
  fixtures/                  # 합성 입력과 결과 예시
```

활성 에이전트는 `agents/registry.yaml`, 공통 스킬의 버전과 경로는
`agents/_shared/releases.yaml`에서 관리합니다. `agent.yaml`의 `assets`는
같은 폴더의 파일과 사용할 공통 스킬 버전을 연결합니다.

새 폴더를 작성한 뒤 등록 목록과 사용자 역할 권한에 추가하고 다음 명령으로 검증하세요.

```bash
uv run python -m kirby.roles --root agents
```

API를 재시작하면 등록한 에이전트의 A2A 주소와 Agent Card가 만들어집니다.
현재 Compose는 에이전트 파일을 이미지에 포함하므로 변경 후 이미지를 다시 빌드합니다.
기존 대화는 저장된 스킬·설정을 유지하며, 파일과 보고서는 MinIO에서 별도로 보관합니다.
작성 규칙은 [에이전트 패키지 문서](docs/AGENT_PACKAGES.md)를 참고하세요.

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

## E2E 테스트 클라이언트

API와 Worker가 실행 중일 때, 별도 터미널에서 브라우저 클라이언트를 시작합니다.

```bash
uv run python -m kirby.client \
  --url http://127.0.0.1:8000 \
  --credentials-file .local/credentials.json
```

`http://127.0.0.1:8088`에서 역할을 선택하고 합성 예제를 불러와 요청을 보내세요.
스트리밍·즉시 응답 후 조회·완료까지 대기 모드를 비교할 수 있습니다.
작업 목록에서 결과를 다시 열거나 실행 중 작업을 구독·취소할 수 있고,
완료한 작업은 같은 Context에서 후속 대화를 이어갑니다. 결과와 이벤트는 JSON으로
다운로드합니다. 화면의 스트림은 작업 상태와 결과를 전달합니다.

인증 토큰은 클라이언트 서버가 파일에서 읽으며 브라우저에 전달하지 않습니다.
기본적으로 파일의 첫 계정을 사용합니다. 다른 계정은 `--credential-index 1`,
다른 화면 포트는 `--port 8089`처럼 지정하세요. 이 클라이언트는 localhost에서만 실행됩니다.

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

## MinIO로 파일 주고받기

실행 중인 `~/works/my-minio`의 버킷과 `.env`를 그대로 연결합니다. 위 Compose
환경변수와 함께 다음을 지정하세요. 키 파일은 읽기 전용으로 마운트합니다.

```bash
export KIRBY_MINIO_CREDENTIALS_FILE="$HOME/works/my-minio/.env"
export KIRBY_MINIO_ENDPOINT=http://minio:9000
export KIRBY_MINIO_PUBLIC_ENDPOINT=http://127.0.0.1:9000
docker compose -f compose.yaml -f compose.minio.yaml up --build -d
```

기존 로컬 서비스의 비공개 설정을 사용할 때는 다음 명령으로 실행합니다.

```bash
docker compose -f compose.yaml -f compose.minio.yaml \
  --env-file .local/services.env -p kirby-local up -d
```

`http://127.0.0.1:8088`을 새로고침하고 역할을 선택한 뒤 파일을 첨부하세요.
업로드가 끝나면 요청을 보냅니다. 예: **“첨부 로그에서 ERROR를 찾아 근거 줄과
분석 범위를 알려줘.”** 완료된 결과의 `report.json`, `report.csv` 링크로 파일을
받습니다. 링크는 15분간 유효하며, 만료되면 **상태 조회**로 갱신합니다.

- 입력: UTF-8 `.txt`, `.log`, `.csv`, `.json`, `.jsonl`, 파일당 최대 **64MiB**,
  같은 Context에 누적 **4개**. 후속 대화에서 기존 첨부를 재사용합니다.
- 전송: 브라우저가 MinIO에 직접 업로드하고 A2A에는 파일 참조만 보냅니다.
  Worker는 디스크로 나누어 내려받고, 모델에는 필요한 근거 일부만 전달합니다.
- 출력: 검증된 분석 결과를 JSON·CSV 파일로 생성합니다. 큰 원본의 모든 행을
  변환하는 작업, PDF·엑셀·압축파일 처리는 아직 지원하지 않습니다.

**스킬과 실행 로직이 함께 필요합니다.** `file-evidence` 스킬은 도구 사용법과
근거·분석 범위를 안내합니다. 파일 권한, 전송, 제한된 읽기·검색 도구, 결과 파일
생성은 `api`, `files`, `runtime`, `storage`가 담당합니다. 파일 전체를 프롬프트에
넣거나 base64로 보내지 않으므로 큰 파일의 메모리 복사를 줄입니다.

이 Compose 구성은 Worker **1GiB**, API **512MiB**로 제한합니다. 세션 볼륨은
디스크를 사용하세요. 상세 검증 수치와 대상 Kubernetes 환경의 미실행 항목은
[STATUS.md](STATUS.md)에 기록합니다. 파일 보존 기간과 자동 정리는 아직 구현하지 않았습니다.

## 검증과 범위

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

기본 테스트는 외부 LLM을 호출하지 않습니다. 실제 Goose 테스트와 PostgreSQL 테스트는
별도 opt-in입니다. 작업·결과·역할 고정은 PostgreSQL에, Goose 세션은 영속 디렉터리에
보관합니다. 실행이 실패하거나 취소되면 해당 Context를 닫고 새 Context로 시작합니다.

첫 운영은 Worker 하나와 읽기 전용 스킬·첨부 파일 도구를 기준으로 합니다. 외부 MCP, OIDC,
Pod 간 자동 복구, 자동 확장, 상세 이벤트 재생은 [후속 계획](docs/FUTURE.md)으로 남겼습니다.
원래 상세 계획서는 ZIP 원본으로 보존했습니다.

현재 검증 결과와 다음 작업은 [STATUS.md](STATUS.md), 모듈 간 계약은
[구조 문서](docs/ARCHITECTURE.md)를 참고하세요.
