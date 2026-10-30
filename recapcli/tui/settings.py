"""本地模型选择与设置。界面只拿无密钥的配置摘要。"""

from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Collapsible, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from ..config import ConfigError, ENV_FILE, MODELS_FILE, list_model_profiles, save_model_profile


class ModelPickerScreen(ModalScreen):
    BINDINGS = [Binding("escape,f2", "dismiss", show=False)]

    def __init__(self, current):
        super().__init__()
        self.current = current

    def compose(self):
        with Vertical(classes="settings-dialog"):
            yield Static("选择模型", classes="dialog-title")
            yield Static("Enter 选择后用于下一轮；这里列出本地配置，不请求接口。", classes="dialog-note")
            yield Input(placeholder="筛选配置名或模型 ID", id="model-filter")
            yield OptionList(id="model-options", markup=False)
            yield Static("", id="model-count", markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Button("编辑所选", id="edit-model")
                yield Button("添加模型", id="add-model", variant="primary")
                yield Button("关闭", id="close-models")

    def on_mount(self):
        try:
            self.profiles = list_model_profiles()
        except ConfigError as error:
            self.profiles = []
            self.notify(str(error), severity="error")
        self._filter("")
        self.query_one("#model-filter", Input).focus()

    def _filter(self, text):
        options = self.query_one("#model-options", OptionList)
        options.clear_options()
        matching = [p for p in self.profiles if text.casefold() in (p["name"] + " " + p["model"]).casefold()]
        for profile in matching:
            flags = ("当前 · " if profile["name"] == self.current else "") + ("默认 · " if profile["default"] else "")
            state = "" if profile["configured"] else " · 待配置密钥"
            options.add_option(Option(f"{flags}{profile['model'] or '未设置模型'} · {profile['name']}{state}", id=profile["name"]))
        if matching:
            options.highlighted = next((i for i, p in enumerate(matching) if p["name"] == self.current), 0)
        self.query_one("#model-count", Static).update(f"{len(matching)} 个配置 · ↑↓ 浏览，Enter 使用；下方可编辑或添加")

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "model-filter":
            self._filter(event.value)

    def on_input_submitted(self, event: Input.Submitted):
        options = self.query_one("#model-options", OptionList)
        if options.highlighted is not None:
            self.dismiss(("select", options.get_option_at_index(options.highlighted).id))

    def on_key(self, event):
        if event.key in {"down", "up"} and self.focused is self.query_one("#model-filter", Input):
            event.stop()
            event.prevent_default()
            self.query_one("#model-options", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.dismiss(("select", event.option.id))

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "add-model":
            self.dismiss(("edit", None))
        elif event.button.id == "edit-model":
            options = self.query_one("#model-options", OptionList)
            if options.highlighted is not None:
                self.dismiss(("edit", options.get_option_at_index(options.highlighted).id))
        else:
            self.dismiss()


class ModelConfigScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", show=False)]

    def __init__(self, profile=None):
        super().__init__()
        self.profile = profile or {}

    def compose(self):
        profile = self.profile
        with Vertical(classes="settings-dialog"):
            yield Static("配置模型 · " + profile["name"] if profile else "添加模型", classes="dialog-title")
            with VerticalScroll(classes="dialog-scroll"):
                if not profile:
                    yield Label("配置名 · 本地别名，英文 / 数字 / 下划线")
                    yield Input(placeholder="例如 work", id="config-name")
                yield Label("模型 ID · 填服务商提供的实际名称")
                yield Input(profile.get("model", ""), placeholder="例如 gpt-5", id="config-model")
                yield Label("接口根地址 · 留空使用 OpenAI 默认地址")
                yield Input(profile.get("base_url", ""), placeholder="https://api.example.com/v1", id="config-url")
                yield Label("接口协议")
                yield Select([("Chat Completions", "chat_completions"), ("Responses", "responses")],
                             value=profile.get("protocol", "chat_completions"), allow_blank=False, id="config-protocol")
                yield Label("API Key · 留空保留已有密钥" + (f"（已从{profile['key_source']}读取）" if profile.get("configured") else ""))
                yield Input(password=True, placeholder="只写入本机 .env，不显示已有密钥", id="config-key")
                with Collapsible(title="工具、图片、流式与密钥变量", collapsed=True):
                    if profile:
                        yield Label("配置名 · 本地别名")
                        yield Input(profile["name"], id="config-name", disabled=True)
                    yield Label("API Key 的环境变量名")
                    yield Input(profile.get("api_key_env", ""), placeholder="留空按配置名自动生成，避免覆盖其他配置", id="config-key-env")
                    yield Checkbox("支持工具调用", value=profile.get("tools", True), id="config-tools")
                    yield Checkbox("支持图片输入", value=profile.get("images", False), id="config-images")
                    yield Checkbox("使用流式输出", value=profile.get("streaming", True), id="config-streaming")
                yield Checkbox("设为下次启动的默认配置", value=profile.get("default", not profile), id="config-default")
                yield Static(f"配置：{MODELS_FILE}\n密钥：{ENV_FILE}\n环境变量优先于 .env；保存不测试接口。",
                             classes="dialog-note", markup=False)
            yield Static("", id="config-error", markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Button("保存并使用", id="save-config", variant="primary")
                yield Button("取消 · Esc", id="cancel-config")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel-config":
            self.dismiss()
            return
        if event.button.id != "save-config":
            return
        value = lambda name: self.query_one("#config-" + name, Input).value.strip()
        checked = lambda name: self.query_one("#config-" + name, Checkbox).value
        name = value("name")
        try:
            save_model_profile(name, model=value("model"), base_url=value("url"), api_key=value("key"),
                               api_key_env=value("key-env") or "RECAP_" + name.upper().replace("-", "_") + "_API_KEY",
                               protocol=str(self.query_one("#config-protocol", Select).value),
                               tools=checked("tools"), images=checked("images"), streaming=checked("streaming"),
                               make_default=checked("default"))
        except (OSError, ValueError) as error:
            self.query_one("#config-error", Static).update(str(error))
            return
        self.query_one("#config-key", Input).value = ""
        self.dismiss(name)
