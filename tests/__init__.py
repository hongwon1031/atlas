"""테스트 package.

설치 없이 `src/`의 `atlas` package를 import할 수 있게 합니다. 이 파일 덕분에
저장소 루트에서 다음 명령만으로 전체 테스트를 실행할 수 있습니다.

    python -m unittest discover -s tests -t .
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
