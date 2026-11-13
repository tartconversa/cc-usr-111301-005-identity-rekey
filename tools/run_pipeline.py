#!/usr/bin/env python
"""隔离模式引导器：与 fixtures 数据目录分离，显式登记受信代码路径。

用法（-I 忽略 PYTHONPATH 且不把 cwd 加入 sys.path，防止数据目录中的
同名模块被 import 劫持）：

    python -I tools/run_pipeline.py fixtures/facts.csv \
        fixtures/assertions.jsonl fixtures/identity.json --output-dir DIR
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.pipeline import main  # noqa: E402

if __name__ == "__main__":
    main()
