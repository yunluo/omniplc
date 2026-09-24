# v0.34.0 重连退避 + 错误结构化 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **环境注意(本仓库)**:Windows cmd,无 `tail`/`head`/`grep`(用 `python -c` 单行或临时脚本);子代理会超时,**用 inline 方式执行**;门禁:`uv run python -m pytest tests -q` + `uvx ruff check src tests` + `uvx mypy src/omniplc`;**阶段提交**,`git add` 只加显式文件。

**Goal:** 落地 v0.34.0 两项 P0:连接退避门控(`reconnect_backoff` + `next_connect_in`)与失败结构化(`ErrorCategory` + `last_error_category`/`last_error_code`)。

**Architecture:** 全部改动收在 `core` 层(`errors.py` 加枚举、`base_client.py` 加门控与错误三件套助手)+ 两处驱动直写点迁移 + aio 转发 + 新测试文件。无构造签名变更,`last_error` 文本契约不变。

**Tech Stack:** Python 3.7.9 兼容(标准库 `enum`/`random`/`time`/`socket`),pytest + monkeypatch,uv 工具链。

**Spec:** `docs/superpowers/specs/2026-09-22-v0.34-reliability-observability-design.md`(r3)。**与 spec 的两处已确认偏差**:①提交拆 3 个阶段 commit(用户"阶段提交"惯例,spec 原写单 commit);②门控拒绝不计 `error_count`(无网络动作,spec §3.5 表原写"含门控命中",实现细化)。

**基线:** v0.33.0(657 测试全过)。

---

## 背景事实(实施前必读)

1. **`BaseClient.connect()`**(`src/omniplc/core/base_client.py:106-150`):锁内 `if self._connected: return True` → `_create_transport()`(在 try **外**,参数类 ValueError 照常上抛、**不进退避**)→ try `transport.connect()` → `self._transport = transport` → try `_after_connect()` → 成功置 `_connected`/清 `_last_error`/计数。
2. **`_execute()`**(`:502-539`):锁内重试循环;成功 `self._last_error = None`(`:526`);`DeviceError` 分支(`:530-534`)不断线;`(OSError, OmniPLCInternalError)` 分支(`:535-538`)标记断开。
3. **`disconnect()`**(`:152-170`):锁内置 None/False,close 失败写 `_last_error`(`:166`)。
4. **`_record_error()`**(`:263-266`):`error_count += 1` + `last_error_at` 时间戳。
5. **`_describe(exc)`**(`:633-638`):`"{TypeName}:{msg}"`,`socket.timeout` → `"通信超时:..."`。
6. **aio 转发区**(`src/omniplc/aio/__init__.py:238-254`):`retries`/`write_retries` property+setter 同款;`last_error` 只读 property(`:211-213`)。aio **无** core.errors 现有导入(需新增 `from ..core.errors import ErrorCategory`)。
7. **镜像守卫是内省制**(`tests/unit/test_aio_mirror_surface.py:24-27`):`BaseClient` 新增公开名必须出现在 `ABaseClient`,否则 `test_base_client_surface_mirrored` 挂。property 不受 `test_async_methods_are_coroutines` 约束(已核实)。
8. **唯一会被门控打破的既有测试**:`tests/unit/test_base_client.py:84` `test_connect_failure_then_lazy_reconnect`(失败后立即重读,门控默认开会拦)→ 加一行 `client.reconnect_backoff = False`。其余核实过不受影响:`test_after_connect_failure_cleans_up`(test_v030:263)第二次 connect 断言 `ok is False` + `_transport is None`,门控路径同样满足;`test_enter_failure_raises_connection_error` 只失败一次(首次失败不经过门控);驱动 `connection_error_lazy_reconnect` 系列失败的是操作而非建连,重连首次即成功不触发门控。
9. **驱动直写 `_last_error` 迁移点**:`plc/ab/ab.py:346`、`:402`;`scanner/keyence_sr.py:116/119/124/127/129`。不迁移会留脏 category。
10. **导入行现状**:`keyence_sr.py:22` `from ..core.base_client import BaseClient, _describe, validate_endpoint`;`:36` `from ..core.errors import DeviceError, OmniPLCInternalError`。`ab.py:37` `from ...core.base_client import BaseClient, validate_endpoint`;`:46` `from ...core.errors import DeviceError, OmniPLCInternalError, ProtocolFrameError`。

---

### Task 1: ErrorCategory 枚举 + 失败三件套基础设施(aio 镜像 + 测试)

**Files:**
- Modify: `src/omniplc/core/errors.py`
- Modify: `src/omniplc/core/__init__.py`
- Modify: `src/omniplc/core/base_client.py`
- Modify: `src/omniplc/plc/ab/ab.py:37,46,346,402`
- Modify: `src/omniplc/scanner/keyence_sr.py:22,36,116-129`
- Modify: `src/omniplc/aio/__init__.py`(last_error property 后,`last_error_category`/`last_error_code` 转发)
- Create: `tests/unit/test_v034_reliability.py`(先只写错误结构化部分)

- [ ] **Step 1.1:errors.py 加枚举**(放在文件末尾 `TransportTimeoutError` 之后)

```python
class ErrorCategory(enum.Enum):
    """失败分类(供上位系统告警分级,见 ``BaseClient.last_error_category``)。

    规则表在 ``base_client._categorize``(顺序敏感)。**新增异常类型时
    必须同步维护该规则表**,否则落入 UNKNOWN 兜底。
    """

    TRANSPORT = "transport"
    PROTOCOL = "protocol"
    DEVICE = "device"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"
```

