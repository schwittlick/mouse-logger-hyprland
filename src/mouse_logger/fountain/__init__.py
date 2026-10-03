"""The data fountain: a local HTTP service holding every recorded path.

Needs the optional extra: uv sync --extra fountain
"""

CACHE_VERSION = 1   # layout of the Feather chunk files; bump to rebuild everything
WIRE_VERSION = 1    # layout of the /paths response
