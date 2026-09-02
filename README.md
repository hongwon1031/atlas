# Atlas — AI Workforce OS

Atlas는 사람이 휴대전화에서 업무를 지시하면 여러 AI 개발 에이전트가 올바른 프로젝트 컨텍스트를 불러오고, 격리된 환경에서 작업하고, 검증 가능한 결과와 Pull Request를 생성하도록 조율하는 AI Workforce Operating System입니다.

> **현재 작업 단계:** In Progress — architecture와 operations contract를 문서화했고 Issue intake, polling, persistence, atomic claim까지 구현했습니다. executor 실행 경로는 아직 구현하지 않았습니다.
>
> 이 저장소는 제품 정의, 실행 계약, 기여 거버넌스와 함께 GitHub Issue를 polling해 Task 후보로 parse·검증하고 SQLite에 저장한 뒤 lease 기반으로 claim하는 worker 코드를 포함합니다. worktree 생성, Claude Code invocation, Run 실행, validation, PR delivery automation, webhook은 아직 구현하지 않았습니다.

## 핵심 MVP

Target MVP는 휴대전화에서 GitHub Issue로 작업을 지시하면 Atlas worker가 Task를 검증하고 claim한 뒤, always-available server의 self-hosted Claude Code worker가 올바른 프로젝트 컨텍스트를 사용해 별도 브랜치에서 작업하고, validation 결과가 포함된 Pull Request를 생성하는 것을 목표로 합니다. `main` 반영은 항상 사람의 승인을 거칩니다.

## Getting Started

Atlas에 기여하는 사람과 AI Agent는 application stack을 먼저 만들지 않고 문서와 Task contract에서 시작합니다.

1. 저장소 전체 작업 규칙인 [AGENTS.md](AGENTS.md)를 읽습니다.
2. [Constitution](docs/constitution.md), [PRD](docs/prd.md), [Architecture](docs/architecture.md), [Security & Governance](docs/security-governance.md)를 순서대로 읽습니다.
3. [Task Schema](docs/specs/task-schema.md)에 맞춰 [Atlas Task Issue Form](.github/ISSUE_TEMPLATE/atlas-task.yml)으로 Objective, Constraints, Acceptance Criteria, Allowed Scope를 작성합니다.
4. [ADR-001](docs/adr/0001-documentation-source-of-truth.md), [ADR-002](docs/adr/0002-initial-mobile-task-channel.md), [ADR-003](docs/adr/0003-initial-execution-environment.md)과 Task에 적용되는 다른 [ADR](docs/adr/README.md)을 확인합니다. `Proposed`는 확정된 구현 근거가 아닙니다.
5. Task 전용 branch에서 작업하고 [PR template](.github/pull_request_template.md)에 변경 파일, 검증, 위험, open question을 기록합니다.
6. 사람의 검토와 승인 전에는 `main`에 merge하지 않습니다.

[ADR-011](docs/adr/0011-initial-implementation-language.md)에 따라 Control Plane의 구현 언어는 Python 3.11 이상이며 런타임 dependency는 없습니다. database와 deployment 환경은 Accepted ADR과 명시적인 구현 Task 없이 선택하지 않습니다.

### Worker 실행

구현된 범위는 Issue polling → Task 저장 → atomic claim까지입니다. Run 실행과 PR delivery는 아직 없습니다.

```bash
# 테스트 (설치 불필요)
python -m unittest discover -s tests -t .

export ATLAS_GITHUB_TOKEN=<repository read 권한 토큰>
export PYTHONPATH=src

# Issue 한 건을 검증만 (저장 없음)
python -m atlas 12

# 후보 Issue를 polling해 valid Task를 저장
# 후보 조건: open + non-PR + `[Atlas Task]` 제목 + `atlas:queued` label
python -m atlas poll
python -m atlas poll --watch --interval 60   # pass마다 한 줄씩 즉시 출력

# 저장된 Task 확인과 claim
python -m atlas tasks
python -m atlas claim --worker-id worker-1 --lease-ttl 900
python -m atlas release <claim-id> --reason done
```

결과는 JSON으로 출력됩니다. exit code는 `0` 성공, `1` validation 실패 또는 claim 대상 없음, `2` source 오류입니다.

**approval gate:** `atlas:queued` label이 approval signal입니다. GitHub는 label 추가를 triage 이상 권한자로 제한하므로, 공개 저장소에서 임의 사용자가 유효한 form을 작성해도 label을 달 수 없어 claim 대상이 되지 않습니다.