同文件头部 `from __future__ import annotations` 后加 `import enum`。

- [ ] **Step 1.2:core/__init__.py 导出**

`from .errors import (...)` 加 `ErrorCategory`,`__all__` 加 `"ErrorCategory"`(紧跟 `"DeviceError"` 之后)。

- [ ] **Step 1.3:base_client.py 加模块级分类助手**(放在 `_describe` 之后)

```python
def _categorize(exc: BaseException) -> ErrorCategory:
    """把异常映射为失败分类(内部函数;规则顺序敏感,勿调换)。

    ``TransportTimeoutError`` 是 ``DeviceError`` 子类、``socket.timeout``
    是 ``OSError`` 子类,必须先判窄类型;新增异常类型须同步本表
    (见 ``ErrorCategory`` docstring)。
    """
    if isinstance(exc, (TransportTimeoutError, socket.timeout)):
        return ErrorCategory.TIMEOUT
    if isinstance(exc, ProtocolFrameError):
        return ErrorCategory.PROTOCOL
    if isinstance(exc, DeviceError):
        return ErrorCategory.DEVICE
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, socket.gaierror)):
        return ErrorCategory.TRANSPORT
    if isinstance(exc, (OSError, TransportClosedError)):
        return ErrorCategory.TRANSPORT
    return ErrorCategory.UNKNOWN


def _extract_code(exc: BaseException) -> Optional[int]:
    """提取原始错误码:DeviceError 取协议码,OSError 取 errno,其余 None。"""
    if isinstance(exc, DeviceError):
        return exc.code
    if isinstance(exc, OSError):
        return exc.errno
    return None
```

同文件 `from .errors import DeviceError, OmniPLCInternalError, TransportClosedError` 改为:

```python
from .errors import (
    DeviceError,
    ErrorCategory,
    OmniPLCInternalError,
    ProtocolFrameError,
    TransportClosedError,
    TransportTimeoutError,
)
```

- [ ] **Step 1.4:base_client.py `__init__` 加状态字段**(在 `self._last_error: Optional[str] = None` 之后)

```python
        self._last_error_category: Optional[ErrorCategory] = None
        self._last_error_code: Optional[int] = None
```

- [ ] **Step 1.5:base_client.py 加 `_set_error`/`_clear_error`**(放在 `_record_error` 之后)

```python
    def _set_error(
        self,
        message: str,
        category: ErrorCategory,
        code: Optional[int],
        record: bool = True,
    ) -> None:
        """登记失败原因三件套(内部方法,须锁内调用)。

        :param record: 是否同时计一次失败统计(门控拒绝等无网络动作的
            失败传 False,不污染 ``stats["error_count"]``)
        """
        self._last_error = message
        self._last_error_category = category
        self._last_error_code = code
        if record:
            self._record_error()

    def _clear_error(self) -> None:
        """清空失败原因三件套(内部方法,须锁内调用)。"""
        self._last_error = None
        self._last_error_category = None
        self._last_error_code = None
```

- [ ] **Step 1.6:base_client.py 加两个只读 property**(放在 `last_error` property 之后)

```python
    @property
    def last_error_category(self) -> Optional[ErrorCategory]:
        """最近一次失败的分类(成功读写后清空为 None)。

        取值见 :class:`~omniplc.core.errors.ErrorCategory`;传输类错误
        为 ``TRANSPORT``,PLC 明确报错为 ``DEVICE``(配
        :attr:`last_error_code` 取原始码),超时独立为 ``TIMEOUT``。
        """
        with self._lock:
            return self._last_error_category

    @property
    def last_error_code(self) -> Optional[int]:
        """最近一次失败的原始错误码(成功读写后清空为 None)。

        PLC 报错取协议原始码(MC 结束码/FINS 结束码/Modbus 异常码),
        传输类错误取 ``errno``,无码为 ``None``。
        """
        with self._lock:
            return self._last_error_code
```

- [ ] **Step 1.7:migrate `base_client.py` 七个写入点**

| 行 | 原代码 | 改为 |
|---|---|---|
| `:123-125` connect 建连失败 | `self._last_error = "连接 {}:{} 失败:{}".format(...)` 与其后 `self._record_error()` | `self._set_error("连接 {}:{} 失败:{}".format(self._ip_address or "-", self._port or "-", exc), _categorize(exc), _extract_code(exc))`(删除原 `_record_error()` 调用行) |
| `:137` connect 握手失败 | `self._last_error = "连接初始化失败:{}".format(_describe(exc))` 与其后 `self._record_error()` | `self._set_error("连接初始化失败:{}".format(_describe(exc)), _categorize(exc), _extract_code(exc))`(删除原 `_record_error()` 行) |
| `:147` connect 成功 | `self._last_error = None` | `self._clear_error()` |
| `:166` disconnect close 失败 | `self._last_error = f"关闭连接失败:{exc}"` 与其后 `self._record_error()` | `self._set_error(f"关闭连接失败:{exc}", _categorize(exc), _extract_code(exc))`(删除原 `_record_error()` 行) |
| `:526` _execute 成功 | `self._last_error = None` | `self._clear_error()` |
| `:531` _execute DeviceError | `self._last_error = _describe(exc)` | `self._set_error(_describe(exc), ErrorCategory.DEVICE, exc.code)`(下一行 `self._counters["device_error_count"] += 1` 保留) |
| `:536` _execute OSError/Internal | `self._last_error = _describe(exc)` | `self._set_error(_describe(exc), _categorize(exc), _extract_code(exc))` |

- [ ] **Step 1.8:migrate 驱动直写点**

