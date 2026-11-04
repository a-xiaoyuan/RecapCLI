"""读取软件版本：安装后取包元数据，源码运行时取 pyproject.toml。"""

from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path
import tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def project_version():
    try:
        return distribution_version("recapcli")
    except PackageNotFoundError:
        pass
    try:
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as file:
            return str(tomllib.load(file)["project"]["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return None
