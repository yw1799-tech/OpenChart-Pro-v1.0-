"""
OpenChart Pro 一键启动脚本
Usage: python run.py
"""

import uvicorn
import webbrowser
import threading
import time
import os
import sys

# 确保项目根目录在 Python 路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def open_browser():
    time.sleep(2)
    webbrowser.open("http://localhost:8888")


if __name__ == "__main__":
    # 确保数据目录存在
    os.makedirs("data", exist_ok=True)

    threading.Thread(target=open_browser, daemon=True).start()

    uvicorn.run("backend.main:app", host="0.0.0.0", port=8888, reload=True, log_level="info")
