# -*- coding: utf-8 -*-
"""OPC-UA手动联机测试:连接后每点位随机值写读 3 轮,关闭连接,日志汇总。

无参数直接跑(每次只测这一个协议):目标 127.0.0.1:4840,内置本协议点位。
    uv run python tools/test_opcua.py [--ip IP] [--port 端口] [--debug] [--seed N] [--rounds N]
传 JSON 配置则按配置运行(自定义 IP/端口/点位;仅取 driver="opcua" 的连接):
    uv run python tools/test_opcua.py 我的配置.json [--debug]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manual_common import run  # noqa: E402

if __name__ == "__main__":
    run("opcua")
