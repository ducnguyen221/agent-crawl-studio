"""A tiny core stand-in for the hard-kill test (SPR-03 FR12.3): runs `ytdlp.run_media_item` against the fake engine
(scenario `tree3`) and so keeps a three-level tree alive until the test kills THIS process with no warning.

    python media_driver.py <src> <job_dir> <fake_ytdlp.py> <scenario>
"""

import os
import sys
import time

src, job_dir, fake, scenario = sys.argv[1:5]
sys.path.insert(0, src)

from agent_crawl_studio import limits, ytdlp  # noqa: E402

dirs = ytdlp.make_item_dirs(job_dir, "0001", None)
tools = ytdlp.Tools(r"C:\tools\ffmpeg\bin", r"C:\Program Files\nodejs\node.exe", "0")
deps = {"engine_prefix": lambda: [sys.executable, "-P", fake, scenario], "poll": 0.1}
ytdlp.run_media_item("https://www.youtube.com/watch?v=jNQXAC9IVRw", "0001", dirs, limits.resolve({})[0], tools,
                     budget=10 ** 12, room=10 ** 12, clock=time.monotonic, deps=deps)
os._exit(0)
