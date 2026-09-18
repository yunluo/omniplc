"""TagTable JSON/CSV 导入与映射协议测试。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

from omniplc.tag import Tag, TagTable


@pytest.fixture
def tags_json(tmp_path: Path) -> Iterator[str]:
    """写入一个示例 JSON 点位表并返回路径。"""
    path = str(tmp_path / "tags.json")
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(
            [
                {"name": "炉温", "address": "D100", "data_type": "float", "scale": 0.1},
                {"name": "启停", "address": "M10", "data_type": "bool"},
            ],
            fp,
            ensure_ascii=False,
        )
    yield path


def test_from_json(tags_json: str) -> None:
    table = TagTable.from_json(tags_json)
    assert len(table) == 2
    assert table["炉温"].address == "D100"
    assert table["炉温"].scale == 0.1
    assert table["启停"].offset == 0.0


def test_from_csv(tmp_path: Path) -> None:
    path = str(tmp_path / "tags.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as fp:
        fp.write("name,address,data_type,scale,offset\n")
        fp.write("压力,hr0,float,0.01,0\n")
        fp.write("计数,D200,int\n")
    table = TagTable.from_csv(path)
    assert len(table) == 2
    assert table["压力"].scale == 0.01
    assert table["计数"].scale == 1.0  # 省略时默认


def test_csv_missing_column(tmp_path: Path) -> None:
    path = str(tmp_path / "bad.csv")
    with open(path, "w", encoding="utf-8", newline="") as fp:
        fp.write("name,address\n")
    with pytest.raises(ValueError):
        TagTable.from_csv(path)


def test_duplicate_name_rejected() -> None:
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
