"""欧姆龙 CIP / 连接型 CIP 客户端——NJ/NX 内置 EtherNet/IP 变量读写。

NJ/NX(Sysmac)系列没有 FINS/TCP-UDP,变量经标准 CIP 显式报文访问;
与罗克韦尔 AB 同属 ODVA EtherNet/IP(端口同为 44818),故继承
:class:`~omniplc.plc.ab.AllenBradleyEthIpClient`,仅覆写走线与变量
模型差异:

- **unconnected 直发(默认)**:目标即消息路由器本体,RRData 的
  Unconnected Data(0xB2)项直接携带服务请求/应答,不包 Unconnected
  Send(0x52)、无背板路由段(AB 必须包 UC Send 经背板路由到槽号)
- **连接型(``connected_messaging=True``)**:Forward Open/Close 的连接
  路径只剩消息路由对象(20 02 24 01);Large(4002)优先、被拒回落
  普通(504)的策略与 AB 相同
- **NJ 变量模型**:标量 BOOL 直接读写(C1 走继承路径);BOOL 数组按
  元素访问(NJ 不做 Logix 的 32 位打包,实际类型由自描述应答决定,
  元素应答存储字类型时按 Logix 口径回退);整型 ``.位号`` 写走 0x4E
  读-改-写,设备侧支持与否随固件,不支持时按 DeviceError 报告不断线
- **NJ STRING**:布局 = 4 字节字符数(len u32)+ 字符(无 Logix 82
  字符上限、无 88 字节填充);写入前先读一次取模板实例号与声明尺寸,
  写入类型域回带实际模板号,值超声明尺寸拒绝(防溢出)。布局以
  Sysmac 手册口径实现,模板号随设备自描述,无需硬编码

发起方厂商号沿用 0x1337(仅标识发起端,目标设备不校验)。
地址语法与 AB 相同:变量名/数组下标/结构成员(如 ``Motor[2].Speed``)。
"""
from __future__ import annotations

import struct

from ..ab import AllenBradleyEthIpClient, codec_cip
from ..ab.ab import _check_bit_range, _single_array_index, _strip_bit, _word_index_path
from ..ab.address import AbTag, parse_ab_tag
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
    # BOOL 数组:按元素访问,实际类型由自描述应答决定(NJ 不做 32 位打包)
    # ------------------------------------------------------------------

    def _read_bool_array_element(self, parsed: AbTag) -> bool:
        """NJ/NX BOOL 数组元素读:按元素直读(内部方法)。

        元素应答 BOOL 即直取;个别固件对元素路径回存储字类型(DWORD)
        时按 Logix ``下标//32`` 打包口径回退,两种固件行为均覆盖。
        """
        cip_type, data = self._read_tag_values(parsed, 1)
        if cip_type == codec_cip.CIP_TYPE_BOOL:
            return bool(codec_cip.decode_values(data, cip_type, 1)[0])
        if cip_type == codec_cip.CIP_TYPE_DWORD:
            index = _single_array_index(parsed)
            _, data = self._read_tag_values(_word_index_path(parsed, index), 1)
            return bool(
                (codec_cip.decode_word(data, codec_cip.CIP_TYPE_DWORD) >> (index % 32)) & 1
            )
        raise ValueError(
            "标签 {!r} 实际类型 {} 不是 BOOL".format(
                parsed.name, codec_cip.type_name(cip_type)
            )
        )

    def _write_bool_impl(self, parsed: AbTag, flag: bool) -> None:
        """NJ/NX 布尔写:BOOL 数组元素按元素直写(内部方法)。

        类型发现对元素路径回存储字类型(DWORD)时,先按元素以 BOOL
        类型直写(类型不符会被 PLC 拒绝、无副作用),被拒再回退
        Logix ``下标//32`` 读-改-写口径。
        """
        if parsed.bit is not None:
            cip_type = self._ensure_type(parsed)
            if cip_type == codec_cip.CIP_TYPE_BOOL:
                raise ValueError(
                    f"BOOL 标签不支持位号后缀:{parsed.name!r}"
                )
            bit = parsed.bit or 0
            _check_bit_range(parsed, cip_type, bit)
            self._modify_word(_strip_bit(parsed), cip_type, bit, flag)
            return
        cip_type = self._ensure_type(parsed)
        if cip_type == codec_cip.CIP_TYPE_BOOL:
            self._transact(
                codec_cip.build_tag_write(
                    codec_cip.tag_type_path(parsed),
                    cip_type,
                    b"\x01" if flag else b"\x00",
                ),
                codec_cip.CIP_SERVICE_WRITE_TAG,
            )
            return
        if cip_type == codec_cip.CIP_TYPE_DWORD:
            index = _single_array_index(parsed)
            try:
                self._transact(
                    codec_cip.build_tag_write(
                        codec_cip.tag_type_path(parsed),
                        codec_cip.CIP_TYPE_BOOL,
                        b"\x01" if flag else b"\x00",
                    ),
                    codec_cip.CIP_SERVICE_WRITE_TAG,
                )
                return
            except DeviceError:
                self._modify_word(
                    _word_index_path(parsed, index),
                    codec_cip.CIP_TYPE_DWORD,
                    index % 32,
                    flag,
                )
                return
        raise ValueError(
            "标签 {!r} 实际类型 {} 不是 BOOL".format(
                parsed.name, codec_cip.type_name(cip_type)
            )
        )

    # ------------------------------------------------------------------
    # NJ/NX STRING:len(u32) + 字符(无 82 上限、无 88 填充),写入前
    # 取实际模板号与声明尺寸
    # ------------------------------------------------------------------

    def _write_string(
        self, address: str, value: str, encoding: str
    ) -> PrimitiveValue:
        """写 NJ/NX STRING(先读模板号与声明尺寸,值超尺寸拒绝)。"""
        parsed = parse_ab_tag(address)
        if parsed.bit is not None:
            raise ValueError(f"字符串标签不支持位访问:{address!r}")
        # 先读:应答 = 0xA0 + 模板实例号(2) + len(u32) + 字符…
        request = codec_cip.build_tag_read(codec_cip.tag_type_path(parsed), 1)
        payload: bytes = self._transact(request, codec_cip.CIP_SERVICE_READ_TAG)
        if len(payload) < 8 or payload[0] != codec_cip.CIP_TYPE_STRUCT:
            raise ValueError(
                "标签 {!r} 实际类型 {},字符串写入需要 STRING".format(
                    address,
                    codec_cip.type_name(payload[0]) if payload else "空应答",
                )
            )
        template_id = payload[2] | (payload[3] << 8)
        usable = len(payload) - 4 - 4  # 结构体尺寸 - 模板头 - 长度域
        if usable <= 0:
            raise ValueError(
                "标签 {!r} 的 STRING 声明尺寸非法:{}".format(address, len(payload) - 4)
            )
        raw = value.encode(encoding)
        if len(raw) > usable:
            raise ValueError(
                "写入值 {} 字符超出 NJ STRING 声明尺寸 {} 字符({!r})".format(
                    len(raw), usable, address
                )
            )
        data = struct.pack("<I", len(raw)) + raw + b"\x00" * (usable - len(raw))
        self._transact(
            codec_cip.build_string_write(
                codec_cip.tag_type_path(parsed), data, template_id=template_id
            ),
            codec_cip.CIP_SERVICE_WRITE_TAG,
        )
        return value
