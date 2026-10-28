"""muse2api-http: OpenAI-compatible HTTP bridge for muse.ai (Hatch/Noise transport).

No browser, no CDP. The bridge speaks the same Noise_XX WebSocket protocol the
muse.ai web client uses, then exposes the agent over standard HTTP APIs.
"""

__version__ = "0.1.5"

import asyncio
import sys
import warnings

# curl_cffi's aio transport needs the selector-based event loop on Windows;
# the default ProactorEventLoop spawns an extra selector thread per session,
# which shows up as the CurlCffiWarning and adds overhead to every request.
if sys.platform == "win32":
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except AttributeError:  # pragma: no cover - non-Windows Python builds
        pass
