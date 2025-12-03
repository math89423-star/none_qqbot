from nonebot import get_plugin_config
from nonebot.plugin import PluginMetadata
from nonebot import on_command
from .config import Config
from datetime import datetime

__plugin_meta__ = PluginMetadata(
    name="time",
    description="",
    usage="",
    config=Config,
)

config = get_plugin_config(Config)

# 创建命令处理器
time_cmd = on_command("时间", aliases={"time"}, priority=5, block=True)

@time_cmd.handle()
async def handle_time_command():
    """处理 /时间 命令"""
    # 获取当前时间
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 发送响应消息
    await time_cmd.finish(f"当前时间：{now}")