`ab.py:37` 改 `from ...core.base_client import BaseClient, _categorize, _extract_code, validate_endpoint`;两处 try/except 改:

```python
        except Exception as exc:
            self._set_error(f"GetAttributesAll 解码失败:{exc}", _categorize(exc), _extract_code(exc))
            return False, None
```

```python
            except Exception as exc:
                self._set_error(f"GetAttributeList 解码失败:{exc}", _categorize(exc), _extract_code(exc))
                return False, None
```

`keyence_sr.py:22` 改 `from ..core.base_client import BaseClient, _categorize, _describe, _extract_code, validate_endpoint`;`:36` 改 `from ..core.errors import DeviceError, ErrorCategory, OmniPLCInternalError`;scan 方法内五处改:

```python
            except socket.timeout:
                # 读码窗口内无应答:链路仍然完好,不断线
                self._drain_line(transport)
                self._set_error(f"扫码读超时({read_timeout}s),未收到应答", ErrorCategory.TIMEOUT, None)
                return False, None
            except (OSError, OmniPLCInternalError) as exc:
                self._set_error(_describe(exc), _categorize(exc), _extract_code(exc))
                self._mark_disconnected()
                return False, None
            text = text.strip()
            if text == SR_RESP_ERROR:
                self._set_error("扫码枪返回 ERROR(未读到条码或距离过远)", ErrorCategory.DEVICE, None)
                return False, None
            if text == SR_RESP_OK or not text:
                self._set_error("扫码枪无读出(OK)", ErrorCategory.DEVICE, None)
                return False, None
            self._clear_error()
            return True, text
```

- [ ] **Step 1.9:aio 转发**(`last_error` property `:211-213` 之后加)

文件导入区(`from ..core.base_client import BaseClient` 附近)加一行:

```python
from ..core.errors import ErrorCategory
```

`last_error` property 后加:

```python
    @property
    def last_error_category(self) -> Optional[ErrorCategory]:
        """最近一次失败的分类(转发同步实例)。"""
        return self._sync.last_error_category

    @property
    def last_error_code(self) -> Optional[int]:
        """最近一次失败的原始错误码(转发同步实例)。"""
        return self._sync.last_error_code
```

- [ ] **Step 1.10:写失败测试(先跑确认 FAIL)**

创建 `tests/unit/test_v034_reliability.py`:

```python
"""v0.34.0 可靠性测试:失败结构化(ErrorCategory)与连接退避门控。"""
from __future__ import annotations

import socket
import time
from typing import List, Optional

import pytest

import omniplc.core.base_client as base_client_mod
from omniplc.core.base_client import BaseClient
from omniplc.core.errors import (
    DeviceError,
    ErrorCategory,
    OmniPLCInternalError,
    ProtocolFrameError,
    TransportClosedError,
    TransportTimeoutError,
)
from omniplc.transport import BaseTransport
from omniplc.types import DataType, PrimitiveValue


class _ScriptedTransport(BaseTransport):
    """脚本化传输:可指定前 N 次 connect 失败。"""

    def __init__(self, fail_connect_times: int = 0) -> None:
        super().__init__()
        self.fail_connect_times = fail_connect_times
        self.connect_calls = 0
        self.close_calls = 0

    def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_calls <= self.fail_connect_times:
            raise OSError("[WinError 10061] 连接被拒绝")

    def close(self) -> None:
        self.close_calls += 1

    def send(self, data: bytes) -> None:
        pass

    def recv(self, size: int) -> bytes:
        return b"\x00" * size


class _ScriptedClient(BaseClient):
    """脚本化驱动:_read/_write 行为由测试注入。"""

    def __init__(self, fail_connect_times: int = 0) -> None:
        super().__init__("127.0.0.1", 502)
        self._fail_connect_times = fail_connect_times
        self._fail_next_op: List[BaseException] = []
        self._read_calls = 0
        self._write_calls = 0
        self.transports: List[_ScriptedTransport] = []

    def script_failure(self, exc: BaseException) -> None:
        """注入一次读写失败。"""
        self._fail_next_op.append(exc)

    def _create_transport(self) -> BaseTransport:
        transport = _ScriptedTransport(self._fail_connect_times)
        self._fail_connect_times = 0  # 只有第一个传输失败
        self.transports.append(transport)
        return transport

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        self._read_calls += 1
        if self._fail_next_op:
            raise self._fail_next_op.pop(0)
        return 3.14

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        self._write_calls += 1
        if self._fail_next_op:
            raise self._fail_next_op.pop(0)


class TestErrorCategory:
    """失败结构化:category + code 与 last_error 同步。"""

    def test_device_error_category_and_code(self) -> None:
        client = _ScriptedClient()
        client.connect()
        client.script_failure(DeviceError("结束代码 0xC059", 0xC059))
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.DEVICE
        assert client.last_error_code == 0xC059
        assert "结束代码 0xC059" in (client.last_error or "")
        assert client.connected is True  # 设备错误不断线

    def test_oserror_category_transport(self) -> None:
        client = _ScriptedClient()
        client.connect()
        client.script_failure(OSError("网络中断"))
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.TRANSPORT
        assert client.connected is False

    def test_socket_timeout_category(self) -> None:
        client = _ScriptedClient()
        client.connect()
        client.script_failure(socket.timeout())
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.TIMEOUT
        assert "通信超时" in (client.last_error or "")

    def test_transport_timeout_error_wins_over_device(self) -> None:
        """顺序敏感:TransportTimeoutError(DeviceError 子类)必须归 TIMEOUT。"""
        client = _ScriptedClient()
        client.connect()
        client.script_failure(TransportTimeoutError("接收超时", 0))
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.TIMEOUT

    def test_protocol_frame_error_category(self) -> None:
        client = _ScriptedClient()
        client.connect()
        client.script_failure(ProtocolFrameError("校验错"))
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.PROTOCOL
        assert client.last_error_code is None

    def test_transport_closed_error_category(self) -> None:
        client = _ScriptedClient()
        client.connect()
        client.script_failure(TransportClosedError("连接未建立"))
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.TRANSPORT
        assert client.last_error_code is None

    def test_unknown_category_fallback(self) -> None:
        """非网络非协议的内部异常落 UNKNOWN 兜底。"""

        class _Weird(OmniPLCInternalError):
            pass

        client = _ScriptedClient()
        client.connect()
        client.script_failure(_Weird("怪异常"))
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.UNKNOWN

    def test_success_clears_all_three(self) -> None:
        client = _ScriptedClient()
        client.connect()
        client.script_failure(OSError("网络中断"))
        client.read("hr0", "short")
        client.read("hr0", "short")  # 重连成功
        assert client.last_error is None
        assert client.last_error_category is None
        assert client.last_error_code is None

    def test_connect_failure_category(self) -> None:
        client = _ScriptedClient(fail_connect_times=1)
        client.reconnect_backoff = False  # v0.34 Task 2 加的属性;Task 1 阶段先注释此行
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.TRANSPORT
        assert "连接 127.0.0.1:502 失败" in (client.last_error or "")

    def test_last_error_text_unchanged(self) -> None:
        """向后兼容:last_error 文本格式与 v0.33 逐字一致。"""
        client = _ScriptedClient()
        client.connect()
        client.script_failure(OSError("网络中断"))
        client.read("hr0", "short")
        assert client.last_error == "OSError:网络中断"
```

