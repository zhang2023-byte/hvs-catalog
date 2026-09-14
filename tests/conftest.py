"""让测试可以导入 tools 目录下的开发检查工具。"""

import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
