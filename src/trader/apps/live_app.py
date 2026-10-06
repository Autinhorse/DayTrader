"""实盘版入口：trader-live。阶段 6 起接入 IBKR；不得导入 trader.research。"""

import sys


def main() -> int:
    print("trader-live：阶段 0 只有骨架，IBKR 接入在阶段 6。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
