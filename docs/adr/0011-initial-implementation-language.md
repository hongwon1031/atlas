# ADR-011: Initial Implementation Language and Runtime

- Status: Accepted
- Date: 2026-09-01
- Accepted: 2026-09-01
- Decision owners: Project owner

## Context

[ADR 등록부](README.md)의 Open Decision 1번은 "Atlas의 초기 개발 언어"였습니다. 첫 vertical slice인 Issue intake를 구현하려면 언어를 먼저 확정해야 합니다. [AGENTS.md](../../AGENTS.md)는 선택되지 않은 언어와 framework를 암묵적으로 확정하지 못하게 하므로 이 결정을 ADR로 남깁니다.

후보는 Python, TypeScript/Node, Go였습니다. 판단 기준은 초기 개발 속도, dependency 최소화, always-available server에서의 운영 부담, 기존 문서와의 정합성입니다.

## Decision

- Atlas Control Plane의 초기 구현 언어는 **Python**이며 최소 지원 버전은 **3.11**입니다.
- 이 슬라이스는 표준 라이브러리만 사용하고 런타임 dependency를 추가하지 않습니다. GitHub REST 호출은 `urllib.request`로 수행합니다.
- 테스트는 표준 라이브러리 `unittest`로 작성해 설치 없이 실행할 수 있게 합니다. `pytest`와 `ruff`는 이후 개발 편의 도구로 도입할 수 있으나 현재 필수 dependency가 아닙니다.
- source layout은 `src/atlas/`, 테스트는 `tests/`이며 packaging metadata는 `pyproject.toml`에 둡니다.
- 새 dependency는 Task 범위와 ADR 근거가 있을 때만 추가합니다.
- 이 결정은 Control Plane에 한정합니다. Executor Adapter가 호출하는 외부 도구의 구현 언어를 제약하지 않습니다.

## Alternatives Considered

### Python

- 장점: 표준 라이브러리만으로 HTTP, JSON, 해시, 테스트가 모두 가능해 dependency 0개로 첫 슬라이스를 끝낼 수 있습니다. [context-memory.md](../context-memory.md)의 예시 manifest가 이미 `pytest`와 `ruff`를 validation command로 씁니다.
- 단점: 단일 바이너리 배포가 불가능하고 server에 런타임을 설치·관리해야 합니다.

### TypeScript / Node

- 장점: GitHub 생태계(Octokit, Actions)와 결합이 쉽고 향후 Web UI와 언어를 통일할 수 있습니다.
- 단점: tsconfig, 빌드, 테스트 러너 등 초기 설정과 dependency가 Python보다 많습니다.

### Go

- 장점: 단일 바이너리로 배포되어 [ADR-009](0009-worker-process-supervision.md)의 stable supervisor 운영이 가장 단순해집니다.
- 단점: 초기 개발 속도가 느리고 기존 문서의 예시와 어긋납니다.

## Consequences

- Open Decision 1번이 해소되고 구현 Task를 시작할 수 있습니다.
- always-available server는 Python 3.11 이상 런타임을 갖춰야 합니다. 배포 방식은 [ADR-009](0009-worker-process-supervision.md)의 stable supervisor 결정과 함께 정합니다.
- dependency 0개 원칙을 유지하는 동안 supply chain 위험이 낮게 유지됩니다. 이후 dependency를 추가하면 이 이점이 사라지므로 ADR 근거를 요구합니다.
- Go를 선택했을 때 얻었을 단일 바이너리 배포 이점은 포기합니다. 운영 부담이 실제 문제가 되면 Executor worker에 한해 다른 언어를 재검토할 수 있습니다.

## Security Impact

- 표준 라이브러리만 사용하므로 third-party package를 통한 공급망 위험이 현재 없습니다.
- GitHub token은 저장소나 설정 파일이 아니라 환경변수(`ATLAS_GITHUB_TOKEN` 또는 `GITHUB_TOKEN`)로 주입합니다.
- provider 응답 원문과 인증 오류는 분류된 category와 메시지로만 노출합니다. [ADR-007](0007-public-repository-security-policy.md)의 redaction 요구를 따릅니다.
- 이후 dependency를 추가할 때 license와 supply chain 검토를 함께 수행합니다.

## Follow-up Tasks

- [x] Project owner가 초기 구현 언어를 Python으로 승인
- [ ] `pytest`와 `ruff`를 개발 dependency로 도입할지와 CI 실행 방식 결정
- [ ] always-available server의 Python 런타임 provisioning 방식을 stable supervisor 결정과 함께 정의
- [ ] dependency 추가 시 요구할 검토 항목 목록 작성
