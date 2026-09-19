"""整体冒烟测试:导入、类继承、构造校验、上下文管理器、异步镜像。"""
from __future__ import annotations

import asyncio

import pytest

import omniplc
from omniplc import (
    BaseClient,
    MelsecMcTcpClient,
    MelsecMcUdpClient,
    MelsecMxClient,
    ModbusBaseClient,
    ModbusRtuClient,
    ModbusTcpClient,
    OmronFinsTcpClient,
    OmronFinsUdpClient,
)
from omniplc.aio import (
    AMelsecMcTcpClient,
    AMelsecMxClient,
    AModbusRtuClient,
    AModbusTcpClient,
    AOmronFinsTcpClient,
    AOmronFinsUdpClient,
)
from omniplc.transport import BaseTransport, SerialTransport, TcpTransport, UdpTransport
from omniplc.types import DataType, McFrame


class TestPublicSurface:
    """公开 API 面。"""

    def test_version(self) -> None:
        assert omniplc.__version__

    def test_all_classes_exported(self) -> None:
        for name in (
            "ModbusTcpClient",
            "ModbusRtuClient",
            "MelsecMcTcpClient",
            "MelsecMcUdpClient",
            "MelsecMxClient",
            "OmronFinsTcpClient",
            "OmronFinsUdpClient",
        ):
            assert hasattr(omniplc, name)


class TestInheritance:
    """类继承树。"""

    def test_modbus_tree(self) -> None:
        assert issubclass(ModbusBaseClient, BaseClient)
        for cls in (ModbusTcpClient, ModbusRtuClient):
            assert issubclass(cls, ModbusBaseClient)

    def test_melsec_tree(self) -> None:
        for cls in (MelsecMcTcpClient, MelsecMcUdpClient, MelsecMxClient):
            assert issubclass(cls, BaseClient)

    def test_transport_tree(self) -> None:
        for cls in (TcpTransport, UdpTransport, SerialTransport):
            assert issubclass(cls, BaseTransport)


class TestConstructorValidation:
    """构造参数校验。"""

    def test_modbus_bad_endpoint(self) -> None:
        with pytest.raises(ValueError):
            ModbusTcpClient("", 502, 1)
        with pytest.raises(ValueError):
            ModbusTcpClient("127.0.0.1", 0, 1)

    def test_modbus_bad_station(self) -> None:
        with pytest.raises(ValueError):
            ModbusTcpClient("127.0.0.1", 502, 300)

    def test_melsec_bad_frame(self) -> None:
        with pytest.raises(ValueError):
            MelsecMcTcpClient("192.168.3.39", 2000, frame="5E")

    def test_melsec_frames_accepted(self) -> None:
        # 枚举与字符串两种写法都接受,属性返回枚举
        for frame in (McFrame.FRAME_3E, McFrame.FRAME_4E, McFrame.FRAME_1E):
            client = MelsecMcTcpClient("192.168.3.39", 2000, frame=frame)
            assert client.frame is frame
        assert MelsecMcTcpClient("192.168.3.39", 2000, frame="1e").frame is McFrame.FRAME_1E

    def test_omron_clients_constructible(self) -> None:
        assert isinstance(OmronFinsTcpClient("192.168.250.1"), BaseClient)
        assert isinstance(OmronFinsUdpClient("192.168.250.1"), BaseClient)


class TestContextManager:
    """上下文管理器行为(真实 socket 拒绝连接)。"""

    def test_refused_connection_raises(self) -> None:
        # 端口 1 在本机回环上通常无人监听 → 连接被拒
        client = ModbusTcpClient("127.0.0.1", 1, 1)
        with pytest.raises(ConnectionError):
            with client:
                pass
        assert client.last_error is not None

    def test_normal_cycle(self, tcp_echo_port: int) -> None:
        with ModbusTcpClient("127.0.0.1", tcp_echo_port, 1) as client:
            assert client.connected is True
        assert client.connected is False


class TestDataTypeNames:
    """类型名称解析。"""

    def test_known_names(self) -> None:
        assert DataType.from_name("FLOAT") is DataType.FLOAT
        assert DataType.from_name(" short ") is DataType.SHORT

    def test_unknown_name(self) -> None:
        with pytest.raises(ValueError):
            DataType.from_name("int128")


class TestAsyncMirror:
    """异步镜像类。"""

    def test_construction(self) -> None:
        for make in (
            lambda: AModbusTcpClient("127.0.0.1", 502, 1),
            lambda: AModbusRtuClient(1),
            lambda: AMelsecMcTcpClient("192.168.3.39", 2000, "3E"),
            lambda: AMelsecMxClient(1),
            lambda: AOmronFinsTcpClient("192.168.250.1"),
            lambda: AOmronFinsUdpClient("192.168.250.1"),
        ):
            client = make()
            asyncio.run(client.close())

    def test_async_refused_connection(self) -> None:
        async def scenario() -> None:
            client = AModbusTcpClient("127.0.0.1", 1, 1)
            ok = await client.connect()
            assert ok is False
            assert client.last_error is not None
            assert client.connected is False
            await client.close()

        asyncio.run(scenario())

    def test_async_context_refused(self) -> None:
        async def scenario() -> None:
            client = AModbusTcpClient("127.0.0.1", 1, 1)
            with pytest.raises(ConnectionError):
                async with client:
                    pass

        asyncio.run(scenario())
