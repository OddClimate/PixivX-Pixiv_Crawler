#!/usr/bin/env python3
"""PixivX — Pixiv 榜单桌面客户端

在 pixiv_api.py 的基础上加图形界面：
- 日榜 / 周榜 / 月榜 / 新人榜 切换
- 左侧列表：勾选 / 排名 / 标题 / 作者 / 页数
- 右侧预览：选中作品时加载缩略图并显示信息
- 「下载全部」/「下载选中」，复用原脚本的重试、超时与限速逻辑
- 主题：跟随系统 / 浅色 / 深色
- 数据源：官方（需代理）/ 镜像（免代理）/ 自动
- 可配置代理、镜像榜单接口、镜像图片域名，保存到 config.json
- 可用 PyInstaller 打包为单文件 exe（见 build_exe.bat）

运行：python pixiv_client.py
"""
import json
import os
import queue
import sys
import threading
import time
import winreg
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from urllib.parse import urlsplit, urlunsplit

import math

import requests
import tkinter as tk
from tkinter import filedialog, messagebox

import ttkbootstrap as tb
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageTk

import pixiv_api as core

APP_NAME = "PixivX"

MODES = [
    ("日榜", "daily"),
    ("周榜", "weekly"),
    ("月榜", "monthly"),
    ("新人榜", "rookie"),
]
RANKING_CONTENT = "illust"
RANKING_LIMIT = 500

LIGHT_THEME = "bootstrap-light"
DARK_THEME = "bootstrap-dark"

SOURCE_CHOICES = [
    ("自动", "auto"),
    ("官方（需代理）", "official"),
    ("镜像（免代理）", "mirror"),
]
DEFAULT_CONFIG = {
    "source": "auto",
    "proxy": "",
    "mirror_api": "https://pix.ipv4.host",
    "mirror_img": "https://i.pixiv.re",
    "theme": "system",
}

# 镜像榜单接口只支持日/周/月榜
MIRROR_MODES = {"daily": "day", "weekly": "week", "monthly": "month"}

CHECKED = "☑"
UNCHECKED = "☐"


def app_dir():
    """打包后取 exe 所在目录，源码运行时取脚本目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    """PyInstaller 单文件模式下资源会被解压到 _MEIPASS。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def config_path():
    return os.path.join(app_dir(), "config.json")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg.update({k: saved[k] for k in DEFAULT_CONFIG if k in saved})
    except (OSError, ValueError):
        pass
    # 兜底：镜像地址被清空或写坏时恢复默认，避免下载器失去可用镜像
    cfg["mirror_api"] = normalize_url(cfg.get("mirror_api"),
                                      DEFAULT_CONFIG["mirror_api"])
    cfg["mirror_img"] = normalize_url(cfg.get("mirror_img"),
                                      DEFAULT_CONFIG["mirror_img"])
    if cfg.get("source") not in ("auto", "official", "mirror"):
        cfg["source"] = "auto"
    if cfg.get("theme") not in ("system", "light", "dark"):
        cfg["theme"] = "system"
    return cfg


def save_config(cfg):
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def apply_proxy(proxy):
    """把代理设置应用到全局 session；留空则清空（回退到环境变量/直连）。"""
    proxy = (proxy or "").strip()
    if proxy:
        core.session.proxies.update({"http": proxy, "https": proxy})
    else:
        core.session.proxies.clear()


def normalize_url(url, default):
    url = (url or "").strip() or default
    if "://" not in url:
        url = "https://" + url
    return url.rstrip("/")


FALLBACK_IMAGE_MIRRORS = ["https://i.pixiv.nl"]


def image_mirror_candidates(primary):
    """返回按优先级排列的镜像图片域名（主域名在前，自动去重）。"""
    out = []
    for u in [primary] + FALLBACK_IMAGE_MIRRORS:
        n = normalize_url(u, DEFAULT_CONFIG["mirror_img"])
        if n and n not in out:
            out.append(n)
    return out


def rewrite_image_host(url, mirror_base):
    """把 i.pximg.net 的图片地址改写到镜像域名，路径保持不变。"""
    if not mirror_base or "i.pximg.net" not in url:
        return url
    src = urlsplit(url)
    dst = urlsplit(normalize_url(mirror_base, DEFAULT_CONFIG["mirror_img"]))
    return urlunsplit((dst.scheme or "https", dst.netloc, src.path, src.query, src.fragment))


def image_ext(url, default=".jpg"):
    ext = os.path.splitext(urlsplit(url).path)[1].lower()
    return ext if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp") else default