**注意**:`reconnect_backoff` 属性在 Task 2 才实现——Task 1 阶段把 `test_connect_failure_category` 里的 `client.reconnect_backoff = False` 行**注释掉**(门控尚不存在,首次失败不经过门控,测试天然通过);Task 2 落地后取消注释。

- [ ] **Step 1.11:跑新测试确认 FAIL → 实现后 PASS**

Run: `uv run python -m pytest tests/unit/test_v034_reliability.py -q`
Expected: Step 1.10 首次运行 FAIL(`ImportError: cannot import name 'ErrorCategory'`),完成 Step 1.1-1.9 后 10 例全 PASS。

- [ ] **Step 1.12:全量门禁**

Run: `uv run python -m pytest tests -q` → **657+10=667 过,0 挂**(既有测试零破坏:`test_after_connect_failure_cleans_up` 等已核实兼容)。
Run: `uvx ruff check src tests` → 零告警。Run: `uvx mypy src/omniplc` → 零问题。

- [ ] **Step 1.13:Commit(阶段提交,显式文件)**

```bash
git add src/omniplc/core/errors.py src/omniplc/core/__init__.py src/omniplc/core/base_client.py src/omniplc/plc/ab/ab.py src/omniplc/scanner/keyence_sr.py src/omniplc/aio/__init__.py tests/unit/test_v034_reliability.py
git commit -m "feat(v0.34): 失败结构化 — ErrorCategory 五值枚举 + last_error_category/code

- ErrorCategory(TRANSPORT/PROTOCOL/DEVICE/TIMEOUT/UNKNOWN)入 core.errors
- _categorize 规则表顺序敏感(TransportTimeoutError 是 DeviceError 子类先判),
  新增异常类型须同步维护(errors.py docstring 已标注)
- _extract_code:DeviceError 取协议原始码,OSError 取 errno,其余 None
- 写入统一收口 _set_error/_clear_error(与 _last_error 同锁同步);
  驱动直写点迁移:AB GetAttributesAll/GetAttributeList 解码失败 ×2、
  SR 扫码枪超时/ERROR/无读出/成功 ×5
- last_error 文本契约不变(向后兼容断言入库);aio 镜像转发,内省守卫通过"
```

---

### Task 2: 连接退避门控(constants + connect 重写 + aio + 测试 + 修既有测试)

**Files:**
- Modify: `src/omniplc/core/constants.py`(MC_1C 常量组之后加退避常量)
- Modify: `src/omniplc/core/base_client.py`(imports、`__init__`、properties、connect/disconnect、私有助手)
- Modify: `src/omniplc/aio/__init__.py`(retries 转发区之后)
- Modify: `tests/unit/test_base_client.py:84-97`(唯一受影响既有测试)
- Modify: `tests/unit/test_v034_reliability.py`(取消 Task 1 注释 + 追加退避测试类)

- [ ] **Step 2.1:constants.py 加常量**

```python
# ---- 连接退避门控(v0.34.0) ----
# 第 n 次连续建连失败后,下一次 connect() 在 uniform(0, min(base*factor^n, max))
# 秒内被拒绝(full jitter,时间戳门控零 sleep)
RECONNECT_BACKOFF_BASE: float = 0.5
RECONNECT_BACKOFF_FACTOR: float = 2.0
RECONNECT_BACKOFF_MAX: float = 30.0
```

- [ ] **Step 2.2:base_client.py imports**

`import random` 加入 import 区(`import socket` 后);constants 导入组加 `RECONNECT_BACKOFF_BASE, RECONNECT_BACKOFF_FACTOR, RECONNECT_BACKOFF_MAX`。

- [ ] **Step 2.3:`__init__` 加门控状态**(在 Step 1.4 两行之后)

