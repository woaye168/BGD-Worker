"""本地 TTS 运行时构建工具。

拆分后的入口：scripts/build_runtime/build.py 的 main() 保持 CLI 兼容。
本包按职责拆分为：
- constants.py   配置常量（依赖列表、过滤规则、torch 索引）
- packaging.py   Python embeddable / pip / torch 安装工具
- gpt_sovits.py  GPT-SoVITS 源码克隆、裁剪、补丁、模型下载
- build.py       构建编排、zip 打包、HF 上传、CLI 入口
"""

from .build import build, main

__all__ = ["build", "main"]
