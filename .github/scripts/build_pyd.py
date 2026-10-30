# -*- coding: utf-8 -*-
"""构建 pyd 无源二进制轮(防源码泄露交付形态,只挂 GitHub Release 附件,不发 PyPI)。

流程:``src/omniplc`` 拷到编译现场 → 剥 PEP563 导入 → **stubgen 出全量 pyi**
→ Cython 全模块出 .c → **删除全部源 .py** → ``bdist_wheel`` 出无源轮
(只剩 .pyd + .pyi + py.typed + dist-info)→ pip 装入本解释器 → ``-I`` 干净
子进程 import 冒烟(import omniplc / aio / native)。

pyi 说明(2026-10-19 方案 B 拍板):pyd 轮无源码,消费方 IDE 零补全/零类型;
构建期用 ``uvx --python 3.12 --from mypy stubgen``(--include-docstrings
--no-import,纯 AST 解析)从当前 src 现生成全量 pyi 打入轮子——与 pyd
同源必然一致、零漂移、零手动维护;py.typed 随之保留(有 pyi 即可注类型
标记)。stubgen 走 uvx 隔离环境,**强制 ``--python 3.12``**(最新 mypy,
纯 Python 轮零编译):3.7 腿若让 uvx 落到系统 3.7,myPy 会解析成 3.7
兼容最后一版 1.4.1 + typed-ast(C 扩展源码编译,其 stubgen.exe 在 3.7
上执行崩 SyntaxError——2026-10-19 CI 实踩;typed-ast 无 cp37-win32 轮
且中文 tokenizer 有前科),显式钉 3.12 由 uv 下载托管解释器绕开;pyi 为
纯文本与解释器版本无关,两腿产物一致。

用法(目标解释器里先装 cython/setuptools/wheel):

    <python> .github/scripts/build_pyd.py [--compiler mingw32]

CI 见 ``.github/workflows/cython.yml``(两腿统一 Cython 3.0.11——它在 3.7 上可按
cp37-win32 轮运行;2026-09 的 0.29.37 配方因**不支持 typing.NamedTuple 编译**
退役,native/siemens.py 的 ErrorType 崩溃 0.29/3.0 都有,由 annotation_typing=False
绕开);本地复现同款命令 + mingw 路(llvm-mingw i686-msvcrt 前置 PATH,
``libs\\libpython37.a`` 在场——2026-09 已铺好)。

2026-09-21 踩平后取消、2026-10-15 复活为 CI 任务的坑(勿再翻车):
- 剥 ``from __future__ import annotations``(0.29 年代配方:该导入会剥错相对
  导入层,报 ``No module named 'omniplc.xxx'``;换 3.0.11 后必要性未再验证,
  统一保留——剥离无副作用,少一个变量);
- 包 init 的 Extension 名**必须保留末段 ``__init__``**:CPython 对
  ``__init__.<suffix>.pyd`` 期望的符号是 ``PyInit_<包名>``(探针实测:文件探针
  报 PyInit_pkg;Cython 0.29 对 'pkg.__init__' 恰生成 PyInit_pkg;Cython 3.0
  两个符号都生成);把 ``__init__`` 从名字里剥掉则 distutils 把包 init 平铺成
  同级 .pyd、无 ``__path__``、相对导入全乱;
- 冒烟必须用干净解释器(``-I`` + 中立 cwd):项目 venv 里 setuptools editable
  安装的 ``_editable_impl_*.pth`` MetaPathFinder 会抢在 site-packages 之前
  拦截 ``omniplc``,冒烟假绿;
- **runner(英文 Windows)stdout 是 cp1252,打印中文会 UnicodeEncodeError**
  (本地 cp936 掩盖;2026-10-16 CI 首跑两腿同点失败根因,已在本地
  ``PYTHONIOENCODING=cp1252`` 条件复现)——脚本开头已把自身 stdout/stderr
  ``reconfigure`` 成 UTF-8 并给子进程兜底 ``PYTHONIOENCODING``(勿删)。
- **失败诊断走 ``::error::`` 工作流命令**(2026-10-28 增):job 日志匿名
  拉不到(需 admin),而 ``::error::`` 行会被平台转成 check-run 注解,
  匿名 API 即可读到失败阶段名与异常摘要——配合 ``_PHASE`` 阶段追踪,
  远程定位「挂在哪一步」零轮询成本。
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 见文件头第 4 条坑:中文输出在 runner 上必须显式切 UTF-8,勿删
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
os.environ["PYTHONIOENCODING"] = "utf-8"  # 子进程(pip/冒烟)也不受 cp1252 影响

HERE = Path(__file__).resolve()
ROOT = HERE.parents[2]  # .github/scripts/build_pyd.py → 仓库根
SRC = ROOT / "src" / "omniplc"
BUILD_ROOT = ROOT / "build" / "pyd"
STAGING = BUILD_ROOT / "staging" / "omniplc"
OUT = ROOT / "dist-pyd"
FUTURE_LINE = "from __future__ import annotations"
# 换行注释占位而非直接删行:保源码行号,traceback 行号仍可对(src 与 .c 一致)
FUTURE_REPLACEMENT = "# [pyd-build] 剥离 PEP563 导入(Cython 兼容,保行号)\n"

# 当前阶段名(main 内逐步推进):失败时随 ::error:: 注解输出(见文件头坑位第 5 条)
_PHASE = "启动"


def parse_version():
    """版本落点以 pyproject.toml 为准(pyproject/__init__/architecture.md 三处同步)。"""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.M)
    if not match:
        raise SystemExit("未能从 pyproject.toml 解析 version")
    return match.group(1)


def prepare_staging():
    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SRC, STAGING)
    # py.typed 保留(2026-10-19 起):pyd 轮提供全量 pyi,类型标记不再误导
    # (此前的注释口径「无源可注,留之反让检查器困惑」随方案 B 撤销)
    stripped = 0
    touched = 0
    for py in sorted(STAGING.rglob("*.py")):
        lines = py.read_text(encoding="utf-8").splitlines(keepends=True)
        changed = False
        for index, line in enumerate(lines):
            if line.rstrip("\r\n") == FUTURE_LINE:
                lines[index] = FUTURE_REPLACEMENT
                stripped += 1
                changed = True
                break
        if changed:
            py.write_text("".join(lines), encoding="utf-8")
            touched += 1
    print("staging:", STAGING)
    print("剥 PEP563:{} 处(涉 {} 文件)".format(stripped, touched))


def generate_stubs():
    """stubgen 从当前 src 现生成全量 pyi 拷入 staging(方案 B,2026-10-19)。

    走 ``uvx stubgen``(隔离环境装最新 mypy),不污染构建解释器;``--no-import``
    纯 AST 解析不 import omniplc(3.7 腿也无需运行时);``--include-docstrings``
    保留中文文档。pyi 纯文本与解释器无关,两腿产物一致。产出校验:pyi 文件数
    与源 .py 数相等。
    """
    out = BUILD_ROOT / "stubs-out"
    out.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        [
            "uvx",
            "--python",
            "3.12",  # 强制托管 3.12:3.7 腿若用系统 3.7 会让 mypy 解析成
            # 1.4.1 + typed-ast(源码编译,stubgen.exe 在 3.7 上执行崩
            # SyntaxError,2026-10-19 CI 实踩),最新 mypy 纯 Python 轮无此问题
            "--from",
            "mypy",
            "stubgen",
            str(SRC),
            "--include-docstrings",
            "--no-import",
            "-o",
            str(out),
        ]
    )
    # stubgen 输出布局:<out>/omniplc/<模块>.pyi → 拷入 STAGING(包根)
    stubs_root = out / "omniplc"
    if not stubs_root.is_dir():
        raise SystemExit("stubgen 输出未按预期布局:{}".format(stubs_root))
    copied = 0
    for pyi in sorted(stubs_root.rglob("*.pyi")):
        rel = pyi.relative_to(stubs_root)
        dest = STAGING / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pyi, dest)
        copied += 1
    src_count = len(list(SRC.rglob("*.py")))
    if copied != src_count:
        raise SystemExit(
            "pyi 数 {} != 源模块数 {}——stubgen 输出不完整".format(copied, src_count)
        )
    total = sum(
        len(p.read_text(encoding="utf-8").splitlines())
        for p in stubs_root.rglob("*.pyi")
    )
    print("stubgen:{} 个 pyi / {} 行(含 docstring)已入 staging".format(copied, total))


def module_name(rel_path):
    # 末段 __init__ 保留在 Extension 名里(见文件头坑位说明);Cython 生成的
    # PyInit_ 符号与 CPython 对 __init__.pyd 的期望一致(探针核实)
    parts = list(rel_path.with_suffix("").parts)
    return "omniplc." + ".".join(parts)


def cythonize_staging():
    from Cython.Build import cythonize
    from setuptools import Extension

    extensions = [
        Extension(module_name(py.relative_to(STAGING)), [str(py)])
        for py in sorted(STAGING.rglob("*.py"))
    ]
    print("待编译模块数:", len(extensions))
    # annotation_typing=False(两腿统一):
    # ① 保证编译轮语义与纯 Python 一致——注解不参与类型推断、不产生隐式转换;
    # ② 绕开 ErrorType 崩溃:注解解析失败(如 native/siemens.py:369 的引号注解
    #    "Sequence[Tuple[int, int, int, int]]" 解析成 ErrorType)时,arg-parsing
    #    代码生成抛 AttributeError('ErrorType' has no 'from_py_call_code')——
    #    0.29.37 与 3.0.11 相同表现(2026-10-15 本地探针实测定性)。
    return cythonize(
        extensions,
        language_level=3,
        compiler_directives={"annotation_typing": False},
        quiet=False,
    )


def drop_sources():
    sources = sorted(STAGING.rglob("*.py"))
    for py in sources:
        py.unlink()
    leftovers = list(STAGING.rglob("*.py"))
    if leftovers:
        raise SystemExit("产物树仍残留 .py:{}".format(leftovers[:5]))
    print(
        "已删源 .py:{} 个(产物树仅剩 .c/.pyd + .pyi + py.typed,无源码可泄)".format(
            len(sources)
        )
    )


def build_wheel(extensions, version, compiler):
    from setuptools import setup
    from setuptools.command.build_ext import build_ext

    class PydBuildExt(build_ext):
        """修正包 init 的导出符号(distutils 默认口径与 Cython 不一致,必修)。

        Cython 对名为 ``<包>.__init__`` 的模块生成的 init 符号是
        ``PyInit_<父包末段>``(末段 ``__init__`` 去尾;.c 实测:aio/__init__.c
        生成 PyInit_aio,与 CPython 对 __init__.<suffix>.pyd 的期望一致);
        而 distutils 默认按扩展名末段拼 ``PyInit___init__`` 写进 .def/EXPORT →
        引用不存在的符号,链接必败(2026-10-15 mingw 实测:
        ``ld.lld: error: <root>: undefined symbol: _PyInit___init__``)。
        """

        def get_export_symbols(self, ext):
            parts = ext.name.split(".")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            return ["PyInit_" + parts[-1]]

    OUT.mkdir(parents=True, exist_ok=True)
    # 切到中立 cwd:避免 setuptools 读到仓库根 pyproject/setup.cfg(元数据只用
    # 下列显式项,deterministic);产物路径全为绝对路径,与 cwd 无关
    setup_cwd = BUILD_ROOT / "setup-cwd"
    setup_cwd.mkdir(parents=True, exist_ok=True)
    os.chdir(str(setup_cwd))
    build_options = {"build_base": str(BUILD_ROOT / "build")}
    # 不启用 build_ext parallel:Windows/MSVC 路径未实测(CI 盲跑成本高),
    # 83 模块串行编译数分钟可接受;要开先在分支上验证
    build_ext_options = {}
    if compiler:
        build_options["compiler"] = compiler
        build_ext_options["compiler"] = compiler
    setup(
        name="omniplc",
        version=version,
        license="MIT",
        packages=[],  # 全走 ext_modules:C 编译产物直接落 build_lib,无源可带
        ext_modules=extensions,
        cmdclass={"build_ext": PydBuildExt},
        options={
            "build": build_options,
            "build_ext": build_ext_options,
            "bdist_wheel": {"dist_dir": str(OUT)},
        },
        script_args=["bdist_wheel"],
    )


def find_wheel():
    wheels = sorted(OUT.glob("omniplc-*.whl"))
    if len(wheels) != 1:
        raise SystemExit("期望产出 1 个 wheel,实得:{}".format([w.name for w in wheels]))
    return wheels[0]


def inject_stubs(wheel):
    """向 wheel 注入 pyi + py.typed(方案 B,2026-10-19)。

    bdist_wheel 的 ``packages=[]`` 只收 ext 编译产物,.pyi/py.typed 不在其列;
    构建后按 zip 追加 + 同步更新 RECORD(uninstall 能干净清掉,规范自洽)。
    两遍重建:一遍加文件,一遍替换 RECORD 内容。
    """
    import base64
    import hashlib
    import zipfile

    inject = [
        (pyi, Path("omniplc") / pyi.relative_to(STAGING).as_posix())
        for pyi in sorted(STAGING.rglob("*.pyi"))
    ]
    typed = STAGING / "py.typed"
    if typed.exists():
        inject.append((typed, Path("omniplc") / "py.typed"))
    if not inject:
        print("注入:无 pyi/py.typed(未生成?)")
        return

    def _record_hash(data):
        digest = hashlib.sha256(data).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    record_name = None
    record_lines = None
    # 第一遍:全部文件(含新 pyi)写出到临时轮
    tmp1 = wheel.with_name(wheel.name + ".tmp1")
    with zipfile.ZipFile(wheel, "r") as zin, zipfile.ZipFile(tmp1, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            zout.writestr(item, data)
            if item.filename.endswith(".dist-info/RECORD"):
                record_name = item.filename
                record_lines = data.decode("utf-8").splitlines()
        for src, arc in inject:
            zout.writestr(arc.as_posix(), src.read_bytes())
    # 第二遍:用更新后的 RECORD 重建(替换内容)——解包序与 inject 元组
    # (pyi, arc)一致:src=staging 绝对路径、arc=轮内相对名。曾写反
    # ``(arc, src)`` 致 src 拿到相对 arc 名、read_bytes 必 FileNotFoundError
    # (2026-10-30 ::error:: 注解 + 本地假轮复现定位,inject 自引入从未成功过)
    for src, arc in inject:
        data = src.read_bytes()
        record_lines.append(
            "{},sha256={},{}".format(arc.as_posix(), _record_hash(data), len(data))
        )
    new_record = ("\n".join(record_lines) + "\n").encode("utf-8")
    tmp2 = wheel.with_name(wheel.name + ".tmp2")
    with zipfile.ZipFile(tmp1, "r") as zin, zipfile.ZipFile(tmp2, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == record_name:
                data = new_record
            zout.writestr(item, data)
    tmp1.unlink()
    tmp2.replace(wheel)
    print("注入:{} 个 pyi + py.typed 已入 wheel,RECORD 已更新".format(len(inject)))


def smoke(wheel):
    """装轮 + 干净子进程冒烟:二合一验证(轮元数据/平台标签被 pip 接受 + 运行可用)。"""
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--force-reinstall",
            str(wheel),
        ]
    )
    neutral = BUILD_ROOT / "smoke-cwd"
    neutral.mkdir(parents=True, exist_ok=True)
    code = (
        "import omniplc, omniplc.aio, omniplc.native\n"
        "assert omniplc.__file__.endswith('.pyd'), omniplc.__file__\n"
        "import importlib.util\n"
        "from pathlib import Path\n"
        "site = importlib.util.find_spec('omniplc').submodule_search_locations\n"
        "root = Path(next(iter(site)))\n"
        "assert (root / 'py.typed').is_file(), 'py.typed 缺失'\n"
        "assert (root / '__init__.pyi').is_file(), '__init__.pyi 缺失'\n"
        "assert len(list(root.rglob('*.pyi'))) >= 80, 'pyi 数量不足'\n"
        "print('pyd smoke ok:', omniplc.__file__)\n"
    )
    subprocess.check_call([sys.executable, "-I", "-c", code], cwd=str(neutral))


def main():
    global _PHASE
    parser = argparse.ArgumentParser(description="构建 pyd 无源二进制轮")
    parser.add_argument(
        "--compiler",
        default=None,
        help="distutils 编译器名(本地 mingw 复现:mingw32;CI 不传=MSVC 默认)",
    )
    args = parser.parse_args()

    print(
        "builder: {} | {}-bit".format(
            sys.version.replace("\n", " "), 64 if sys.maxsize > 2**32 else 32
        )
    )
    if OUT.exists():
        for old in OUT.glob("*.whl"):
            old.unlink()  # 防陈旧产物混入(如换版本号重跑)
    if BUILD_ROOT.exists():
        # 每次全量重建:防陈旧 .o/.pyd 被"视为最新"混进轮子(发布物确定性优先)
        shutil.rmtree(BUILD_ROOT)
    _PHASE = "解析版本"
    version = parse_version()
    _PHASE = "staging+剥PEP563"
    prepare_staging()
    _PHASE = "stubgen(uvx)"
    generate_stubs()
    _PHASE = "cythonize"
    extensions = cythonize_staging()
    _PHASE = "删源"
    drop_sources()
    _PHASE = "bdist_wheel(编译+链接)"
    build_wheel(extensions, version, args.compiler)
    _PHASE = "定位 wheel"
    wheel = find_wheel()
    print("wheel:{} | {} bytes".format(wheel.name, wheel.stat().st_size))
    _PHASE = "注入 pyi"
    inject_stubs(wheel)
    _PHASE = "装轮+冒烟"
    smoke(wheel)
    print("DONE:", wheel.name)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        # argparse --help/-h 走 SystemExit(0),不算失败不注解
        if exc.code not in (None, 0):
            print("::error::build_pyd 阶段[{}]失败(code={}):{}".format(_PHASE, exc.code, exc))
        raise
    except BaseException:
        import traceback

        traceback.print_exc()
        print(
            "::error::build_pyd 阶段[{}]异常:{!r}(完整 traceback 见步骤日志)".format(
                _PHASE, sys.exc_info()[1]
            )
        )
        raise
