# -*- coding: utf-8 -*-
"""后台运行器（Windows）：让长任务脱离终端独立存活。

背景：直接在前台跑 `scripts/ingest_one.py`，命令会话一结束进程就被回收；
而构建一本大书要跑几个小时。用本脚本 + WMI 创建进程即可常驻后台。

    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = 'd:\\src\\agent-history\\.venv\\Scripts\\python.exe -u ' +
                      'd:\\src\\agent-history\\backend\\scripts\\run_bg.py ' +
                      'scripts/ingest_one.py 资治通鉴 --llm --concurrency 3 --no-store'
        CurrentDirectory = 'd:\\src\\agent-history\\backend' }

日志：backend/logs/<目标脚本名>.log（追加，行缓冲），随时可看进度。
环境变量：PYTHONIOENCODING（默认 utf-8）、LLM_MIN_INTERVAL（默认 2）。
"""
from __future__ import annotations

import os
import runpy
import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
os.chdir(BASE)
sys.path.insert(0, str(BASE))

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("LLM_MIN_INTERVAL", "2")

if len(sys.argv) < 2:
    sys.exit("用法: python -u scripts/run_bg.py <目标脚本> [参数...]")

# --config <json>：参数写在配置文件里（UTF-8），避免命令行传中文时的编码问题。
#   {"target": "scripts/ingest_one.py", "argv": ["资治通鉴", "--llm"],
#    "env": {"LLM_MIN_INTERVAL": "2"}}
if sys.argv[1] == "--config":
    import json
    cfg = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    os.environ.update({str(k): str(v)
                       for k, v in (cfg.get("env") or {}).items()})
    target, rest = cfg["target"], [str(x) for x in cfg.get("argv", [])]
else:
    target, rest = sys.argv[1], sys.argv[2:]
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)
log = open(LOG_DIR / (Path(target).stem + ".log"), "a",
           encoding="utf-8", buffering=1)
sys.stdout = log
sys.stderr = log

sys.argv = [target] + rest
print(f"=== run_bg start: {sys.argv}", flush=True)
try:
    runpy.run_path(str(BASE / target), run_name="__main__")
except BaseException as e:      # 异常也要落到日志里
    traceback.print_exc()
    print("=== run_bg FAILED:", e, flush=True)
finally:
    print("=== run_bg DONE", flush=True)