def probe_official(timeout=5):
    """快速探测 pixiv.net 是否可达（走当前代理设置）。"""
    try:
        r = core.session.get("https://www.pixiv.net/robots.txt", timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


def system_is_dark():
    """读取 Windows 的“应用模式”设置：AppsUseLightTheme == 0 表示深色。"""
    try:
        key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            value, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
            return int(value) == 0
    except OSError:
        return False


def fetch_ranking(mode, content=RANKING_CONTENT, limit=RANKING_LIMIT):
    """官方源：分页拉取榜单，返回带元信息的条目列表。"""
    entries, seen = [], set()
    page = 1
    while len(entries) < limit:
        url = (f"{core.RANKING_URL}?format=json&mode={mode}&content={content}"
               f"&p={page}")
        r = core.get_with_retry(url)
        if r.status_code == 404:  # 翻过榜单末尾
            break
        r.raise_for_status()
        data = r.json()
        contents = data.get("contents") or []
        if not contents:
            break
        for item in contents:
            pid = str(item.get("illust_id", ""))
            if not pid or pid in seen:
                continue
            seen.add(pid)
            tags = item.get("tags") or []
            entries.append({
                "pid": pid,
                "rank": item.get("rank"),
                "title": item.get("title") or item.get("illust_title") or "",
                "user": item.get("user_name") or "",
                "thumb": item.get("url") or "",
                "pages": item.get("illust_page_count") or 1,
                "tags": list(tags)[:10],
                "originals": None,
                "mirrored": False,
            })
            if len(entries) >= limit:
                break
        total = data.get("rank_total")
        if total and len(entries) >= total:
            break
        time.sleep(core.REQUEST_DELAY)
        page += 1
    return entries[:limit]


def fetch_ranking_mirror(mode, api_base, image_mirror,
                         limit=RANKING_LIMIT, timeout=30):
    """镜像源：从 pixivic 后端接口拉日/周/月榜（无需代理）。"""
    mirror_mode = MIRROR_MODES.get(mode)
    if not mirror_mode:
        raise RuntimeError("镜像源不支持新人榜，请改用官方源（需要代理）")

    base = normalize_url(api_base, DEFAULT_CONFIG["mirror_api"])
    now = time.time()
    # 当天的榜单常常还没生成，往前找最多 4 天
    dates = [time.strftime("%Y-%m-%d", time.localtime(now - 86400 * i))
             for i in range(4)]
    for date in dates:
        entries, seen = [], set()
        page = 1
        while len(entries) < limit:
            url = f"{base}/ranks?date={date}&mode={mirror_mode}&page={page}&size=30"
            r = core.session.get(url, timeout=timeout)
            r.raise_for_status()
            data = (r.json() or {}).get("data") or []
            if not data:
                break
            for item in data:
                if item.get("type") == "ad_image" or "adId" in item:
                    continue  # 跳过插在榜单里的广告
                pid = str(item.get("id") or "")
                if not pid or pid in seen:
                    continue
                urls = [u.get("original") for u in (item.get("imageUrls") or [])
                        if u.get("original")]
                if not urls:
                    continue
                seen.add(pid)
                artist = (item.get("artistPreView") or {}).get("name") or ""
                tags = [t.get("translatedName") or t.get("name") or ""
                        for t in (item.get("tags") or [])]
                entries.append({
                    "pid": pid,
                    "rank": len(entries) + 1,
                    "title": item.get("title") or "",
                    "user": artist,
                    "thumb": rewrite_image_host(urls[0], image_mirror),
                    "pages": len(urls),
                    "tags": tags[:10],
                    "originals": urls,
                    "mirrored": True,
                })
                if len(entries) >= limit:
                    break
            page += 1
            time.sleep(core.REQUEST_DELAY)
        if entries:
            return entries[:limit]
    return []


def _download_image_with_fallback(original_url, save_path, mirrors):
    """依次尝试镜像域名；返回 (是否成功, 失败原因)。"""
    if "i.pximg.net" not in original_url:
        errs = []
        ok = core.download_image(original_url, save_path, errors=errs)
        return ok, ("" if ok else (errs[0] if errs else "下载失败"))

    failures = []
    for idx, base in enumerate(mirrors):
        errs = []
        target = rewrite_image_host(original_url, base)
        # 主域名多给一次重试，备用域名只试一次，避免超时等待过久
        ok = core.download_image(target, save_path,
                                 retries=2 if idx == 0 else 1, errors=errs)
        if ok:
            return True, ""
        failures.append(f"{base}: {errs[0] if errs else '失败'}")
    return False, "；".join(failures)


def download_entry(entry, folder, image_mirror=None, stop_event=None):
    """下载单个作品的全部原图，返回 (成功张数, 总张数, 失败原因列表)。"""
    if stop_event is not None and stop_event.is_set():
        return 0, 0, []  # 已请求停止：排队中的任务直接跳过
    pid = entry["pid"]
    urls = entry.get("originals") or core.get_original_urls(pid)
    mirrors = image_mirror_candidates(image_mirror) if image_mirror else []
    ok = 0
    errors = []
    for i, img_url in enumerate(urls):
        if stop_event is not None and stop_event.is_set():
            break  # 已请求停止：不再开始下一张，正在下载的那张会自然结束
        if mirrors:
            target_url = rewrite_image_host(img_url, mirrors[0])
            path = os.path.join(folder, f"{pid}_{i}{image_ext(target_url)}")
            success, reason = _download_image_with_fallback(
                img_url, path, mirrors)
        else:
            path = os.path.join(folder, f"{pid}_{i}{image_ext(img_url)}")
            errs = []
            success = core.download_image(img_url, path, errors=errs)
            reason = "" if success else (errs[0] if errs else "下载失败")
        if success:
            ok += 1
        else:
            errors.append(f"第 {i + 1} 张：{reason}")
    return ok, len(urls), errors


class ThemeSwitch(tk.Label):
    """胶囊形日夜开关。

    用 Pillow 以 4 倍尺寸绘制再 LANCZOS 降采样，圆和图标边缘都是平滑的，
    不会被 Tk Canvas 的锯齿影响。浅色：金色太阳滑块在左；深色：深色月亮滑块在右。
    """

    W, H = 66, 30
    SCALE = 4

    def __init__(self, parent, dark, command, bg):
        super().__init__(parent, bd=0, highlightthickness=0, bg=bg,
                         cursor="hand2")
        self._dark = bool(dark)
        self._pos = 1.0 if self._dark else 0.0
        self._ui_dark = bool(dark)
        self._hover = False
        self._photo = None
        self.command = command
        self.bind("<Button-1>", lambda _e: self.command(not self._dark))
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self._render()

    def _set_hover(self, value):
        self._hover = value
        self._render()

    def set_palette(self, ui_dark, bg):
        self._ui_dark = bool(ui_dark)
        self.configure(bg=bg)
        self._render()

    def set_dark(self, dark, animate=False):
        dark = bool(dark)
        if dark == self._dark:
            self._render()
            return
        self._dark = dark
        target = 1.0 if dark else 0.0
        if animate:
            self._animate(target)
        else:
            self._pos = target
            self._render()

    def _animate(self, target):
        start = self._pos
        steps = 12

        def step(i=0):
            t = i / steps
            eased = t * t * (3 - 2 * t)
            self._pos = start + (target - start) * eased
            self._render()
            if i < steps:
                self.after(12, lambda: step(i + 1))
        step()

    @staticmethod
    def _lerp(c1, c2, t):
        return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))

    def _render(self):
        out = self._compose()
        self._photo = ImageTk.PhotoImage(out)
        self.configure(image=self._photo)

    def _compose(self):
        """按当前状态绘制一张 66x30 的 PIL 图（内部 4 倍超采样）。"""
        s = self.SCALE
        W, H = self.W * s, self.H * s
        bg = self.cget("bg")

        if self._ui_dark:
            track = "#49515e" if self._hover else "#3f4652"
            outline = "#545d6b" if self._hover else "#4a525f"
            knob_top = "#3d4553" if self._hover else "#353d4a"
            knob_bottom = "#2b323d" if self._hover else "#252b34"
            icon = (238, 242, 248, 255)
            shadow_alpha = 95
        else:
            track = "#e1e6ef" if self._hover else "#e9edf4"
            outline = "#d6dce8" if self._hover else "#dde3ee"
            knob_top = "#efd07a" if self._hover else "#e9c96c"
            knob_bottom = "#ddb74e" if self._hover else "#d7b047"
            icon = (255, 255, 255, 255)
            shadow_alpha = 55

        img = Image.new("RGBA", (W, H), bg)
        d = ImageDraw.Draw(img, "RGBA")

        # 胶囊轨道
        d.rounded_rectangle([s, 3 * s, W - s - 1, H - 3 * s - 1],
                            radius=12 * s, fill=track, outline=outline,
                            width=s)

        cy = H / 2
        x_left, x_right = 13 * s, (self.W - 13) * s
        cx = x_left + (x_right - x_left) * self._pos
        r = 10 * s

        # 滑块阴影：高斯模糊，边缘柔和
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        sd.ellipse([cx - r - 0.6 * s, cy - r + 0.6 * s,
                    cx + r + 0.6 * s, cy + r + 1.8 * s],
                   fill=(0, 0, 0, shadow_alpha))
        shadow = shadow.filter(ImageFilter.GaussianBlur(1.4 * s))
        img = Image.alpha_composite(img, shadow)

        # 滑块：竖向渐变
        size = int(2 * r)
        grad = Image.new("RGBA", (size, size))
        gd = ImageDraw.Draw(grad)
        top = ImageColor.getrgb(knob_top)
        bottom = ImageColor.getrgb(knob_bottom)
        for yy in range(size):
            t = yy / (size - 1)
            gd.line([(0, yy), (size, yy)],
                    fill=self._lerp(top, bottom, t) + (255,))
        knob_mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(knob_mask).ellipse([0, 0, size - 1, size - 1],
                                          fill=255)
        img.paste(grad, (int(cx - r), int(cy - r)), knob_mask)

        if self._dark:
            img = self._draw_moon(img, cx, cy, s, icon)
        else:
            self._draw_sun(ImageDraw.Draw(img, "RGBA"), cx, cy, s, icon)

        return img.convert("RGB").resize((self.W, self.H), Image.LANCZOS)

    def _draw_sun(self, d, cx, cy, s, color):
        rr = 3.2 * s
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=color)
        lw = max(2, int(1.5 * s))
        for k in range(8):
            ang = math.radians(k * 45)
            dx, dy = math.cos(ang), math.sin(ang)
            x1, y1 = cx + dx * 5.2 * s, cy + dy * 5.2 * s
            x2, y2 = cx + dx * 6.9 * s, cy + dy * 6.9 * s
            d.line([x1, y1, x2, y2], fill=color, width=lw)
            cap = lw / 2
            d.ellipse([x2 - cap, y2 - cap, x2 + cap, y2 + cap], fill=color)

    def _draw_moon(self, img, cx, cy, s, color):
        W, H = img.size
        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        rad = 4.6 * s
        md.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=255)
        ox, oy = 2.5 * s, -2.2 * s
        md.ellipse([cx - rad + ox, cy - rad + oy, cx + rad + ox, cy + rad + oy],
                   fill=0)
        layer = Image.new("RGBA", (W, H), color)
        layer.putalpha(mask)
        return Image.alpha_composite(img, layer)


