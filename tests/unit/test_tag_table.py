"""TagTable JSON/CSV 导入、映射协议与 read_tag/write_tag 缩放契约测试。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

import pytest

from omniplc import BaseClient
from omniplc.core.errors import DeviceError
from omniplc.tag import Tag, TagTable
from omniplc.transport.base import BaseTransport
from omniplc.types import DataType, PrimitiveValue


@pytest.fixture
def tags_json(tmp_path: Path) -> Iterator[str]:
    """写入一个示例 JSON 点位表并返回路径。"""
    path = str(tmp_path / "tags.json")
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(
            [
                {
                    "tag_id": "furnace_temp",
                    "address": "D100",
                    "data_type": "float",
                    "scale": 0.1,
                    "remark": "炉温",
                },
                {"tag_id": "start_stop", "address": "M10", "data_type": "bool"},
            ],
            fp,
            ensure_ascii=False,
        )
    yield path


def test_from_json(tags_json: str) -> None:
    table = TagTable.from_json(tags_json)
    assert len(table) == 2
    assert table["furnace_temp"].address == "D100"
    assert table["furnace_temp"].scale == 0.1
    assert table["furnace_temp"].remark == "炉温"
    assert table["start_stop"].offset == 0.0
    assert table["start_stop"].remark == ""  # 省略 remark 默认空


def test_from_csv(tmp_path: Path) -> None:
    path = str(tmp_path / "tags.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as fp:
        fp.write("tag_id,address,data_type,scale,offset,remark\n")
        fp.write("pressure,hr0,float,0.01,0,压力\n")
        fp.write("counter,D200,int\n")
    table = TagTable.from_csv(path)
    assert len(table) == 2
    assert table["pressure"].scale == 0.01
    assert table["pressure"].remark == "压力"
    assert table["counter"].scale == 1.0  # 省略时默认
    assert table["counter"].remark == ""  # 省略 remark 列默认空


def test_csv_missing_column(tmp_path: Path) -> None:
    path = str(tmp_path / "bad.csv")
    with open(path, "w", encoding="utf-8", newline="") as fp:
        fp.write("tag_id,address\n")
    with pytest.raises(ValueError):
        TagTable.from_csv(path)


def test_duplicate_tag_id_rejected() -> None:
    table = TagTable([Tag("a", "D0", "short")])
    with pytest.raises(ValueError):
        table.add(Tag("a", "D1", "short"))


def test_mapping_protocol() -> None:
    table = TagTable([Tag("a", "D0", "short"), Tag("b", "D1", "short")])
    assert list(iter(table)) == ["a", "b"]
    assert "a" in table
    assert "c" not in table
    with pytest.raises(KeyError):
        table["c"]


def test_add_validation() -> None:
    with pytest.raises(ValueError):
        TagTable([Tag("", "D0", "short")])
    with pytest.raises(ValueError):
        TagTable([Tag("x", "", "short")])
    with pytest.raises(ValueError):
        TagTable([Tag("x", "D0", "short", scale=0)])


# ----------------------------------------------------------------------
# read_tag / write_tag 缩放契约(记录型假客户端,不依赖具体协议)
# ----------------------------------------------------------------------


class _NullTransport(BaseTransport):
    """无收发假传输(点位读写不触碰网络)。"""

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, data: bytes) -> None:
        pass

    def recv(self, size: int) -> bytes:
        return b""


class _RecordingClient(BaseClient):
    """记录写调用、返回预置读值的假客户端。"""

    def __init__(self) -> None:
        super().__init__("127.0.0.1", 502)
        self.writes: List[Tuple[str, DataType, PrimitiveValue]] = []
        self.reads: Dict[str, PrimitiveValue] = {}

    def _create_transport(self) -> BaseTransport:
        return _NullTransport()

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        if address not in self.reads:
            raise DeviceError(f"模拟设备侧无值:{address}", 0)
        return self.reads[address]

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        self.writes.append((address, data_type, value))


def _recording_client() -> _RecordingClient:
    """挂上假传输并置为已连接(同 test_opentcp 的 _attach 惯例)。"""
    client = _RecordingClient()
    transport = _NullTransport()
    transport.receive_timeout = 5.0
    client._transport = transport
    client._connected = True
    return client


def test_read_tag_scaling() -> None:
    """数值点位读出后应用 scale/offset;BOOL/STRING 直通;读失败原样返回。"""
    client = _recording_client()
    client.bind_tags(
        TagTable(
            [
                Tag("furnace_temp", "D100", "float", scale=0.1, offset=-5.0, remark="炉温"),
                Tag("start_stop", "M10", "bool", remark="启停"),
                Tag("steel_grade", "D200", "string", remark="牌号"),
            ]
        )
    )
    client.reads = {"D100": 12345.0, "M10": True, "D200": "Q235"}
    assert client.read_tag("furnace_temp") == (True, 12345.0 * 0.1 - 5.0)
    assert client.read_tag("start_stop") == (True, True)
    assert client.read_tag("steel_grade") == (True, "Q235")
    assert client.read_tag(Tag("ad_hoc", "D100", "float", scale=1.0)) == (True, 12345.0)


def test_read_tag_failure_and_binding() -> None:
    """读失败 → (False, None);未绑表/标识不存在 → ValueError。"""
    client = _recording_client()
    with pytest.raises(ValueError):
        client.read_tag("furnace_temp")  # 未绑表
    client.bind_tags(TagTable([Tag("furnace_temp", "D100", "float")]))
    assert client.read_tag("furnace_temp") == (False, None)  # 设备侧无值
    with pytest.raises(ValueError):
        client.read_tag("pressure")  # 标识不存在


def test_write_tag_int_result_restored() -> None:
    """回归:逆缩放还原整数,整数点位不再被底层 require_int 拒收。"""
    client = _recording_client()
    client.bind_tags(
        TagTable(
            [
                Tag("counter", "D0", "int", remark="计数"),
                Tag("half_speed", "D2", "int", scale=0.5),
                Tag("furnace_temp", "D4", "float", scale=2.0, offset=1.0),
            ]
        )
    )
    assert client.write_tag("counter", 100) is True
    assert client.write_tag("half_speed", 50.0) is True
    assert client.write_tag("furnace_temp", 9.5) is True
    assert client.writes[0] == ("D0", DataType.INT, 100)  # 修复前是 100.0 → ValueError
    assert isinstance(client.writes[0][2], int)
    assert client.writes[1] == ("D2", DataType.INT, 100)  # 50/0.5 → 还原 int
    assert client.writes[2] == ("D4", DataType.FLOAT, 4.25)  # 非整值保持 float


def test_write_tag_bool_string_and_scale_zero() -> None:
    """BOOL/STRING 直通不逆缩放;scale=0 直传 Tag 实例时 write_tag 期拒绝。"""
    client = _recording_client()
    client.bind_tags(TagTable([Tag("start_stop", "M0", "bool"), Tag("steel_grade", "D0", "string")]))
    assert client.write_tag("start_stop", True) is True
    assert client.write_tag("steel_grade", "Q235") is True
    assert client.writes[0] == ("M0", DataType.BOOL, True)
    assert client.writes[1] == ("D0", DataType.STRING, "Q235")
    with pytest.raises(ValueError):
        client.write_tag(Tag("bad", "D9", "int", scale=0), 1)
    assert len(client.writes) == 2


def test_from_json_rejects_non_object(tmp_path: Path) -> None:
    """JSON 元素不是对象 → ValueError(而非 TypeError)。"""
    path = str(tmp_path / "bad.json")
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(["not-a-dict"], fp)
    with pytest.raises(ValueError):
        TagTable.from_json(path)


def test_from_csv_rejects_short_row(tmp_path: Path) -> None:
    """CSV 短行缺列 → ValueError(而非静默变成 "None" 字符串)。"""
    path = str(tmp_path / "short.csv")
    with open(path, "w", encoding="utf-8", newline="") as fp:
        fp.write("tag_id,address,data_type\n")
        fp.write("only-id\n")
    with pytest.raises(ValueError):
        TagTable.from_csv(path)


def test_from_csv_rejects_bad_number(tmp_path: Path) -> None:
    """scale/offset 单元格非数字 → ValueError。"""
    path = str(tmp_path / "nan.csv")
    with open(path, "w", encoding="utf-8", newline="") as fp:
        fp.write("tag_id,address,data_type,scale\n")
        fp.write("bad,D0,int,abc\n")
    with pytest.raises(ValueError):
        TagTable.from_csv(path)