승인은 polling 시점의 필터가 아니라 **Task에 저장되는 지속 상태**입니다. `claim`은 저장된 승인을 다시 확인하고, poller는 label이 제거되거나 Issue가 닫히면 승인을 회수하고 진행 중인 claim까지 해제합니다. `--no-queue-label`은 label 없는 후보의 **등록만** 허용하며 승인하지는 않으므로 approval 정책을 우회할 수 없습니다.

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `ATLAS_GITHUB_TOKEN` 또는 `GITHUB_TOKEN` | 없음 | repository read 권한 토큰 |
| `ATLAS_DB_PATH` | `atlas.db` | SQLite operational store 경로 |
| `ATLAS_REPOSITORY` | `hongwon1031/atlas` | polling 대상 |
| `ATLAS_POLL_INTERVAL_SECONDS` | `60` | `--watch` 간격 |
| `ATLAS_LEASE_TTL_SECONDS` | `900` | claim lease TTL |
| `ATLAS_DISABLE_QUEUE_LABEL` | 미설정 | approval gate 해제. 신뢰된 repository에서만 사용 |

token은 저장소에 두지 않고 환경변수로만 주입합니다. database 파일도 commit하지 않습니다. public repository의 Issue는 token 없이도 조회되지만 rate limit이 훨씬 낮습니다.

## Project Status

| 영역 | 상태 | 근거와 완료 조건 |
| --- | --- | --- |
| 초기 문서·거버넌스 foundation | Complete | Agent guide, Task/PR contract, ADR register 존재 |
| Codex Cloud manual delivery | Proven Manually | 사람 prompt → Codex branch 변경 → Codex PR → 사람 merge |
| Runtime·isolation specification | In Progress | ADR-009~010은 Proposed이며 구현 전 사람 승인 필요 |
| Issue intake core | In Progress | 단건 fetch·parse·validate와 회귀 테스트 구현; valid Atlas Task live E2E 확인 필요 |
| Polling, persistence, claim, lease | In Progress | polling·SQLite store·atomic claim·lease 구현; live valid Task로 end-to-end 확인 필요 |
| self-hosted Claude Code automated path | Planned | primary automated executor로 결정됐지만 invocation 미구현 |
| Atlas-to-Codex Cloud automation | Feasibility Unverified | adapter로 표시하기 전 integration validation 필요 |
| Polling, claim, recovery, routing, validation delivery | Not Implemented | 문서 계약만 존재 |
| API·Gemini·local-model adapters, dedicated mobile UI | Planned | vertical slice 이후 후보 |

상태 label은 `Complete`, `Proven Manually`, `In Progress`, `Planned`, `Not Implemented`, `Feasibility Unverified`만 사용합니다. 설계 결정이나 수동 성공을 구현 완료로 계산하지 않습니다.

## Accepted Operating Model

- **Documentation:** GitHub Markdown이 canonical source of truth입니다. Notion은 선택적인 human-friendly mirror이며 실행 정책의 근거가 아닙니다.
- **Task intake:** GitHub Issues와 기존 [Atlas Task Issue Form](.github/ISSUE_TEMPLATE/atlas-task.yml)을 사용합니다.
- **Primary automated executor:** always-available server의 self-hosted Claude Code worker입니다.
- **Secondary executor:** Codex Cloud는 사람이 직접 전달하는 manual executor 또는 secondary executor로 유지합니다.
- **Approval:** 모든 변경은 독립 branch와 PR을 사용하며 사람만 최종 merge합니다.

## Current Usable Workflow

현재 사용 가능한 흐름은 수동 Executor delivery입니다. Atlas는 아직 Task를 자동 ingest, route, execute, recover, validate, deliver하지 않습니다.

1. 사용자가 Task를 Codex Cloud 또는 Claude Code에 수동으로 제공합니다. GitHub Issue를 만들었다면 사람이 해당 Issue를 Executor에게 전달합니다.
2. Executor가 Task 전용 branch를 생성하거나 업데이트합니다.
3. Executor가 작업과 가능한 검증을 수행하고 Pull Request를 엽니다.
4. 사람이 PR을 검토하고 merge, 수정 요청, 폐기를 결정합니다.

Codex Cloud에서 **사람 prompt → Codex branch 변경 → Codex PR → 사람 merge** 흐름은 수동으로 입증됐습니다. 동일 수준의 Atlas-to-Codex 자동 호출은 검증되지 않았습니다.

