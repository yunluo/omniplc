# -*- coding: utf-8 -*-
"""西门子 S7手动联机测试:连接后每点位随机值写读 3 轮,关闭连接,日志汇总。

手动测试脚本(非单元测试,manual_ 前缀避免被 PyCharm/pytest 收集)。
无参数直接跑(每次只测这一个协议):目标 127.0.0.1:102(rack0/slot1),内置本协议点位。
    uv run python tools/manual_siemens_s7.py [--ip IP] [--port 端口] [--debug] [--seed N] [--rounds N]
传 JSON 配置则按配置运行(自定义 IP/端口/点位;仅取 driver="siemens_s7" 的连接):
    uv run python tools/manual_siemens_s7.py 我的配置.json [--debug]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manual_common import run  # noqa: E402

if __name__ == "__main__":
    run("siemens_s7")
