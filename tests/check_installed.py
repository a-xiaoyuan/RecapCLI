"""安装包冒烟：在源码目录外用独立环境的 python -I 显式运行。"""

import asyncio
from importlib.metadata import version
from importlib.resources import files
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile


MODEL = "installation-check-model"
KEY = "fake-installation-check-key"


def check_import():
    import recapcli

    location = Path(recapcli.__file__).resolve()
    assert location.is_relative_to(Path(sysconfig.get_path("purelib")).resolve()), location
    assert (files("recapcli.tui") / "violet.tcss").is_file()
    return location


async def check_settings(save):
    import httpx2
    from openai import OpenAI
    from textual.widgets import Input

    from recapcli.config import CONFIG_DIR, ENV_FILE, MODELS_FILE, PROJECT_ROOT, load_config
    from recapcli.tui.app import RecapShell
    from recapcli.tui.state import load_shell_state

    expected = os.environ.get("RECAPCLI_CONFIG_DIR") or str(Path(os.environ["APPDATA"]) / "recapcli")
    assert CONFIG_DIR == Path(expected)
    assert CONFIG_DIR != PROJECT_ROOT

    def no_request(request):
        raise AssertionError("保存和恢复模型设置不应请求模型")

    def offline_client(**options):
        return OpenAI(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(no_request)))

    state = load_shell_state(Path.cwd())
    assert state.ready is (not save)
    app = RecapShell(state, client_factory=offline_client)
    async with app.run_test(size=(100, 36)) as pilot:
        if save:
            await pilot.press("f3")
            await pilot.pause()
            app.screen.query_one("#config-model", Input).value = MODEL
            app.screen.query_one("#config-url", Input).value = "https://offline.invalid/v1"
            app.screen.query_one("#config-key", Input).value = KEY
            await pilot.click("#save-config")
            for _ in range(100):
                await pilot.pause(0.05)
                if app.driver.client is not None and not app._busy:
                    break
            assert app.driver.client is not None, "保存后模型配置未启用"
        else:
            # 第二个进程重新启动 TUI，并用保存的默认配置初始化客户端。
            app._submit_value("/model default")
            for _ in range(100):
                await pilot.pause(0.05)
                if app.driver.client is not None and not app._busy:
                    break
            assert app.driver.model == MODEL
        config = load_config()
        assert (config.model, config.api_key, config.base_url) == (MODEL, KEY, "https://offline.invalid/v1")
        assert ENV_FILE.is_file() and MODELS_FILE.is_file()
        assert KEY not in MODELS_FILE.read_text(encoding="utf-8")
        assert MODEL in str(app.query_one("#key-hints").render())
    print("settings_saved" if save else "settings_restored_after_restart")


def main():
    location = check_import()
    if len(sys.argv) == 2:
        asyncio.run(check_settings(save=sys.argv[1] == "save"))
        return

    executable = Path(sys.executable).with_name("recapcli.exe" if os.name == "nt" else "recapcli")
    output = subprocess.check_output([str(executable), "--version"], text=True)
    assert version("recapcli") in output
    subprocess.run([str(executable), "--help"], check=True, stdout=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory(prefix="recapcli-installed-", dir=Path.cwd()) as directory:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("OPENAI_") and key not in {"RECAPCLI_CONFIG_DIR", "PYTHONPATH"}}
        # 覆盖 OS 配置根，只核对默认路径，不触碰本机真实凭据。
        env["APPDATA"] = env["XDG_CONFIG_HOME"] = directory
        subprocess.run([sys.executable, "-I", "-c",
                        "import os; from pathlib import Path; from recapcli.config import CONFIG_DIR; "
                        "assert CONFIG_DIR == Path(os.environ['APPDATA']) / 'recapcli'"],
                       cwd=directory, env=env, check=True)
        for config_dir in (None, str(Path(directory) / "config")):
            if config_dir:
                env["RECAPCLI_CONFIG_DIR"] = config_dir
            for mode in ("save", "restart"):
                subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), mode],
                               cwd=directory, env=env, check=True, timeout=60)
    print(f"installed_package={location}\ninstallation_check_passed")


if __name__ == "__main__":
    main()