## Target Automated Workflow

1. 사용자가 휴대전화에서 GitHub Issue를 생성합니다.
2. Atlas worker가 Task를 parse하고 schema, permission, Project, scope를 검증합니다.
3. Planner가 계획과 위험도를 분류하고 사람이 승인한 범위를 확인합니다.
4. Context Builder가 Project별 context packet을 구성합니다.
5. Router가 capability, security scope, availability에 맞는 Executor를 선택합니다.
6. worker가 Task를 lease로 idempotent하게 claim합니다.
7. 전용 worktree 또는 clone과 Task branch를 준비합니다.
8. Task마다 새로운 executor process를 시작합니다.
9. Validator가 Acceptance Criteria, project command, diff scope, forbidden path, secret을 검사합니다.
10. Delivery Adapter가 PR과 mobile-friendly result summary를 생성합니다.
11. 사람이 승인, 수정 요청, 취소 중 하나를 선택하고 merge를 결정합니다.

Initial MVP ingestion은 [ADR-008](docs/adr/0008-initial-github-event-ingestion.md)의 polling-first이며 Accepted입니다. polling, Task 등록, atomic claim은 동작합니다. `atlas:queued` label이 approval signal이고, comment command, webhook, Run 실행, PR delivery는 아직 동작하지 않습니다.

## Executor and Model Support

Atlas는 orchestrator, dispatcher, state manager, delivery coordinator입니다. Planner·Researcher·Implementer·Reviewer·Validator·Reporter는 역할이며 특정 model account가 아닙니다. Claude Code, Codex Cloud, future API model, local model은 [Agent Registry](docs/specs/agent-registry.md)를 통해 교체 가능한 Executor로 취급합니다.

| Executor | 상태 | 현재 의미 |
| --- | --- | --- |
| Codex Cloud manual | Proven Manually | 사람이 직접 prompt를 전달하는 branch-to-PR workflow |
| self-hosted Claude Code | Planned | always-available server의 primary automated executor; invocation 미구현 |
| Atlas-to-Codex Cloud adapter | Feasibility Unverified | integration feasibility와 control boundary 검증 필요 |
| Claude API, OpenAI API, Gemini | Not Implemented | future provider adapters |
| local models | Not Implemented | future local adapters |

개인과 회사 account는 별도 authentication profile과 worker registration으로 표현하며 credential, usage, Project scope를 공유하지 않습니다.

## Runtime and Hosting Options

- Target worker는 always-available server에서 실행하므로 개인 PC가 켜져 있을 필요가 없습니다.
- [ADR-009](docs/adr/0009-worker-process-supervision.md)은 PoC에서 `tmux` 사용을 제안합니다. tmux는 process persistence일 뿐 service manager나 Task isolation boundary가 아닙니다.
- stable operation은 systemd 또는 Docker 중 하나를 후속 결정해 startup, restart, logging, health, lifecycle을 관리합니다.
- [ADR-010](docs/adr/0010-task-execution-isolation.md)은 Task마다 branch, worktree/clone, executor process, Run ID, log scope, timeout, cancellation을 분리하도록 제안합니다.
- 하나의 persistent Claude conversation, shell session, tmux pane을 여러 Task 또는 Project가 공유하지 않습니다.

자세한 lifecycle, lease, recovery, cleanup 계약은 [Execution Runtime Specification](docs/specs/execution-runtime.md)을 따릅니다. 이 항목들은 현재 specification이며 server나 worker가 존재한다는 뜻이 아닙니다.

## Current Limitations

- webhook ingestion이 없습니다. polling만 있으며 지연은 interval에 좌우됩니다.
- Run record를 만들지 않습니다. claim은 Task lease까지이고 `active_run_id`는 계속 null입니다.
- heartbeat와 worker restart reconciliation이 없습니다. lease는 TTL 만료와 grace period로만 회수됩니다.
- public GitHub REST 조회·목록 경로는 실제 응답으로 확인했지만 valid Atlas Task Issue의 live E2E는 아직 수행하지 않았습니다.
- operational store는 단일 SQLite 파일이라 여러 host가 공유할 수 없습니다.
- schema migration runner가 없습니다. `schema_meta.schema_version`만 기록합니다.
- `atlas:queued` label을 추가한 actor의 repository permission을 Atlas가 직접 재확인하지 않습니다. GitHub의 label 권한 제한에 의존합니다.
- 승인 회수는 polling pass에서 일어나므로 label 제거와 회수 사이에 interval만큼의 창이 있습니다. claim 직전에 GitHub 최신 상태를 재조회하지는 않습니다.
- reconciliation을 위해 닫힌 Issue까지 조회하므로 첫 polling pass의 API 호출량이 늘어납니다.
- worker process supervision, persistence, heartbeat, crash recovery가 없습니다.
- self-hosted Claude Code invocation과 Codex automated adapter가 없습니다.
- automated context building, routing, usage detection, validation, PR delivery, mobile notification이 없습니다.
- precise remaining quota를 자동 확인하지 않습니다. 공식 API가 없을 때는 수동 입력과 실행 signal을 사용하도록만 제안했습니다.
- production deployment, dedicated mobile UI, fully autonomous planning은 범위 밖입니다.

