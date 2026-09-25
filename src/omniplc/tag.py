"""可选点位表(Tag):标识 → 地址 + 类型 + 缩放 + 备注,支持 JSON/CSV 导入。"""
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

    :param tag_id: 点位标识(项目内唯一,一般用字母/数字),如 ``"furnace_temp"``
    :param address: 协议地址,如 ``"D100"``、``"hr0"``,语法由协议决定
    :param data_type: 数据类型名称,见 :class:`omniplc.types.DataType`
    :param scale: 线性缩放系数,读取后 ``值 = 原始值 * scale + offset``
    :param offset: 线性偏移量
    :param remark: 备注说明(一般为中文,供人员查看记录,可选),如 ``"炉温"``
    """

    tag_id: str
    address: str
    data_type: str
    scale: float = 1.0
    offset: float = 0.0
    remark: str = ""


class TagTable(Mapping[str, Tag]):
    """点位表:按标识管理 :class:`Tag` 的只读映射。

    用法::

        table = TagTable.from_json("tags.json")
        client.bind_tags(table)
        ok, value = client.read_tag("furnace_temp")

    也可不绑定,直接把 :class:`Tag` 传给 ``read_tag``/``write_tag``。

    表内容在构造时一次性确定,之后**只读**——实际场景点位表来自
    JSON/CSV 导入,运行期不需要增删;只读也保证了并发轮询遍历
    (``for tag_id in table``)不会被写入打断。
    """

    def __init__(self, tags: Optional[Iterable[Tag]] = None) -> None:
        """创建点位表(构造后只读)。

        :param tags: 初始点位集合,标识重复或字段非法抛出 ValueError
        """
        self._tags: Dict[str, Tag] = {}
        if tags is not None:
            for tag in tags:
                self._validate_and_insert(tag)

    def _validate_and_insert(self, tag: Tag) -> None:
        """校验一个点位并写入内部字典(内部方法,仅构造期调用)。"""
        if not tag.tag_id or not tag.tag_id.strip():
            raise ValueError("点位标识不能为空")
        if not tag.address or not tag.address.strip():
            raise ValueError(f"点位 {tag.tag_id!r} 的地址不能为空")
        if tag.scale == 0:
            raise ValueError(f"点位 {tag.tag_id!r} 的 scale 不能为 0(写入无法逆缩放)")
        if tag.tag_id in self._tags:
            raise ValueError(f"点位标识重复:{tag.tag_id!r}")
        self._tags[tag.tag_id] = tag

    @classmethod
    def from_json(cls, path: PathLike, encoding: str = "utf-8-sig") -> "TagTable":
        """从 JSON 文件导入点位表。

        文件格式为对象数组::

            [
                {"tag_id": "furnace_temp", "address": "D100", "data_type": "float",
                 "scale": 0.1, "offset": 0, "remark": "炉温"},
                {"tag_id": "start_stop", "address": "M10", "data_type": "bool",
                 "remark": "启停"}
            ]

        :param path: JSON 文件路径
        :param encoding: 文件编码,默认 utf-8-sig(兼容 Excel 导出)
        :raises ValueError: 格式错误
        """
        with open(path, "r", encoding=encoding) as fp:
            data = json.load(fp)
        if not isinstance(data, list):
            raise ValueError(f"JSON 点位表必须是数组,收到:{type(data).__name__}")
        return cls(_tag_from_record(item) for item in data)

    @classmethod
    def from_csv(cls, path: PathLike, encoding: str = "utf-8-sig") -> "TagTable":
        """从 CSV 文件导入点位表。

        表头必须为 ``tag_id,address,data_type,scale,offset,remark``,
        其中 ``scale``/``offset``/``remark`` 可省略(默认 1.0/0.0/空)。

        :param path: CSV 文件路径
        :param encoding: 文件编码,默认 utf-8-sig
        """
        with open(path, "r", encoding=encoding, newline="") as fp:
            reader = csv.DictReader(fp)
            names = reader.fieldnames or []
            for required in ("tag_id", "address", "data_type"):
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
        return f"TagTable({sorted(self._tags)!r})"


def _tag_from_record(record: Mapping[str, object]) -> Tag:
    """把一行 JSON/CSV 记录转换为 Tag(内部函数)。

    :raises ValueError: 记录不是对象、缺必需字段或必填单元格为空
    """
    if not isinstance(record, dict):
        raise ValueError(f"点位记录必须是对象,收到:{type(record).__name__}")
    try:
        tag_id = _required_cell(record["tag_id"], "tag_id")
        address = _required_cell(record["address"], "address")
        data_type = _required_cell(record["data_type"], "data_type").lower()
    except KeyError as exc:
        raise ValueError(f"点位记录缺少必需字段:{exc},记录:{record}") from exc
    scale = _to_float(record.get("scale"), 1.0)
    offset = _to_float(record.get("offset"), 0.0)
    remark = "" if record.get("remark") is None else str(record.get("remark")).strip()
    return Tag(
        tag_id=tag_id,
        address=address,
        data_type=data_type,
        scale=scale,
        offset=offset,
        remark=remark,
    )


def _required_cell(value: object, field: str) -> str:
    """取必填文本单元格,``None``/空白视为空(CSV 短行的缺列即 ``None``)。"""
    text = "" if value is None else str(value).strip()
    if not text:
        raise ValueError(f"点位记录字段 {field!r} 不能为空")
    return text


def _to_float(value: object, default: float) -> float:
    """把 JSON/CSV 单元格安全转换为 float,空值用默认(内部函数)。

    :raises ValueError: 非空值不是合法数字
    """
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        raise ValueError(f"点位数值字段格式错误:{value!r}") from None
