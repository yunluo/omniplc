# -*- coding: utf-8 -*-
"""三菱 MC 3C 帧(串口 ASCII,格式 4)手动联机测试:连接后每点位随机值写读 3 轮,关闭连接,日志汇总。

无参数直接跑(每次只测这一个协议):目标 串口 COM4 9600-8N1,内置本协议点位。
    uv run python tools/test_melsec_mc_3c.py [--ip IP] [--port 端口] [--debug] [--seed N] [--rounds N]
传 JSON 配置则按配置运行(自定义 IP/端口/点位;仅取 driver="melsec_mc_serial" 且 frame="3C" 的连接):
    uv run python tools/test_melsec_mc_3c.py 我的配置.json [--debug]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manual_common import run  # noqa: E402

if __name__ == "__main__":
    run("melsec_mc_serial", frame="3C")
