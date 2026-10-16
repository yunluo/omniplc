# -*- coding: utf-8 -*-
"""构建 pyd 无源二进制轮(防源码泄露交付形态,只挂 GitHub Release 附件,不发 PyPI)。

流程:``src/omniplc`` 拷到编译现场 → 剥 PEP563 导入 → Cython 全模块出 .c →
**删除全部源 .py** → ``bdist_wheel`` 出无源轮(只剩 .pyd + dist-info)→
pip 装入本解释器 → ``-I`` 干净子进程冒烟(import omniplc / aio / native)。

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
  拦截 ``omniplc``,冒烟假绿。
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parents[2]  # .github/scripts/build_pyd.py → 仓库根
SRC = ROOT / "src" / "omniplc"
BUILD_ROOT = ROOT / "build" / "pyd"
STAGING = BUILD_ROOT / "staging" / "omniplc"
OUT = ROOT / "dist-pyd"
FUTURE_LINE = "from __future__ import annotations"
# 换行注释占位而非直接删行:保源码行号,traceback 行号仍可对(src 与 .c 一致)
FUTURE_REPLACEMENT = "# [pyd-build] 剥离 PEP563 导入(Cython 兼容,保行号)\n"


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
    typed = STAGING / "py.typed"
    if typed.exists():
        typed.unlink()  # pyd-only 轮不带类型标记(无源可注,留之反让检查器困惑)
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
    print("已删源 .py:{} 个(产物树仅剩 .c/.pyd,无源可泄)".format(len(sources)))


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
    build_ext_options = {"parallel": os.cpu_count() or 4}
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


def smoke(wheel):
    """装轮 + 干净子进程冒烟:二合一验证(轮元数据/平台标签被 pip 接受 + 运行可用)。"""
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--force-reinstall", str(wheel)]
    )
    neutral = BUILD_ROOT / "smoke-cwd"
    neutral.mkdir(parents=True, exist_ok=True)
    code = (
        "import omniplc, omniplc.aio, omniplc.native\n"
        "assert omniplc.__file__.endswith('.pyd'), omniplc.__file__\n"
        "print('pyd smoke ok:', omniplc.__file__)\n"
    )
    subprocess.check_call([sys.executable, "-I", "-c", code], cwd=str(neutral))


def main():
    parser = argparse.ArgumentParser(description="构建 pyd 无源二进制轮")
    parser.add_argument(
        "--compiler",
        default=None,
        help="distutils 编译器名(本地 mingw 复现:mingw32;CI 不传=MSVC 默认)",
    )
    args = parser.parse_args()

    print(
        "builder: {} | {}-bit".format(
            sys.version.replace("\n", " "), 64 if sys.maxsize > 2 ** 32 else 32
        )
    )
    if OUT.exists():
        for old in OUT.glob("*.whl"):
            old.unlink()  # 防陈旧产物混入(如换版本号重跑)
    if BUILD_ROOT.exists():
        # 每次全量重建:防陈旧 .o/.pyd 被"视为最新"混进轮子(发布物确定性优先)
        shutil.rmtree(BUILD_ROOT)
    version = parse_version()
    prepare_staging()
    extensions = cythonize_staging()
    drop_sources()
    build_wheel(extensions, version, args.compiler)
    wheel = find_wheel()
    print("wheel:{} | {} bytes".format(wheel.name, wheel.stat().st_size))
    smoke(wheel)
    print("DONE:", wheel.name)


if __name__ == "__main__":
    main()
