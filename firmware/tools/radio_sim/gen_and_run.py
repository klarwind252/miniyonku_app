#!/usr/bin/env python3
"""se_resend_sim.cpp / rc_retry_sim.cpp は実ソースから抜き出したスナップショット。
ソースを変えたら、同じ要領で該当関数を差し替えてから実行する。"""
import subprocess, os
here=os.path.dirname(os.path.abspath(__file__))
for src in ("se_resend_sim.cpp","rc_retry_sim.cpp"):
    exe=os.path.join(here, src[:-4])
    subprocess.check_call(["g++","-std=c++17","-O2","-w",os.path.join(here,src),"-o",exe])
    print("###",src); subprocess.check_call([exe])
