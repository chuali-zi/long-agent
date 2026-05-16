"""kyagent — 面向麒麟操作系统的安全智能运维 Agent."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("kyagent")
except PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = ["__version__"]
