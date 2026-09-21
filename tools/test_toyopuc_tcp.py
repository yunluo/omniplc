# -*- coding: utf-8 -*-
"""丰田 TOYOPUC TCP手动联机测试:连接后每点位随机值写读 3 轮,关闭连接,日志汇总。

配置:tools/manual_test.json 中 driver="toyopuc_tcp" 的连接(可传位置参数换配置文件)。
用法:uv run python tools/test_toyopuc_tcp.py [配置路径] [--debug] [--seed N] [--rounds N]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manual_common import run  # noqa: E402

if __name__ == "__main__":
    run("toyopuc_tcp")
