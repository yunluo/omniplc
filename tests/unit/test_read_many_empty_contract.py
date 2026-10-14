"""read_many([]) 全客户端契约守卫(review-1033 §七 P2)。

契约(基类 :meth:`~omniplc.core.base_client.BaseClient.read_many` docstring):
**空列表返回 ``[]``**(与 ``write_many`` 一致);``read_batch``/``write_batch``
的空入参是 ``ValueError``(「整批」契约)——两种批量入口的不对称是已拍板的
契约选择(review-1023 §四 P3-1),但**覆写 read_many 委托 read_batch 的
客户端**会把空列表语义丢掉,实测 15/30 客户端抛异常(串口两族更早:
在 ``configure_serial`` 前置断言处抛,连参数校验都没走到)。

本测试参数化全部可构造驱动(复用构造冒烟清单),把该契约锁死并防回归。
"""

from __future__ import annotations

import pytest

from test_manual_common_smoke import _build, _connections


@pytest.mark.parametrize("conn", _connections(), ids=lambda c: c["driver"])
def test_read_many_empty_returns_empty_list(conn: dict) -> None:
    """全部驱动:``read_many([], "ushort") == []``(零传输、不抛)。

    现场影响:按"空点表即空结果"写的上层循环(如"本周期没有要采的点")
    原会拿到异常;``write_many([])`` 侧本来就合规,读写需对称。
    """
    client = _build(conn)
    assert client.read_many([], "ushort") == []
    # 写侧对称(原本即合规,一并锁死)
    assert client.write_many([]) == []