```python
        self._reconnect_backoff: bool = True
        self._next_connect_at: float = 0.0
        self._connect_fail_count: int = 0
```

- [ ] **Step 2.4:`connect()` 整体重写**(替换 `:106-150` 方法体)

```python
    def connect(self) -> bool:
        """建立连接(幂等:已连接时直接返回 True)。

        失败后进入**指数退避门控**(v0.34.0):第 n 次连续失败后,
        ``uniform(0, min(0.5 × 2ⁿ, 30))`` 秒内的再次连接直接拒绝——
        时间戳比较,不发包、不 sleep。连接成功或显式
        :meth:`disconnect` 后门控与失败计数全部重置;
        :attr:`reconnect_backoff` 置 False 可整体关闭。

        :return: 是否成功
        """
        with self._lock:
            if self._connected:
                return True
            now = time.monotonic()
            if self._reconnect_backoff and now < self._next_connect_at:
                delay = self._next_connect_at - now
                self._set_error(
                    f"连接退避中:{delay:.1f} 秒后允许重连",
                    ErrorCategory.TRANSPORT,
                    None,
                    record=False,
                )
                return False
            # 传输对象创建独立于 try:参数类错误(如未配置串口参数)照常上抛
            transport = self._create_transport()
            try:
                transport.connect_timeout = self._connect_timeout
                transport.receive_timeout = self._receive_timeout
                transport.connect()
            except Exception as exc:
                # 建连失败:任何异常都清理为"未连接"(防脏 socket/传输逃逸)
                self._connected = False
                self._register_connect_failure()
                self._set_error(
                    "连接 {}:{} 失败:{}".format(
                        self._ip_address or "-", self._port or "-", exc
                    ),
                    _categorize(exc),
                    _extract_code(exc),
                )
                try:
                    transport.close()
                except Exception:
                    pass
                return False
            self._transport = transport
            try:
                self._after_connect()
            except Exception as exc:
                # 握手/会话初始化失败:清理到干净状态,下次事务惰性重连
                self._register_connect_failure()
                self._set_error(
                    "连接初始化失败:{}".format(_describe(exc)),
                    _categorize(exc),
                    _extract_code(exc),
                )
                try:
                    transport.close()
                except Exception:
                    pass
                self._transport = None
                self._connected = False
                return False
            self._connected = True
            self._clear_error()
            self._reset_backoff()
            self._counters["connect_count"] += 1
            self._timestamps["last_connect_at"] = time.monotonic()
            return True
```

- [ ] **Step 2.5:`disconnect()` 加门控重置**(在 `self._connected = False` 之后、`if transport is None` 之前加一行)

```python
            self._reset_backoff()
```

- [ ] **Step 2.6:加私有助手**(放在 `_mark_disconnected` 之后)

```python
    def _register_connect_failure(self) -> None:
        """登记一次建连失败并推进退避门控(内部方法,须锁内调用)。"""
        cap = min(
            RECONNECT_BACKOFF_BASE
            * (RECONNECT_BACKOFF_FACTOR ** self._connect_fail_count),
            RECONNECT_BACKOFF_MAX,
        )
        self._next_connect_at = time.monotonic() + random.uniform(0.0, cap)
        self._connect_fail_count += 1

    def _reset_backoff(self) -> None:
        """清空退避门控(连接成功或显式断开时,内部方法,须锁内调用)。"""
        self._next_connect_at = 0.0
        self._connect_fail_count = 0
```

- [ ] **Step 2.7:加公共 property + setter**(放在 `write_retries` setter 之后、`last_error` property 之前,与 retries 同族)

```python
    @property
    def reconnect_backoff(self) -> bool:
        """连接失败后的指数退避门控(默认开)。

        PLC 断电/网线松动时,紧密轮询的调用方不再形成高频重连风暴;
        置 False 恢复"失败后立即可重连"的旧行为。
        """
        return self._reconnect_backoff

    @reconnect_backoff.setter
    def reconnect_backoff(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError(f"reconnect_backoff 必须为布尔值,收到:{enabled!r}")
        self._reconnect_backoff = enabled

    @property
    def next_connect_in(self) -> Optional[float]:
        """距下次允许连接的剩余秒数;None = 无门控,可立即连接。"""
        with self._lock:
            if not self._reconnect_backoff:
                return None
            remaining = self._next_connect_at - time.monotonic()
            return remaining if remaining > 0 else None
```

- [ ] **Step 2.8:aio 转发**(`write_retries` setter `:254` 之后)

```python
    @property
    def reconnect_backoff(self) -> bool:
        """连接失败后的指数退避门控(转发同步实例)。"""
        return self._sync.reconnect_backoff

    @reconnect_backoff.setter
    def reconnect_backoff(self, enabled: bool) -> None:
        self._sync.reconnect_backoff = enabled

    @property
    def next_connect_in(self) -> Optional[float]:
        """距下次允许连接的剩余秒数;None = 可立即连接(转发同步实例)。"""
        return self._sync.next_connect_in
```

- [ ] **Step 2.9:修唯一受影响的既有测试**(`tests/unit/test_base_client.py:84-97`,加一行+注释)

```python
    def test_connect_failure_then_lazy_reconnect(self) -> None:
        client = _ScriptedClient(fail_connect_times=1)
        client.reconnect_backoff = False  # 本测验证惰性重连语义本身;退避行为归 v034 测试
        # 第一次:第一个传输 connect 失败
        ok, value = client.read("hr0", "short")
        ...  # 其余断言原样不动
```

- [ ] **Step 2.10:取消 Task 1 注释**——`tests/unit/test_v034_reliability.py` 的 `test_connect_failure_category` 中 `client.reconnect_backoff = False` 行恢复生效(删除注释标记)。

