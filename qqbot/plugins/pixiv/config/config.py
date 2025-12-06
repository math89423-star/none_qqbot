import configparser
from pathlib import Path

config_dir = Path(__file__).parent.parent
config_path = config_dir / 'config.conf'

config = configparser.ConfigParser()
config.read(config_path)

# 读取配置项
USE_PROXY = config.getboolean('DEFAULT', 'USE_PROXY', fallback=True)
PROXY = config.get('DEFAULT', 'PROXY', fallback='http://127.0.0.1:7890')
PROXY_URL = config.get('DEFAULT', 'PROXY_URL', fallback='https://quiet-hill-31f3.math89423.workers.dev/')
PIXIV_COOKIE = config.get('DEFAULT', 'PIXIV_COOKIE', fallback='PHPSESSID=14916444_EuNtNE3Yd2ZZ50A7UzivUlxP7O2hLP7s; device_token=ccd49454e972c3b547f1db56a3560575; p_ab_id=1; p_ab_id_2=1')
EXCLUDE_DURATION = config.getint('DEFAULT', 'EXCLUDE_DURATION', fallback=3600)
COOLDOWN_TIME = config.getint('DEFAULT', 'COOLDOWN_TIME', fallback=25)
MAX_DOWNLOAD_CHUNK = config.getint('DEFAULT', 'MAX_DOWNLOAD_CHUNK', fallback=1024 * 64)
DOWNLOAD_TIMEOUT = config.getint('DEFAULT', 'DOWNLOAD_TIMEOUT', fallback=60)
MAX_ATTEMPTS = config.getint('DEFAULT', 'MAX_ATTEMPTS', fallback=2)
