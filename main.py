# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""NatScope 桌面入口。

打包：pyinstaller natscope.spec
直接运行：python main.py
"""

import sys


def main() -> int:
    # 高 DPI 必须在创建窗口前设置，否则界面会被系统拉伸发虚
    from natscope.gui.app import enable_dpi_awareness

    enable_dpi_awareness()

    from natscope.gui.app import run

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