## Next Implementation Scope

다음 Sprint는 provider invocation보다 먼저 mock executor로 worker control path를 검증합니다.

1. ~~Atlas GitHub Issue를 parse하고 validate합니다.~~ (완료 — `src/atlas/`)
2. approved 또는 queued Task를 polling합니다.
3. 한 Task를 idempotent하게 claim합니다.
4. 최소 Task·Run·lease·heartbeat 상태를 persist합니다.
5. 격리된 worktree와 branch를 만듭니다.
6. mock executor를 새 process로 호출합니다.
7. 결과를 validate하고 draft PR을 생성합니다.
8. self-hosted Claude Code invocation은 별도 후속 PR에서 추가합니다.

## 왜 Atlas인가

- AI 코딩 세션이 끝나도 프로젝트의 중요한 맥락을 보존합니다.
- 여러 모델과 실행기의 중복 작업과 충돌을 조율합니다.
- PC를 열지 않고도 휴대전화에서 작업 지시와 결과 검토를 수행합니다.
- 모델별 가용성, 비용, 사용량과 역할 적합성을 라우팅에 반영합니다.
- 프로젝트와 Workspace 사이의 코드, 문서, 자격증명, 기억을 격리합니다.
- 계획, 실행, 로그, 테스트, 비용, 결과를 관찰 가능하게 만듭니다.

## 핵심 문서

- [제품 비전](docs/vision.md)
- [프로젝트 헌법](docs/constitution.md)
- [PRD v0.1](docs/prd.md)
- [시스템 아키텍처 v0.2](docs/architecture.md)
- [컨텍스트와 메모리 설계](docs/context-memory.md)
- [에이전트 역할, 라우터와 스케줄러](docs/agents-router-scheduler.md)
- [모바일 워크플로와 UX](docs/mobile-workflow.md)
- [보안, 격리와 거버넌스](docs/security-governance.md)
- [기존 시스템 조사 계획](docs/research/existing-systems.md)
- [로드맵, Epic과 백로그](docs/roadmap.md)
- [ADR 등록부와 미해결 결정](docs/adr/README.md)

### 실행 계약

- [Task Schema](docs/specs/task-schema.md)
- [Task State Machine](docs/specs/task-state-machine.md)
- [GitHub Issue Command Contract](docs/specs/issue-command-contract.md)
- [Pull Request Output Contract](docs/specs/pr-output-contract.md)
- [Execution Runtime](docs/specs/execution-runtime.md)
- [Agent Registry](docs/specs/agent-registry.md)
- [Usage and Availability](docs/specs/usage-availability.md)
- [GitHub Event Ingestion](docs/specs/github-event-ingestion.md)
- [Project Lifecycle](docs/specs/project-lifecycle.md)

## Repository Structure

