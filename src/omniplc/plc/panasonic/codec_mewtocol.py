"""松下 MEWTOCOL-COM 帧编解码(纯函数,ASCII 文本帧)。

帧布局(对照 HSL ``PanasonicMewtocol``、MewtocolNet(OpenLogics)与
Panasonic《MEWTOCOL Communication - User's Manual》口径,BCC 测试向量
``%01#RCSX0000`` → ``1D`` 取自 MewtocolNet 单元测试):

- 请求 = ``%`` + 站号(2 位,``EE`` 或十进制 01~99)+ ``#`` + 命令文本
  + BCC(2 位大写十六进制)+ CR(0x0D);**无 ETX**
- 正常响应 = ``%`` + 站号(2)+ ``$`` + 命令名回显(2:RC/RD/WC/WD)
  + 数据(十六进制文本)+ BCC(2)+ CR —— 数据固定从第 7 字节(下标 6)开始
- 错误响应 = ``%`` + 站号(2)+ ``!`` + 错误码(2 字符)+ BCC(2)+ CR
- BCC = 从 ``%`` 起至 BCC 前所有字符的异或
- 应答站号按手册应为请求站号回显;但现场存在**直连口径**的应答方
  (工具口直连单元、部分模拟器)不论请求站号一律自报 ``EE`` —— HSL
  ``PanasonicMewtocol`` 默认站号即 0xEE 且不校验应答站号,故校验时放行 EE

命令:RCS/WCS 单接点读/写(字号 3 位十进制 + 位号 1 位十六进制)、
RD/WD 数据区读/写(起止编号各 5 位十进制,每字 4 位十六进制、高字节在前,
多字数据低字在前)。
"""
from __future__ import annotations

from typing import List

from ...core.constants import MEWTOCOL_STATION_DIRECT
from ...core.errors import DeviceError, ProtocolFrameError

# 响应头偏移:%(1) + 站号(2) + $(1) = 4;命令名回显 2 字节至下标 6
_RESPONSE_DATA_OFFSET = 6
_RESPONSE_MIN_SIZE = 9

# 错误码 → 含义(来源:MEWTOCOL 手册错误码表,HSL English.cs 同口径)
_ERROR_MESSAGES = {
    "20": "未定义错误(命令不能执行)",
    "21": "NACK 错误(远程单元未正确识别或数据错误)",
    "22": "WACK 错误(远程单元接收缓冲已满)",
    "23": "多口错误(远程单元号 01~16 与本机重复)",
    "24": "传输格式错误(数据不符传输格式/帧溢出/数据错误)",
    "25": "硬件错误(传输系统硬件停止)",
    "26": "单元号错误(远程单元号超出 01~63)",
    "27": "不支持错误(接收数据帧溢出/帧长不一致)",
    "28": "无应答错误(远程单元不存在,超时)",
    "29": "缓冲关闭错误(收发缓冲处于关闭状态)",
    "30": "超时错误(持续处于传输禁止状态)",
    "40": "BCC 错误(指令数据传输错误)",
    "41": "格式错误(指令信息不符传输格式)",
    "42": "不支持错误(发送了不支持的指令/目标站不支持)",
    "43": "处理步骤错误(挂起时又发送附加指令)",
    "50": "链接设定错误(设定了不存在的链接号)",
    "51": "同时操作错误(本机发送缓冲已满)",
    "52": "传输抑制错误(不能向其他单元传输)",
    "53": "忙错误(正在处理其他指令)",
    "60": "参数错误(指令中含不可用代码/未指定区域)",
    "61": "数据错误(接点号/区号/数据码制越界或区域指定错误)",
    "62": "寄存器错误(未登录状态下过量登录数据)",
    "63": "PLC 模式错误(运行模式不能处理该指令)",
    "65": "保护错误(存储保护状态下写程序区/系统寄存器)",
    "66": "地址错误(地址数据码制/溢出/范围错误)",
    "67": "数据缺失错误(要读的数据不存在)",
}


def station_text(station: int) -> str:
    """站号 → 2 字符文本:直连 ``EE``,否则两位十进制(01~99)。

    :raises ValueError: 站号非法
    """
    if station == MEWTOCOL_STATION_DIRECT:
        return "EE"
    if not 1 <= station <= 99:
        raise ValueError("MEWTOCOL 站号必须在 1~99 或 0xEE(直连):{}".format(station))
    return "{:02d}".format(station)