class PixivClient:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.minsize(980, 640)
        self._center_window(1240, 820)

        self.style = root.style
        self.config = load_config()
        apply_proxy(self.config.get("proxy", ""))

        self.entries = []
        self.entries_by_pid = {}
        self.checked = set()
        self.loading = False
        self.downloading = False
        self.pool = None
        self._preview_ref = None  # 防止 PhotoImage 被垃圾回收
        self._preview_cache = {}
        self._hover_iid = None
        self.closing = False
        # 后台线程 → UI 线程的唯一通道。tkinter 不是线程安全的，
        # 工作线程只往队列里塞回调，由主线程定时取出执行。
        self.ui_queue = queue.Queue()

        self.save_dir = os.path.join(
            app_dir(), time.strftime("%Y-%m-%d") + "_PixivDailyPicture")

        self._build_ui()
        self._apply_theme()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(50, self._drain_ui_queue)
        self.refresh()

    def _center_window(self, w, h):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = min(w, max(900, sw - 80))
        h = min(h, max(600, sh - 80))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # ---------------- 平滑交互 ----------------
    def _smooth_scroll(self, widget, direction, steps=3):
        """把一次滚轮拆成几帧执行，滚动看起来更顺。"""
        def step(i=0):
            if i >= steps:
                return
            widget.yview_scroll(direction, "units")
            self.root.after(14, lambda: step(i + 1))
        step()

    def _on_tree_wheel(self, event):
        self._smooth_scroll(self.tree, -1 if event.delta > 0 else 1)
        return "break"

    def _on_log_wheel(self, event):
        self._smooth_scroll(self.log_text, -1 if event.delta > 0 else 1)
        return "break"

    def _hover_color(self):
        if "dark" in str(getattr(self.style.theme, "name", "")):
            return "#2f353b"
        return "#eaf1ff"

    def _apply_tree_style(self):
        try:
            self.style.configure("Treeview", rowheight=30, borderwidth=0,
                                 relief="flat")
            self.style.configure("Treeview.Heading", borderwidth=0,
                                 relief="flat")
            self.style.configure("TPanedwindow", sashwidth=6)
        except Exception:
            pass
        self.tree.tag_configure("hover", background=self._hover_color())

    def _on_tree_motion(self, event):
        iid = self.tree.identify_row(event.y)
        if iid == self._hover_iid:
            return
        self._clear_hover()
        self._hover_iid = iid
        if iid and iid not in self.tree.selection():
            self.tree.item(iid, tags=["hover"])

    def _clear_hover(self, _event=None):
        if self._hover_iid:
            try:
                tags = [t for t in self.tree.item(self._hover_iid, "tags")
                        if t != "hover"]
                self.tree.item(self._hover_iid, tags=tags)
            except tk.TclError:
                pass
        self._hover_iid = None

    def _start_indeterminate(self):
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

    def _stop_indeterminate(self):
        try:
            self.progress.stop()
        except Exception:
            pass
        self.progress.configure(mode="determinate")

    def _animate_progress(self, target):
        try:
            cur = float(self.progress.cget("value"))
        except Exception:
            cur = 0.0
        target = float(target)

        def step(i=0, steps=6):
            if i > steps:
                return
            self.progress.configure(value=cur + (target - cur) * (i / steps))
            self.root.after(18, lambda: step(i + 1, steps))
        step()

    # ---------------- 线程 → UI 队列 ----------------
    def _post(self, fn):
        """供后台线程调用：把回调排到主线程执行。"""
        self.ui_queue.put(fn)

    def _drain_ui_queue(self):
        if self.closing:
            return
        while True:
            try:
                fn = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:
                self.log(f"[界面错误] {e}")
        self.root.after(50, self._drain_ui_queue)

    # ---------------- 主题 ----------------
    def _current_dark(self):
        theme = self.config.get("theme", "system")
        if theme == "light":
            return False
        if theme == "dark":
            return True
        return system_is_dark()

    def _frame_bg(self):
        return getattr(self.style.colors, "bg", "#ffffff")

    def _apply_theme(self, animate_switch=False):
        dark = self._current_dark()
        try:
            self.style.theme_use(DARK_THEME if dark else LIGHT_THEME)
        except Exception:
            pass
        self._sync_text_colors()
        if hasattr(self, "tree"):
            self._apply_tree_style()
        if hasattr(self, "theme_switch"):
            self.theme_switch.set_palette(dark, self._frame_bg())
            self.theme_switch.set_dark(dark, animate=animate_switch)

    def _on_theme_switch(self, wants_dark):
        self.config["theme"] = "dark" if wants_dark else "light"
        save_config(self.config)
        self._apply_theme(animate_switch=True)

    def _sync_text_colors(self):
        colors = self.style.colors
        bg = getattr(colors, "bg", "#ffffff")
        fg = getattr(colors, "fg", "#212529")
        select_bg = getattr(colors, "selectbg", "#0d6efd")
        self.log_text.configure(
            bg=bg, fg=fg, insertbackground=fg,
            selectbackground=select_bg, selectforeground="#ffffff")

    # ---------------- 界面 ----------------
    def _build_ui(self):
        header = tb.Frame(self.root, padding=(14, 12, 14, 6))
        header.pack(fill="x")
        tb.Label(header, text=APP_NAME,
                 font=("Microsoft YaHei UI", 18, "bold"),
                 bootstyle="primary").pack(side="left")
        tb.Label(header, text="Pixiv 榜单浏览与下载 · 日榜 · 周榜 · 月榜 · 新人榜",
                 bootstyle="secondary").pack(side="left", padx=(12, 0), pady=(6, 0))

        # 右侧：主题分段按钮 + 设置
        tb.Button(header, text="设置", width=6, bootstyle="secondary-outline",
                  command=self.open_settings).pack(side="right")
        self.theme_switch = ThemeSwitch(
            header, dark=self._current_dark(), command=self._on_theme_switch,
            bg=self._frame_bg())
        self.theme_switch.pack(side="right", padx=(0, 12), pady=2)

        # ---- 第二行：榜单 + 保存目录 ----
        bar = tb.Frame(self.root, padding=(14, 0, 14, 8))
        bar.pack(fill="x")

        tb.Label(bar, text="榜单").pack(side="left", padx=(0, 6))
        self.mode_var = tk.StringVar(value=MODES[0][0])
        mode_box = tb.Combobox(bar, textvariable=self.mode_var, state="readonly",
                               width=8, values=[name for name, _ in MODES])
        mode_box.pack(side="left", padx=(0, 10))
        mode_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh())

        self.refresh_btn = tb.Button(bar, text="刷新", width=6,
                                     bootstyle="primary-outline",
                                     command=self.refresh)
        self.refresh_btn.pack(side="left", padx=(0, 16))

        tb.Label(bar, text="保存到").pack(side="left", padx=(0, 6))
        self.dir_var = tk.StringVar(value=self.save_dir)
        tb.Entry(bar, textvariable=self.dir_var).pack(
            side="left", fill="x", expand=True)
        tb.Button(bar, text="浏览…", bootstyle="secondary-outline",
                  command=self.choose_dir).pack(side="left", padx=(6, 0))

        # ---- 第三行：批量操作 ----
        actions = tb.Frame(self.root, padding=(14, 0, 14, 8))
        actions.pack(fill="x")
        tb.Button(actions, text="全选", bootstyle="secondary-outline",
                  command=self.check_all).pack(side="left")
        tb.Button(actions, text="全不选", bootstyle="secondary-outline",
                  command=self.uncheck_all).pack(side="left", padx=(6, 0))

        self.stop_btn = tb.Button(
            actions, text="停止下载", bootstyle="danger-outline",
            state="disabled", command=self.stop_download)
        self.stop_btn.pack(side="right", padx=(8, 0))
        self.download_btn = tb.Button(
            actions, text="下载选中", bootstyle="primary",
            command=self.start_download)
        self.download_btn.pack(side="right", padx=(8, 0))

        # ---- 主体：左列表 + 右预览 ----
        body = tb.Panedwindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        def _init_sash():
            try:
                width = body.winfo_width()
                if width > 100:
                    body.sashpos(0, int(width * 0.62))
            except Exception:
                pass
        self.root.after(150, _init_sash)

        left = tb.Frame(body)
        body.add(left, weight=3)

        columns = ("check", "rank", "title", "user", "pages")
        self.tree = tb.Treeview(left, columns=columns, show="headings",
                                selectmode="browse", bootstyle="primary")
        headings = {
            "check": ("", 36), "rank": ("#", 48), "title": ("标题", 430),
            "user": ("作者", 150), "pages": ("页数", 56),
        }
        for col, (text, width) in headings.items():
            self.tree.heading(col, text=text)
            self.tree.column(
                col, width=width,
                anchor="w" if col in ("title", "user") else "center",
                stretch=(col == "title"))
        yscroll = tb.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")

        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        self.tree.bind("<MouseWheel>", self._on_tree_wheel)
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Leave>", self._clear_hover)
        self.tree.bind("<space>", self._toggle_selected)
        self._apply_tree_style()

        right = tb.Frame(body, padding=(12, 0, 0, 0))
        body.add(right, weight=2)

        tb.Label(right, text="预览", bootstyle="secondary",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(
            anchor="w", pady=(0, 6))
        self.preview_label = tb.Label(right, text="选择左侧作品查看预览",
                                      anchor="center", justify="center")
        self.preview_label.pack(fill="both", expand=True)

        self.info_label = tb.Label(right, text="", anchor="nw", justify="left",
                                   wraplength=380)
        self.info_label.pack(fill="x", pady=(6, 0))
        right.bind("<Configure>", lambda e: self.info_label.configure(
            wraplength=max(220, e.width - 24)))

        # ---- 底部：进度 + 状态 + 日志 ----
        bottom = tb.Frame(self.root, padding=(14, 0, 14, 10))
        bottom.pack(fill="x")

        self.progress = tb.Progressbar(bottom, mode="determinate",
                                       bootstyle="success-striped")
        self.progress.pack(fill="x")
        # 两行状态：第一行勾选数量，第二行下载进度/状态，避免数字挤在一起
        self.checked_label = tb.Label(bottom, text="已勾选 0 个",
                                      bootstyle="secondary")
        self.checked_label.pack(anchor="w", pady=(4, 0))
        self.status_var = tk.StringVar(value="就绪")
        tb.Label(bottom, textvariable=self.status_var).pack(anchor="w", pady=(2, 4))

        tb.Label(bottom, text="日志", bootstyle="secondary",
                 font=("Microsoft YaHei UI", 9, "bold")).pack(
            anchor="w", pady=(6, 2))
        log_frame = tb.Frame(bottom)
        log_frame.pack(fill="x")
        self.log_text = tk.Text(log_frame, height=6, state="disabled",
                                wrap="word", relief="flat", borderwidth=0,
                                highlightthickness=0, padx=8, pady=6,
                                font=("Microsoft YaHei UI", 9))
        log_scroll = tb.Scrollbar(log_frame, orient="vertical",
                                  command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        self.log_text.bind("<MouseWheel>", self._on_log_wheel)

    # ---------------- 基础操作 ----------------
    def log(self, msg):
        def _do():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self._post(_do)

    def set_status(self, text):
        self._post(lambda: self.status_var.set(text))

    def _set_checked_label(self):
        self.checked_label.configure(text=f"已勾选 {len(self.checked)} 个")

    def choose_dir(self):
        path = filedialog.askdirectory(initialdir=self.save_dir)
        if path:
            self.save_dir = path
            self.dir_var.set(path)

    def current_mode(self):
        name = self.mode_var.get()
        for label, mode in MODES:
            if label == name:
                return mode
        return MODES[0][1]

    # ---------------- 设置 ----------------
    def open_settings(self):
        win = tb.Toplevel(self.root)
        win.title("设置")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        frame = tb.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)

        cur_source = next((lab for lab, val in SOURCE_CHOICES
                           if val == self.config.get("source")), SOURCE_CHOICES[0][0])
        source_var = tk.StringVar(value=cur_source)
        proxy_var = tk.StringVar(value=self.config.get("proxy", ""))
        api_var = tk.StringVar(value=self.config.get("mirror_api")
                               or DEFAULT_CONFIG["mirror_api"])
        img_var = tk.StringVar(value=self.config.get("mirror_img")
                               or DEFAULT_CONFIG["mirror_img"])
        status_var = tk.StringVar(value="")

        tb.Label(frame, text="数据源").grid(row=0, column=0, sticky="w", pady=6)
        tb.Combobox(frame, textvariable=source_var, state="readonly", width=22,
                    values=[lab for lab, _ in SOURCE_CHOICES]).grid(
            row=0, column=1, sticky="w", padx=(10, 0))

        tb.Label(frame, text="代理").grid(row=1, column=0, sticky="w", pady=6)
        tb.Entry(frame, textvariable=proxy_var, width=38).grid(
            row=1, column=1, sticky="we", padx=(10, 6))
        tb.Button(frame, text="测试", bootstyle="secondary-outline",
                  command=lambda: self._test_proxy(proxy_var.get(), status_var)).grid(
            row=1, column=2, sticky="w")

        tb.Label(frame, text="镜像榜单接口").grid(row=2, column=0, sticky="w", pady=6)
        tb.Entry(frame, textvariable=api_var, width=38).grid(
            row=2, column=1, columnspan=2, sticky="we", padx=(10, 0))

        tb.Label(frame, text="镜像图片域名").grid(row=3, column=0, sticky="w", pady=6)
        tb.Entry(frame, textvariable=img_var, width=38).grid(
            row=3, column=1, columnspan=2, sticky="we", padx=(10, 0))

        tb.Label(frame, text="镜像测试").grid(row=4, column=0, sticky="w", pady=6)
        tb.Button(frame, text="测试镜像", bootstyle="secondary-outline",
                  command=lambda: self._test_mirror(
                      api_var.get(), img_var.get(), proxy_var.get(),
                      status_var)).grid(
            row=4, column=1, columnspan=2, sticky="w", padx=(10, 0))

        tb.Label(frame, text="代理示例：http://127.0.0.1:7890 或 socks5://127.0.0.1:7891",
                 bootstyle="secondary").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
        tb.Label(frame, text="镜像地址留空会自动恢复默认值",
                 bootstyle="secondary").grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(2, 0))
        tb.Label(frame, textvariable=status_var, bootstyle="info",
                 wraplength=420, justify="left").grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(4, 0))

        btns = tb.Frame(frame)
        btns.grid(row=8, column=0, columnspan=3, sticky="we", pady=(12, 0))

        def reset_mirror():
            api_var.set(DEFAULT_CONFIG["mirror_api"])
            img_var.set(DEFAULT_CONFIG["mirror_img"])
            status_var.set("镜像地址已恢复默认，点保存后生效")

        tb.Button(btns, text="恢复默认", bootstyle="secondary-outline",
                  command=reset_mirror).pack(side="left")

        def do_save():
            label_to_value = {lab: val for lab, val in SOURCE_CHOICES}
            self.config["source"] = label_to_value.get(source_var.get(), "auto")
            self.config["proxy"] = proxy_var.get().strip()
            self.config["mirror_api"] = normalize_url(
                api_var.get(), DEFAULT_CONFIG["mirror_api"])
            self.config["mirror_img"] = normalize_url(
                img_var.get(), DEFAULT_CONFIG["mirror_img"])
            save_config(self.config)
            apply_proxy(self.config["proxy"])
            self._apply_theme()
            win.destroy()
            self.log(f"设置已保存：数据源={source_var.get()}，代理="
                     f"{self.config['proxy'] or '直连'}")
            self.refresh()

        tb.Button(btns, text="保存", bootstyle="primary",
                  command=do_save).pack(side="right")
        tb.Button(btns, text="取消", bootstyle="secondary-outline",
                  command=win.destroy).pack(side="right", padx=(0, 8))

    def _test_mirror(self, api_url, img_url, proxy, status_var):
        """测试镜像榜单接口和图片域名（用榜单里的真实图片检测是否卡住）。"""
        api = normalize_url(api_url, DEFAULT_CONFIG["mirror_api"])
        primary = normalize_url(img_url, DEFAULT_CONFIG["mirror_img"])
        mirrors = image_mirror_candidates(primary)
        proxy = (proxy or "").strip()

        def work():
            s = requests.Session()
            if proxy:
                s.proxies.update({"http": proxy, "https": proxy})
            parts = []
            sample = None
            # 榜单接口：用昨天的日期，当天的榜单常常还没生成
            try:
                date = time.strftime("%Y-%m-%d",
                                     time.localtime(time.time() - 86400))
                r = s.get(f"{api}/ranks?date={date}&mode=day&page=1&size=30",
                          headers=core.HEADERS, verify=False, timeout=10)
                data = (r.json() or {}).get("data") or []
                if r.status_code < 400 and data:
                    parts.append(f"榜单接口 OK（{len(data)} 条）")
                    for item in data:
                        urls = item.get("imageUrls") or []
                        if urls and urls[0].get("original"):
                            sample = urls[0]["original"]
                            break
                else:
                    parts.append(f"榜单接口异常（HTTP {r.status_code}）")
            except Exception as e:
                parts.append(f"榜单接口失败：{e}")

            # 图片：读取最多 512 KB，确认不是“只回开头一点就卡住”
            last = None
            used = None
            if sample:
                for base in mirrors:
                    try:
                        target = rewrite_image_host(sample, base)
                        rr = s.get(target, headers=core.HEADERS, verify=False,
                                   timeout=(10, 20), stream=True)
                        if rr.status_code >= 400:
                            last = f"{base} 返回 HTTP {rr.status_code}"
                            rr.close()
                            continue
                        total = 0
                        try:
                            for chunk in rr.iter_content(64 * 1024):
                                total += len(chunk)
                                if total >= 512 * 1024:
                                    break
                        finally:
                            rr.close()
                        if total > 0:
                            used = (base, total)
                            break
                        last = f"{base} 未返回数据"
                    except Exception as e:
                        last = f"{base}：{e}"
                if used:
                    base, total = used
                    note = "" if base == primary else "，主域名失败已回退"
                    parts.append(
                        f"图片测试 OK（{base}，已读取 {max(1, total // 1024)} KB{note}）")
                else:
                    parts.append(f"图片测试失败：{last}")
            else:
                try:
                    rr = s.get(primary + "/", headers=core.HEADERS,
                               verify=False, timeout=10)
                    if rr.status_code < 500:
                        parts.append(f"图片域名 OK（HTTP {rr.status_code}）")
                    else:
                        parts.append(f"图片域名异常（HTTP {rr.status_code}）")
                except Exception as e:
                    parts.append(f"图片域名失败：{e}")
            s.close()
            self._post(lambda: status_var.set("；".join(parts)))

        status_var.set("测试中…")
        threading.Thread(target=work, daemon=True).start()

    def _test_proxy(self, proxy, status_var):
        proxy = (proxy or "").strip()

        def work():
            try:
                s = requests.Session()
                if proxy:
                    s.proxies.update({"http": proxy, "https": proxy})
                r = s.get("https://www.pixiv.net/robots.txt", timeout=8)
                msg = (f"连接成功（HTTP {r.status_code}）" if r.status_code < 500
                       else f"目标返回 HTTP {r.status_code}")
                s.close()
            except Exception as e:
                msg = f"连接失败：{e}"
            self._post(lambda: status_var.set(msg))

        status_var.set("测试中…")
        threading.Thread(target=work, daemon=True).start()

    # ---------------- 数据源选择 ----------------
    def resolve_source(self):
        """返回 'official' 或 'mirror'；自动模式会先探测官方是否可达。"""
        configured = self.config.get("source", "auto")
        if configured == "official":
            return "official"
        if configured == "mirror":
            return "mirror"
        return "official" if probe_official() else "mirror"

    # ---------------- 列表与勾选 ----------------
    def refresh(self):
        if self.loading or self.downloading:
            return
        self.loading = True
        self.refresh_btn.configure(state="disabled")
        self.clear_tree()
        self.checked.clear()
        self._preview_cache.clear()
        self._set_checked_label()
        self.status_var.set("正在获取榜单…")
        self._start_indeterminate()
        mode = self.current_mode()
        mode_label = self.mode_var.get()
        self.log(f"获取{mode_label}…")

        def work():
            try:
                source = self.resolve_source()
                if source == "mirror":
                    entries = fetch_ranking_mirror(
                        mode, self.config.get("mirror_api", ""),
                        self.config.get("mirror_img", ""))
                    source_label = "镜像"
                else:
                    entries = fetch_ranking(mode)
                    source_label = "官方"
                self._post(lambda: self.render_entries(entries, source_label))
            except Exception as e:
                err = str(e)
                self._post(lambda err=err: self.on_refresh_error(err))

        threading.Thread(target=work, daemon=True).start()

    def on_refresh_error(self, err):
        self.loading = False
        self._stop_indeterminate()
        self.refresh_btn.configure(state="normal")
        self.status_var.set("获取榜单失败")
        self.log(f"[错误] 获取榜单失败：{err}")
        messagebox.showerror("获取失败", f"无法获取榜单：\n{err}")

    def clear_tree(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)

    def render_entries(self, entries, source_label):
        self.loading = False
        self._stop_indeterminate()
        self.refresh_btn.configure(state="normal")
        self.entries = entries
        self.entries_by_pid = {e["pid"]: e for e in entries}
        for e in entries:
            self.tree.insert("", "end", iid=e["pid"], values=(
                UNCHECKED,
                e["rank"] if e["rank"] is not None else "",
                e["title"],
                e["user"],
                e["pages"],
            ))
        self.progress.configure(maximum=max(1, len(entries)), value=0)
        self.status_var.set(f"已加载 {len(entries)} 个作品（{source_label}源）")
        self.log(f"已加载 {len(entries)} 个作品（{source_label}源）")

    def on_tree_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        if col != "#1":
            return
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self._toggle_row(iid)
        return "break"

    def _toggle_row(self, iid):
        if iid in self.checked:
            self.checked.discard(iid)
            glyph = UNCHECKED
        else:
            self.checked.add(iid)
            glyph = CHECKED
        values = list(self.tree.item(iid, "values"))
        values[0] = glyph
        self.tree.item(iid, values=values)
        # 勾选数量只更新固定标签，不能覆盖下载进度状态
        self._set_checked_label()

    def _toggle_selected(self, _event=None):
        sel = self.tree.selection()
        if sel:
            self._toggle_row(sel[0])
        return "break"

    def check_all(self):
        self.checked = set(self.entries_by_pid)
        for pid in self.checked:
            values = list(self.tree.item(pid, "values"))
            values[0] = CHECKED
            self.tree.item(pid, values=values)
        self._set_checked_label()

    def uncheck_all(self):
        for pid in self.checked:
            values = list(self.tree.item(pid, "values"))
            values[0] = UNCHECKED
            self.tree.item(pid, values=values)
        self.checked.clear()
        self._set_checked_label()

    # ---------------- 预览 ----------------
    def on_tree_select(self, _event):
        self._clear_hover()
        sel = self.tree.selection()
        if not sel:
            return
        pid = sel[0]
        entry = self.entries_by_pid.get(pid)
        if not entry:
            return
        self.info_label.configure(text=(
            f"PID: {entry['pid']}\n"
            f"排名: {entry['rank']}\n"
            f"标题: {entry['title']}\n"
            f"作者: {entry['user']}\n"
            f"页数: {entry['pages']}\n"
            f"标签: {', '.join(entry['tags']) if entry['tags'] else '—'}"
        ))
        cached = self._preview_cache.get(pid)
        if cached is not None:
            self._preview_ref = cached
            self.preview_label.configure(image=cached, text="")
            return
        self.preview_label.configure(text="加载预览中…", image="")
        # 镜像条目：优先用原始地址 + 主/备域名依次尝试
        if entry.get("mirrored") and entry.get("originals"):
            candidates = [rewrite_image_host(entry["originals"][0], base)
                          for base in image_mirror_candidates(
                              self.config.get("mirror_img", ""))]
        else:
            thumb = entry.get("thumb")
            candidates = [thumb] if thumb else []
        if not candidates:
            self.preview_label.configure(text="（无缩略图）")
            return

        def work():
            last_err = None
            for url in candidates:
                try:
                    r = core.session.get(url, headers=core.HEADERS,
                                         verify=False, timeout=(10, 60))
                    r.raise_for_status()
                    img = Image.open(BytesIO(r.content))
                    img.thumbnail((440, 580))
                    # PhotoImage 必须回到主线程创建，否则 Tk 会报错
                    self._post(lambda img=img: self.show_preview(pid, img))
                    return
                except Exception as e:
                    last_err = e
                    core.session.close()  # 丢弃可能卡住的连接
            err = str(last_err)
            self._post(lambda err=err, n=len(candidates): self.preview_label.configure(
                text=f"预览加载失败（已尝试 {n} 个镜像域名）：{err}"))

        threading.Thread(target=work, daemon=True).start()

    def show_preview(self, pid, img):
        # 用户可能已经点了别的行，避免旧请求覆盖
        sel = self.tree.selection()
        if not sel or sel[0] != pid:
            return
        photo = ImageTk.PhotoImage(img)
        self._preview_cache[pid] = photo
        self._preview_ref = photo
        self.preview_label.configure(image=photo, text="")

    # ---------------- 下载 ----------------
    def start_download(self):
        if self.downloading or self.loading:
            return
        if not self.entries:
            messagebox.showinfo("提示", "列表为空，先刷新榜单。")
            return
        targets = [e for e in self.entries if e["pid"] in self.checked]
        if not targets:
            messagebox.showinfo("提示", "还没有勾选任何作品。")
            return

        # 镜像源下载时把 i.pximg.net 改写到配置的图片镜像域名
        image_mirror = None
        if any(e.get("mirrored") for e in targets):
            image_mirror = self.config.get("mirror_img", "")
        self._begin_download(targets, image_mirror)

    def _begin_download(self, targets, image_mirror=None):
        # 以输入框里的路径为准（用户可能手动改过）
        folder = self.dir_var.get().strip() or self.save_dir
        self.save_dir = folder
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            messagebox.showerror("目录不可用", f"无法创建保存目录：\n{folder}\n{e}")
            return

        self.downloading = True
        self.stop_event = threading.Event()
        self.stop_btn.configure(state="normal", text="停止下载")
        self.download_btn.configure(state="disabled")
        self.refresh_btn.configure(state="disabled")
        self.progress.configure(maximum=len(targets), value=0)
        self.status_var.set(f"开始下载 {len(targets)} 个作品…")
        self.log(f"保存目录：{self.save_dir}")
        self.log(f"开始下载 {len(targets)} 个作品，并发 {core.WORKERS}")

        done = {"count": 0, "images": 0, "fail": 0}

        def work():
            self.pool = ThreadPoolExecutor(max_workers=core.WORKERS)
            futures = {self.pool.submit(download_entry, e, self.save_dir,
                                        image_mirror, self.stop_event): e["pid"]
                       for e in targets}
            for fut in as_completed(futures):
                if fut.cancelled():
                    continue
                pid = futures[fut]
                try:
                    result = fut.result()
                    ok, total = result[0], result[1]
                    errs = result[2] if len(result) > 2 else []
                    done["images"] += ok
                    if errs:
                        msg = (f"PID {pid}: {ok}/{total} 张完成，"
                               f"失败 {len(errs)} 张（{errs[0]}）")
                    else:
                        msg = f"PID {pid}: {ok}/{total} 张完成"
                except Exception as e:
                    done["fail"] += 1
                    msg = f"PID {pid}: 失败 {e}"
                done["count"] += 1
                if not self.stop_event.is_set():
                    self._post(lambda m=msg, c=done["count"]: self.on_one_done(c, m))
            if self.stop_event.is_set():
                self._post(self.on_download_stopped)
            else:
                self._post(lambda f=done["fail"]: self.on_download_done(f))

        threading.Thread(target=work, daemon=True).start()

    def stop_download(self):
        """请求停止：排队任务会直接跳过，正在下载的图片下完当前这张后结束。"""
        if not self.downloading:
            return
        self.stop_event.set()
        self.stop_btn.configure(state="disabled", text="正在停止…")
        self.status_var.set("正在停止下载…")
        self.log("正在停止下载…（当前图片下载完后停止）")

    def on_one_done(self, count, msg):
        self._animate_progress(count)
        self.status_var.set(f"下载中 {count}/{int(float(self.progress.cget('maximum')))}")
        self.log(msg)

    def on_download_done(self, fails=0):
        if fails:
            self._finish_download(f"下载完成（{fails} 个失败）",
                                  f"下载完成，{fails} 个作品失败")
        else:
            self._finish_download("下载完成", "全部下载完成")

    def on_download_stopped(self):
        self._finish_download("下载已停止", "下载已停止")

    def _finish_download(self, status, log_msg):
        self.downloading = False
        self.stop_btn.configure(state="disabled", text="停止下载")
        self.download_btn.configure(state="normal")
        self.refresh_btn.configure(state="normal")
        self.status_var.set(status)
        self.log(log_msg)

    # ---------------- 关闭 ----------------
    def on_close(self):
        if self.downloading:
            if not messagebox.askyesno("正在下载", "下载尚未完成，确定退出吗？"):
                return
            self.closing = True
            if self.pool:
                self.pool.shutdown(wait=False, cancel_futures=True)
            self.root.destroy()
            # 线程池里的线程不是守护线程，直接结束进程避免卡在退出
            os._exit(0)
        self.closing = True
        self.root.destroy()


def run_selftest():
    """打包后自检：验证镜像接口在 exe 内可用，结果写到 exe 同目录。"""
    lines = []
    try:
        entries = fetch_ranking_mirror(
            "daily", DEFAULT_CONFIG["mirror_api"],
            DEFAULT_CONFIG["mirror_img"], limit=30)
        lines.append(f"mirror_entries={len(entries)}")
        if entries:
            lines.append(f"first_pid={entries[0]['pid']}")
    except Exception as e:
        lines.append(f"mirror_error={type(e).__name__}: {e}")
    try:
        with open(os.path.join(app_dir(), "selftest.txt"), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def main():
    if "--selftest" in sys.argv:
        run_selftest()
        return
    dark = system_is_dark()
    root = tb.Window(themename=DARK_THEME if dark else LIGHT_THEME)
    try:
        ico = resource_path("pixiv.ico")
        if os.path.exists(ico):
            root.iconbitmap(ico)
    except Exception:
        pass
    PixivClient(root)
    root.mainloop()


if __name__ == "__main__":
    main()