- [ ] **Step 2.11:追加退避测试类**(追加到 `test_v034_reliability.py` 末尾)

```python
class TestReconnectBackoff:
    """连接退避门控:full jitter 时间戳门控,零 sleep。"""

    def test_gate_blocks_immediate_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            base_client_mod.random, "uniform", lambda a, b: 0.25
        )
        client = _ScriptedClient(fail_connect_times=1)
        ok, _ = client.read("hr0", "short")
        assert ok is False  # 真实建连失败
        ok, _ = client.read("hr0", "short")
        assert ok is False  # 立即重试被门控拦下
        assert "退避" in (client.last_error or "")
        assert client.last_error_category is ErrorCategory.TRANSPORT
        assert client.last_error_code is None
        assert len(client.transports) == 1  # 门控拒绝不再建传输

    def test_gate_does_not_count_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """门控拒绝无网络动作,不计 error_count。"""
        monkeypatch.setattr(base_client_mod.random, "uniform", lambda a, b: 0.25)
        client = _ScriptedClient(fail_connect_times=1)
        client.read("hr0", "short")
        client.read("hr0", "short")  # 门控拒绝
        assert client.stats["error_count"] == 1  # 只有真实建连失败计入

    def test_gate_expires_allowing_reconnect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(base_client_mod.random, "uniform", lambda a, b: 0.05)
        client = _ScriptedClient(fail_connect_times=1)
        ok, _ = client.read("hr0", "short")
        assert ok is False
        time.sleep(0.08)  # 门控窗口 0.05s 过期(裕量 30ms)
        ok, value = client.read("hr0", "short")
        assert ok is True
        assert value == 3.14

    def test_zero_jitter_allows_immediate_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """uniform 取下界 0:门控时间戳=now,立即重试放行。"""
        monkeypatch.setattr(base_client_mod.random, "uniform", lambda a, b: 0.0)
        client = _ScriptedClient(fail_connect_times=1)
        client.read("hr0", "short")
        ok, _ = client.read("hr0", "short")
        assert ok is True

    def test_backoff_resets_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(base_client_mod.random, "uniform", lambda a, b: 0.05)
        client = _ScriptedClient(fail_connect_times=1)
        client.read("hr0", "short")
        time.sleep(0.08)
        client.read("hr0", "short")  # 重连成功
        assert client._connect_fail_count == 0
        assert client._next_connect_at == 0.0
        assert client.next_connect_in is None

    def test_backoff_resets_on_disconnect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(base_client_mod.random, "uniform", lambda a, b: 0.25)
        client = _ScriptedClient(fail_connect_times=1)
        client.read("hr0", "short")
        assert client.next_connect_in is not None
        client.disconnect()
        assert client.next_connect_in is None
        assert client._connect_fail_count == 0

    def test_next_connect_in_semantics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(base_client_mod.random, "uniform", lambda a, b: 0.25)
        client = _ScriptedClient()
        assert client.next_connect_in is None  # 从未失败
        client._fail_connect_times = 1
        client.read("hr0", "short")
        remaining = client.next_connect_in
        assert remaining is not None and 0 < remaining <= 0.25

    def test_backoff_capped_at_max(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from omniplc.core.constants import RECONNECT_BACKOFF_MAX

        captured: dict = {}

        def _fake_uniform(a: float, b: float) -> float:
            captured["cap"] = b
            return b

        monkeypatch.setattr(base_client_mod.random, "uniform", _fake_uniform)
        client = _ScriptedClient(fail_connect_times=12)
        for _ in range(12):
            assert client.connect() is False
        assert captured["cap"] == RECONNECT_BACKOFF_MAX  # 0.5×2ⁿ 在 n=6 起封顶 30s
        assert client._connect_fail_count == 12

    def test_backoff_disabled_matches_legacy(self) -> None:
        """关闭退避:失败后立即可重连(等同 v0.33 行为)。"""
        client = _ScriptedClient(fail_connect_times=1)
        client.reconnect_backoff = False
        ok, _ = client.read("hr0", "short")
        assert ok is False
        ok, value = client.read("hr0", "short")
        assert ok is True and value == 3.14

    def test_backoff_setter_validation(self) -> None:
        client = _ScriptedClient()
        with pytest.raises(ValueError):
            client.reconnect_backoff = "yes"  # type: ignore[assignment]
        client.reconnect_backoff = False
        assert client.reconnect_backoff is False
        client.reconnect_backoff = True
        assert client.reconnect_backoff is True


class TestAioBackoffAndErrorSurfaces:
    """aio 镜像:退避与错误结构化表面转发(模式同 TestAioStatsForwarding)。"""

    def test_surfaces_forwarded(self) -> None:
        import omniplc.aio as aio

        sync = _ScriptedClient(fail_connect_times=1)
        async_client = aio.AModbusTcpClient.__new__(aio.AModbusTcpClient)
        aio.ABaseClient.__init__(async_client, sync)
        # 属性转发
        assert async_client.reconnect_backoff is True
        async_client.reconnect_backoff = False
        assert sync.reconnect_backoff is False
        sync.reconnect_backoff = True
        # 触发一次真实失败后错误三件套 + 门控剩余时间转发
        sync.read("hr0", "short")
        assert async_client.last_error == sync.last_error
        assert async_client.last_error_category is ErrorCategory.TRANSPORT
        assert async_client.last_error_code is None
        assert async_client.next_connect_in == sync.next_connect_in
        assert async_client.next_connect_in is not None
```

