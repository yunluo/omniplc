# -*- coding: utf-8 -*-
"""三菱 MC 3E 帧(TCP,QnA 兼容)手动联机测试:连接后每点位随机值写读 3 轮,关闭连接,日志汇总。

配置:tools/manual_test.json 中 driver="melsec_mc_tcp" 且 frame="3E" 的连接。
用法:uv run python tools/test_melsec_mc_3e.py [配置路径] [--debug] [--seed N] [--rounds N]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manual_common import run  # noqa: E402

if __name__ == "__main__":
    run("melsec_mc_tcp", frame="3E")
