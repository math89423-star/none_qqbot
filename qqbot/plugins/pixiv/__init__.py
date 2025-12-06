import os
import asyncio
import traceback
import time
import logging
import aiofiles
import json  
from nonebot import on_command, logger
from nonebot.adapters.onebot.v11 import MessageSegment, Bot, Event
from .config.config import (
    COOLDOWN_TIME, 
    PROXY, 
    PROXY_URL
)
from .api.pixiv_api import (
    search_pixiv_by_tag,
    download_original_image,
    cleanup_temp_files,
    download_and_process_preview
)
# 创建日志
logger = logging.getLogger()
logging.basicConfig(level = logging.INFO,format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# 请求冷却机制
last_request_time = {}
# 加载角色数据文件
character_data = {}
config_dir = os.path.dirname(os.path.abspath(__file__))
character_file = os.path.join(config_dir, 'character.json')
if os.path.exists(character_file):
    try:
        with open(character_file, 'r', encoding='utf-8') as f:
            character_data = json.load(f)
        logger.info(f"角色数据加载成功，共 {len(character_data)} 个角色")
    except Exception as e:
        logger.info(f"加载角色数据失败: {str(e)}")
        character_data = {}  # 加载失败时清空数据
else:
    logger.warning("角色数据文件 character.json 不存在，将使用空数据")

# 核心command命令
pixiv_cmd = on_command("搜图", aliases={"p"}, priority=5, block=True)
@pixiv_cmd.handle()
async def handle_pixiv_command(bot: Bot, event: Event):
    """处理 /pixiv 命令 - 原图优先模式"""
    # ===== 新增：冷却机制检查 =====
    user_id = event.get_user_id()
    current_time = time.time()
    # 检查是否在冷却中
    if user_id in last_request_time:
        elapsed = current_time - last_request_time[user_id]
        if elapsed < COOLDOWN_TIME:
            remaining = COOLDOWN_TIME - elapsed
            await bot.send(event, f"请求过于频繁，请等待 {remaining:.1f} 秒后再试")
            return
    # 更新最后请求时间
    last_request_time[user_id] = current_time
    raw_message = str(event.get_message()).strip()
    command_str = event.get_plaintext().split()[0]
    args = raw_message[len(command_str):].strip()
    if not args:
        await bot.send(event, "请提供搜索标签，例如：\n/pixiv 鸣潮\n/p 鸣潮")
        return
    tags = [tag.strip() for tag in args.split() if tag.strip()]
    logger.info(f"Pixiv搜索请求: {tags}")
    try:
        # 1. 搜索作品
        result = await search_pixiv_by_tag(tags)
        # 2. 构建消息内容
        msg_content = (
            f"🎨 作品标题: {result['title']}\n"
            f"👤 作者: {result['author']} (ID: {result['author_id']})\n"
            f"🆔 作品ID: {result['pid']}\n"
            f"🔗 作品链接: {result['work_url']}\n\n"
            f"⏳ 正在下载原图 (可能需要较长时间)..."
        )
        # 发送初步信息
        await bot.send(event, msg_content)
        # 3. 安全下载原图
        try:
            # 清理旧临时文件
            await cleanup_temp_files()
            # 下载原图
            file_path = await download_original_image(result['image_url'])
            # 检查文件是否存在
            if not file_path or not file_path.exists():
                if file_path is None:
                    logger.warning("⚠️ 原图压缩失败，将使用预览图")
                else:
                    raise FileNotFoundError(f"文件不存在: {file_path}")
                # 降级发送预览图
                fallback_msg = (
                    f"⚠️ 原图过大或压缩失败，已自动降级为预览图\n"
                    f"🔗 原图下载: {result['image_url']}\n\n"
                    f"🖼️ 当前显示预览图（点击链接下载原图）:"
                )
                await bot.send(event, fallback_msg)
                # 发送预览图
                preview_data = await download_and_process_preview(result['preview_url'])
                await bot.send(event, MessageSegment.image(preview_data))
                return
            # 检查文件大小
            file_size = file_path.stat().st_size
            if file_size > 10 * 1024 * 1024:  # 超过10MB
                logger.warning(f"⚠️ 图片过大 ({file_size/1024/1024:.1f}MB)，已自动降级为预览图")
                fallback_msg = (
                    f"⚠️ 原图过大（{file_size/1024/1024:.1f}MB），已自动降级为预览图\n"
                    f"🔗 原图下载: {result['image_url']}\n\n"
                    f"🖼️ 当前显示预览图（点击链接下载原图）:"
                )
                await bot.send(event, fallback_msg)
                # 发送预览图
                preview_data = await download_and_process_preview(result['preview_url'])
                await bot.send(event, MessageSegment.image(preview_data))
                return
            # 发送原图
            logger.info(f"准备发送文件路径: {file_path}")
            start_time = time.time()
            # 读取文件内容
            try:
                async with aiofiles.open(file_path, 'rb') as f:
                    image_data = await f.read()
                await bot.send(event, MessageSegment.image(image_data))
                logger.info(f"✅ 原图发送成功! 耗时: {time.time()-start_time:.1f}s")
            except Exception as e:
                logger.info(f"发送失败: {str(e)}")
                raise e
            # 4. 同步清理文件（确保发送完成后再删除）
            try:
                # 等待一小段时间确保消息完全发送
                await asyncio.sleep(1)
                if file_path.exists():
                    file_path.unlink()
                    logger.debug(f"✅ 已清理临时文件: {file_path}")
            except Exception as e:
                logger.warning(f"清理文件警告 {file_path}: {str(e)}")
        except Exception as e:
            error_msg = str(e)
            logger.info(f"原图发送失败: {error_msg}\n{traceback.format_exc()}")
            # 降级方案：发送预览图 + 原图链接
            fallback_msg = (
                f"⚠️ 原图发送失败（可能文件过大或网络问题），已自动降级\n"
                f"🔗 原图下载: {result['image_url']}\n\n"
                f"🖼️ 当前显示预览图（点击链接下载原图）:"
            )
            await bot.send(event, fallback_msg)
            # 发送预览图
            preview_data = await download_and_process_preview(result['preview_url'])
            await bot.send(event, MessageSegment.image(preview_data))
    except Exception as e:
        error_msg = str(e)
        logger.info(f"Pixiv搜索失败: {error_msg}\n{traceback.format_exc()}")
        # 优化错误提示
        if "Cookie" in error_msg or "cookie" in error_msg.lower():
            error_msg = (
                "⚠️ Cookie无效！请重新获取Pixiv Cookie:\n"
                "1. 登录 https://www.pixiv.net\n"
                "2. 按 F12 打开开发者工具\n"
                "3. 进入 Application → Storage → Cookies\n"
                "4. 复制整个 Cookie 内容"
            )
        elif "代理" in error_msg or "proxy" in error_msg.lower() or "Proxy" in error_msg:
            error_msg = (
                f"⚠️ 代理配置问题！请检查:\n"
                f"- 本地代理: {PROXY}\n"
                f"- Cloudflare 代理: {PROXY_URL}\n"
                "- 确保代理软件正常运行"
            )
        elif "timeout" in error_msg.lower() or "超时" in error_msg:
            error_msg = (
                "⚠️ 请求超时！可能是网络不稳定或代理延迟过高\n"
                "建议:\n"
                "1. 检查代理是否正常运行\n"
                "2. 尝试更换标签\n"
                "3. 检查Cloudflare Workers是否可用"
            )
        elif "memory access out of bounds" in error_msg or "内存" in error_msg:
            error_msg = (
                "⚠️ 内存溢出！原图过大导致\n"
                "已自动降级发送预览图\n"
                "您也可以通过作品链接下载原图"
            )
        elif "404" in error_msg or "403" in error_msg:
            error_msg = (
                "⚠️ 无法访问图片资源\n"
                "可能是代理配置有误或Pixiv限制"
            )
        else:
            error_msg = f"发生未知错误: {error_msg}"
        await bot.send(event, f"❌ 搜索失败: {error_msg}")

# 搜图帮助命令
help_cmd = on_command("搜图帮助", aliases={"sotu"}, priority=5, block=True)
@help_cmd.handle()
async def handle_help_command(bot: Bot, event: Event):
    """处理 /搜图帮助 [归属] [角色名] - 查询角色昵称"""
    # 获取原始文本并移除命令前缀
    raw_text = event.get_plaintext()
    # 定义所有命令前缀
    command_prefixes = [
        "/搜图帮助", "搜图帮助",
        "/sotu", "sotu"
    ]
    # 移除命令前缀并获取参数
    args = raw_text.strip()
    for prefix in command_prefixes:
        if args.startswith(prefix):
            # 只移除第一个匹配的前缀
            args = args[len(prefix):].strip()
            break
    logger.debug(f"处理搜图帮助命令，参数: '{args}'")
    # 情况1: 无参数 - 显示所有归属
    if not args:
        if not character_data:
            await bot.send(event, "❌ 角色数据库为空，请联系管理员初始化数据")
            return
        franchises = sorted(character_data.keys())
        msg = "📚 当前支持的作品归属:\n\n"
        msg += "• " + "\n• ".join(f"「{f}」" for f in franchises)
        msg += "\n\n💡 使用方法: /搜图帮助 [归属名] [角色名]"
        await bot.send(event, msg)
        return
    # 拆分参数 (最多两部分)
    parts = args.split(maxsplit=1)
    # 情况2: 仅归属名 - 列出归属下的角色
    if len(parts) == 1:
        franchise = parts[0]
        # 验证归属是否存在
        if franchise not in character_data:
            # 尝试模糊匹配归属
            matches = [f for f in character_data if franchise in f]
            if matches:
                msg = f"⚠️ 未找到归属「{franchise}」，您可能想查询:\n"
                msg += "• " + "\n• ".join(f"「{m}」" for m in matches)
            else:
                msg = f"❌ 未找到归属「{franchise}」\n可用归属: {', '.join(character_data)}"
            await bot.send(event, msg)
            return
        # 获取归属下的角色列表
        franchise_data = character_data[franchise]
        roles = sorted(franchise_data.keys())
        msg = f"🎭 归属「{franchise}」角色列表 ({len(roles)}个):\n\n"
        msg += "• " + "\n• ".join(roles)
        msg += f"\n\n🔍 查询别名: /搜图帮助 {franchise} [角色名]"
        await bot.send(event, msg)
        return
    # 情况3: 归属 + 角色名 - 查询角色别名
    franchise, character = parts
    # 验证归属
    if franchise not in character_data:
        matches = [f for f in character_data if franchise in f]
        if matches:
            msg = f"⚠️ 归属「{franchise}」不存在，推荐:\n"
            msg += "• " + "\n• ".join(f"「{m}」" for m in matches)
        else:
            msg = f"❌ 无效归属「{franchise}」，使用 /搜图帮助 查看可用归属"
        await bot.send(event, msg)
        return
    # 验证角色
    franchise_data = character_data[franchise]
    if character not in franchise_data:
        # 在归属内模糊匹配角色
        matches = [c for c in franchise_data if character in c]
        if matches:
            msg = f"🔍 在「{franchise}」中未找到「{character}」，推荐:\n"
            msg += "• " + "\n• ".join(matches)
        else:
            msg = f"❌ 「{franchise}」中不存在角色「{character}」"
        await bot.send(event, msg)
        return
    # 获取并展示别名
    aliases = franchise_data[character].get("别名", [])
    if not aliases:
        await bot.send(event, f"ℹ️ 角色「{character}」(归属: {franchise}) 未设置别名")
        return
    # 格式化别名列表
    alias_list = []
    for i, alias in enumerate(aliases, 1):
        clean_alias = alias.strip().replace("  ", " ")
        alias_list.append(f"{i}. {clean_alias}")
    msg = f"✅ 角色「{character}」别名列表\n"
    msg += f"所属作品: {franchise}\n\n"
    msg += "\n".join(alias_list)
    msg += "\n\n💡 使用这些别名进行搜图效果更佳"
    await bot.send(event, msg)