- [ ] **Step 2.12:跑测试**

Run: `uv run python -m pytest tests/unit/test_v034_reliability.py tests/unit/test_base_client.py tests/unit/test_aio_mirror_surface.py -q`
Expected: 全 PASS(新 11 例 + 既有 base_client + 镜像守卫)。

- [ ] **Step 2.13:全量门禁**

Run: `uv run python -m pytest tests -q` → **678 过,0 挂**。`uvx ruff check src tests` 零告警;`uvx mypy src/omniplc` 零问题。

- [ ] **Step 2.14:Commit**

```bash
git add src/omniplc/core/constants.py src/omniplc/core/base_client.py src/omniplc/aio/__init__.py tests/unit/test_base_client.py tests/unit/test_v034_reliability.py
git commit -m "feat(v0.34): 连接退避门控 — reconnect_backoff + next_connect_in

- exponential + full jitter: uniform(0, min(0.5×2ⁿ, 30)), monotonic()
  时间戳门控零 sleep — 不占锁等待、不占 aio executor 线程
- 守门在 connect() 入口(公开调用与 _execute 惰性重连同路径);
  _create_transport 参数错误在 try 外,照常上抛不进退避
- 门控拒绝契约 = 置 last_error 三件套(不计 error_count)+ 返回 False
- 连接成功 / 显式 disconnect 全重置;reconnect_backoff 可写属性
  (默认开,retries 同款 bool 校验 setter)+ next_connect_in 只读
- 唯一受影响既有测试 test_connect_failure_then_lazy_reconnect 显式
  关闭退避(该测验证惰性重连语义本身;退避行为归 v034 测试)
- aio 镜像 setter/getter 转发,内省守卫通过"
```

---

### Task 3: 文档 + 版本五落点同步

**Files:**
- Modify: `README.md`(错误处理节、连接退避新节、路线图)
- Modify: `docs/architecture.md`(状态链、§2.1、§4、§5、版本履历表)
- Modify: `pyproject.toml:3`
- Modify: `src/omniplc/__init__.py:76`
- Modify: `uv.lock`(uv lock 自动)

- [ ] **Step 3.1:README 错误处理节**(`#### 错误处理约定` 段落 `:365-367` 之后插入)

```markdown
**失败分类与原始码(v0.34.0 起)**:除 `last_error` 文本外,另有两个只读
属性供上位系统分类告警——`client.last_error_category`(`ErrorCategory`
枚举:`TRANSPORT`/`PROTOCOL`/`DEVICE`/`TIMEOUT`/`UNKNOWN`)与
`client.last_error_code`(PLC 原始错误码,如 MC 结束码 `0xC059`、Modbus
异常码 `2`、FINS 结束码 `0x2108`;传输类错误为 `errno`,无码为 `None`)。
成功读写后三者一并清空::

    from omniplc import ErrorCategory

    ok, value = client.read_ushort("D100")
    if not ok:
        cat = client.last_error_category
        if cat is ErrorCategory.DEVICE:
            ...  # PLC 报错:看 last_error_code 区分地址越界/功能不支持
        elif cat is ErrorCategory.TRANSPORT:
            ...  # PLC 不可达:最高级告警
        elif cat is ErrorCategory.TIMEOUT:
            ...  # 链路完好但超时:查负载/串扰/超时配置
```

- [ ] **Step 3.2:README 连接退避新节**(插在 `#### 连接健康统计` 节之前)

```markdown
#### 连接退避(v0.34.0 起)

连接失败(建连或握手)后自动进入**指数退避门控**:第 n 次连续失败后,
下一次 `connect()` 在 `uniform(0, min(0.5 × 2ⁿ, 30))` 秒内被直接拒绝
(返回 `False`,`last_error` 提示"连接退避中")——时间戳比较,**不发包、
不 sleep**,PLC 断电/网线松动时紧密轮询的调用方不再形成高频重连风暴。
连接成功或显式 `disconnect()` 后门控与失败计数全部重置。

- 默认开启;`client.reconnect_backoff = False` 一行恢复 v0.33 行为
- `client.next_connect_in`:距下次允许连接的剩余秒数(`None` = 可立即连)
- 门控拒绝不计入 `stats["error_count"]`(无真实网络动作)
```

- [ ] **Step 3.3:README 路线图**(`- **v0.33.0(当前)**` 行之前插入新行,并把 v0.33.0 行的 `(当前)` 去掉)

```markdown
- **v0.34.0(当前)**:可靠性 P0 双项——(1)**连接退避门控**:建连/握手失败后指数退避(full jitter,`uniform(0, min(0.5×2ⁿ, 30))` 秒,`monotonic()` 时间戳门控零 sleep),`reconnect_backoff` 可写属性(默认开,`retries` 同款)+ `next_connect_in` 只读属性;防 PLC 断电高频重连风暴,门控拒绝不计 `error_count`,连接成功/显式断开全重置;(2)**失败结构化**:`ErrorCategory` 五值枚举(TRANSPORT/PROTOCOL/DEVICE/TIMEOUT/UNKNOWN,TIMEOUT 独立——链路完好可重试,与坏帧区分)+ `last_error_category`/`last_error_code` 只读属性(`DeviceError.code` 协议原始码 / `OSError.errno` 直通),`last_error` 文本契约不变;失败写入统一收口 `_set_error`/`_clear_error`(SR 扫码枪 ×5、AB 解码 ×2 直写点迁移);aio 镜像全量转发,内省守卫通过。决策与依据:`docs/superpowers/specs/2026-09-22-v0.34-reliability-observability-design.md`。
```

