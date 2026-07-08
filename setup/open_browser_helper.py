"""
start.bat から呼ばれる。自身を DETACHED で再起動してすぐ制御を返し、
バックグラウンドでサーバー起動を待ってからブラウザを開く。
"""
import subprocess
import sys
import time
import urllib.request
import webbrowser

HEALTH = "http://localhost:8000/health"
ADMIN  = "http://localhost:8000/admin/"
TIMEOUT = 15

def wait_and_open():
    for _ in range(TIMEOUT):
        time.sleep(1)
        try:
            urllib.request.urlopen(HEALTH, timeout=1)
            break
        except Exception:
            pass
    webbrowser.open(ADMIN)

if __name__ == "__main__":
    if "--bg" not in sys.argv:
        # 自身をデタッチプロセスとして起動し即座に return
        CREATE_NO_WINDOW   = 0x08000000
        DETACHED_PROCESS   = 0x00000008
        subprocess.Popen(
            [sys.executable, __file__, "--bg"],
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
            close_fds=True,
        )
    else:
        wait_and_open()
