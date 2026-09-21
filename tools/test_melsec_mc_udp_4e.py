# -*- coding: utf-8 -*-
"""三菱 MC 4E 帧(UDP)手动联机测试:连接后每点位随机值写读 3 轮,关闭连接,日志汇总。

配置:tools/manual_test.json 中 driver="melsec_mc_udp" 且 frame="4E" 的连接。
用法:uv run python tools/test_melsec_mc_udp_4e.py [配置路径] [--debug] [--seed N] [--rounds N]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manual_common import run  # noqa: E402

if __name__ == "__main__":
    run("melsec_mc_udp", frame="4E")
