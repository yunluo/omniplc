# 黄金报文样本(Golden Frames)

协议编解码测试的核心数据源:**不依赖真机/模拟器**,任何人 clone 后即可跑全量 codec 测试。

## 文件命名

```
<driver>_<action>_<编号>.json
例如:modbus_rtu_read_holding_001.json
      mc_3e_batch_read_001.json
      fins_udp_area_read_001.json
```

## JSON 格式约定

```json
{
    "name": "读保持寄存器 2 个(典型请求)",
    "request_hex": "010300000002c40b",
    "response_hex": "0103040014000a3a30",
    "expect": {
        "values": [20, 10]
    }
}
```

- `request_hex` / `response_hex`:报文十六进制(无空格,大小写不限),覆盖完整帧
  (Modbus 含 MBAP 或站号+CRC;MC/FINS 含各自帧头)
- `expect`:解码后的期望结构,由各驱动 codec 测试代码解释
  (如 `values` 逐点值、`error_code` 异常码等)
- 可选 `setup`:环境字段(station 站号、frame 帧型、monitoring_timer、
  transaction_id、transport 等),如 `{"frame": "3E", "station": 255}`

## 用法

codec 单元测试(如 `tests/unit/test_golden_modbus.py`、`tests/unit/test_mc_codec.py`、
`tests/unit/test_fins_codec.py`)按驱动分组加载,断言:

1. `encode(...) == bytes.fromhex(request_hex)`(编码方向)
2. `decode(bytes.fromhex(response_hex)) == expect`(解码方向)

样本由各驱动生成器产出(独立实现计算校验,避免"实现生成测试"的同源偏差):
`generate_modbus_samples.py`、`generate_mc_samples.py`、`generate_fins_samples.py`
(`uv run python tests/golden/generate_modbus_samples.py`);真机/模拟器抓包可直接手工添加。

## 样本来源建议

- 协议官方手册中的完整示例帧(标注手册章节)
- 真机/仿真器抓包(Wireshark 过滤 `modbusbus` / `tcp.port==2000` / `tcp.port==9600`)
- 注意包含**异常路径**样本:Modbus 异常码、MC 结束码非 0、FINS 结束码非 0
