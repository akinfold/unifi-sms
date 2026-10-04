import os
import signal
import subprocess
import sys
import time

os.umask(0o077)
subprocess.run([sys.executable, "manage.py", "migrate", "--noinput"], check=True)
subprocess.run([sys.executable, "manage.py", "bootstrap"], check=True)
children = [
    subprocess.Popen(
        [
            "gunicorn",
            "config.wsgi:application",
            "--bind",
            "0.0.0.0:8000",
            "--workers",
            "1",
            "--threads",
            "4",
            "--timeout",
            "60",
            "--access-logfile",
            "/dev/null",
        ]
    ),
    subprocess.Popen([sys.executable, "manage.py", "runworker"]),
]
stopping = False


def shutdown(*_):
    global stopping
    stopping = True
    for child in children:
        if child.poll() is None:
            child.terminate()


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
while not stopping and all(child.poll() is None for child in children):
    time.sleep(0.5)
exit_code = 0 if stopping else 1
shutdown()
for child in children:
    try:
        child.wait(timeout=20)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
sys.exit(exit_code)
