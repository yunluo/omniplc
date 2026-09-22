"""可选点位表(Tag):名称 → 地址 + 类型 + 缩放,支持 JSON/CSV 导入。"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, Mapping, Optional, Union

PathLike = Union[str, os.PathLike]


@dataclass
class Tag:
    """点位定义。

    :param name: 点位名称(项目内唯一),如 ``"炉温"``
    :param address: 协议地址,如 ``"D100"``、``"hr0"``,语法由协议决定
    :param data_type: 数据类型名称,见 :class:`omniplc.types.DataType`
    :param scale: 线性缩放系数,读取后 ``值 = 原始值 * scale + offset``
    :param offset: 线性偏移量
    """

    name: str
    address: str
    data_type: str
    scale: float = 1.0
    offset: float = 0.0


class TagTable(Mapping[str, Tag]):
    """点位表:按名称管理 :class:`Tag` 的只读映射。

    用法::

        table = TagTable.from_json("tags.json")
        client.bind_tags(table)
        ok, value = client.read_tag("炉温")

    也可不绑定,直接把 :class:`Tag` 传给 ``read_tag``/``write_tag``。
    """

    def __init__(self, tags: Optional[Iterable[Tag]] = None) -> None:
        """创建点位表。

        :param tags: 初始点位集合,重复名称抛出 ValueError
        """
        self._tags: Dict[str, Tag] = {}
        if tags is not None:
            for tag in tags:
                self.add(tag)

    def add(self, tag: Tag) -> None:
        """添加一个点位。

        :param tag: 点位定义
        :raises ValueError: 名称重复或字段为空
        """
        if not tag.name or not tag.name.strip():
            raise ValueError("点位名称不能为空")
        if not tag.address or not tag.address.strip():
            raise ValueError(f"点位 {tag.name!r} 的地址不能为空")
        if tag.name in self._tags:
            raise ValueError(f"点位名称重复:{tag.name!r}")
        self._tags[tag.name] = tag

    @classmethod
    def from_json(cls, path: PathLike, encoding: str = "utf-8-sig") -> "TagTable":
        """从 JSON 文件导入点位表。

        文件格式为对象数组::

            [
                {"name": "炉温", "address": "D100", "data_type": "float",
                 "scale": 0.1, "offset": 0},
                {"name": "启停", "address": "M10", "data_type": "bool"}
            ]

        :param path: JSON 文件路径
        :param encoding: 文件编码,默认 utf-8-sig(兼容 Excel 导出)
        :raises ValueError: 格式错误
        """
        with open(path, "r", encoding=encoding) as fp:
            data = json.load(fp)
        if not isinstance(data, list):
            raise ValueError("JSON 点位表必须是数组,收到:{}".format(type(data).__name__))
        return cls(_tag_from_record(item) for item in data)

    @classmethod
    def from_csv(cls, path: PathLike, encoding: str = "utf-8-sig") -> "TagTable":
        """从 CSV 文件导入点位表。

        表头必须为 ``name,address,data_type,scale,offset``,
        其中 ``scale``/``offset`` 可省略(默认 1.0/0.0)。

        :param path: CSV 文件路径
        :param encoding: 文件编码,默认 utf-8-sig
        """
        with open(path, "r", encoding=encoding, newline="") as fp:
            reader = csv.DictReader(fp)
            names = reader.fieldnames or []
            for required in ("name", "address", "data_type"):
                if required not in names:
                    raise ValueError(f"CSV 缺少必需列:{required!r},表头:{names}")
            return cls(_tag_from_record(row) for row in reader)

    def __getitem__(self, key: str) -> Tag:
        return self._tags[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._tags)

    def __len__(self) -> int:
        return len(self._tags)

    def __contains__(self, key: object) -> bool:
        return key in self._tags

    def __repr__(self) -> str:
        return "TagTable({!r})".format(sorted(self._tags))


def _tag_from_record(record: Mapping[str, object]) -> Tag:
    """把一行 JSON/CSV 记录转换为 Tag(内部函数)。"""
    try:
        name = str(record["name"]).strip()
        address = str(record["address"]).strip()
        data_type = str(record["data_type"]).strip().lower()
    except KeyError as exc:
        raise ValueError(f"点位记录缺少必需字段:{exc},记录:{record}") from exc
    scale = _to_float(record.get("scale"), 1.0)
    offset = _to_float(record.get("offset"), 0.0)
    return Tag(name=name, address=address, data_type=data_type, scale=scale, offset=offset)


def _to_float(value: object, default: float) -> float:
    """把 JSON/CSV 单元格安全转换为 float,空值用默认(内部函数)。"""
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value))
