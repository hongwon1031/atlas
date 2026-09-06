"""repository를 살펴 어떤 검증을 수행할지 결정합니다.

**추측으로 명령을 만들지 않습니다.** 모든 step은 repository에서 발견한 파일이나
설정을 근거로 선택하고, 그 근거를 계획에 함께 남깁니다.

Atlas가 하지 않는 일입니다.

- dependency 설치. `npm install`, `pip install`, `poetry install`, `uv sync`를
  자동으로 실행하지 않습니다. 없으면 없다고 보고합니다.
- package.json에 정의되지 않은 임의 script 실행.
- 사용자 Issue 본문이나 임의 텍스트를 명령에 넣는 일.
- shell wrapper 사용. 모든 명령은 argv list입니다.

## repository 코드 실행과 신뢰 정책

**`shell=False`는 sandbox가 아닙니다.** Atlas가 shell을 거치지 않을 뿐,
실행된 repository 코드는 스스로 subprocess를 띄우고 network에 접속하고 host
filesystem을 읽고 쓸 수 있습니다. 환경변수를 줄이는 것도 sandbox가 아닙니다.

구체적으로 다음은 임의 코드 실행 경로입니다.

- Python 테스트: import만으로 module 최상위 코드가 실행되고, `conftest.py`도
  실행됩니다.
- `npm run <script>`: script 본문이 임의 shell 문자열입니다. 이름만 allowlist에
  넣어도 본문은 무엇이든 될 수 있습니다.
- mypy: config에 선언된 plugin을 import합니다.

executor에게 shell 도구를 주지 않았더라도, executor가 test나 `package.json`을
고친 뒤 validation이 그것을 실행하면 **그 제한을 우회하는 경로**가 됩니다.

MVP에서는 실제 sandbox를 만들지 않습니다. 대신 **명시적 신뢰 정책 뒤에** 둡니다.

- 기본값은 `untrusted`입니다(fail closed).
- `untrusted`에서는 repository 코드를 실행하지 않는 step만 수행합니다.
- repository 코드를 실행하는 step은 `trusted`에서만 수행합니다.
- 신뢰 여부와 무관하게 실행된 코드는 격리되지 않습니다. 이 문서와 코드는
  "network가 차단된다"고 주장하지 않습니다. 차단하지 않기 때문입니다.

## contract 강도

도구 설정이 있다고 해서 곧바로 "이 repository는 그 도구로 게이트한다"는
뜻은 아닙니다. `pyproject.toml`의 `[tool.ruff]`는 편집기 설정만 담는 경우가
흔합니다. 그래서 근거의 강도를 구분합니다.

- **strong** — 전용 config 파일(`ruff.toml`, `mypy.ini`, `pyrightconfig.json`,
  `pytest.ini` 등)이 있거나 프로젝트 dependency에 그 도구가 선언돼 있습니다.
  이 경우 도구가 없으면 검증을 수행하지 못한 것이므로 `error`입니다.
- **weak** — `pyproject.toml` 안의 `[tool.X]` table만 있습니다. 도구가 있으면
  실행하고, 없으면 `skipped`로 남깁니다.

이 구분이 없으면 편집기 설정만 있는 repository가 도구 미설치만으로 실패합니다.
반대로 구분을 아예 두지 않으면 진짜로 요구하는 repository를 통과시킵니다.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tomllib
from fnmatch import fnmatch
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from .validation_models import StepKind, ValidationPlan, ValidationStep

# package.json에서 실행을 허용하는 script 이름입니다. 이 밖의 이름은
# 실행하지 않습니다.
ALLOWED_NODE_SCRIPTS: tuple[str, ...] = ("test", "lint", "typecheck", "build")

# lockfile로 package manager를 정합니다. 추측하지 않습니다.
LOCKFILE_TO_MANAGER: tuple[tuple[str, str], ...] = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("package-lock.json", "npm"),
)

# 소스와 테스트로 인정하는 디렉터리 이름. repository 전체를 무작정 훑지
# 않습니다.
SOURCE_DIRS: tuple[str, ...] = ("src",)
TEST_DIRS: tuple[str, ...] = ("tests", "test")

DEFAULT_STEP_TIMEOUT_SECONDS = 900.0

# repository 코드를 실행하지 않는 step만 남기는 기본 정책입니다.
TRUST_UNTRUSTED = "untrusted"
TRUST_TRUSTED = "trusted"
TRUST_POLICIES: frozenset[str] = frozenset({TRUST_UNTRUSTED, TRUST_TRUSTED})

# 신뢰 정책이 없을 때 repository 코드 실행 step에 남기는 사유.
UNTRUSTED_SKIP_REASON = "active_validation_requires_trust"

# stdlib unittest discover가 찾는 기본 pattern입니다. 이 pattern에 맞지 않으면
# "Ran 0 tests"로 조용히 통과합니다.
UNITTEST_PATTERN = "test*.py"


@dataclass(frozen=True)
class ToolContract:
    """도구를 요구하는 근거와 그 강도."""

    present: bool = False
    strong: bool = False
    evidence: tuple[str, ...] = ()

    def merged(self, other: ToolContract) -> ToolContract:
        return ToolContract(
            present=self.present or other.present,
            strong=self.strong or other.strong,
            evidence=self.evidence + other.evidence,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "strength": "strong" if self.strong else ("weak" if self.present else "none"),
            "evidence": list(self.evidence),
        }


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except (OSError, ValueError):
        return {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _module_available(module: str, python: str) -> bool:
    """대상 interpreter에서 module을 쓸 수 있는지 확인합니다.

    같은 interpreter면 import하지 않고 spec만 조회합니다. module 코드를
    실행하지 않습니다.
    """

    if python != sys.executable:
        # 다른 interpreter는 여기서 판단하지 않습니다. 실행 시점에 드러납니다.
        return False
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


@dataclass(frozen=True)
class RepositoryFacts:
    """repository에서 발견한 사실. 판단은 하지 않습니다."""

    root: Path
    files: tuple[str, ...] = ()
    source_dirs: tuple[str, ...] = ()
    test_dirs: tuple[str, ...] = ()
    pyproject: dict[str, Any] = field(default_factory=dict)
    package_json: dict[str, Any] = field(default_factory=dict)
    node_manager: str | None = None
    node_modules: bool = False
    has_python_files: bool = False

    @property
    def is_python(self) -> bool:
        return (
            any(name in self.files for name in PYTHON_MARKER_FILES)
            or bool(self.source_dirs)
            or bool(self.test_dirs)
            or self.has_python_files
        )

    @property
    def is_node(self) -> bool:
        return bool(self.package_json)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": list(self.files),
            "source_dirs": list(self.source_dirs),
            "test_dirs": list(self.test_dirs),
            "node_manager": self.node_manager,
            "node_modules": self.node_modules,
            "has_python_files": self.has_python_files,
        }


# 이 중 하나라도 있으면 Python repository로 봅니다. 설정만 있고 소스
# 디렉터리가 없는 repository도 있습니다.
PYTHON_MARKER_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "setup.py",
    "ruff.toml",
    ".ruff.toml",
    "mypy.ini",
    ".mypy.ini",
    "pyrightconfig.json",
)

# 존재 여부만 확인할 파일들.
_INTERESTING_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "setup.py",
    "ruff.toml",
    ".ruff.toml",
    "mypy.ini",
    ".mypy.ini",
    "pyrightconfig.json",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
)


def inspect_repository(root: Path | str) -> RepositoryFacts:
    """repository 구조를 읽습니다. 명령을 실행하지 않습니다."""

    base = Path(root)
    files = tuple(name for name in _INTERESTING_FILES if (base / name).is_file())
    source_dirs = tuple(name for name in SOURCE_DIRS if (base / name).is_dir())
    test_dirs = tuple(name for name in TEST_DIRS if (base / name).is_dir())

    pyproject = _read_toml(base / "pyproject.toml") if "pyproject.toml" in files else {}

    package_json: dict[str, Any] = {}
    if "package.json" in files:
        try:
            package_json = json.loads(_read_text(base / "package.json")) or {}
        except (ValueError, TypeError):
            package_json = {}
        if not isinstance(package_json, dict):
            package_json = {}

    manager = None
    for lockfile, name in LOCKFILE_TO_MANAGER:
        if lockfile in files:
            manager = name
            break
    if manager is None and package_json:
        # lockfile이 없으면 npm으로 봅니다. package.json이 있으면 npm은 항상
        # 그 형식을 이해합니다.
        manager = "npm"

    # 최상위에 python 파일이 있으면 그것도 근거입니다. 깊은 탐색은 하지
    # 않습니다. repository 전체를 훑으면 느리고 결정적이지 않습니다.
    try:
        has_python = any(base.glob("*.py"))
    except OSError:
        has_python = False

    return RepositoryFacts(
        root=base,
        files=files,
        source_dirs=source_dirs,
        test_dirs=test_dirs,
        pyproject=pyproject,
        package_json=package_json,
        node_manager=manager,
        node_modules=(base / "node_modules").is_dir(),
        has_python_files=has_python,
    )


def _declared_python_dependencies(pyproject: dict[str, Any]) -> set[str]:
    """프로젝트가 선언한 dependency 이름을 모읍니다."""

    names: set[str] = set()
    project = pyproject.get("project") or {}
    groups: list[Any] = [project.get("dependencies") or []]
    for extra in (project.get("optional-dependencies") or {}).values():
        groups.append(extra)
    for group in (pyproject.get("dependency-groups") or {}).values():
        groups.append(group)
    for group in groups:
        if not isinstance(group, list):
            continue
        for entry in group:
            if not isinstance(entry, str):
                continue
            name = entry.strip().split(";")[0]
            for separator in ("==", ">=", "<=", "~=", "!=", ">", "<", "[", " "):
                name = name.split(separator)[0]
            if name:
                names.add(name.strip().lower())
    return names


def _python_tool_contract(
    facts: RepositoryFacts, tool: str, config_files: tuple[str, ...], pyproject_table: str
) -> ToolContract:
    """도구를 요구하는 근거와 강도를 판정합니다."""

    contract = ToolContract()
    for name in config_files:
        if name in facts.files:
            contract = contract.merged(ToolContract(True, True, (name,)))

    if tool in _declared_python_dependencies(facts.pyproject):
        contract = contract.merged(
            ToolContract(True, True, (f"pyproject.toml:dependencies:{tool}",))
        )

    table = (facts.pyproject.get("tool") or {}).get(pyproject_table)
    if isinstance(table, dict) and table:
        contract = contract.merged(
            ToolContract(True, False, (f"pyproject.toml:[tool.{pyproject_table}]",))
        )
    return contract


def _unittest_targets(root: Path, test_dir: str) -> tuple[int, int]:
    """test 디렉터리의 python 파일 수와 unittest pattern에 맞는 파일 수.

    `unittest discover`는 pattern에 맞는 파일이 하나도 없어도 "Ran 0 tests"로
    exit 0을 냅니다. 그것을 통과로 취급하면 검증하지 않은 변경을 통과시킵니다.
    """

    base = root / test_dir
    try:
        python_files = [p for p in base.rglob("*.py") if p.name != "__init__.py"]
    except OSError:
        return (0, 0)
    matching = [p for p in python_files if fnmatch(p.name, UNITTEST_PATTERN)]
    return (len(python_files), len(matching))


def _pytest_contract(facts: RepositoryFacts) -> ToolContract:
    contract = ToolContract()
    if "pytest.ini" in facts.files:
        contract = contract.merged(ToolContract(True, True, ("pytest.ini",)))
    if "tox.ini" in facts.files and "[pytest]" in _read_text(facts.root / "tox.ini"):
        contract = contract.merged(ToolContract(True, True, ("tox.ini:[pytest]",)))
    if "setup.cfg" in facts.files and "[tool:pytest]" in _read_text(facts.root / "setup.cfg"):
        contract = contract.merged(ToolContract(True, True, ("setup.cfg:[tool:pytest]",)))
    table = (facts.pyproject.get("tool") or {}).get("pytest")
    if isinstance(table, dict) and table:
        contract = contract.merged(
            ToolContract(True, True, ("pyproject.toml:[tool.pytest.ini_options]",))
        )
    if "pytest" in _declared_python_dependencies(facts.pyproject):
        contract = contract.merged(
            ToolContract(True, True, ("pyproject.toml:dependencies:pytest",))
        )
    return contract


def _tool_step(
    *,
    name: str,
    kind: StepKind,
    contract: ToolContract,
    argv: tuple[str, ...],
    available: bool,
    timeout: float,
    executes_repository_code: bool = False,
) -> ValidationStep:
    """contract 강도와 도구 가용성을 합쳐 step을 만듭니다."""

    evidence = {"contract": contract.to_dict(), "available": available}
    if not contract.present:
        return ValidationStep(
            name=name,
            kind=kind,
            required=False,
            executes_repository_code=executes_repository_code,
            evidence=evidence,
            skip_reason="no_contract",
        )
    if not available:
        if contract.strong:
            # repository가 명시적으로 요구하는데 실행할 수 없습니다. 통과로
            # 볼 수 없습니다.
            return ValidationStep(
                name=name,
                kind=kind,
                required=True,
                executes_repository_code=executes_repository_code,
                evidence=evidence,
                error_reason="command_missing",
            )
        return ValidationStep(
            name=name,
            kind=kind,
            required=False,
            executes_repository_code=executes_repository_code,
            evidence=evidence,
            skip_reason="command_missing_weak_contract",
        )
    return ValidationStep(
        name=name,
        kind=kind,
        required=True,
        executes_repository_code=executes_repository_code,
        evidence=evidence,
        argv=argv,
        timeout_seconds=timeout,
    )


def _python_steps(
    facts: RepositoryFacts,
    python: str,
    which: Callable[[str], str | None],
    timeout: float,
) -> list[ValidationStep]:
    steps: list[ValidationStep] = []

    # -- tests ---------------------------------------------------------
    pytest_contract = _pytest_contract(facts)
    pytest_available = _module_available("pytest", python) or bool(which("pytest"))
    if pytest_contract.present:
        steps.append(
            _tool_step(
                name="pytest",
                kind=StepKind.TESTS,
                contract=pytest_contract,
                argv=(python, "-m", "pytest", "-q"),
                available=pytest_available,
                timeout=timeout,
                executes_repository_code=True,
            )
        )
    elif facts.test_dirs:
        # 표준 라이브러리만으로 실행합니다. dependency를 요구하지 않습니다.
        target = facts.test_dirs[0]
        total, matching = _unittest_targets(facts.root, target)
        evidence = {
            "contract": {
                "present": True,
                "strength": "strong",
                "evidence": [f"{target}/"],
            },
            "runner": "stdlib unittest",
            "python_files": total,
            "unittest_pattern": UNITTEST_PATTERN,
            "matching_files": matching,
        }
        if total and not matching:
            # pytest 형식(`*_test.py` 등)만 있습니다. unittest discover는
            # 아무것도 찾지 못하고 exit 0을 냅니다. 통과로 볼 수 없습니다.
            steps.append(
                ValidationStep(
                    name="unittest",
                    kind=StepKind.TESTS,
                    required=True,
                    evidence=evidence,
                    error_reason="test_runner_mismatch",
                )
            )
        elif not total:
            steps.append(
                ValidationStep(
                    name="tests",
                    kind=StepKind.TESTS,
                    required=False,
                    evidence=evidence,
                    skip_reason="no_tests_discovered",
                )
            )
        else:
            steps.append(
                ValidationStep(
                    name="unittest",
                    kind=StepKind.TESTS,
                    required=True,
                    executes_repository_code=True,
                    evidence=evidence,
                    argv=(python, "-m", "unittest", "discover", "-s", target, "-t", "."),
                    timeout_seconds=timeout,
                )
            )
    else:
        steps.append(
            ValidationStep(
                name="tests",
                kind=StepKind.TESTS,
                required=False,
                evidence={"searched": list(TEST_DIRS)},
                skip_reason="no_tests_discovered",
            )
        )

    # -- compile -------------------------------------------------------
    targets = tuple(facts.source_dirs + facts.test_dirs)
    if targets:
        steps.append(
            ValidationStep(
                name="compileall",
                kind=StepKind.COMPILE,
                required=True,
                evidence={"targets": list(targets), "runner": "stdlib compileall"},
                argv=(python, "-m", "compileall", "-q", *targets),
                timeout_seconds=timeout,
            )
        )
    else:
        steps.append(
            ValidationStep(
                name="compileall",
                kind=StepKind.COMPILE,
                required=False,
                evidence={"searched": list(SOURCE_DIRS + TEST_DIRS)},
                skip_reason="no_source_directories",
            )
        )

    # -- lint ----------------------------------------------------------
    ruff = _python_tool_contract(facts, "ruff", ("ruff.toml", ".ruff.toml"), "ruff")
    ruff_path = which("ruff")
    steps.append(
        _tool_step(
            name="ruff",
            kind=StepKind.LINT,
            contract=ruff,
            argv=(ruff_path or "ruff", "check", "."),
            available=bool(ruff_path),
            timeout=timeout,
        )
    )

    # -- typecheck -----------------------------------------------------
    mypy = _python_tool_contract(facts, "mypy", ("mypy.ini", ".mypy.ini"), "mypy")
    mypy_available = _module_available("mypy", python) or bool(which("mypy"))
    steps.append(
        _tool_step(
            name="mypy",
            kind=StepKind.TYPECHECK,
            contract=mypy,
            argv=(python, "-m", "mypy", "."),
            available=mypy_available,
            timeout=timeout,
            # mypy는 config에 선언된 plugin을 import합니다. repository가
            # 제어하는 코드가 실행됩니다.
            executes_repository_code=True,
        )
    )

    pyright = _python_tool_contract(facts, "pyright", ("pyrightconfig.json",), "pyright")
    if pyright.present:
        pyright_path = which("pyright")
        steps.append(
            _tool_step(
                name="pyright",
                kind=StepKind.TYPECHECK,
                contract=pyright,
                argv=(pyright_path or "pyright",),
                available=bool(pyright_path),
                timeout=timeout,
            )
        )

    return steps


def _node_steps(
    facts: RepositoryFacts, which: Callable[[str], str | None], timeout: float
) -> list[ValidationStep]:
    scripts = facts.package_json.get("scripts")
    scripts = scripts if isinstance(scripts, dict) else {}
    manager = facts.node_manager or "npm"
    manager_path = which(manager)

    steps: list[ValidationStep] = []
    for script in ALLOWED_NODE_SCRIPTS:
        kind = {
            "test": StepKind.TESTS,
            "lint": StepKind.LINT,
            "typecheck": StepKind.TYPECHECK,
            "build": StepKind.BUILD,
        }[script]
        # build는 이번 범위에서 항상 optional입니다.
        optional = script == "build"

        if script not in scripts or not str(scripts.get(script) or "").strip():
            steps.append(
                ValidationStep(
                    name=f"{manager}-{script}",
                    kind=kind,
                    required=False,
                    evidence={"package_json_scripts": sorted(scripts)},
                    skip_reason="script_not_defined",
                )
            )
            continue

        evidence = {
            "script": script,
            "manager": manager,
            "lockfile": next(
                (name for name, value in LOCKFILE_TO_MANAGER if value == manager
                 and name in facts.files),
                None,
            ),
            "node_modules": facts.node_modules,
        }
        if not manager_path:
            steps.append(
                ValidationStep(
                    name=f"{manager}-{script}",
                    kind=kind,
                    required=not optional,
                    # 정의된 script는 실행 대상입니다. 지금 실행하지 못하더라도
                    # 신뢰 정책이 먼저 판단할 수 있게 표시해 둡니다.
                    executes_repository_code=True,
                    evidence=evidence,
                    error_reason="command_missing",
                )
            )
            continue
        if not facts.node_modules:
            # 설치하지 않습니다. 없으면 검증할 수 없다고 보고합니다.
            steps.append(
                ValidationStep(
                    name=f"{manager}-{script}",
                    kind=kind,
                    required=not optional,
                    executes_repository_code=True,
                    evidence=evidence,
                    error_reason="dependencies_not_installed",
                )
            )
            continue

        steps.append(
            ValidationStep(
                name=f"{manager}-{script}",
                kind=kind,
                required=not optional,
                # script 본문은 임의 shell 문자열입니다. 이름을 allowlist로
                # 막아도 본문이 무엇을 하는지는 통제하지 못합니다.
                executes_repository_code=True,
                evidence=evidence,
                argv=(manager_path, "run", script),
                timeout_seconds=timeout,
            )
        )
    return steps


def apply_trust_policy(
    steps: list[ValidationStep], trusted: bool
) -> list[ValidationStep]:
    """신뢰 정책이 없으면 repository 코드를 실행하는 step을 제거합니다.

    실행하지 않은 것을 실패로 만들지 않습니다. 대신 required에서 내려 두고
    사유를 남깁니다. 판정은 "검증했다"가 아니라 "정적 검사만 했다"로 기록됩니다.
    """

    if trusted:
        return steps
    adjusted: list[ValidationStep] = []
    for step in steps:
        if not step.executes_repository_code:
            adjusted.append(step)
            continue
        # 이미 다른 사유가 붙어 있어도 신뢰 정책이 이깁니다. 어차피 실행하지
        # 않을 step 때문에 Run을 실패시키면 안 됩니다. 예를 들어
        # `dependencies_not_installed`는 실행할 때만 의미가 있습니다.
        adjusted.append(
            replace(
                step,
                required=False,
                argv=(),
                error_reason="",
                skip_reason=UNTRUSTED_SKIP_REASON,
                evidence={
                    **step.evidence,
                    "planned_argv_removed": True,
                    "superseded_reason": step.error_reason or step.skip_reason or None,
                },
            )
        )
    return adjusted


def build_plan(
    root: Path | str,
    *,
    python: str | None = None,
    which: Callable[[str], str | None] | None = None,
    timeout_seconds: float = DEFAULT_STEP_TIMEOUT_SECONDS,
    trusted: bool = False,
    trust_reason: str = "",
) -> ValidationPlan:
    """repository에서 발견한 근거만으로 검증 계획을 만듭니다.

    `trusted`가 아니면 repository 코드를 실행하는 step을 계획에서 제거합니다.
    기본값은 신뢰하지 않는 쪽입니다.
    """

    facts = inspect_repository(root)
    python = python or sys.executable
    which = which or shutil.which

    steps: list[ValidationStep] = [
        ValidationStep(
            name="workspace-integrity",
            kind=StepKind.WORKSPACE_INTEGRITY,
            required=True,
            evidence={"performed_by": "atlas"},
        ),
        ValidationStep(
            name="git-policy",
            kind=StepKind.GIT_POLICY,
            required=True,
            evidence={"performed_by": "atlas"},
        ),
    ]

    ecosystems: list[str] = []
    if facts.is_python:
        ecosystems.append("python")
        steps.extend(_python_steps(facts, python, which, timeout_seconds))
    if facts.is_node:
        ecosystems.append("node")
        steps.extend(_node_steps(facts, which, timeout_seconds))

    if not ecosystems:
        steps.append(
            ValidationStep(
                name="tests",
                kind=StepKind.TESTS,
                required=False,
                evidence={"reason": "지원하는 ecosystem을 찾지 못했습니다."},
                skip_reason="no_tests_discovered",
            )
        )

    steps = apply_trust_policy(steps, trusted)
    return ValidationPlan(
        steps=tuple(steps),
        ecosystem="+".join(ecosystems) if ecosystems else "unknown",
        discovery=facts.to_dict(),
        trust={
            "policy": TRUST_TRUSTED if trusted else TRUST_UNTRUSTED,
            "reason": trust_reason,
            # 실행된 repository 코드는 격리되지 않습니다. 정확히 적습니다.
            "sandboxed": False,
            "network_denied": False,
        },
    )
