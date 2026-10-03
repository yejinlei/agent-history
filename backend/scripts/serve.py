# -*- coding: utf-8 -*-
"""后台常驻服务启动器（Windows）。

为什么需要它：直接 `uvicorn app.main:app` 跑在命令窗口里，窗口/会话一结束
服务就没了；而构建一本书要跑几个小时，服务必须能脱离终端独立存活。

用法（脱离进程树启动，不会被命令会话回收）：

    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = 'd:\\src\\agent-history\\.venv\\Scripts\\python.exe -u ' +
                      'd:\\src\\agent-history\\backend\\scripts\\serve.py'
        CurrentDirectory = 'd:\\src\\agent-history\\backend' }

    curl http://127.0.0.1:8000/api/health          # 就绪检查（首次约 1~2 分钟）
    # 停止：Stop-Process -Id (Get-NetTCPConnection -LocalPort 8000).OwningProcess

日志：backend/logs/serve.log（追加，行缓冲）。
可用环境变量：PORT（默认 8000）、LLM_MIN_INTERVAL（默认 2，网关限流用）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
os.chdir(BASE)
sys.path.insert(0, str(BASE))

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("LLM_MIN_INTERVAL", "2")

LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)
log = open(LOG_DIR / "serve.log", "a", encoding="utf-8", buffering=1)
sys.stdout = log
sys.stderr = log

import uvicorn  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"=== serve.py starting on 127.0.0.1:{port} ===", flush=True)
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="info")
