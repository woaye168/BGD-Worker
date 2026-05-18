# @purpose: 本地 TTS 运行时打包脚本的兼容入口（已拆分为 build_runtime/ 包）
# @layer: build-tool
# @invariants:
#   - 本文件为向后兼容薄壳，逻辑全部转发到 build_runtime/ 子包
#   - 直接运行：python scripts/build_runtime.py --version 0.3.0 --target cpu

import sys
from pathlib import Path

# 确保 scripts/ 在 sys.path 中（作为包根）
scripts_dir = Path(__file__).resolve().parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from build_runtime.build import main

if __name__ == "__main__":
    sys.exit(main())
