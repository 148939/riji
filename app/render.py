#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把日记数据渲染成一个自包含的日历 HTML (无依赖, 双击即开)."""
from __future__ import annotations

import json
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
TEMPLATE = APP_ROOT / "web" / "index.html"
PLACEHOLDER = "__RIJI_DATA__"


def build_html(payload: dict) -> str:
    template = TEMPLATE.read_text(encoding="utf-8")
    if PLACEHOLDER not in template:
        raise SystemExit("模板缺少占位符 %s" % PLACEHOLDER)
    data = json.dumps(payload, ensure_ascii=False, default=str)
    data = data.replace("</", "<\\/")
    return template.replace(PLACEHOLDER, data, 1)


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import riji

    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else riji.DEFAULT_DATA
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else data_dir.parent / "index.html"
    out.write_text(build_html(riji.build_payload(data_dir)), encoding="utf-8")
    print("已生成", out)
