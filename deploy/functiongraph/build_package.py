# -*- coding: utf-8 -*-
"""打包 FunctionGraph 函数 zip: fg_handler.py + requests 及依赖。

用法:
    python build_package.py
产出: 同目录下 fg_package.zip(上传到华为云函数工作流)
"""
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
ZIP = os.path.join(HERE, "fg_package.zip")
MIRROR = "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"

if os.path.exists(BUILD):
    shutil.rmtree(BUILD)
os.makedirs(BUILD)

# 固定版本保证与 FunctionGraph 各 Python 运行时兼容(3.7+)
subprocess.check_call([sys.executable, "-m", "pip", "install", "--target", BUILD,
                       "requests==2.31.0", "urllib3==1.26.20", *MIRROR])
shutil.copy(os.path.join(HERE, "fg_handler.py"), BUILD)

if os.path.exists(ZIP):
    os.remove(ZIP)
with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(BUILD):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            p = os.path.join(root, f)
            z.write(p, os.path.relpath(p, BUILD))

print("built: %s (%.1f KB)" % (ZIP, os.path.getsize(ZIP) / 1024))
