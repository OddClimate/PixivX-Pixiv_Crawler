"""Pixiv 接口封装。

- 带重试和限速的 GET 请求
- 获取作品的全部 original 原图地址
- 下载单张图片

榜单抓取逻辑在 pixiv_client.py 的 fetch_ranking() / fetch_ranking_mirror() 中。
"""
import re
import time

import requests
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning

disable_warnings(InsecureRequestWarning)

RANKING_URL = "https://www.pixiv.net/ranking.php"
PIXIV_URL = "https://www.pixiv.net/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": PIXIV_URL,
}
WORKERS = 3  # 并发下载线程数，可按网络情况调整
REQUEST_DELAY = 0.3  # 每次请求后的延迟(秒)，降低被风控概率

session = requests.Session()


def get_with_retry(url, retries=4, timeout=30):
    """GET 请求带重试：代理偶发断流(SSL EOF)时逐个尝试，全失败才抛异常。

    失败后 session.close() 丢弃可能已断开的复用连接，否则 urllib3 会一直
    复用同一根断掉的连接导致重试也失败。
    """
    last_err = None
    for attempt in range(retries):
        try:
            return session.get(url, headers=HEADERS, verify=False, timeout=timeout)
        except Exception as e:
            last_err = e
            session.close()  # 丢弃断连，下次用全新连接
            time.sleep(REQUEST_DELAY * (attempt + 1))  # 失败退避
    raise last_err


def get_original_urls(pid):
    """通过 ajax 接口获取某作品的全部 original 原图地址。"""
    url = f"https://www.pixiv.net/ajax/illust/{pid}/pages?lang=zh"
    r = get_with_retry(url)
    r.raise_for_status()
    time.sleep(REQUEST_DELAY)
    return [u.replace("\\", "") for u in re.findall(r'"original":"(.*?)"}', r.text)]


def download_image(img_url, save_path, retries=3, errors=None):
    """下载单张图片；失败自动重试，retries 次后放弃，返回是否成功。

    errors 可传入一个 list，最终失败原因会追加进去（供界面显示）。
    """
    last_err = None
    for attempt in range(retries):
        try:
            r = session.get(img_url, headers=HEADERS, verify=False, timeout=60)
            r.raise_for_status()
            with open(save_path, "wb") as f:
                f.write(r.content)
            return True
        except Exception as e:
            last_err = e
            session.close()  # 断流时丢弃复用连接，强制走新连接
            if attempt == retries - 1:
                if errors is not None:
                    errors.append(str(last_err))
                return False
        finally:
            time.sleep(REQUEST_DELAY)  # 每次请求(含重试)后都留间隔
    if errors is not None:
        errors.append(str(last_err) if last_err else "下载失败")
    return False
