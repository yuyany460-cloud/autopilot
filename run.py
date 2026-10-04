# -*- coding: utf-8 -*-
"""免安装启动器（应对嵌入式 Python 的 sys.path 隔离）。

有些 Windows 上的 Python 是"嵌入式发行版"，目录里带一个 ``python312._pth``，
它会开启隔离模式：当前目录、``PYTHONPATH`` 和脚本所在目录**都不会**
进入 ``sys.path``。这种情况下 ``python -m autopilot`` 会报"找不到模块"。

本脚本把项目根目录显式塞进 ``sys.path``，因此无论用哪种 Python 都能跑：

    python run.py selfcheck
    python run.py run "打开记事本写一句话"
    python run.py serve

推荐做法仍然是 ``pip install -e .``，那样可以直接用 ``apilot`` 命令。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    try:
        from autopilot.cli import main as cli_main
    except ImportError as exc:  # pragma: no cover
        print(f"无法导入 autopilot：{exc}", file=sys.stderr)
        print(f"项目根目录：{ROOT}", file=sys.stderr)
        return 1
    os.chdir(ROOT)
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
