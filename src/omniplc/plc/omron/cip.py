"""欧姆龙 CIP / 连接型 CIP 客户端——NJ/NX 内置 EtherNet/IP 变量读写。

NJ/NX(Sysmac)系列没有 FINS/TCP-UDP,变量经标准 CIP 显式报文访问;
与罗克韦尔 AB 同属 ODVA EtherNet/IP(端口同为 44818),故继承
:class:`~omniplc.plc.ab.AllenBradleyEthIpClient`,仅覆写三处走线差异:

- **unconnected 直发(默认)**:目标即消息路由器本体,RRData 的
  Unconnected Data(0xB2)项直接携带服务请求/应答,不包 Unconnected
  Send(0x52)、无背板路由段(AB 必须包 UC Send 经背板路由到槽号)
- **连接型(``connected_messaging=True``)**:Forward Open/Close 的连接
  路径只剩消息路由对象(20 02 24 01);Large(4002)优先、被拒回落
  普通(504)的策略与 AB 相同
- **NJ 变量模型**:标量 BOOL 直接读写(C1 走继承路径);BOOL 数组按
  元素访问(NJ 不做 Logix 的 32 位打包,实际类型由自描述应答决定);
  整型 ``.位号`` 写走 0x4E 读-改-写,设备侧支持与否随固件,不支持时按
  DeviceError 报告不断线

发起方厂商号沿用 0x1337(仅标识发起端,目标设备不校验)。STRING 结构
布局与 AB 不同(长度域宽度待真机核证),v1 显式拒绝字符串读写。
地址语法与 AB 相同:变量名/数组下标/结构成员(如 ``Motor[2].Speed``)。
"""
from __future__ import annotations

from ..ab import AllenBradleyEthIpClient, codec_cip
from ...core.constants import AB_EIP_DEFAULT_PORT
from ...core.errors import DeviceError
from ...types import PrimitiveValue


class OmronCipClient(AllenBradleyEthIpClient):
    """欧姆龙 NJ/NX 系列 CIP 客户端(内置 EtherNet/IP,44818)。

    :example::

        client = OmronCipClient("192.168.0.10", 44818)
        client.connect()
        ok, value = client.read_int("TestVar")
        ok = client.write_bool("RunFlag", True)
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = AB_EIP_DEFAULT_PORT,
        connected_messaging: bool = False,
    ) -> None:
        """初始化欧姆龙 CIP 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口,EtherNet/IP 默认 44818
        :param connected_messaging: True 走 connected 消息(Forward Open +
            SendUnitData);默认 False 走 unconnected 直发
        :raises ValueError: 参数非法
        """
        super().__init__(ip_address, port, 0, connected_messaging)

    # ------------------------------------------------------------------
    # 走线差异:目标即消息路由器,无背板路由、不包 UC Send
    # ------------------------------------------------------------------

    def _route_path(self) -> bytes:
        """连接路径路由段:NJ/NX 内置口 CPU 即目标,无路由段(内部方法)。"""
        return b""

    def _wrap_unconnected(self, cip_request: bytes) -> bytes:
        """unconnected 直发:目标即消息路由器,不包 UC Send(内部方法)。"""
        return cip_request

    def _parse_unconnected_reply(self, reply: bytes, request_service: int) -> bytes:
        """直发应答解析:0xB2 项内直接是服务应答(内部方法)。"""
        return codec_cip.parse_direct_service_reply(reply, request_service)

    # ------------------------------------------------------------------
    # NJ/NX STRING:结构布局与 AB 不同,待真机核证,显式拒绝
    # ------------------------------------------------------------------

    def _read_string(
        self, address: str, length: int, encoding: str
    ) -> PrimitiveValue:
        """NJ/NX 暂不支持字符串读取(布局待真机核证,内部方法)。"""
        raise DeviceError(
            "NJ/NX 字符串结构布局待真机核证,暂不支持字符串读取", 0
        )

    def _write_string(
        self, address: str, value: str, encoding: str
    ) -> PrimitiveValue:
        """NJ/NX 暂不支持字符串写入(布局待真机核证,内部方法)。"""
        raise DeviceError(
            "NJ/NX 字符串结构布局待真机核证,暂不支持字符串写入", 0
        )
