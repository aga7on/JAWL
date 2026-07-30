import os
import time
from pathlib import Path


data = Path(os.environ["JAWL_DATA_DIR"])
data.mkdir(parents=True, exist_ok=True)
(data / "agent.pid").write_text(str(os.getpid()), encoding="utf-8")
stop = data / "agent.stop"
shared = Path(os.environ["JAWL_SANDBOX_DIR"])
shared.mkdir(parents=True, exist_ok=True)
with (shared / "instance-worker-seen.txt").open("a", encoding="utf-8") as stream:
    stream.write(os.environ["JAWL_INSTANCE_ID"] + "\n")
try:
    while not stop.exists():
        time.sleep(0.05)
finally:
    (data / "agent.pid").unlink(missing_ok=True)