```text
atlas/
├── AGENTS.md                         # AI Agent 작업·리뷰·보안 규칙
├── README.md                         # 프로젝트 진입점과 문서 지도
├── pyproject.toml                    # Python packaging metadata (dependency 없음)
├── .github/
│   ├── ISSUE_TEMPLATE/
│   │   └── atlas-task.yml            # 구조화된 Atlas Task 입력
│   └── pull_request_template.md      # PR 결과와 검증 보고 형식
├── src/atlas/                        # worker 구현
│   ├── policy.py                     # repository allowlist와 scope 어휘
│   ├── config.py                     # polling interval, backoff, lease TTL 설정
│   ├── schema.py                     # Task domain model과 상태
│   ├── parser.py                     # Issue Form body parser
│   ├── validation.py                 # 필수 필드와 invariant 검증
│   ├── idempotency.py                # idempotency key와 in-process 중복 방지
│   ├── issue_source.py               # IssueSource/IssueLister 경계와 GitHub REST adapter
│   ├── intake.py                     # fetch → parse → validate 조립
│   ├── polling.py                    # candidate Issue polling과 등록
│   ├── store.py                      # SQLite operational store, atomic claim, lease
│   └── cli.py                        # show / poll / claim / release / tasks
├── tests/                            # 단위 테스트 (표준 unittest)
└── docs/
    ├── vision.md                     # Mission, 핵심 가치, 성공 상태
    ├── constitution.md               # 저장소의 최상위 운영 원칙
    ├── prd.md                        # 목표, 범위, 요구사항, 성공 조건
    ├── architecture.md               # 컴포넌트, 데이터, 상태 개요
    ├── context-memory.md             # 컨텍스트 packet과 memory 정책
    ├── agents-router-scheduler.md    # 역할, capability, routing 정책
    ├── mobile-workflow.md            # 모바일 Task와 review 흐름
    ├── security-governance.md        # 격리, 권한, 승인, 위협 모델
    ├── roadmap.md                    # Milestone, Epic, 백로그
    ├── specs/                        # Task, runtime, registry, ingestion의 정규 계약
    ├── adr/                          # Accepted/Proposed 결정, 대안, 영향, 후속 작업
    └── research/                     # 외부 시스템 조사와 Build/Adopt 근거
```

`docs/specs/`는 실행 가능한 계약을 제품 방향 문서와 분리합니다. 기존 문서 경로는 이동하지 않아 링크 호환성을 유지하고, ADR은 인덱스와 개별 파일을 함께 관리합니다.

## 설계 원칙

- **Context First:** 모델보다 올바른 컨텍스트 선별이 우선입니다.
- **Project Memory:** 기억은 대화 세션이 아니라 프로젝트 저장소에 남습니다.
- **Human Approval:** 위험한 변경과 최종 반영은 사람이 승인합니다.
- **Vendor Neutrality:** Codex, Claude Code, OpenHands 등 실행기를 교체할 수 있어야 합니다.
- **Observable Work:** 계획, 실행, 로그, 테스트, 비용, 결과를 추적합니다.
- **Least Privilege:** 역할과 작업에 필요한 최소 권한만 부여합니다.

## Initial Operating Model Decisions

- [x] GitHub Markdown을 canonical documentation source로 승인
- [x] GitHub Issues와 Atlas Task Issue Form을 초기 mobile intake로 승인
- [x] self-hosted Claude Code worker를 primary automated executor로 승인
- [x] Codex Cloud를 manual/secondary executor로 분류
- [x] 초기 구현 언어를 Python으로 확정 (ADR-011)
- [x] ADR-008 polling-first ingestion 승인
- [x] ADR-012 operational state store 승인
- [ ] ADR-009 tmux PoC와 stable supervisor 전환 제안 검토
- [ ] ADR-010 Task/Run isolation 제안 검토
- [ ] always-available server의 hosting 위치와 stable 운영 policy 결정
- [ ] Issue → claim → Claude Code → validation → PR E2E 구현과 검증

| 영역 | 상태 | 완료 기준 |
| --- | --- | --- |
| 제품 정의 | Complete | Accepted ADR-001~003 반영 |
| 운영 명세 | In Progress | Proposed ADR-004~007·009~010 검토와 open question 해소 |
| 수동 delivery | Proven Manually | Codex branch → PR → human merge 재현 |
| Issue intake | In Progress | 단건 fetch·parse·검증 구현; valid Task live E2E 확인 필요 |
| 자동 실행 환경 | Not Implemented | mock vertical slice 이후 Claude Code integration 검증 |

## 문서 운영

- 중요한 기술 결정은 [ADR](docs/adr/README.md)로 남깁니다.
- 모든 변경은 독립 브랜치와 Pull Request로 제출합니다.
- 완료 주장은 자동 테스트 또는 명시적인 검증 근거를 포함해야 합니다.
- [ADR-001](docs/adr/0001-documentation-source-of-truth.md)에 따라 merge된 GitHub Markdown이 canonical source입니다. Notion은 선택적인 mirror입니다.

이 초기 문서 세트는 [Atlas Notion 문서](https://app.notion.com/p/3cd9f036b307814f888fe2fb827a230a?pvs=204)를 2026-08-31 기준으로 옮겼습니다. 이 링크는 역사적 출처 또는 선택적 mirror이며, 현재 내용의 source of truth는 이 repository의 merge된 Markdown입니다.
