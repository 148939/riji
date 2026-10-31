# -*- coding: utf-8 -*-
"""容器入口：先把 /data 交给 riji 用户，再降权跑服务。

宿主机挂进来的目录通常是 root 的，直接以 riji 运行会写不进去。
"""
import os
import pwd
import subprocess
import sys

DATA = os.environ.get("RIJI_DATA", "/data")
USER = "riji"


def main():
    if os.geteuid() != 0:
        return run()
    try:
        subprocess.run(["chown", "-R", "%s:%s" % (USER, USER), DATA], check=False)
    except Exception:
        pass
    try:
        info = pwd.getpwnam(USER)
        os.setgid(info.pw_gid)
        os.setuid(info.pw_uid)
    except Exception as exc:
        print("[entry] 降权失败，继续以当前用户运行: %s" % exc, file=sys.stderr)
    return run()


def run():
    cmd = [sys.executable, "server.py", "--data", DATA, "--port",
           os.environ.get("PORT", "8888"), "--lan", "--cloud"]
    os.execv(sys.executable, cmd)


if __name__ == "__main__":
    sys.exit(main() or 0)
