import os
import asyncio
import traceback
import configparser
import time
import logging
import aiohttp
import aiofiles
from nonebot import on_command, logger, get_driver
from nonebot.adapters.onebot.v11 import MessageSegment, Bot, Event
from pathlib import Path
from datetime import datetime, timezone, timedelta


# 创建日志
logger = logging.getLogger('logger')
logger.setLevel(logging.DEBUG)  # 设置最低日志级别

# 导入Pixiv逻辑
from .pixiv import (
    search_pixiv_by_tag,
    download_original_image,
    cleanup_temp_files,
    COOLDOWN_TIME,
    PROXY_URL
)

# 读取配置
config_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(config_dir, 'config.conf')
config = configparser.ConfigParser()
config.read(config_path)

# 优先使用环境变量，其次使用默认值
PROXY = config.get('DEFAULT', 'PROXY', fallback='http://127.0.0.1:7890')
USE_PROXY = config.getboolean('DEFAULT', 'USE_PROXY', fallback=True)

# 请求冷却机制
last_request_time = {}  # {user_id: last_request_time}



# ====== Nonebot2插件逻辑 ======
pixiv_cmd = on_command("pixiv", aliases={"p"}, priority=5, block=True)

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
                logger.error(f"发送失败: {str(e)}")
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
            logger.error(f"原图发送失败: {error_msg}\n{traceback.format_exc()}")
            
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
        logger.error(f"Pixiv搜索失败: {error_msg}\n{traceback.format_exc()}")
        
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

# ====== 预览图处理函数（降级用） ======
async def download_and_process_preview(image_url: str) -> bytes:
    """下载并处理预览图（小尺寸）"""
    try:
        proxy = PROXY if USE_PROXY else None
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.pixiv.net/"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(
                image_url, headers=headers, proxy=proxy, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status != 200:
                    raise Exception(f"预览图下载失败，状态码: {response.status}")
                return await response.read()
    except Exception as e:
        logger.error(f"预览图处理失败: {str(e)}")
        raise Exception(f"预览图处理失败: {str(e)}")