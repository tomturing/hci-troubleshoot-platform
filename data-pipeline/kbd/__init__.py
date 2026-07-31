"""KBD 知识生产管道包。

从仓库根目录运行 ``uv run python -m data-pipeline.kbd.run`` 即可。包加载时统一
准备 ``backend/shared`` 的源码路径；Stage 6 还会在任何生产阶段开始前进行契约预检。
``PYTHONPATH=data-pipeline:backend python -m kbd.run`` 仍作为兼容入口支持。
"""

from .runtime import bootstrap_repository_imports

bootstrap_repository_imports()