def bcc(text: str) -> str:
    """BCC 校验和:文本全部字符异或,2 位大写十六进制。"""
    value = 0
    for char in text:
        value ^= ord(char)
    return "{:02X}".format(value)


def build_read_contact(station: str, area: str, word: int, bit: int) -> bytes:
    """构造 RCS 读单接点请求。"""
    return _assemble(station, "RCS{}{:03d}{:X}".format(area, word, bit))


def build_write_contact(station: str, area: str, word: int, bit: int, value: bool) -> bytes:
    """构造 WCS 写单接点请求。"""
    return _assemble(station, "WCS{}{:03d}{:X}{}".format(area, word, bit, 1 if value else 0))


def build_read_words(station: str, area: str, start: int, word_count: int) -> bytes:
    """构造 RD 数据区读请求(起止编号各 5 位十进制)。"""
    end = start + word_count - 1
    return _assemble(station, "RD{}{:05d}{:05d}".format(area, start, end))


def build_write_words(station: str, area: str, start: int, words: List[int]) -> bytes:
    """构造 WD 数据区写请求,逐字 4 位十六进制、高字节在前。"""
    end = start + len(words) - 1
    data = "".join("{:04X}".format(word) for word in words)
    return _assemble(station, "WD{}{:05d}{:05d}{}".format(area, start, end, data))


def parse_response(response: bytes, station: str, command: str) -> str:
    """解析响应帧,返回数据文本(无数据为空串)。

    :param response: 完整响应帧(TCP 拼接帧或 UDP 整包)
    :param station: 请求使用的站号文本(校验回显;应答自报直连站号 ``EE`` 时放行)
    :param command: 期望的命令名回显(2 字符:RC/RD/WC/WD)
    :raises omniplc.core.errors.DeviceError: PLC 错误响应(! 帧,链路正常)
    :raises omniplc.core.errors.ProtocolFrameError: 帧结构/站号/BCC/回显不符
    """
    if len(response) < _RESPONSE_MIN_SIZE:
        raise ProtocolFrameError(
            "MEWTOCOL 响应过短(至少 {} 字节):{}".format(_RESPONSE_MIN_SIZE, len(response))
        )
    text = response.decode("ascii", errors="replace")
    if text[-1] != "\r":
        raise ProtocolFrameError("MEWTOCOL 响应未以 CR 结束:{!r}".format(text[-8:]))
    if text[0] != "%" or text[3] not in ("$", "!"):
        raise ProtocolFrameError(
            "MEWTOCOL 响应帧头非法:{!r}(应为 %HH$ 或 %HH!)".format(text[:4])
        )
    if text[1:3] != station and text[1:3] != "EE":
        raise ProtocolFrameError(
            "MEWTOCOL 站号不匹配:期望 {},收到 {}".format(station, text[1:3])
        )
    body = text[:-3]
    expected_bcc = text[-3:-1]
    if bcc(body) != expected_bcc:
        raise ProtocolFrameError(
            "MEWTOCOL BCC 校验失败:期望 {},收到 {}".format(bcc(body), expected_bcc)
        )
    if text[3] == "!":
        code = text[4:6]
        message = _ERROR_MESSAGES.get(code, "未知错误")
        raise DeviceError(
            "MEWTOCOL 错误码 {}:{}".format(code, message), int(code)
        )
    echo = text[4:6]
    if echo != command:
        raise ProtocolFrameError(
            "MEWTOCOL 命令回显不匹配:期望 {},收到 {}".format(command, echo)
        )
    return text[_RESPONSE_DATA_OFFSET:-3]


def parse_expected_size(data_chars: int) -> int:
    """按响应数据字符数推算 TCP 应精确接收的响应总长。"""
    return _RESPONSE_DATA_OFFSET + data_chars + 3


def _assemble(station: str, command_text: str) -> bytes:
    """组装完整请求帧:%HH#文本 + BCC + CR(内部函数)。"""
    body = "%{}#{}".format(station, command_text)
    return (body + bcc(body) + "\r").encode("ascii")