- [ ] **Step 3.4:architecture.md**——先 Read 定位再 Edit,共五处:

1. **状态链(文件头 :3 附近)**:按既有格式在链首追加 v0.34.0 摘要(仿 v0.33.0 条目格式:`v0.34.0(2026-09-24):连接退避门控 + 失败结构化(ErrorCategory);`)。
2. **§2.1 入口与返回风格**:可写属性清单补 `reconnect_backoff`(v0.34,行为调优族),只读运行态清单补 `next_connect_in`/`last_error_category`/`last_error_code`。
3. **§4 连接状态机与惰性重连(≈:290-309)** 小节末尾追加:

```markdown
**连接退避门控(v0.34.0)**:`connect()` 失败(建连或 `_after_connect`
握手)后,下一次 `connect()` 在 `uniform(0, min(0.5 × 2ⁿ, 30))` 秒内
(n = 连续失败次数)直接拒绝——`monotonic()` 时间戳比较,零 sleep、
不占锁等待;连接成功或显式 `disconnect()` 全重置;
`reconnect_backoff = False` 关闭;`next_connect_in` 暴露剩余秒数。
门控拒绝不计 `error_count`(无网络动作)。状态机在
"已断开 → connect()" 转移上多一个"退避中"前置判断,其余转移不变。
```

4. **§5 错误体系** 追加:

```markdown
**失败分类(v0.34.0)**:`last_error` 文本之外,`BaseClient` 另暴露
`last_error_category: Optional[ErrorCategory]` 与
`last_error_code: Optional[int]`,成功后与 `last_error` 一并清空。
分类规则(`base_client._categorize`,顺序敏感——`TransportTimeoutError`
是 `DeviceError` 子类必须先判):

| 异常 | category | code |
|---|---|---|
| `TransportTimeoutError` / `socket.timeout` | TIMEOUT | `errno` 或 None |
| `ProtocolFrameError` | PROTOCOL | None |
| `DeviceError`(其余) | DEVICE | `exc.code`(协议原始码) |
| `ConnectionRefused/Reset`、`gaierror`、其余 `OSError`、`TransportClosedError` | TRANSPORT | `errno` 或 None |
| 其他(含裸内部异常) | UNKNOWN | None |

写入统一经 `_set_error`/`_clear_error`(与 `_last_error` 同锁同步),
驱动直写点(SR 扫码枪、AB 解码)已全部迁移。**新增异常类型时必须同步规则表**。
```

5. **版本履历表(≈:946)**:加 v0.34.0 行(格式同邻行)。

- [ ] **Step 3.5:版本号两处 + uv.lock**

`pyproject.toml:3`:`version = "0.33.0"` → `version = "0.34.0"`;
`src/omniplc/__init__.py:76`:`__version__ = "0.33.0"` → `"0.34.0"`;
Run: `uv lock` → Expected: `Updated omniplc v0.33.0 -> v0.34.0`。
(`description` 本版不改——协议面无新增。)

- [ ] **Step 3.6:门禁三件套 + smoke**

Run: `uv run python -m pytest tests -q` → 678 过;`uvx ruff check src tests` 零告警;`uvx mypy src/omniplc` 零问题;
Run: `uv run python -c "import omniplc; print(omniplc.__version__)"` → `0.34.0`。

- [ ] **Step 3.7:Commit**

```bash
git add README.md docs/architecture.md pyproject.toml src/omniplc/__init__.py uv.lock
git commit -m "chore(release): v0.34.0 — 文档与五落点版本同步

- README:错误处理节补分类告警示例;新增连接退避节;路线图 v0.34.0
- architecture.md:状态链、§2.1 属性清单、§4 状态机退避分支、
  §5 失败分类规则表、版本履历表
- pyproject / __version__ / uv.lock → 0.34.0"
```

---

### Task 4: 终验(不 push)

- [ ] **Step 4.1:全量门禁三件套**(`uv run python -m pytest tests -q` / `uvx ruff check src tests` / `uvx mypy src/omniplc`)全绿。
- [ ] **Step 4.2:smoke**——`uv run python -c` 单行:导入 0.34.0、构造 `_ScriptedClient` 同款冒烟(`MelsecMcTcpClient` 实例化 + `reconnect_backoff`/`next_connect_in`/`last_error_category` 属性可读)。
- [ ] **Step 4.3:`git log --oneline -4` 确认三个 commit 干净(无多余文件混入)。**
- [ ] **Step 4.4:**汇报并**等用户确认后再推双远程**(push 需单独确认,惯例不变)。

---

## Self-Review 记录

- **Spec 覆盖**:§2 退避(算法/门控/生命周期/aio/属性)→ Task 2;§3 错误(枚举/规则表/code/写入点/驱动迁移/aio/README 示例)→ Task 1+3;§4 测试向量逐条有对应测试;§4.2 文档 → Task 3;§4.3 五落点 → Task 3;§4.4 不破坏 → 既有 657 测试零破坏是门禁。两处已声明偏差(3-commit 拆分、门控不计 error_count)见计划头部。
- **占位符扫描**:无 TBD/TODO;所有代码步骤含完整代码;"Read 定位再 Edit" 仅用于 architecture.md 五处锚点(给了完整插入文本)。
- **类型一致性**:`ErrorCategory`、`reconnect_backoff`/`next_connect_in`/`last_error_category`/`last_error_code`、`_categorize`/`_extract_code`/`_set_error`/`_clear_error`/`_register_connect_failure`/`_reset_backoff`、`RECONNECT_BACKOFF_BASE/FACTOR/MAX` 全文一致。
