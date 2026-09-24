---
name: Bug 报告
about: 报告协议行为不符、异常崩溃、性能问题等
title: "[Bug] "
labels: bug
assignees: ""
---

## 环境

- **omniplc 版本**:`pip show omniplc | grep Version` 或 `python -c "import omniplc; print(omniplc.__version__)"`
- **Python 版本**:`python --version`
- **操作系统**:
- **PLC 型号 / 固件版本**(协议类必填):
- **走线**(TCP/UDP/串口 + 协议):
- **相关 extras**(comtypes / pyads / python-snap7 / asyncua 任一):______

## 复现步骤

1.
2.
3.

最小代码示例:

```python
# 触发异常的最小客户端调用
```

## 期望行为

<!-- 你期望发生什么 -->

## 实际行为

<!-- 实际发生了什么,含 traceback -->

```
<粘贴完整错误信息>
```

## 诊断信息

- `client.last_error` 输出:
- `client.last_error_category`(`TRANSPORT`/`PROTOCOL`/`DEVICE`/`TIMEOUT`/`UNKNOWN`):
- `client.last_error_code`(PLC 原始错误码 / errno,若有):
- `client.stats` 健康统计快照(若可复现):

## 已尝试

<!-- 已排查的方向 / 临时绕过方法 -->

## 备注

<!-- 真机是否复现 / 是否影响生产 / 相关 issue 链接 -->