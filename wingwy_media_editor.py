#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
wingwy音视频剪辑器  (wingwy Media Editor)
  Tab 1  音视频转换  —— 单个/批量转换、合并音频/字幕、可编辑命令模板
  Tab 2  裁剪拼接    —— 播放器式预览 + 可缩放时间轴 + 入出点标记 + 剪断/删除 + 多文件拼接 + 导出

依赖：ffmpeg.exe / ffprobe.exe / ffplay.exe（放在程序同目录，或系统 PATH 里；
      也可在程序右上角点「获取 FFmpeg」自动下载安装）
运行：python wingwy_media_editor.py
"""

import base64
import bisect
import configparser
import copy
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
import zipfile
from collections import deque
from tkinter import ttk, filedialog, messagebox

APP_NAME = "wingwy音视频剪辑器"
APP_VERSION = "1.0.0"

# FFmpeg 自动下载（Windows 官方推荐构建：gyan.dev）
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_VER_URL = "https://www.gyan.dev/ffmpeg/builds/release-version"

# ---------------------------
# 1. Windows 高 DPI 支持
# ---------------------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def app_dir():
    """程序所在目录（打包成 exe 时取 exe 目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = app_dir()


def tool_path(name):
    """优先使用程序目录下的 ffmpeg / ffprobe / ffplay，其次系统 PATH。"""
    for cand in (os.path.join(APP_DIR, name + ".exe"), os.path.join(APP_DIR, name)):
        if os.path.exists(cand):
            return cand
    found = shutil.which(name)
    if found:
        return found
    return name + (".exe" if os.name == "nt" else "")


FFMPEG = tool_path("ffmpeg")
FFPROBE = tool_path("ffprobe")
FFPLAY = tool_path("ffplay")


def refresh_tools():
    """重新解析工具路径（下载/更新 ffmpeg 之后调用）。"""
    global FFMPEG, FFPROBE, FFPLAY
    FFMPEG = tool_path("ffmpeg")
    FFPROBE = tool_path("ffprobe")
    FFPLAY = tool_path("ffplay")


def ffmpeg_version(path=None):
    """返回版本号字符串，例如 '9.0.2-full_build-www.gyan.dev'；失败返回 None。"""
    path = path or FFMPEG
    try:
        p = subprocess.run([path, "-version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           creationflags=CREATE_NO_WINDOW, timeout=8)
        first = (p.stdout or "").splitlines()[0] if p.stdout else ""
        m = re.search(r"ffmpeg version (\S+)", first)
        if m:
            return m.group(1)
        return first.strip()[:60] or None
    except Exception:
        return None


def detect_ffmpeg():
    """返回 (版本, 来源说明, 可执行路径)；未检测到返回 (None, None, None)。"""
    local = os.path.join(APP_DIR, "ffmpeg.exe")
    if os.path.exists(local):
        v = ffmpeg_version(local)
        if v:
            return v, "内置", local
    found = shutil.which("ffmpeg")
    if found:
        v = ffmpeg_version(found)
        if v:
            return v, "系统 PATH", found
    return None, None, None

# ---------------------------
# 2. 配置文件（config.ini）
# ---------------------------
CONFIG_FILE = os.path.join(APP_DIR, "config.ini")

AUDIO_EXTS = (".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg", ".opus",
              ".wma", ".ac3", ".eac3", ".dts", ".amr", ".ape")
SUBTITLE_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup", ".ttml", ".txt")
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".m4v", ".ts", ".webm")


def load_config():
    config = configparser.ConfigParser()
    defaults = {
        'video_encodings': {
            'hevc_nvenc': '-c:v hevc_nvenc',
            'h264_nvenc': '-c:v h264_nvenc',
            'av1_nvenc': '-c:v av1_nvenc',
            'libx264': '-c:v libx264',
            'libx265': '-c:v libx265',
            'libaom-av1': '-c:v libaom-av1'
        },
        'audio_encodings': {
            'aac 128k': '-c:a aac -b:a 128k',
            'aac 256k': '-c:a aac -b:a 256k',
            'aac 64k': '-c:a aac -b:a 64k',
            'aac 320k': '-c:a aac -b:a 320k'
        },
        'bitrates': {
            '4M': '4M',
            '10M': '10M',
            '1000k': '1000k',
            '2000k': '2000k',
            '3000k': '3000k'
        },
        'resolutions': {
            'original': '',
            '720p': '1280x720',
            '1080p': '1920x1080',
            '4k': '3840x2160'
        }
    }
    if os.path.exists(CONFIG_FILE):
        try:
            config.read(CONFIG_FILE, encoding='utf-8')
        except Exception as e:
            messagebox.showerror("配置文件错误", f"读取配置文件出错：{e}")
            config.read_dict(defaults)
    else:
        config.read_dict(defaults)
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                config.write(f)
        except Exception as e:
            messagebox.showwarning("警告", f"写入默认配置文件失败：{e}")
    return config


config = load_config()

AUDIO_BITRATE_CHOICES = ['64k', '96k', '128k', '192k', '256k', '320k']

# 封装格式：显示名 -> 扩展名
CONTAINER_EXTS = {
    "mp4": ".mp4", "mkv": ".mkv", "mov": ".mov", "webm": ".webm", "ts": ".ts",
    "mp3": ".mp3", "m4a": ".m4a", "wav": ".wav", "flac": ".flac", "aac": ".aac",
    "ogg": ".ogg", "opus": ".opus", "mka": ".mka", "flv": ".flv", "avi": ".avi",
}
VIDEO_CONTAINERS = ["mp4", "mkv", "mov", "webm", "ts"]
AUDIO_CONTAINERS = ["mp3", "m4a", "wav", "flac", "aac", "ogg"]

# 流复制时，按“实际编码”挑能装的容器（扩展名不可信，.mp3 里装 AAC 很常见）
AUDIO_COPY_FMT = {
    "aac": "m4a", "alac": "m4a", "mp3": "mp3", "mp2": "mp3",
    "flac": "flac", "opus": "opus", "vorbis": "ogg", "speex": "ogg",
}
VIDEO_COPY_FMT = {
    "mov": "mp4", "mp4": "mp4", "m4a": "mp4", "matroska": "mkv", "webm": "webm",
    "mpegts": "ts", "flv": "flv", "avi": "avi", "asf": "wmv",
}


def audio_copy_fmt(codec):
    codec = (codec or "").lower()
    if codec in AUDIO_COPY_FMT:
        return AUDIO_COPY_FMT[codec]
    if codec.startswith("pcm_"):
        return "wav"
    return "mka"


# ---------------------------
# 3. 公共小工具
# ---------------------------
def fmt_tc(seconds, ms=True):
    """秒 -> 00:00:00.000"""
    if seconds is None:
        seconds = 0.0
    neg = seconds < 0
    seconds = abs(float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if ms:
        return "%s%02d:%02d:%06.3f" % ("-" if neg else "", h, m, s)
    return "%s%02d:%02d:%02d" % ("-" if neg else "", h, m, int(s))


def probe_media(path):
    """读取媒体信息；失败返回 None。"""
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=CREATE_NO_WINDOW)
        data = json.loads(p.stdout or "{}")
    except Exception:
        return None

    streams = data.get("streams") or []
    v = None
    for s in streams:
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            v = s
            break
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None and a is None:
        return None

    dur = 0.0
    try:
        dur = float((data.get("format") or {}).get("duration") or 0)
    except Exception:
        dur = 0.0
    if dur <= 0:
        for s in (v, a):
            try:
                dur = float((s or {}).get("duration") or 0)
            except Exception:
                dur = 0.0
            if dur > 0:
                break

    fps = 25.0
    if v is not None:
        for key in ("avg_frame_rate", "r_frame_rate"):
            fr = (v.get(key) or "").strip()
            if "/" in fr:
                try:
                    n, d = fr.split("/")[:2]
                    n, d = float(n), float(d)
                    if n > 0 and d > 0:
                        fps = n / d
                        break
                except Exception:
                    pass

    rot = 0
    if v is not None:
        for sd in (v.get("side_data_list") or []):
            if "rotation" in sd:
                try:
                    rot = int(float(sd["rotation"]))
                except Exception:
                    pass
        if not rot:
            try:
                rot = int(float((v.get("tags") or {}).get("rotate", 0)))
            except Exception:
                pass

    w = int((v or {}).get("width") or 0)
    h = int((v or {}).get("height") or 0)
    if abs(rot) % 180 == 90:
        w, h = h, w

    return {
        "path": path,
        "duration": max(dur, 0.001),
        "w": max(w, 0),
        "h": max(h, 0),
        "fps": fps if fps > 0 else 25.0,
        "has_video": v is not None,
        "has_audio": a is not None,
        "vcodec": (v or {}).get("codec_name") or "",
        "acodec": (a or {}).get("codec_name") or "",
        "sample_rate": int((a or {}).get("sample_rate") or 0),
        "channels": int((a or {}).get("channels") or 0),
        "format_name": (data.get("format") or {}).get("format_name") or "",
        "rotation": rot,
    }


class FileDrop:
    """Windows 文件拖放：给窗口挂 WM_DROPFILES 钩子（不依赖第三方库）。"""

    WM_DROPFILES = 0x0233
    GWLP_WNDPROC = -4

    def __init__(self, root, callback):
        self.root = root
        self.callback = callback
        self.ok = False
        self._old = None
        self._new = None
        self._pending = []
        self._lock = threading.Lock()
        if os.name != "nt":
            return
        try:
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            hwnd = int(root.winfo_id())
            proto = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p,
                                       ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
            self._new = proto(self._wndproc)
            set_ptr = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
            set_ptr.restype = ctypes.c_void_p
            set_ptr.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            self._old = set_ptr(ctypes.c_void_p(hwnd), self.GWLP_WNDPROC,
                                ctypes.cast(self._new, ctypes.c_void_p))
            user32.CallWindowProcW.restype = ctypes.c_ssize_t
            user32.CallWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
            shell32.DragQueryFileW.restype = ctypes.c_uint
            shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                               ctypes.c_wchar_p, ctypes.c_uint]
            shell32.DragFinish.argtypes = [ctypes.c_void_p]
            shell32.DragAcceptFiles.argtypes = [ctypes.c_void_p, ctypes.c_bool]
            shell32.DragAcceptFiles(ctypes.c_void_p(hwnd), True)
            self._hwnd = hwnd
            self.ok = True
        except Exception:
            self.ok = False
        if self.ok:
            self.root.after(200, self._poll)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == self.WM_DROPFILES:
            try:
                # 只做纯 ctypes 操作，绝不在这里碰 Tk（会破坏 Tcl 事件循环）
                self._collect(wparam)
            except Exception:
                pass
            return 0
        return ctypes.windll.user32.CallWindowProcW(
            ctypes.c_void_p(self._old), ctypes.c_void_p(hwnd), msg, wparam, lparam)

    def _collect(self, hdrop):
        shell32 = ctypes.windll.shell32
        n = shell32.DragQueryFileW(ctypes.c_void_p(hdrop), 0xFFFFFFFF, None, 0)
        paths = []
        for i in range(n):
            ln = shell32.DragQueryFileW(ctypes.c_void_p(hdrop), i, None, 0)
            buf = ctypes.create_unicode_buffer(ln + 1)
            shell32.DragQueryFileW(ctypes.c_void_p(hdrop), i, buf, ln + 1)
            if buf.value:
                paths.append(buf.value)
        shell32.DragFinish(ctypes.c_void_p(hdrop))
        if paths:
            with self._lock:
                self._pending.extend(paths)

    def _poll(self):
        paths = []
        with self._lock:
            if self._pending:
                paths = self._pending[:]
                self._pending.clear()
        if paths:
            try:
                self.callback(paths)
            except Exception:
                pass
        try:
            self.root.after(200, self._poll)
        except Exception:
            pass


def probe_keyframes(path):
    """取视频关键帧时间点（秒）。流复制剪切只能切在关键帧上。"""
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
           "-show_entries", "frame=pts_time", "-of", "csv=p=0", path]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=CREATE_NO_WINDOW)
    except Exception:
        return []
    out = []
    for line in (p.stdout or "").splitlines():
        t = line.strip().split(",")[0]
        try:
            out.append(float(t))
        except ValueError:
            pass
    return out


def read_exact(stream, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def read_png_frame(stream):
    """从管道里读出一整帧 PNG（按块解析，直到 IEND）。"""
    sig = read_exact(stream, 8)
    if not sig or sig != b"\x89PNG\r\n\x1a\n":
        return None
    out = bytearray(sig)
    while True:
        hdr = read_exact(stream, 8)
        if not hdr:
            return None
        ln = int.from_bytes(hdr[:4], "big")
        ctype = hdr[4:8]
        body = read_exact(stream, ln + 4)
        if body is None:
            return None
        out += hdr + body
        if ctype == b"IEND":
            break
    return bytes(out)


# ---------------------------
# 4. Tab 1：音视频转换（原 ffmpeg_gui6 功能，逻辑保持不变）
# ---------------------------
class ConvertTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self._last_template = ""
        self.create_widgets()

    def create_widgets(self):
        # ---- 转换模式 ----
        mode_frame = ttk.LabelFrame(self, text="转换模式")
        mode_frame.pack(fill="x", padx=10, pady=5)
        self.conversion_mode = tk.StringVar(value="single")
        ttk.Radiobutton(mode_frame, text="单个文件转换", variable=self.conversion_mode,
                        value="single").pack(side="left", padx=5, pady=5)
        ttk.Radiobutton(mode_frame, text="文件夹批量转换", variable=self.conversion_mode,
                        value="folder").pack(side="left", padx=5, pady=5)

        # ---- 路径设置 ----
        path_frame = ttk.LabelFrame(self, text="路径设置")
        path_frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(path_frame, text="输入路径:").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        self.input_path = tk.StringVar()
        ttk.Entry(path_frame, textvariable=self.input_path, width=80).grid(row=0, column=1, padx=5, pady=5)
        ttk.Button(path_frame, text="浏览", command=self.browse_input_path).grid(row=0, column=2, padx=5, pady=5)

        ttk.Label(path_frame, text="输出路径:").grid(row=1, column=0, padx=5, pady=5, sticky="e")
        self.output_path = tk.StringVar()
        ttk.Entry(path_frame, textvariable=self.output_path, width=80).grid(row=1, column=1, padx=5, pady=5)
        ttk.Button(path_frame, text="浏览", command=self.browse_output_path).grid(row=1, column=2, padx=5, pady=5)

        self.use_audio_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(path_frame, text="音频输入:", variable=self.use_audio_var,
                        command=self.toggle_audio_input).grid(row=2, column=0, padx=5, pady=5, sticky="e")
        self.audio_input_path = tk.StringVar()
        self.audio_entry = ttk.Entry(path_frame, textvariable=self.audio_input_path, width=80, state=tk.DISABLED)
        self.audio_entry.grid(row=2, column=1, padx=5, pady=5)
        self.audio_browse = ttk.Button(path_frame, text="浏览", command=self.browse_audio_path, state=tk.DISABLED)
        self.audio_browse.grid(row=2, column=2, padx=5, pady=5)

        self.use_sub_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(path_frame, text="字幕输入:", variable=self.use_sub_var,
                        command=self.toggle_sub_input).grid(row=3, column=0, padx=5, pady=5, sticky="e")
        self.sub_input_path = tk.StringVar()
        self.sub_entry = ttk.Entry(path_frame, textvariable=self.sub_input_path, width=80, state=tk.DISABLED)
        self.sub_entry.grid(row=3, column=1, padx=5, pady=5)
        self.sub_browse = ttk.Button(path_frame, text="浏览", command=self.browse_sub_path, state=tk.DISABLED)
        self.sub_browse.grid(row=3, column=2, padx=5, pady=5)

        ttk.Label(path_frame, text="说明：勾选后才会合并音频/字幕；批量模式下也可填写文件夹，按同名文件自动匹配。",
                  foreground="#666666").grid(row=4, column=1, padx=5, pady=(0, 5), sticky="w")

        # ---- 编码设置 ----
        settings_frame = ttk.LabelFrame(self, text="编码设置")
        settings_frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(settings_frame, text="视频编码:").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        self.video_codec_var = tk.StringVar()
        video_codec_list = list(config["video_encodings"].keys())
        self.video_codec_combo = ttk.Combobox(settings_frame, textvariable=self.video_codec_var,
                                              values=video_codec_list, state="readonly", width=20)
        self.video_codec_combo.current(0)
        self.video_codec_combo.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        ttk.Label(settings_frame, text="音频编码:").grid(row=0, column=2, padx=5, pady=5, sticky="e")
        self.audio_codec_var = tk.StringVar()
        audio_codec_list = list(config["audio_encodings"].keys())
        self.audio_codec_combo = ttk.Combobox(settings_frame, textvariable=self.audio_codec_var,
                                              values=audio_codec_list, state="readonly", width=20)
        self.audio_codec_combo.current(0)
        self.audio_codec_combo.grid(row=0, column=3, padx=5, pady=5, sticky="w")

        ttk.Label(settings_frame, text="视频码率:").grid(row=1, column=0, padx=5, pady=5, sticky="e")
        self.bitrate_var = tk.StringVar()
        bitrate_list = list(config["bitrates"].keys())
        self.bitrate_combo = ttk.Combobox(settings_frame, textvariable=self.bitrate_var,
                                          values=bitrate_list, state="normal", width=20)
        self.bitrate_combo.current(0)
        self.bitrate_combo.grid(row=1, column=1, padx=5, pady=5, sticky="w")

        ttk.Label(settings_frame, text="音频码率:").grid(row=1, column=2, padx=5, pady=5, sticky="e")
        self.audio_bitrate_var = tk.StringVar(value="128k")
        self.audio_bitrate_combo = ttk.Combobox(settings_frame, textvariable=self.audio_bitrate_var,
                                                values=AUDIO_BITRATE_CHOICES, state="normal", width=20)
        self.audio_bitrate_combo.grid(row=1, column=3, padx=5, pady=5, sticky="w")

        ttk.Label(settings_frame, text="分辨率:").grid(row=2, column=0, padx=5, pady=5, sticky="e")
        self.resolution_var = tk.StringVar()
        resolution_list = list(config["resolutions"].keys())
        self.resolution_combo = ttk.Combobox(settings_frame, textvariable=self.resolution_var,
                                             values=resolution_list, state="normal", width=20)
        self.resolution_combo.current(0)
        self.resolution_combo.grid(row=2, column=1, padx=5, pady=5, sticky="w")
        ttk.Label(settings_frame, text="（可手填，如 1920x1080；填 original 表示不缩放）",
                  foreground="#666666").grid(row=2, column=2, columnspan=2, padx=5, pady=5, sticky="w")

        ttk.Label(settings_frame, text="其它参数:").grid(row=3, column=0, padx=5, pady=5, sticky="e")
        self.extra_params_var = tk.StringVar()
        ttk.Entry(settings_frame, textvariable=self.extra_params_var, width=80).grid(
            row=3, column=1, columnspan=3, padx=5, pady=5, sticky="w")

        # ---- 按钮 ----
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=10)
        self.preview_button = ttk.Button(btn_frame, text="任务预览", command=self.preview_tasks)
        self.preview_button.pack(side=tk.LEFT, padx=5)
        self.start_button = ttk.Button(btn_frame, text="开始转换", command=self.start_conversion)
        self.start_button.pack(side=tk.LEFT, padx=5)

        # ---- 任务总览 ----
        overview_frame = ttk.LabelFrame(self, text="任务总览 —— 命令模板（可直接编辑，开始转换时以此为准）")
        overview_frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(overview_frame,
                  text="可用占位符：{input_file} 输入文件、{output_file} 输出文件、{audio_file} 音频、{sub_file} 字幕"
                       "（占位符会被替换为实际路径，请保留引号）",
                  foreground="#666666").pack(anchor="w", padx=5, pady=(5, 0))
        self.cmd_text = tk.Text(overview_frame, height=7, wrap="word", undo=True)
        self.cmd_text.pack(fill="both", expand=True, padx=5, pady=5)

        # ---- 待转换文件列表 ----
        list_frame = ttk.LabelFrame(self, text="待转换文件列表")
        list_frame.pack(fill="x", padx=10, pady=5)
        self.filelist_text = tk.Text(list_frame, height=5, wrap="none")
        self.filelist_text.pack(fill="both", expand=True, padx=5, pady=5)
        self.filelist_text.config(state=tk.DISABLED)

        # ---- 日志 ----
        log_frame = ttk.LabelFrame(self, text="日志")
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)
        self.log_text = tk.Text(log_frame, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=self.log_scrollbar.set)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

    # ---------- 路径浏览 ----------
    def browse_input_path(self):
        if self.conversion_mode.get() == "single":
            filename = filedialog.askopenfilename(
                title="选择视频文件",
                filetypes=[("视频文件", "*.mp4;*.mkv;*.avi;*.mov;*.flv;*.wmv"), ("所有文件", "*.*")])
            if filename:
                self.input_path.set(filename)
        else:
            foldername = filedialog.askdirectory(title="选择视频文件夹")
            if foldername:
                self.input_path.set(foldername)

    def browse_output_path(self):
        foldername = filedialog.askdirectory(title="选择输出文件夹")
        if foldername:
            self.output_path.set(foldername)

    def browse_audio_path(self):
        filename = filedialog.askopenfilename(
            title="选择音频文件（批量模式可先填文件夹路径）",
            filetypes=[("音频文件", "*.mp3;*.wav;*.aac;*.m4a;*.flac;*.ogg;*.opus;*.wma;*.ac3;*.dts"),
                       ("所有文件", "*.*")])
        if filename:
            self.audio_input_path.set(filename)

    def browse_sub_path(self):
        filename = filedialog.askopenfilename(
            title="选择字幕文件（批量模式可先填文件夹路径）",
            filetypes=[("字幕文件", "*.srt;*.ass;*.ssa;*.vtt;*.sub;*.idx;*.sup"),
                       ("所有文件", "*.*")])
        if filename:
            self.sub_input_path.set(filename)

    # ---------- 复选框联动 ----------
    def toggle_audio_input(self):
        state = tk.NORMAL if self.use_audio_var.get() else tk.DISABLED
        self.audio_entry.config(state=state)
        self.audio_browse.config(state=state)

    def toggle_sub_input(self):
        state = tk.NORMAL if self.use_sub_var.get() else tk.DISABLED
        self.sub_entry.config(state=state)
        self.sub_browse.config(state=state)

    def log(self, message):
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)

    def disable_controls(self):
        self.start_button.config(state=tk.DISABLED)
        self.preview_button.config(state=tk.DISABLED)

    def enable_controls(self):
        self.start_button.config(state=tk.NORMAL)
        self.preview_button.config(state=tk.NORMAL)

    # ---------- 参数解析 ----------
    def _resolve_bitrate(self, text):
        text = (text or "").strip()
        if not text:
            return ""
        return config["bitrates"].get(text, text)

    def _resolve_resolution(self, text):
        text = (text or "").strip()
        if not text:
            return ""
        if text.lower() in ("original", "none", "原始", "原始分辨率"):
            return ""
        if text in config["resolutions"]:
            return config["resolutions"][text]
        return text

    def _subtitle_codec(self, files):
        ext = ""
        if files:
            ext = os.path.splitext(files[0])[1].lower()
        if ext in (".mp4", ".m4v", ".mov"):
            return "mov_text"
        return "srt"

    # ---------- 收集文件 ----------
    def _collect_files(self, show_error=True):
        input_path = self.input_path.get().strip()
        if not input_path:
            if show_error:
                messagebox.showerror("错误", "请先选择输入路径")
            return None

        if self.conversion_mode.get() == "single":
            if not os.path.isfile(input_path):
                if show_error:
                    messagebox.showerror("错误", "选择的文件不存在")
                return None
            return [input_path]

        if not os.path.isdir(input_path):
            if show_error:
                messagebox.showerror("错误", "选择的文件夹不存在")
            return None
        files = [os.path.join(input_path, f) for f in sorted(os.listdir(input_path))
                 if f.lower().endswith(VIDEO_EXTS)]
        if not files:
            if show_error:
                messagebox.showinfo("提示", "未在所选文件夹中找到视频文件")
            return None
        return files

    def _update_file_list(self, files):
        lines = [f"{i + 1}. " + f.replace(os.sep, "/") for i, f in enumerate(files)]
        self.filelist_text.config(state=tk.NORMAL)
        self.filelist_text.delete(1.0, tk.END)
        self.filelist_text.insert(tk.END, "\n".join(lines))
        self.filelist_text.config(state=tk.DISABLED)

    def _set_template(self, template):
        self.cmd_text.delete(1.0, tk.END)
        self.cmd_text.insert(tk.END, template)

    # ---------- 生成命令模板 ----------
    def build_template(self, files=None):
        video_params = config["video_encodings"].get(self.video_codec_var.get().strip(), "")
        audio_params = config["audio_encodings"].get(self.audio_codec_var.get().strip(), "")
        v_bitrate = self._resolve_bitrate(self.bitrate_var.get())
        a_bitrate = self._resolve_bitrate(self.audio_bitrate_var.get())
        if a_bitrate:
            audio_params = re.sub(r"-b:a\s+\S+", "", audio_params).strip()
        resolution = self._resolve_resolution(self.resolution_var.get())
        extra = self.extra_params_var.get().strip()
        use_audio = self.use_audio_var.get()
        use_sub = self.use_sub_var.get()

        parts = ["ffmpeg.exe", "-noautorotate", "-y", "-i", '"{input_file}"']

        if use_audio or use_sub:
            idx = 1
            audio_idx = None
            sub_idx = None
            if use_audio:
                parts += ["-i", '"{audio_file}"']
                audio_idx = idx
                idx += 1
            if use_sub:
                parts += ["-i", '"{sub_file}"']
                sub_idx = idx
                idx += 1
            parts += ["-map", "0:v:0"]
            if audio_idx is not None:
                parts += ["-map", f"{audio_idx}:a:0"]
            if sub_idx is not None:
                parts += ["-map", f"{sub_idx}:s:0"]

        parts += ["-map_metadata", "0"]
        if video_params:
            parts.append(video_params)
        if v_bitrate:
            parts += ["-b:v", v_bitrate]
        if resolution:
            parts += ["-vf", f"scale={resolution}"]
        if audio_params:
            parts.append(audio_params)
        if a_bitrate:
            parts += ["-b:a", a_bitrate]
        if use_sub:
            parts += ["-c:s", self._subtitle_codec(files)]
        if extra:
            parts.append(extra)
        parts += ['"{output_file}"']
        return " ".join(parts)

    # ---------- 任务预览 ----------
    def preview_tasks(self):
        files = self._collect_files()
        if files is None:
            return

        current = self.cmd_text.get("1.0", tk.END).strip()
        if current and self._last_template and current != self._last_template:
            if not messagebox.askyesno("确认", "命令模板已被手动修改，重新生成会覆盖修改，是否继续？"):
                self._update_file_list(files)
                return

        template = self.build_template(files)
        self._set_template(template)
        self._last_template = template
        self._update_file_list(files)
        self.log("已生成任务预览，可手工修改命令模板后再点击“开始转换”。")

    # ---------- 匹配音频/字幕 ----------
    def _resolve_side_file(self, input_file, side_path, exts):
        side_path = (side_path or "").strip()
        if not side_path:
            return None
        base = os.path.splitext(os.path.basename(input_file))[0]

        if os.path.isdir(side_path):
            try:
                for f in sorted(os.listdir(side_path)):
                    name, e = os.path.splitext(f)
                    if name == base and e.lower() in exts:
                        return os.path.join(side_path, f)
            except OSError:
                return None
            return None

        if self.conversion_mode.get() == "folder":
            folder = os.path.dirname(input_file)
            try:
                for f in sorted(os.listdir(folder)):
                    name, e = os.path.splitext(f)
                    if name == base and e.lower() in exts:
                        return os.path.join(folder, f)
            except OSError:
                pass

        return side_path

    # ---------- 开始转换 ----------
    def start_conversion(self):
        files = self._collect_files()
        if files is None:
            return

        template = self.cmd_text.get("1.0", tk.END).strip()
        if not template:
            template = self.build_template(files)
            self._set_template(template)
            self._last_template = template

        self._update_file_list(files)

        missing = [p for p in ("{input_file}", "{output_file}") if p not in template]
        if missing:
            messagebox.showerror("错误", "命令模板缺少占位符：" + "、".join(missing) +
                                 "\n\n请保留 {input_file} 和 {output_file}，否则无法定位输入输出文件。")
            return

        if self.use_audio_var.get():
            path = self.audio_input_path.get().strip()
            if not path:
                messagebox.showerror("错误", "已勾选音频输入，但未填写音频路径")
                return
            if not os.path.exists(path):
                messagebox.showerror("错误", f"音频路径不存在：\n{path}")
                return
            if "{audio_file}" not in template:
                messagebox.showerror("错误", "已勾选音频输入，但命令模板中没有 {audio_file} 占位符")
                return

        if self.use_sub_var.get():
            path = self.sub_input_path.get().strip()
            if not path:
                messagebox.showerror("错误", "已勾选字幕输入，但未填写字幕路径")
                return
            if not os.path.exists(path):
                messagebox.showerror("错误", f"字幕路径不存在：\n{path}")
                return
            if "{sub_file}" not in template:
                messagebox.showerror("错误", "已勾选字幕输入，但命令模板中没有 {sub_file} 占位符")
                return

        self.disable_controls()
        threading.Thread(target=self.do_conversion, args=(files, template), daemon=True).start()

    def do_conversion(self, file_list, template):
        use_audio = self.use_audio_var.get()
        use_sub = self.use_sub_var.get()

        for input_file in file_list:
            specified_output = self.output_path.get().strip()
            if specified_output:
                output_folder = specified_output
            else:
                if self.conversion_mode.get() == "single":
                    output_folder = os.path.join(os.path.dirname(input_file), "converted")
                else:
                    output_folder = os.path.join(self.input_path.get().strip(), "converted")
            if not os.path.exists(output_folder):
                try:
                    os.makedirs(output_folder)
                except Exception as e:
                    self.log_text.after(0, lambda err=e: self.log(f"创建输出文件夹失败: {err}"))
                    continue

            base_name = os.path.splitext(os.path.basename(input_file))[0]
            ext = os.path.splitext(input_file)[1]
            output_file = os.path.join(output_folder, base_name + "_trans" + ext)
            output_file_disp = output_file.replace(os.sep, "/")

            audio_file = ""
            sub_file = ""
            skip = False
            if use_audio:
                matched = self._resolve_side_file(input_file, self.audio_input_path.get(), AUDIO_EXTS)
                if not matched:
                    self.log_text.after(0, lambda f=input_file: self.log(
                        f"跳过（未找到匹配的音频文件）：{f.replace(os.sep, '/')}"))
                    skip = True
                else:
                    audio_file = matched
            if not skip and use_sub:
                matched = self._resolve_side_file(input_file, self.sub_input_path.get(), SUBTITLE_EXTS)
                if not matched:
                    self.log_text.after(0, lambda f=input_file: self.log(
                        f"跳过（未找到匹配的字幕文件）：{f.replace(os.sep, '/')}"))
                    skip = True
                else:
                    sub_file = matched
            if skip:
                continue

            cmd = (template
                   .replace("{input_file}", input_file)
                   .replace("{output_file}", output_file)
                   .replace("{audio_file}", audio_file)
                   .replace("{sub_file}", sub_file))
            cmd_disp = cmd.replace(os.sep, "/")

            self.log_text.after(0, lambda f=input_file, c=cmd_disp: self.log(
                f"开始转换：{f.replace(os.sep, '/')}\n执行命令：{c}"))
            try:
                process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, universal_newlines=True,
                                           encoding="utf-8", errors="replace",
                                           cwd=APP_DIR,
                                           creationflags=CREATE_NO_WINDOW)
                throttle_interval = 2.0
                last_frame_time = time.time()
                latest_frame_line = None
                for line in iter(process.stdout.readline, ''):
                    if not line:
                        break
                    line_stripped = line.rstrip()
                    if "frame=" in line_stripped:
                        latest_frame_line = line_stripped
                        current_time = time.time()
                        if current_time - last_frame_time >= throttle_interval:
                            self.log_text.after(0, lambda l=latest_frame_line: self.log(l))
                            last_frame_time = current_time
                    else:
                        self.log_text.after(0, lambda l=line_stripped: self.log(l))
                if latest_frame_line is not None:
                    self.log_text.after(0, lambda l=latest_frame_line: self.log(l))
                process.stdout.close()
                process.wait()
                if process.returncode == 0:
                    self.log_text.after(0, lambda o=output_file_disp: self.log(f"转换成功：{o}\n"))
                else:
                    self.log_text.after(0, lambda rc=process.returncode, f=input_file: self.log(
                        f"转换失败（返回码 {rc}）：{f.replace(os.sep, '/')}\n"))
            except Exception as e:
                self.log_text.after(0, lambda err=e: self.log(f"转换过程中发生异常：{err}\n"))

        self.log_text.after(0, lambda: messagebox.showinfo("提示", "全部转换任务已完成"))
        self.log_text.after(0, self.enable_controls)


# ---------------------------
# 5. Tab 2：裁剪拼接（时间轴剪辑）
# ---------------------------
class TrimTab(ttk.Frame):
    """播放器式剪辑界面：预览 + 可缩放时间轴 + 入出点标记 + 剪断删除 + 拼接 + 导出"""

    RULER_H = 26          # 时间标尺高度
    STRIP_H = 78          # 缩略图带高度
    TL_H = RULER_H + STRIP_H + 16
    PV_FPS = 15           # 预览播放帧率
    STEPS = [0.04, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800]

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app

        # ---- 数据模型 ----
        self.sources = []        # [media_info]
        self.pieces = []         # [{"src": idx, "start": 源内秒, "end": 源内秒}]
        self.spans = []          # [(piece_idx, 时间轴起点, 时间轴终点)]
        self.total = 0.0

        # ---- 视图状态 ----
        self.playhead = 0.0
        self.mark_in = None
        self.mark_out = None
        self.selected_piece = None
        self.pps = 40.0          # 每秒像素（缩放级别）
        self.view_start = 0.0

        # ---- 缓存 / 线程 ----
        self.filmstrips = {}     # src_idx -> {"times": [...], "imgs": [...], "w":..,"h":..}
        self.waveforms = {}      # src_idx -> {"per": 秒/点, "peaks": [0..1, ...]}
        self.keyframes = {}      # src_idx -> [关键帧秒, ...]（流复制用）
        self.undo_stack = []
        self.redo_stack = []
        self._frame_token = 0
        self._frame_img = None
        self._last_frame = None
        self._resize_job = None
        self._tl_drag = False
        self._drag_mode = None
        self._fit_pending = False

        # 播放
        self._playing = False
        self._play_proc = None
        self._play_stop = None
        self._play_buf = None
        self._play_cond = None
        self._play_start = 0.0
        self._play_wall = 0.0
        self._play_after = None
        self._audio_proc = None
        self._audio_piece = None
        self._scrub_proc = None
        self._scrub_ts = 0.0
        self._frame_req_ts = 0.0
        self._pv_dragging = False
        self._bg_procs = []
        self._cache_token = 0
        self.pv_zoom = tk.StringVar(value="适应窗口")
        self._pan_x = 0
        self._pan_y = 0
        self._drop = None

        # 导出
        self._export_proc = None
        self._export_thread = None
        self._export_cancelled = False

        self._build_ui()
        self._bind_keys()
        self._update_status()
        self.after(120, self._first_layout)

    @property
    def media_kind(self):
        """当前素材类型：video / audio（由素材本身推导，避免状态不同步）。"""
        if not self.sources:
            return None
        return "video" if self.sources[0].get("has_video") else "audio"

    # ================= UI =================
    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # ---- 顶部工具条 ----
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        ttk.Button(bar, text="打开文件", command=self.open_file).pack(side="left", padx=(0, 4))
        ttk.Button(bar, text="追加文件（拼接）", command=self.append_file).pack(side="left", padx=4)
        ttk.Button(bar, text="清空", command=self.clear_all).pack(side="left", padx=4)
        ttk.Button(bar, text="中断", command=self.abort_all).pack(side="left", padx=4)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(bar, text="撤销", command=self.undo).pack(side="left", padx=2)
        ttk.Button(bar, text="重做", command=self.redo).pack(side="left", padx=2)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(bar, text="缩放").pack(side="left", padx=(2, 2))
        ttk.Button(bar, text="－", width=3, command=lambda: self.zoom_at(1 / 1.4)).pack(side="left")
        ttk.Button(bar, text="＋", width=3, command=lambda: self.zoom_at(1.4)).pack(side="left")
        ttk.Button(bar, text="适应窗口", command=self.zoom_fit).pack(side="left", padx=4)
        self.zoom_label = ttk.Label(bar, text="", foreground="#666666")
        self.zoom_label.pack(side="left", padx=6)

        self.file_label = ttk.Label(bar, text="未打开文件", foreground="#666666")
        self.file_label.pack(side="right", padx=6)

        # ---- 预览区 ----
        pv_wrap = tk.Frame(self, bg="#0f1014", bd=0, highlightthickness=0)
        pv_wrap.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        pv_wrap.rowconfigure(0, weight=1)
        pv_wrap.columnconfigure(0, weight=1)
        self.pv = tk.Canvas(pv_wrap, bg="#0f1014", highlightthickness=0, bd=0, height=280)
        self.pv.grid(row=0, column=0, sticky="nsew")
        self.pv_img_id = self.pv.create_image(0, 0, anchor="center")
        self.pv_ph = self.pv.create_text(0, 0, text="把视频 / 音频文件直接拖进来，或点「打开文件」",
                                        fill="#7d8494", font=("Microsoft YaHei UI", 13), justify="center")
        self.pv.bind("<Configure>", self._on_pv_resize)
        self.pv.bind("<Button-1>", self._pv_press)
        self.pv.bind("<B1-Motion>", self._pv_drag)
        self.pv.bind("<ButtonRelease-1>", self._pv_release)
        self.pv.bind("<Control-Button-1>", self._pv_pan_start)
        self.pv.bind("<Control-B1-Motion>", self._pv_pan_move)
        self.pv.bind("<Control-ButtonRelease-1>", self._pv_pan_end)
        self.pv.bind("<MouseWheel>", self._tl_wheel)
        self.pv.bind("<Control-MouseWheel>", self._tl_ctrl_wheel)

        # ---- 播放控制条 ----
        ctrl = ttk.Frame(self)
        ctrl.grid(row=2, column=0, sticky="ew", padx=8, pady=(2, 4))

        ttk.Button(ctrl, text="⏮", width=4, command=self.goto_start).pack(side="left")
        ttk.Button(ctrl, text="◀|", width=4, command=lambda: self.step(-1)).pack(side="left", padx=2)
        self.play_btn = ttk.Button(ctrl, text="▶ 播放", width=9, command=self.toggle_play)
        self.play_btn.pack(side="left", padx=2)
        ttk.Button(ctrl, text="|▶", width=4, command=lambda: self.step(1)).pack(side="left", padx=2)
        ttk.Button(ctrl, text="⏭", width=4, command=self.goto_end).pack(side="left")

        self.tc_label = ttk.Label(ctrl, text="00:00:00.000 / 00:00:00.000",
                                  font=("Consolas", 12, "bold"), foreground="#1f3a93")
        self.tc_label.pack(side="left", padx=14)

        ttk.Label(ctrl, text="画面:").pack(side="left", padx=(6, 2))
        self.zoom_combo = ttk.Combobox(
            ctrl, textvariable=self.pv_zoom, state="readonly", width=8,
            values=["适应窗口", "10%", "25%", "50%", "100%", "150%", "200%", "400%"])
        self.zoom_combo.pack(side="left")
        self.zoom_combo.bind("<<ComboboxSelected>>", lambda e: self._on_pv_zoom())

        self.var_sound = tk.BooleanVar(value=True)
        self.chk_sound = ttk.Checkbutton(ctrl, text="预览声音", variable=self.var_sound,
                                         command=self._on_sound_toggle)
        self.chk_sound.pack(side="left", padx=(12, 4))

        # ---- 时间轴 ----
        tl_wrap = ttk.Frame(self)
        tl_wrap.grid(row=3, column=0, sticky="ew", padx=8)
        tl_wrap.columnconfigure(0, weight=1)
        self.tl = tk.Canvas(tl_wrap, height=self.TL_H, bg="#f2f3f6", highlightthickness=1,
                            highlightbackground="#d5d8e0", bd=0, takefocus=1)
        self.tl.grid(row=0, column=0, sticky="ew")
        self.tl.bind("<Configure>", self._on_tl_configure)
        self.tl.bind("<Button-1>", self._tl_press)
        self.tl.bind("<B1-Motion>", self._tl_drag_move)
        self.tl.bind("<ButtonRelease-1>", self._tl_release)
        self.tl.bind("<Double-Button-1>", self._tl_double)
        self.tl.bind("<MouseWheel>", self._tl_wheel)
        self.tl.bind("<Control-MouseWheel>", self._tl_ctrl_wheel)
        self.tl.bind("<Shift-MouseWheel>", self._tl_ctrl_wheel)

        self.hbar = ttk.Scrollbar(tl_wrap, orient="horizontal", command=self._on_hbar)
        self.hbar.grid(row=1, column=0, sticky="ew", pady=(2, 0))

        # ---- 编辑操作区 ----
        edit = ttk.Frame(self)
        edit.grid(row=4, column=0, sticky="ew", padx=8, pady=6)

        ttk.Button(edit, text="标记入点 (I)", command=self.mark_in_at_playhead).pack(side="left", padx=(0, 3))
        ttk.Button(edit, text="标记出点 (O)", command=self.mark_out_at_playhead).pack(side="left", padx=3)
        ttk.Button(edit, text="清除标记", command=self.clear_marks).pack(side="left", padx=3)
        ttk.Separator(edit, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(edit, text="✂ 剪断选区", command=self.split_selection).pack(side="left", padx=3)
        ttk.Button(edit, text="删除选区", command=self.delete_selection).pack(side="left", padx=3)
        ttk.Button(edit, text="只保留选区", command=self.keep_selection).pack(side="left", padx=3)
        ttk.Button(edit, text="删除选中片段", command=self.delete_selected_piece).pack(side="left", padx=3)
        ttk.Separator(edit, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(edit, text="全部还原", command=self.reset_cut).pack(side="left", padx=3)

        # ---- 状态行 ----
        info_row = ttk.Frame(self)
        info_row.grid(row=5, column=0, sticky="ew", padx=14, pady=(0, 2))
        self.status_label = ttk.Label(info_row, text="", foreground="#666666")
        self.status_label.pack(side="left")
        ttk.Label(info_row, text="空格 播放 · I/O 入出点 · Del 删除 · ←→ 逐帧 · Shift+←→ 1秒 · "
                                "滚轮平移 · Ctrl+滚轮 缩放",
                  foreground="#9aa1b0").pack(side="right")

        # ---- 导出设置 ----
        exp = ttk.LabelFrame(self, text="导出")
        exp.grid(row=6, column=0, sticky="ew", padx=8, pady=(0, 6))
        exp.columnconfigure(0, weight=1)

        r0 = ttk.Frame(exp)
        r0.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 2))
        r0.columnconfigure(1, weight=1)
        ttk.Label(r0, text="输出文件:").grid(row=0, column=0, padx=(0, 4), sticky="e")
        self.out_var = tk.StringVar()
        ttk.Entry(r0, textvariable=self.out_var).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(r0, text="浏览", command=self.browse_output).grid(row=0, column=2, padx=4)

        # 处理方式 + 封装格式
        r1 = ttk.Frame(exp)
        r1.grid(row=1, column=0, sticky="w", padx=6, pady=2)
        ttk.Label(r1, text="处理方式:").pack(side="left")
        self.mode_var = tk.StringVar(value="encode")
        ttk.Radiobutton(r1, text="重编码（精确剪切）", variable=self.mode_var, value="encode",
                        command=self._on_mode_change).pack(side="left", padx=(4, 2))
        ttk.Radiobutton(r1, text="流复制（快速无损）", variable=self.mode_var, value="copy",
                        command=self._on_mode_change).pack(side="left", padx=2)
        ttk.Label(r1, text="封装格式:").pack(side="left", padx=(16, 3))
        self.fmt_var = tk.StringVar(value="mp4")
        self.fmt_combo = ttk.Combobox(r1, textvariable=self.fmt_var, values=["mp4"],
                                      state="readonly", width=8)
        self.fmt_combo.pack(side="left")
        self.fmt_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_format_ext())
        self.fmt_hint = ttk.Label(r1, text="", foreground="#9aa1b0")
        self.fmt_hint.pack(side="left", padx=(10, 0))

        # 视频流
        r2 = ttk.Frame(exp)
        r2.grid(row=2, column=0, sticky="w", padx=6, pady=2)
        self.do_video = tk.BooleanVar(value=True)
        self.chk_video = ttk.Checkbutton(r2, text="视频", variable=self.do_video,
                                         command=self._on_stream_toggle)
        self.chk_video.pack(side="left")
        ttk.Label(r2, text="编码:").pack(side="left", padx=(10, 3))
        self.vcodec_var = tk.StringVar(value="default")
        vlist = ["default"] + list(config["video_encodings"].keys())
        self.vcodec_combo = ttk.Combobox(r2, textvariable=self.vcodec_var, values=vlist,
                                         state="readonly", width=16)
        self.vcodec_combo.pack(side="left")
        self.vcodec_combo.bind("<<ComboboxSelected>>", lambda e: self._on_stream_toggle())

        self.vbit_label = ttk.Label(r2, text="码率:")
        self.vbit_var = tk.StringVar(value=list(config["bitrates"].keys())[0])
        self.vbit_combo = ttk.Combobox(r2, textvariable=self.vbit_var,
                                       values=list(config["bitrates"].keys()),
                                       state="readonly", width=11)

        # 音频流
        r3 = ttk.Frame(exp)
        r3.grid(row=3, column=0, sticky="w", padx=6, pady=2)
        self.do_audio = tk.BooleanVar(value=True)
        self.chk_audio = ttk.Checkbutton(r3, text="音频", variable=self.do_audio,
                                         command=self._on_stream_toggle)
        self.chk_audio.pack(side="left")
        ttk.Label(r3, text="编码:").pack(side="left", padx=(10, 3))
        self.acodec_var = tk.StringVar(value="default")
        alist = ["default"] + list(config["audio_encodings"].keys())
        self.acodec_combo = ttk.Combobox(r3, textvariable=self.acodec_var, values=alist,
                                         state="readonly", width=16)
        self.acodec_combo.pack(side="left")
        ttk.Label(r3, text="default = 不指定编码参数，按输出容器默认处理；码率仅在选了具体视频编码时可设",
                  foreground="#9aa1b0").pack(side="left", padx=(14, 0))

        # 按钮行
        r4 = ttk.Frame(exp)
        r4.grid(row=4, column=0, sticky="ew", padx=6, pady=(4, 6))
        r4.columnconfigure(3, weight=1)
        self.export_btn = ttk.Button(r4, text="开始导出", command=self.start_export)
        self.export_btn.grid(row=0, column=0, padx=(0, 6))
        self.cancel_btn = ttk.Button(r4, text="取消", command=self.cancel_export, state=tk.DISABLED)
        self.cancel_btn.grid(row=0, column=1, padx=(0, 10))
        self.progress = ttk.Progressbar(r4, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=2, columnspan=2, sticky="ew")

        self._update_containers()
        self._update_stream_checks()
        exp.columnconfigure(2, weight=1)

        # ---- 日志 ----
        logf = ttk.LabelFrame(self, text="日志")
        logf.grid(row=7, column=0, sticky="ew", padx=8, pady=(0, 8))
        logf.columnconfigure(0, weight=1)
        self.log_text = tk.Text(logf, height=4, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(logf, orient="vertical", command=self.log_text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=sb.set)

    def _first_layout(self):
        self._draw_timeline()

    def _on_tl_configure(self, event):
        if self._fit_pending and self.total > 0 and event.width > 100:
            self._fit_pending = False
            self.zoom_fit()
            return
        self._draw_timeline()

    def _on_tab_shown(self):
        """切换到本 tab 时（此时控件才真正布局）补一次适配与取帧。"""
        if self._fit_pending:
            self.zoom_fit()
        else:
            self._draw_timeline()
        if self.pieces and not self._playing:
            self.request_frame()

    # ================= 基础工具 =================
    def log(self, msg):
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)

    def _ui(self, fn):
        """从工作线程安全地把回调排到主线程。"""
        try:
            self.after(0, fn)
        except Exception:
            pass

    # ---------- 后台进程管理 ----------
    def _spawn_bg(self, cmd, stdout=None, stderr=subprocess.DEVNULL):
        """启动一个后台 ffmpeg 并登记，方便统一中断。"""
        self._bg_procs = [p for p in self._bg_procs if p.poll() is None]
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                             stdout=stdout if stdout is not None else subprocess.DEVNULL,
                             stderr=stderr, creationflags=CREATE_NO_WINDOW)
        self._bg_procs.append(p)
        return p

    def _kill_bg(self):
        """杀掉所有后台 ffmpeg（缩略图 / 波形 / 预览取帧）。"""
        n = 0
        for p in list(getattr(self, "_bg_procs", [])):
            try:
                if p.poll() is None:
                    p.kill()
                    n += 1
            except Exception:
                pass
        self._bg_procs = []
        self._cache_token += 1
        self._frame_token += 1
        return n

    def abort_all(self):
        """中断：停掉所有后台任务（缓存生成、播放、预览、导出）。"""
        killed = self._kill_bg()
        self._stop_play()
        self._stop_scrub_audio()
        if self._export_proc is not None:
            self.cancel_export()
        self.log("已中断后台任务（结束 %d 个 ffmpeg 进程）" % killed)

    def shutdown(self):
        """关闭窗口时清理，避免残留 ffmpeg 进程。"""
        try:
            self._kill_bg()
        except Exception:
            pass
        try:
            self._stop_play()
            self._stop_scrub_audio()
        except Exception:
            pass
        if self._export_proc is not None:
            try:
                self._export_proc.kill()
            except Exception:
                pass

    def _tl_width(self):
        w = self.tl.winfo_width()
        return w if w > 20 else 1200

    def _x(self, t):
        return (t - self.view_start) * self.pps

    def _t(self, x):
        return self.view_start + x / self.pps

    def _layout(self):
        self.spans = []
        acc = 0.0
        for i, p in enumerate(self.pieces):
            d = max(0.0, p["end"] - p["start"])
            self.spans.append((i, acc, acc + d))
            acc += d
        self.total = acc
        if self.playhead > self.total:
            self.playhead = self.total

    def _map_time(self, t):
        """时间轴时间 -> (piece_index, 源内时间)"""
        if not self.pieces:
            return None, 0.0
        t = max(0.0, min(t, self.total))
        for idx, t0, t1 in self.spans:
            if t <= t1 or idx == self.spans[-1][0]:
                p = self.pieces[idx]
                return idx, min(p["end"], p["start"] + (t - t0))
        idx = self.spans[-1][0]
        return idx, self.pieces[idx]["end"]

    def _pieces_from(self, start_t):
        """从时间轴 start_t 开始到结尾的分段（源内起止）。"""
        out = []
        for idx, t0, t1 in self.spans:
            if t1 <= start_t + 1e-6:
                continue
            p = self.pieces[idx]
            s = p["start"] + max(0.0, start_t - t0)
            e = p["end"]
            if e - s > 1e-4:
                out.append((idx, s, e))
        return out

    def _clamp_view(self):
        w = self._tl_width()
        view_dur = w / self.pps
        if self.total <= view_dur:
            self.view_start = 0.0
        else:
            self.view_start = max(0.0, min(self.view_start, self.total - view_dur * 0.92))

    # ================= 打开 / 追加 / 清空 =================
    MEDIA_FILETYPES = [
        ("音视频文件", "*.mp4;*.mkv;*.avi;*.mov;*.flv;*.wmv;*.m4v;*.ts;*.webm;*.mp3;*.wav;*.aac;"
                       "*.m4a;*.flac;*.ogg;*.opus;*.wma;*.ac3;*.eac3;*.dts;*.ape;*.amr"),
        ("视频文件", "*.mp4;*.mkv;*.avi;*.mov;*.flv;*.wmv;*.m4v;*.ts;*.webm"),
        ("音频文件", "*.mp3;*.wav;*.aac;*.m4a;*.flac;*.ogg;*.opus;*.wma;*.ac3;*.dts;*.ape"),
        ("所有文件", "*.*"),
    ]

    def open_file(self):
        path = filedialog.askopenfilename(title="选择视频 / 音频文件",
                                          filetypes=self.MEDIA_FILETYPES)
        if path:
            self._add_paths([path], replace=True)

    def append_file(self):
        if not self.sources:
            self.open_file()
            return
        path = filedialog.askopenfilename(title="追加文件（拼接到时间轴末尾）",
                                          filetypes=self.MEDIA_FILETYPES)
        if path:
            self._add_paths([path], replace=False)

    def _add_paths(self, paths, replace=False):
        """打开 / 追加 / 拖入文件的统一入口。"""
        paths = [p for p in paths if p and os.path.isfile(p)]
        if not paths:
            return
        infos, bad = [], []
        for p in paths:
            info = probe_media(p)
            if info:
                infos.append(info)
            else:
                bad.append(p)
        if bad:
            messagebox.showwarning("提示", "以下文件无法解析，已跳过：\n" +
                                   "\n".join(os.path.basename(b) for b in bad[:8]))
        if not infos:
            return

        if replace or not self.sources:
            self._load_first(infos[0])
            rest = infos[1:]
        else:
            rest = infos

        if not rest:
            return
        # 追加时不允许音视频混排（导出管线要求同一类型）
        ok, skipped = [], []
        for i in rest:
            (ok if bool(i["has_video"]) == (self.media_kind == "video") else skipped).append(i)
        if skipped:
            messagebox.showwarning(
                "提示", "当前时间轴是%s素材，不能混入%s文件：\n%s" % (
                    "视频" if self.media_kind == "video" else "音频",
                    "音频" if self.media_kind == "video" else "视频",
                    "\n".join(os.path.basename(i["path"]) for i in skipped[:8])))
        if not ok:
            return
        self._stop_play()
        self._push_undo()
        for info in ok:
            self.sources.append(info)
            idx = len(self.sources) - 1
            self.pieces.append({"src": idx, "start": 0.0, "end": info["duration"]})
            self.log("追加：%s（%s）" % (os.path.basename(info["path"]), fmt_tc(info["duration"])))
            self._build_cache(idx)
        self._layout()
        self._update_stream_checks()
        self._draw_timeline()
        self._update_status()

    def _load_first(self, info):
        """把某个素材作为时间轴的第一段（清空原有内容）。"""
        self._stop_play()
        self._kill_bg()
        self._pan_x = self._pan_y = 0
        self.sources = [info]
        self.pieces = [{"src": 0, "start": 0.0, "end": info["duration"]}]
        self.filmstrips = {}
        self.waveforms = {}
        self.keyframes = {}
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.playhead = 0.0
        self.mark_in = self.mark_out = None
        self.selected_piece = None
        self.view_start = 0.0
        self._layout()
        self.zoom_fit()
        self.file_label.config(text=os.path.basename(info["path"]))
        base, _ = os.path.splitext(info["path"])
        self.out_var.set(base + "_cut")
        self._update_containers()
        if self.media_kind == "video":
            self.log("已打开：%s（%s，%dx%d，%.3ffps%s）" % (
                info["path"], fmt_tc(info["duration"]), info["w"], info["h"], info["fps"],
                "，含音频" if info["has_audio"] else "，无音频"))
        else:
            self.log("已打开音频：%s（%s%s）" % (
                info["path"], fmt_tc(info["duration"]),
                "，含视频流" if info["has_video"] else ""))
        self._build_cache(0)
        self._update_stream_checks(reset=True)
        self._reset_preview_placeholder()
        self.request_frame(force=True)
        self._update_status()

    def clear_all(self):
        self._stop_play()
        self._kill_bg()
        self._pan_x = self._pan_y = 0
        self.sources = []
        self.pieces = []
        self.spans = []
        self.total = 0.0
        self.filmstrips = {}
        self.waveforms = {}
        self.keyframes = {}
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.playhead = 0.0
        self.mark_in = self.mark_out = None
        self.selected_piece = None
        self.view_start = 0.0
        self.out_var.set("")
        self.file_label.config(text="未打开文件")
        self._frame_token += 1
        self._frame_img = None
        self._last_frame = None
        self._reset_preview_placeholder()
        self._update_stream_checks()
        self._draw_timeline()
        self._update_status()

    def _reset_preview_placeholder(self):
        self._frame_token += 1
        self._frame_img = None
        self.pv.delete("all")
        self.pv_img_id = self.pv.create_image(0, 0, anchor="center")
        self.pv_ph = None
        self._pv_wave_key = None
        if not self.sources:
            txt = "把视频 / 音频文件直接拖进来，或点「打开文件」"
            self.pv_ph = self.pv.create_text(
                self.pv.winfo_width() // 2, self.pv.winfo_height() // 2, text=txt,
                fill="#7d8494", font=("Microsoft YaHei UI", 13), justify="center")
        elif self.media_kind == "audio":
            self._draw_preview_wave()
        else:
            self.pv_ph = self.pv.create_text(
                self.pv.winfo_width() // 2, self.pv.winfo_height() // 2, text="加载中…",
                fill="#7d8494", font=("Microsoft YaHei UI", 13), justify="center")

    # ================= 预览区大波形（音频素材） =================
    def _draw_preview_wave(self, force=False):
        if self.media_kind != "audio" or not self.pieces:
            return
        c = self.pv
        w, h = c.winfo_width(), c.winfo_height()
        if w < 40 or h < 40:
            return
        key = (round(self.view_start, 3), round(self.pps, 3), w, h, len(self.pieces),
               tuple((p["src"], round(p["start"], 2), round(p["end"], 2)) for p in self.pieces))
        if not force and key == getattr(self, "_pv_wave_key", None):
            self._draw_pv_playhead()
            return
        self._pv_wave_key = key

        c.delete("wave")
        if self.pv_ph is not None:
            c.itemconfig(self.pv_ph, state="hidden")

        top, bot = 18, h - 18
        mid = (top + bot) / 2.0
        half = (bot - top) / 2.0
        c.create_line(0, mid, w, mid, fill="#232a38", tags="wave")

        for (idx, t0, t1) in self.spans:
            p = self.pieces[idx]
            x0 = max(0.0, self._x(t0))
            x1 = min(float(w), self._x(t1))
            if x1 - x0 < 1:
                continue
            c.create_rectangle(x0, top, x1, bot, fill="#141821", outline="", tags="wave")
            wave = self.waveforms.get(p["src"])
            if not wave or not wave["peaks"]:
                c.create_text((x0 + x1) / 2, mid, text="波形生成中…", fill="#3f4a63",
                              font=("Microsoft YaHei UI", 10), tags="wave")
                continue
            peaks = wave["peaks"]
            per = wave.get("per") or 0.001
            step = 1 if (x1 - x0) < 1800 else 2
            px = int(x0)
            while px < x1:
                t = self._t(px + step / 2.0)
                st = p["start"] + (t - t0)
                i = int(st / per)
                if 0 <= i < len(peaks):
                    amp = peaks[i] * half
                    if amp < 1.0:
                        amp = 1.0
                    c.create_line(px, mid - amp, px, mid + amp, fill="#4f9ae0", tags="wave")
                px += step

        # 选区
        if self.mark_in is not None and self.mark_out is not None:
            a, b = sorted((self.mark_in, self.mark_out))
            xa, xb = self._x(a), self._x(b)
            if xb - xa > 0.5:
                c.create_rectangle(xa, top, xb, bot, fill="#ff5a5a", stipple="gray25",
                                   outline="#e04a4a", width=2, tags="wave")
                for x in (xa, xb):
                    c.create_line(x, top - 6, x, bot + 6, fill="#ffd24a", width=2, tags="wave")

        self._draw_pv_playhead()

    def _draw_pv_playhead(self):
        c = self.pv
        c.delete("pvhead")
        if self.media_kind != "audio" or not self.pieces:
            return
        w, h = c.winfo_width(), c.winfo_height()
        x = self._x(self.playhead)
        if x < -2 or x > w + 2:
            return
        c.create_line(x, 4, x, h - 4, fill="#ff5252", width=2, tags="pvhead")
        c.create_polygon(x - 6, 0, x + 6, 0, x, 9, fill="#ff5252", outline="", tags="pvhead")
        c.tag_raise("pvhead")

    # ================= 缩略图 / 波形缓存 =================
    def _build_cache(self, idx):
        if self.sources[idx]["has_video"]:
            self._build_filmstrip(idx)
        else:
            self._build_waveform(idx)

    def _build_waveform(self, idx):
        info = self.sources[idx]
        if info.get("has_video"):
            return          # 视频素材不做波形（长片解码太费时）
        ar = int(max(50, min(800, 240000 / max(1.0, info["duration"]))))
        token = self._cache_token

        def work():
            cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-threads", "2", "-i", info["path"],
                   "-vn", "-ac", "1", "-ar", str(ar), "-f", "s16le", "-"]
            raw = b""
            try:
                p = self._spawn_bg(cmd, stdout=subprocess.PIPE)
                raw = p.communicate()[0] or b""
            except Exception:
                raw = b""
            peaks = []
            per = 0.0
            if raw and token == self._cache_token:
                import array
                arr = array.array("h")
                arr.frombytes(raw[:len(raw) // 2 * 2])
                if sys.byteorder == "big":
                    arr.byteswap()
                chunk = max(1, len(arr) // 1600)
                per = chunk / float(ar)
                for i in range(0, len(arr), chunk):
                    seg = arr[i:i + chunk]
                    peaks.append(max(abs(v) for v in seg) / 32768.0)
                mx = max(peaks) if peaks else 0.0
                if mx > 0.001:
                    peaks = [min(1.0, p / mx) for p in peaks]
            if token != self._cache_token:
                return
            self._ui(lambda: self._store_waveform(idx, per, peaks))

        threading.Thread(target=work, daemon=True).start()

    def _store_waveform(self, idx, per, peaks):
        self.waveforms[idx] = {"per": per, "peaks": peaks}
        self._pv_wave_key = None
        self._draw_timeline()
        self._draw_preview_wave(force=True)

    def _build_filmstrip(self, idx):
        info = self.sources[idx]
        if not info.get("has_video"):
            return
        step = max(0.4, info["duration"] / 200.0)
        tmp = tempfile.mkdtemp(prefix="ffgui_thumb_")
        token = self._cache_token
        # 长视频只解关键帧，速度快几十倍
        fast = info["duration"] > 120

        def work():
            files = []
            variants = [["-skip_frame", "nokey"], []] if fast else [[]]
            for extra in variants:
                if token != self._cache_token:
                    break
                cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-threads", "2"] + extra + [
                    "-i", info["path"],
                    "-vf", "fps=%.6f,scale=-2:74" % (1.0 / step),
                    "-frames:v", "240",
                    os.path.join(tmp, "t_%05d.png")]
                try:
                    p = self._spawn_bg(cmd, stdout=subprocess.DEVNULL)
                    p.wait()
                except Exception:
                    pass
                try:
                    files = sorted(f for f in os.listdir(tmp) if f.endswith(".png"))
                except Exception:
                    files = []
                if files:
                    break
            if token != self._cache_token:
                shutil.rmtree(tmp, ignore_errors=True)
                return
            self._ui(lambda: self._load_filmstrip(idx, tmp, files, step, token))

        threading.Thread(target=work, daemon=True).start()

    def _load_filmstrip(self, idx, tmp, files, step, token=None):
        if token is not None and token != self._cache_token:
            shutil.rmtree(tmp, ignore_errors=True)
            return
        times, imgs = [], []
        for i, f in enumerate(files):
            try:
                img = tk.PhotoImage(file=os.path.join(tmp, f))
            except Exception:
                continue
            times.append(i * step)
            imgs.append(img)
        shutil.rmtree(tmp, ignore_errors=True)
        self.filmstrips[idx] = {"times": times, "imgs": imgs,
                                "w": imgs[0].width() if imgs else 96,
                                "h": imgs[0].height() if imgs else 74}
        self._draw_timeline()

    def _thumb_at(self, src_idx, t):
        strip = self.filmstrips.get(src_idx)
        if not strip or not strip["imgs"]:
            return None
        i = bisect.bisect_left(strip["times"], t)
        if i >= len(strip["times"]):
            i = len(strip["times"]) - 1
        if i > 0 and abs(strip["times"][i - 1] - t) < abs(strip["times"][i] - t):
            i -= 1
        return strip["imgs"][i]

    def _draw_waveform(self, c, piece, t0, t1, x0, x1, w, rh):
        """音频片段画波形；视频片段只显示占位（不做波形，长片太慢）。"""
        top = rh + 4
        bot = rh + self.STRIP_H - 4
        mid = (top + bot) / 2.0
        half = (bot - top) / 2.0
        xs = max(0, int(x0))
        xe = min(w, int(x1))
        if self.sources[piece["src"]].get("has_video"):
            if xe - xs > 60:
                c.create_text((xs + xe) / 2.0, mid, text="缩略图生成中…",
                              fill="#a8b4c8", font=("Microsoft YaHei UI", 9))
            return
        wave = self.waveforms.get(piece["src"])
        if not wave or not wave["peaks"]:
            if xe > xs:
                c.create_text((xs + xe) / 2.0, mid, text="♪ 波形生成中…",
                              fill="#7f96c4", font=("Microsoft YaHei UI", 9))
            return
        peaks = wave["peaks"]
        per = wave.get("per") or 0.001
        c.create_line(xs, mid, xe, mid, fill="#c3d2ea")
        step_px = 2 if (xe - xs) < 2600 else 3
        for px in range(xs, xe, step_px):
            t = self._t(px + step_px / 2.0)
            st = piece["start"] + (t - t0)
            i = int(st / per)
            if i < 0 or i >= len(peaks):
                continue
            amp = peaks[i] * half
            if amp < 1.0:
                amp = 1.0
            c.create_line(px, mid - amp, px, mid + amp, fill="#4a7bc8")

    # ================= 时间轴绘制 =================
    def _draw_timeline(self):
        c = self.tl
        c.delete("all")
        w = self._tl_width()
        h = self.TL_H
        rh = self.RULER_H
        c.configure(scrollregion=(0, 0, w, h))

        # 背景
        c.create_rectangle(0, 0, w, h, fill="#f2f3f6", outline="")

        if not self.pieces:
            c.create_text(w // 2, h // 2, text="把视频 / 音频拖进窗口，或点「打开文件」；"
                                               "拖动播放头、按 I/O 标记、删除选区",
                          fill="#9aa1b0", font=("Microsoft YaHei UI", 10))
            self._draw_hbar(w)
            return

        # 标尺刻度
        step = self.STEPS[-1]
        for s in self.STEPS:
            if s * self.pps >= 76:
                step = s
                break
        t = int(self.view_start / step) * step
        while t <= self._t(w) + step:
            x = self._x(t)
            if -2 <= x <= w + 2:
                c.create_line(x, rh - 8, x, rh, fill="#9aa1b0")
                c.create_text(x + 3, rh - 13, text=self._ruler_text(t, step), anchor="w",
                              fill="#6b7280", font=("Consolas", 8))
                # 竖向参考线
                c.create_line(x, rh, x, rh + self.STRIP_H, fill="#e3e6ec")
            t += step
        c.create_line(0, rh, w, rh, fill="#c9cfda")

        # 片段
        for (idx, t0, t1) in self.spans:
            x0 = self._x(t0)
            x1 = self._x(t1)
            if x1 < -4 or x0 > w + 4:
                continue
            p = self.pieces[idx]
            sel = (idx == self.selected_piece)
            fill = "#bfd8ff" if sel else ("#e9eef8" if idx % 2 == 0 else "#e2e9f6")
            outline = "#1a5fc0" if sel else "#a7b7d4"
            c.create_rectangle(x0, rh + 2, x1, rh + self.STRIP_H - 2,
                               fill=fill, outline=outline, width=2 if sel else 1)

            # 缩略图平铺（末尾半格允许溢出，随后会被下一段的底色盖住）
            strip = self.filmstrips.get(p["src"])
            if strip and strip["imgs"]:
                tw = max(40, strip["w"])
                th = strip["h"]
                y = rh + 2 + max(0, (self.STRIP_H - 4 - th) // 2)
                sx = max(x0, 0.0)
                while sx < min(x1, w):
                    tmid = self._t(sx + tw / 2.0)
                    st = p["start"] + (tmid - t0)
                    st = max(p["start"], min(p["end"], st))
                    img = self._thumb_at(p["src"], st)
                    if img is not None:
                        c.create_image(sx, y, image=img, anchor="nw")
                    sx += tw
            else:
                self._draw_waveform(c, p, t0, t1, x0, x1, w, rh)

            # 片段编号
            if x1 - x0 > 34:
                c.create_text(min(max(x0 + 6, 4), w - 30), rh + self.STRIP_H - 10,
                              text=str(idx + 1), anchor="w",
                              fill="#3d5a8a", font=("Consolas", 8, "bold"))

        # 关键帧刻度（流复制模式下显示可切点）
        if self.mode_var.get() == "copy":
            vt0, vt1 = self._t(0), self._t(w)
            for (idx, t0, t1) in self.spans:
                p = self.pieces[idx]
                kfs = self.keyframes.get(p["src"])
                if not kfs:
                    continue
                a = max(t0, vt0)
                b = min(t1, vt1)
                if b <= a:
                    continue
                sa = p["start"] + (a - t0)
                sb = p["start"] + (b - t0)
                lo = bisect.bisect_left(kfs, sa)
                hi = bisect.bisect_right(kfs, sb)
                for k in kfs[lo:hi]:
                    x = self._x(t0 + (k - p["start"]))
                    c.create_line(x, rh - 7, x, rh - 1, fill="#e08a00", width=2)

        # 片段分界（剪切点）
        for k, (idx, t0, t1) in enumerate(self.spans):
            if k == 0:
                continue
            x = self._x(t0)
            if -3 <= x <= w + 3:
                c.create_rectangle(x - 3, rh, x + 3, rh + self.STRIP_H,
                                   fill="#f2f3f6", outline="")
                c.create_line(x, rh, x, rh + self.STRIP_H, fill="#4a5d80", width=2)

        # 选区
        if self.mark_in is not None and self.mark_out is not None:
            a, b = sorted((self.mark_in, self.mark_out))
            xa, xb = self._x(a), self._x(b)
            if xb - xa > 0.5:
                c.create_rectangle(xa, rh, xb, rh + self.STRIP_H,
                                   fill="#ff5a5a", stipple="gray25", outline="#e04a4a", width=2)

        # 入出点标记线
        for tmark, color, tag in ((self.mark_in, "#2e7d32", "IN"), (self.mark_out, "#c62828", "OUT")):
            if tmark is None:
                continue
            x = self._x(tmark)
            c.create_line(x, rh - 2, x, rh + self.STRIP_H + 2, fill=color, width=2, dash=(4, 2))
            c.create_text(x, rh + self.STRIP_H + 8, text=tag, fill=color,
                          font=("Consolas", 8, "bold"))

        self._draw_playhead()
        self._draw_hbar(w)
        self._draw_preview_wave()

    def _ruler_text(self, t, step):
        if step < 1:
            return "%.2fs" % t if step >= 0.1 else "%.2f" % t
        if t < 3600:
            return "%02d:%02d" % (int(t // 60), int(t % 60))
        return fmt_tc(t, ms=False)

    def _draw_playhead(self):
        c = self.tl
        c.delete("playhead")
        self._draw_pv_playhead()
        if not self.pieces:
            return
        x = self._x(self.playhead)
        if x < -8 or x > self._tl_width() + 8:
            return
        c.create_line(x, 0, x, self.RULER_H + self.STRIP_H, fill="#d93025", width=2, tags="playhead")
        c.create_polygon(x - 6, 0, x + 6, 0, x, 9, fill="#d93025", outline="#d93025", tags="playhead")
        c.tag_raise("playhead")

    def _draw_hbar(self, w):
        if self.total <= 0:
            self.hbar.set(0, 1)
            return
        view_dur = w / self.pps
        first = self.view_start / self.total
        last = min(1.0, (self.view_start + view_dur) / self.total)
        self.hbar.set(max(0.0, first), max(0.0, last))

    def _update_status(self):
        if not self.pieces:
            self.status_label.config(text="")
            self.tc_label.config(text="00:00:00.000 / 00:00:00.000")
            return
        txt = "片段 %d 个 · 成片时长 %s" % (len(self.pieces), fmt_tc(self.total))
        if self.mark_in is not None and self.mark_out is not None:
            a, b = sorted((self.mark_in, self.mark_out))
            txt += " · 选区 %s ~ %s（%.3fs）" % (fmt_tc(a), fmt_tc(b), b - a)
        self.status_label.config(text=txt)
        self.tc_label.config(text="%s / %s" % (fmt_tc(self.playhead), fmt_tc(self.total)))
        self.zoom_label.config(text="%.1f px/s" % self.pps)

    # ================= 时间轴交互 =================
    def _tl_press(self, event):
        if not self.pieces:
            return
        self._stop_play()
        self._tl_drag = True
        x, y = event.x, event.y

        # Shift + 拖动 = 直接拖出选区（入点→出点）
        if event.state & 0x1:
            self._drag_mode = "select"
            t0 = max(0.0, min(self.total, self._t(x)))
            self.mark_in = self.mark_out = t0
            self.selected_piece = None
            self._seek_to(t0, immediate=False)
            self._draw_timeline()
            self._update_status()
            return

        self._drag_mode = "seek"
        if y > self.RULER_H:      # 在缩略图区域：同时选中片段
            self.selected_piece = None
            for (idx, t0, t1) in self.spans:
                if self._x(t0) <= x <= self._x(t1):
                    self.selected_piece = idx
                    break
        self._seek_to(self._t(x), immediate=True)
        self._draw_timeline()
        self._update_status()

    def _tl_drag_move(self, event):
        if not self.pieces or not self._tl_drag:
            return
        if self._drag_mode == "select":
            t = max(0.0, min(self.total, self._t(event.x)))
            self.mark_out = t
            self._seek_to(t, immediate=False)
            self._scrub_audio()
            self._draw_timeline()
            self._update_status()
            return
        self._seek_to(self._t(event.x), immediate=False)
        self._scrub_audio()

    def _tl_release(self, event):
        self._tl_drag = False
        if not self.pieces:
            return
        if self._drag_mode == "select":
            self._drag_mode = None
            if self.mark_in is not None and self.mark_out is not None:
                if abs(self.mark_out - self.mark_in) < 1e-3:
                    self.mark_in = self.mark_out = None
            self._draw_timeline()
            self._update_status()
            return
        self._seek_to(self._t(event.x), immediate=True)

    def _tl_double(self, event):
        if not self.pieces:
            return
        t = self._t(event.x)
        idx, _ = self._map_time(t)
        if idx is None:
            return
        _, t0, t1 = self.spans[idx]
        self.mark_in, self.mark_out = t0, t1
        self.selected_piece = idx
        self._draw_timeline()
        self._update_status()

    def _tl_wheel(self, event):
        if not self.pieces:
            return
        delta = -1 if event.delta > 0 else 1
        self.view_start += delta * (self._tl_width() / self.pps) * 0.08
        self._clamp_view()
        self._draw_timeline()

    def _tl_ctrl_wheel(self, event):
        if not self.pieces:
            return
        factor = 1.2 if event.delta > 0 else 1 / 1.2
        self.zoom_at(factor, event.x)

    def _on_hbar(self, *args):
        w = self._tl_width()
        view_dur = w / self.pps
        if args and args[0] == "moveto":
            self.view_start = float(args[1]) * self.total
        elif args and args[0] == "scroll":
            amount = int(args[1])
            unit = view_dur * (0.1 if args[2] == "units" else 0.9)
            self.view_start += amount * unit
        self._clamp_view()
        self._draw_timeline()

    def zoom_at(self, factor, x_anchor=None):
        if not self.pieces:
            return
        w = self._tl_width()
        if x_anchor is None:
            x_anchor = w / 2
        t_anchor = self._t(x_anchor)
        self.pps = max(0.5, min(6000.0, self.pps * factor))
        self.view_start = t_anchor - x_anchor / self.pps
        self._clamp_view()
        self._draw_timeline()
        self._update_status()

    def zoom_fit(self):
        w = self.tl.winfo_width()
        if w <= 20:
            # 控件还没布局（比如 tab 未显示），等 Configure 再适应
            self._fit_pending = True
            return
        if self.total > 0:
            self.pps = max(0.5, (w - 16) / self.total)
        self.view_start = 0.0
        self._fit_pending = False
        self._draw_timeline()
        self._update_status()

    # ================= 播放头 / 预览 =================
    def _seek_to(self, t, immediate=True):
        t = max(0.0, min(self.total, t))
        self.playhead = t
        self._draw_playhead()
        self._update_status()
        if immediate:
            self._frame_req_ts = time.time()
            self.request_frame()
        else:
            # 拖动时按 ~90ms 节流取帧，保证画面跟手
            now = time.time()
            if now - getattr(self, "_frame_req_ts", 0) > 0.09:
                self._frame_req_ts = now
                self.request_frame()

    # ---------- 拖动时的声音反馈 ----------
    def _scrub_audio(self):
        if self._playing or not self.var_sound.get() or not self.pieces:
            return
        idx, st = self._map_time(self.playhead)
        if idx is None:
            return
        src = self.sources[self.pieces[idx]["src"]]
        if not src.get("has_audio"):
            return
        now = time.time()
        if now - getattr(self, "_scrub_ts", 0) < 0.15:
            return
        self._scrub_ts = now
        self._stop_scrub_audio()
        cmd = [FFPLAY, "-nodisp", "-vn", "-autoexit", "-loglevel", "quiet",
               "-ss", "%.3f" % max(0.0, st), "-t", "0.30", src["path"]]
        try:
            self._scrub_proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        except Exception:
            self._scrub_proc = None

    def _stop_scrub_audio(self):
        if getattr(self, "_scrub_proc", None) is not None:
            try:
                self._scrub_proc.kill()
            except Exception:
                pass
            self._scrub_proc = None

    def _preview_size(self, for_play=False):
        w = self.pv.winfo_width()
        h = self.pv.winfo_height()
        if w < 60 or h < 60:
            w, h = 960, 540
        w -= 16
        h -= 16
        info = self.sources[0] if self.sources else {"w": 16, "h": 9}
        if not info.get("w") or not info.get("h"):
            return 960, 540

        zoom = (self.pv_zoom.get() or "适应窗口").strip()
        if zoom != "适应窗口":
            try:
                pct = float(zoom.rstrip("%")) / 100.0
            except ValueError:
                pct = 0.0
            if pct > 0:
                pw = max(80.0, info["w"] * pct)
                ph = max(45.0, info["h"] * pct)
                limit = 1280 if for_play else 2560
                if pw > limit:
                    ph = ph * limit / pw
                    pw = limit
                if for_play and ph > 800:
                    pw = pw * 800 / ph
                    ph = 800
                pw = int(pw) // 2 * 2
                ph = int(ph) // 2 * 2
                return max(80, pw), max(46, ph)

        ar = info["w"] / max(1, info["h"])
        pw = min(w, 1280 if for_play else 960)
        ph = pw / ar
        if ph > h:
            ph = h
            pw = ph * ar
        pw = max(160, int(pw) // 2 * 2)
        ph = max(90, int(ph) // 2 * 2)
        return pw, ph

    def request_frame(self, timeline_t=None, force=False):
        if not self.pieces or self._playing:
            return
        if self.media_kind != "video":
            return
        t = self.playhead if timeline_t is None else timeline_t
        idx, st = self._map_time(t)
        if idx is None:
            return
        src_info = self.sources[self.pieces[idx]["src"]]
        if not src_info["has_video"]:
            return
        src = src_info["path"]
        pw, ph = self._preview_size()
        self._frame_token += 1
        token = self._frame_token

        def work():
            cmd = [FFMPEG, "-hide_banner", "-loglevel", "error",
                   "-ss", "%.3f" % max(0.0, st), "-i", src,
                   "-frames:v", "1", "-vf", "scale=%d:-2" % pw,
                   "-f", "image2pipe", "-vcodec", "png", "-"]
            data = b""
            try:
                p = self._spawn_bg(cmd, stdout=subprocess.PIPE)
                data = p.communicate()[0] or b""
            except Exception:
                data = b""
            if data:
                self._ui(lambda: self._apply_frame(token, data))

        threading.Thread(target=work, daemon=True).start()

    def _apply_frame(self, token, data):
        if token != self._frame_token:
            return
        try:
            img = tk.PhotoImage(data=base64.b64encode(data))
        except Exception:
            return
        self._frame_img = img
        self._last_frame = data
        self._place_preview_image(img)

    def _place_preview_image(self, img=None):
        img = img or self._frame_img
        if img is None:
            return
        w = self.pv.winfo_width()
        h = self.pv.winfo_height()
        self.pv.coords(self.pv_img_id, w // 2 + self._pan_x, h // 2 + self._pan_y)
        self.pv.itemconfig(self.pv_img_id, image=img)
        if self.pv_ph is not None:
            self.pv.itemconfig(self.pv_ph, state="hidden")

    # ---------- 画面缩放 ----------
    def _on_pv_zoom(self):
        self._pan_x = self._pan_y = 0
        if self.media_kind == "video" and self.pieces:
            self.request_frame()
        self._place_preview_image()

    def _pv_pan_start(self, event):
        if self.media_kind != "video":
            return
        self._pan_drag = (event.x, event.y, self._pan_x, self._pan_y)
        self.pv.config(cursor="fleur")

    def _pv_pan_move(self, event):
        d = getattr(self, "_pan_drag", None)
        if not d:
            return
        self._pan_x = d[2] + (event.x - d[0])
        self._pan_y = d[3] + (event.y - d[1])
        self._place_preview_image()

    def _pv_pan_end(self, event):
        self._pan_drag = None
        try:
            self.pv.config(cursor="")
        except Exception:
            pass

    def _on_pv_resize(self, event):
        if self.pv_ph is not None:
            self.pv.coords(self.pv_ph, event.width // 2, event.height // 2)
        self._place_preview_image()
        if self.media_kind == "audio":
            self._draw_preview_wave(force=True)
        if self._resize_job is not None:
            try:
                self.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.after(180, self._after_resize)

    # ---------- 预览区点击 / 拖动（音频素材 = 大波形定位） ----------
    def _pv_press(self, event):
        if self.media_kind == "audio":
            if not self.pieces:
                return
            self._stop_play()
            self._pv_dragging = True
            self._seek_to(self._t(event.x), immediate=True)
        else:
            self.toggle_play()

    def _pv_drag(self, event):
        if getattr(self, "_pv_dragging", False):
            self._seek_to(self._t(event.x), immediate=False)
            self._scrub_audio()

    def _pv_release(self, event):
        if getattr(self, "_pv_dragging", False):
            self._pv_dragging = False
            if self.pieces:
                self._seek_to(self._t(event.x), immediate=True)

    def _after_resize(self):
        self._resize_job = None
        if self.pieces and not self._playing:
            self.request_frame()

    # ================= 播放 =================
    def toggle_play(self):
        if not self.pieces:
            return
        if self._playing:
            self._stop_play()
        else:
            self.start_play()

    def start_play(self):
        if not self.pieces:
            return
        if self.playhead >= self.total - 1e-3:
            self.playhead = 0.0
        self._stop_play()
        self._play_start = self.playhead
        self._playing = True
        self._audio_piece = None
        self.play_btn.config(text="⏸ 暂停")
        self._frame_token += 1

        if self.media_kind == "video":
            self._start_frame_pipeline()

        self._sync_audio_preview()     # 先把声音拉起来，再起表，避免启动延迟造成偏差
        self._play_wall = time.time()
        self._play_tick()

    def _start_frame_pipeline(self):
        pw, ph = self._preview_size(for_play=True)
        pieces = self._pieces_from(self.playhead)
        if not pieces:
            self._playing = False
            return

        inputs, graph = [], []
        for i, (pi, s, e) in enumerate(pieces):
            src = self.sources[self.pieces[pi]["src"]]["path"]
            inputs += ["-ss", "%.3f" % s, "-t", "%.3f" % (e - s), "-i", src]
            graph.append(
                "[%d:v]setpts=PTS-STARTPTS,fps=%d,scale=%d:%d:force_original_aspect_ratio=decrease,"
                "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1[v%d]" %
                (i, self.PV_FPS, pw, ph, pw, ph, i))
        if len(pieces) == 1:
            graph.append("[v0]null[vout]")
        else:
            graph.append("".join("[v%d]" % i for i in range(len(pieces))) +
                         "concat=n=%d:v=1:a=0[vout]" % len(pieces))

        cmd = ([FFMPEG, "-hide_banner", "-loglevel", "error", "-y"] + inputs +
               ["-filter_complex", ";".join(graph), "-map", "[vout]", "-an",
                "-f", "image2pipe", "-vcodec", "png", "-"])
        try:
            self._play_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            self.log("播放启动失败：%s" % e)
            return

        self._play_stop = threading.Event()
        self._play_buf = deque()
        self._play_cond = threading.Condition(threading.Lock())
        proc = self._play_proc
        threading.Thread(target=self._play_reader, args=(proc,), daemon=True).start()

    def _play_reader(self, proc):
        """把 PNG 帧读进缓冲区；缓冲区满时阻塞，形成背压。"""
        i = 0
        stop = self._play_stop
        cond = self._play_cond
        buf = self._play_buf
        while not stop.is_set():
            data = read_png_frame(proc.stdout)
            if not data:
                break
            with cond:
                while len(buf) >= 24 and not stop.is_set():
                    cond.wait(0.2)
                if stop.is_set():
                    break
                buf.append((i, data))
                cond.notify_all()
            i += 1
        stop.set()

    def _play_tick(self):
        if not self._playing:
            return
        if self.media_kind == "video" and self._play_buf is not None:
            target = (time.time() - self._play_wall) * self.PV_FPS
            frame = None
            with self._play_cond:
                # 丢弃已经过期的帧，取最接近当前时刻的一帧
                while self._play_buf and self._play_buf[0][0] < target - 1:
                    self._play_buf.popleft()
                if self._play_buf and self._play_buf[0][0] <= target + 0.6:
                    frame = self._play_buf.popleft()
                self._play_cond.notify_all()

            if frame is not None:
                i, data = frame
                try:
                    img = tk.PhotoImage(data=base64.b64encode(data))
                    self._frame_img = img
                    self._place_preview_image(img)
                except Exception:
                    pass
                self.playhead = min(self.total, self._play_start + i / float(self.PV_FPS))
                self._draw_playhead()
                self._auto_scroll()
                self._update_status()
            elif self._play_proc is not None and self._play_proc.poll() is not None:
                with self._play_cond:
                    empty = not self._play_buf
                if empty:
                    self._stop_play(ended=True)
                    return
        else:
            # 纯音频：按墙钟推进播放头
            self.playhead = min(self.total, self._play_start + (time.time() - self._play_wall))
            self._draw_playhead()
            self._auto_scroll()
            self._update_status()
            if self.playhead >= self.total - 1e-3:
                self._stop_play(ended=True)
                return

        self._sync_audio_preview()
        self._play_after = self.after(16, self._play_tick)

    # ---------- 预览声音（ffplay -nodisp） ----------
    def _on_sound_toggle(self):
        if not self.var_sound.get():
            self._stop_audio_preview()

    def _sync_audio_preview(self):
        if not self.var_sound.get():
            return
        idx, st = self._map_time(self.playhead)
        if idx is None or idx == self._audio_piece:
            return
        self._stop_audio_preview()
        self._audio_piece = idx
        piece = self.pieces[idx]
        src = self.sources[piece["src"]]
        if not src["has_audio"]:
            return
        dur = max(0.05, piece["end"] - st)
        cmd = [FFPLAY, "-nodisp", "-vn", "-autoexit", "-loglevel", "quiet",
               "-ss", "%.3f" % max(0.0, st), "-t", "%.3f" % dur, src["path"]]
        try:
            self._audio_proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        except Exception:
            self._audio_proc = None

    def _stop_audio_preview(self):
        if self._audio_proc is not None:
            try:
                self._audio_proc.kill()
            except Exception:
                pass
            self._audio_proc = None
        self._audio_piece = None

    def _auto_scroll(self):
        x = self._x(self.playhead)
        w = self._tl_width()
        if x > w - 30 or x < 0:
            self.view_start = max(0.0, self.playhead - (w * 0.5) / self.pps)
            self._clamp_view()
            self._draw_timeline()

    def _stop_play(self, ended=False):
        self._playing = False
        self._stop_audio_preview()
        self._stop_scrub_audio()
        if self._play_after is not None:
            try:
                self.after_cancel(self._play_after)
            except Exception:
                pass
            self._play_after = None
        if self._play_stop is not None:
            self._play_stop.set()
        if self._play_cond is not None:
            try:
                with self._play_cond:
                    self._play_cond.notify_all()
            except Exception:
                pass
        if self._play_proc is not None:
            try:
                self._play_proc.kill()
            except Exception:
                pass
            self._play_proc = None
        if self._play_buf is not None:
            self._play_buf.clear()
        self._play_buf = None
        self._play_stop = None
        try:
            self.play_btn.config(text="▶ 播放")
        except Exception:
            pass
        if ended:
            self.playhead = self.total
            self._draw_playhead()
            self._update_status()

    def goto_start(self):
        self._stop_play()
        self._seek_to(0.0)

    def goto_end(self):
        self._stop_play()
        self._seek_to(self.total)

    def step(self, direction):
        if not self.pieces:
            return
        self._stop_play()
        info = self.sources[self.pieces[max(0, self._map_time(self.playhead)[0])]["src"]]
        frame = 1.0 / info["fps"] if info["fps"] else 0.04
        self._seek_to(self.playhead + direction * frame)

    # ================= 编辑操作 =================
    def _push_undo(self):
        self.undo_stack.append(copy.deepcopy(self.pieces))
        if len(self.undo_stack) > 100:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def undo(self):
        if not self.undo_stack:
            return
        self._stop_play()
        self.redo_stack.append(copy.deepcopy(self.pieces))
        self.pieces = self.undo_stack.pop()
        self._after_edit()

    def redo(self):
        if not self.redo_stack:
            return
        self._stop_play()
        self.undo_stack.append(copy.deepcopy(self.pieces))
        self.pieces = self.redo_stack.pop()
        self._after_edit()

    def _after_edit(self):
        self._layout()
        self.selected_piece = None
        if self.mark_in is not None:
            self.mark_in = min(self.mark_in, self.total)
        if self.mark_out is not None:
            self.mark_out = min(self.mark_out, self.total)
        self.playhead = min(self.playhead, self.total)
        self._clamp_view()
        self._draw_timeline()
        self._update_status()
        self.request_frame(force=True)

    def _selection(self):
        if self.mark_in is None or self.mark_out is None:
            return None
        a, b = sorted((self.mark_in, self.mark_out))
        if b - a < 1e-3:
            return None
        return a, b

    def mark_in_at_playhead(self):
        if not self.pieces:
            return
        t = self._snap_keyframe(self.playhead)
        if abs(t - self.playhead) > 1e-3:
            self.log("入点已吸附到关键帧 %s（流复制只能切在关键帧）" % fmt_tc(t))
        self.mark_in = t
        if self.mark_out is not None and self.mark_out <= self.mark_in:
            self.mark_out = None
        self._draw_timeline()
        self._update_status()

    def mark_out_at_playhead(self):
        if not self.pieces:
            return
        t = self._snap_keyframe(self.playhead)
        if abs(t - self.playhead) > 1e-3:
            self.log("出点已吸附到关键帧 %s（流复制只能切在关键帧）" % fmt_tc(t))
        self.mark_out = t
        if self.mark_in is not None and self.mark_in >= self.mark_out:
            self.mark_in = None
        self._draw_timeline()
        self._update_status()

    def clear_marks(self):
        self.mark_in = self.mark_out = None
        self._draw_timeline()
        self._update_status()

    def split_at(self, t):
        for (idx, t0, t1) in self.spans:
            if t0 + 1e-3 < t < t1 - 1e-3:
                p = self.pieces[idx]
                cut = p["start"] + (t - t0)
                if cut - p["start"] < 1e-3 or p["end"] - cut < 1e-3:
                    return False
                self.pieces[idx:idx + 1] = [
                    {"src": p["src"], "start": p["start"], "end": cut},
                    {"src": p["src"], "start": cut, "end": p["end"]},
                ]
                return True
        return False

    def split_selection(self):
        if not self.pieces:
            return
        self._stop_play()
        sel = self._selection()
        self._push_undo()
        n = 0
        mid = None
        if sel:
            a, b = sel
            if self.mode_var.get() == "copy":
                a, b = self._snap_keyframe(a), self._snap_keyframe(b)
                sel = (a, b) if b > a else None
            if sel:
                mid = (a + b) / 2.0
                for t in sorted(sel, reverse=True):
                    if self.split_at(t):
                        n += 1
                        self._layout()
        else:
            mid = self.playhead
            tcut = self._snap_keyframe(self.playhead) if self.mode_var.get() == "copy" else self.playhead
            if self.split_at(tcut):
                n += 1
                self._layout()
        self._after_edit()
        if mid is not None and self.spans:
            for (idx, t0, t1) in self.spans:
                if t0 <= mid <= t1:
                    self.selected_piece = idx
                    break
            self._draw_timeline()
        self.log("剪断：%d 处（选中片段后按 Delete 可删除）" % n)

    def _remove_range(self, a, b):
        a = max(0.0, a)
        b = min(self.total, b)
        if b - a < 1e-4:
            return
        new = []
        for (idx, t0, t1) in self.spans:
            p = self.pieces[idx]
            if t1 <= a + 1e-6 or t0 >= b - 1e-6:
                new.append(dict(p))
                continue
            if t0 < a - 1e-6:
                new.append({"src": p["src"], "start": p["start"],
                            "end": p["start"] + (a - t0)})
            if t1 > b + 1e-6:
                new.append({"src": p["src"], "start": p["start"] + (b - t0),
                            "end": p["end"]})
        self.pieces = [p for p in new if p["end"] - p["start"] > 0.01]

    def delete_selection(self):
        if not self.pieces:
            return
        sel = self._selection()
        if not sel:
            messagebox.showinfo("提示", "请先按 I / O 标记入点与出点，或用鼠标在时间轴上拖出选区。")
            return
        self._stop_play()
        self._push_undo()
        a, b = sel
        self._remove_range(a, b)
        self.mark_in = self.mark_out = None
        self.playhead = a
        self._after_edit()
        self.log("已删除选区 %s ~ %s" % (fmt_tc(a), fmt_tc(b)))

    def keep_selection(self):
        if not self.pieces:
            return
        sel = self._selection()
        if not sel:
            messagebox.showinfo("提示", "请先标记入点与出点。")
            return
        self._stop_play()
        self._push_undo()
        a, b = sel
        self._remove_range(b, self.total)
        self._layout()
        self._remove_range(0, a)
        self.mark_in, self.mark_out = 0.0, max(0.0, b - a)
        self.playhead = 0.0
        self._after_edit()
        self.log("只保留选区：%.3fs" % (b - a))

    def delete_selected_piece(self):
        if not self.pieces:
            return
        idx = self.selected_piece
        if idx is None:
            idx, _ = self._map_time(self.playhead)
        if idx is None or idx >= len(self.pieces):
            return
        self._stop_play()
        self._push_undo()
        _, t0, _ = self.spans[idx]
        del self.pieces[idx]
        self.playhead = min(t0, max(0.0, self.total))
        self._after_edit()
        self.log("已删除片段 %d" % (idx + 1))

    def reset_cut(self):
        if not self.sources:
            return
        self._stop_play()
        self._push_undo()
        self.pieces = [{"src": i, "start": 0.0, "end": s["duration"]}
                       for i, s in enumerate(self.sources)]
        self.mark_in = self.mark_out = None
        self.playhead = 0.0
        self._after_edit()
        self.log("已还原为原始素材")

    # ================= 导出 =================
    def browse_output(self):
        if not self.sources:
            return
        init = self.out_var.get().strip()
        fmt = self._current_fmt()
        ext = CONTAINER_EXTS.get(fmt, "." + fmt)
        if fmt in AUDIO_CONTAINERS:
            types = [(fmt.upper(), "*" + ext)] + [
                (f.upper(), "*" + CONTAINER_EXTS[f]) for f in AUDIO_CONTAINERS if f != fmt]
        else:
            types = [(fmt.upper(), "*" + ext)] + [
                (f.upper(), "*" + CONTAINER_EXTS[f]) for f in VIDEO_CONTAINERS if f != fmt]
        types.append(("所有文件", "*.*"))
        path = filedialog.asksaveasfilename(
            title="选择输出文件", defaultextension=ext,
            initialfile=os.path.basename(init) if init else "output" + ext,
            initialdir=os.path.dirname(init) if init else APP_DIR,
            filetypes=types)
        if path:
            self.out_var.set(path)

    def _on_stream_toggle(self):
        """视频码率只在选了具体视频编码时出现；取消勾选则禁用对应编码下拉。"""
        try:
            copy = self.mode_var.get() == "copy"
            show_bit = (not copy) and bool(self.do_video.get()) and self.vcodec_var.get() != "default"
            if show_bit:
                if not self.vbit_combo.winfo_ismapped():
                    self.vbit_label.pack(side="left", padx=(10, 3))
                    self.vbit_combo.pack(side="left")
            else:
                self.vbit_label.pack_forget()
                self.vbit_combo.pack_forget()
            if copy:
                self.vcodec_combo.config(state="disabled")
                self.acodec_combo.config(state="disabled")
            else:
                self.vcodec_combo.config(state="readonly" if self.do_video.get() else "disabled")
                self.acodec_combo.config(state="readonly" if self.do_audio.get() else "disabled")
            self._update_containers()
        except Exception:
            pass

    # ---------- 封装格式 / 处理方式 ----------
    def _source_fmt(self):
        """流复制时的目标容器：按真实编码/容器推导，不看扩展名。"""
        if not self.sources:
            return "mp4"
        has_v = any(s["has_video"] for s in self.sources)
        if self.do_video.get() and has_v:
            fn = (self.sources[0].get("format_name") or "").split(",")[0].strip().lower()
            return VIDEO_COPY_FMT.get(fn, "mkv")
        return audio_copy_fmt(self.sources[0].get("acodec"))

    def _current_fmt(self):
        if self.mode_var.get() == "copy":
            return self._source_fmt()
        return (self.fmt_var.get() or "mp4").strip()

    def _update_containers(self):
        if not hasattr(self, "fmt_combo"):
            return
        if self.mode_var.get() == "copy":
            fmt = self._source_fmt()
            self.fmt_combo.config(values=[fmt], state="disabled")
            self.fmt_var.set(fmt)
            self._apply_format_ext()
            return
        do_v = bool(self.do_video.get())
        do_a = bool(self.do_audio.get())
        vals = AUDIO_CONTAINERS if (do_a and not do_v) else VIDEO_CONTAINERS
        self.fmt_combo.config(values=vals, state="readonly")
        if self.fmt_var.get() not in vals:
            self.fmt_var.set(vals[0])
        self._apply_format_ext()

    def _apply_format_ext(self):
        if not hasattr(self, "out_var"):
            return
        fmt = self._current_fmt()
        ext = CONTAINER_EXTS.get(fmt, "." + fmt)
        path = self.out_var.get().strip()
        if path:
            base, _ = os.path.splitext(path)
            self.out_var.set(base + ext)

    def _on_mode_change(self):
        if self.mode_var.get() == "copy":
            hint = "流复制：不重新编码，切点对齐关键帧；容器按实际编码自动选择"
            if self.sources:
                real_ext = os.path.splitext(self.sources[0]["path"])[1].lower().lstrip(".")
                fmt = self._source_fmt()
                if real_ext and real_ext != fmt:
                    hint += "（.%s → .%s）" % (real_ext, fmt)
                    info0 = self.sources[0]
                    codec_desc = info0.get("acodec") or "—"
                    if info0.get("has_video"):
                        codec_desc = "%s + %s" % (info0.get("vcodec") or "—", codec_desc)
                    self.log("注意：源文件扩展名是 .%s，实际编码为 %s，"
                             "无法装进 .%s 容器；流复制输出自动改为 .%s（同样无损）。" % (
                                 real_ext, codec_desc, real_ext, fmt))
            self.fmt_hint.config(text=hint)
            self._ensure_keyframes()
        else:
            self.fmt_hint.config(text="")
        self._on_stream_toggle()
        self._apply_format_ext()
        self._draw_timeline()

    # ---------- 关键帧（流复制对齐用） ----------
    def _ensure_keyframes(self):
        for idx, s in enumerate(self.sources):
            if not s.get("has_video") or idx in self.keyframes:
                continue
            self.keyframes[idx] = []

            def work(i=idx, path=s["path"]):
                kfs = probe_keyframes(path)
                self._ui(lambda: self._store_keyframes(i, kfs))

            threading.Thread(target=work, daemon=True).start()

    def _store_keyframes(self, idx, kfs):
        self.keyframes[idx] = kfs
        self._draw_timeline()

    def _snap_keyframe(self, timeline_t):
        """流复制模式下把时间点吸附到最近的关键帧。"""
        if self.mode_var.get() != "copy" or not self.pieces:
            return timeline_t
        idx, st = self._map_time(timeline_t)
        if idx is None:
            return timeline_t
        piece = self.pieces[idx]
        kfs = self.keyframes.get(piece["src"]) or []
        if not kfs:
            return timeline_t
        i = bisect.bisect_left(kfs, st)
        cands = []
        if i > 0:
            cands.append(kfs[i - 1])
        if i < len(kfs):
            cands.append(kfs[i])
        if not cands:
            return timeline_t
        best = min(cands, key=lambda k: abs(k - st))
        if best < piece["start"] - 0.05 or best > piece["end"] + 0.05:
            return timeline_t
        _, t0, _ = self.spans[idx]
        return max(0.0, min(self.total, t0 + (best - piece["start"])))

    def _update_stream_checks(self, reset=False):
        has_v = any(s["has_video"] for s in self.sources) if self.sources else False
        has_a = any(s["has_audio"] for s in self.sources) if self.sources else False
        try:
            self.chk_video.config(state=("normal" if has_v else "disabled"))
            self.chk_audio.config(state=("normal" if has_a else "disabled"))
            self.zoom_combo.config(state=("readonly" if self.media_kind == "video" else "disabled"))
        except Exception:
            pass
        if reset:
            self.do_video.set(has_v)
            self.do_audio.set(has_a)
        if not has_v:
            self.do_video.set(False)
        if not has_a:
            self.do_audio.set(False)
        self._on_stream_toggle()

    def _copy_boundary_check(self):
        """返回不在关键帧上的切点（流复制会从上一个关键帧开始）。"""
        bad = []
        for p in self.pieces:
            kfs = self.keyframes.get(p["src"]) or []
            if not kfs:
                continue
            dur = self.sources[p["src"]]["duration"]
            for t in (p["start"], p["end"]):
                if t <= 0.02 or t >= dur - 0.02:
                    continue
                i = bisect.bisect_left(kfs, t)
                cands = []
                if i > 0:
                    cands.append(kfs[i - 1])
                if i < len(kfs):
                    cands.append(kfs[i])
                if not cands:
                    continue
                if abs(min(cands, key=lambda k: abs(k - t)) - t) > 0.05:
                    bad.append(t)
        return bad

    def _copy_signature(self, src):
        return (src.get("vcodec"), src.get("w"), src.get("h"),
                round(src.get("fps") or 0, 2),
                src.get("acodec"), src.get("sample_rate"), src.get("channels"))

    def _build_copy_cmd(self, out_path):
        """流复制：concat 分离器 + -c copy，全程不重编码。"""
        # 各段编码参数必须一致，否则无法直接拼接
        sigs = set()
        for p in self.pieces:
            sigs.add(self._copy_signature(self.sources[p["src"]]))
        if len(sigs) > 1:
            return "mismatch", None

        lines = []
        for p in self.pieces:
            src = self.sources[p["src"]]
            path = src["path"].replace("\\", "/").replace("'", "'\\''")
            lines.append("file '%s'" % path)
            if p["start"] > 0.001:
                lines.append("inpoint %.3f" % p["start"])
            if p["end"] < src["duration"] - 0.001:
                lines.append("outpoint %.3f" % p["end"])

        fd, listfile = tempfile.mkstemp(prefix="ffgui_concat_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        cmd = [FFMPEG, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
               "-i", listfile, "-c", "copy"]
        has_v = any(s["has_video"] for s in self.sources)
        has_a = any(s["has_audio"] for s in self.sources)
        do_v = self.do_video.get() and has_v
        do_a = self.do_audio.get() and has_a
        if do_v and not do_a:
            cmd += ["-map", "0:v:0?", "-an"]
        elif do_a and not do_v:
            cmd += ["-map", "0:a:0?", "-vn"]
        else:
            cmd += ["-map", "0:v:0?", "-map", "0:a:0?"]
        cmd.append(out_path)
        return cmd, listfile

    def _build_export_cmd(self, out_path):
        do_video = self.do_video.get() and any(s["has_video"] for s in self.sources)
        do_audio = self.do_audio.get() and any(s["has_audio"] for s in self.sources)

        first = self.sources[self.pieces[0]["src"]]
        W = max(2, int(first["w"]) // 2 * 2)
        H = max(2, int(first["h"]) // 2 * 2)
        fps = first["fps"] or 25.0
        fps_str = ("%.6f" % fps).rstrip("0").rstrip(".")

        inputs, graph = [], []
        input_count = 0
        n = len(self.pieces)

        for i, p in enumerate(self.pieces):
            src = self.sources[p["src"]]
            d = max(0.02, p["end"] - p["start"])
            inputs += ["-ss", "%.3f" % p["start"], "-t", "%.3f" % d, "-i", src["path"]]
            my_idx = input_count
            input_count += 1

            if do_video:
                graph.append(
                    "[%d:v]setpts=PTS-STARTPTS,scale=%d:%d:force_original_aspect_ratio=decrease,"
                    "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=%s[v%d]" %
                    (my_idx, W, H, W, H, fps_str, i))
            if do_audio:
                if src["has_audio"]:
                    graph.append(
                        "[%d:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                        "asetpts=PTS-STARTPTS,apad=whole_dur=%.3f[a%d]" % (my_idx, d, i))
                else:
                    inputs += ["-f", "lavfi", "-t", "%.3f" % d, "-i", "anullsrc=r=48000:cl=stereo"]
                    graph.append("[%d:a]asetpts=PTS-STARTPTS[a%d]" % (input_count, i))
                    input_count += 1

        # 拼接
        if do_video and do_audio:
            if n == 1:
                graph.append("[v0]null[vout]")
                graph.append("[a0]anull[aout]")
            else:
                graph.append("".join("[v%d][a%d]" % (i, i) for i in range(n)) +
                             "concat=n=%d:v=1:a=1[vout][aout]" % n)
        elif do_video:
            if n == 1:
                graph.append("[v0]null[vout]")
            else:
                graph.append("".join("[v%d]" % i for i in range(n)) +
                             "concat=n=%d:v=1:a=0[vout]" % n)
        elif do_audio:
            if n == 1:
                graph.append("[a0]anull[aout]")
            else:
                graph.append("".join("[a%d]" % i for i in range(n)) +
                             "concat=n=%d:v=0:a=1[aout]" % n)

        cmd = [FFMPEG, "-hide_banner", "-y"] + inputs
        cmd += ["-filter_complex", ";".join(graph)]
        if do_video:
            cmd += ["-map", "[vout]"]
        if do_audio:
            cmd += ["-map", "[aout]"]

        # 编码：default = 不指定参数（用 ffmpeg 按容器默认编码）
        vcodec = self.vcodec_var.get().strip()
        acodec = self.acodec_var.get().strip()
        if do_video and vcodec and vcodec != "default":
            vparams = config["video_encodings"].get(vcodec, "")
            if vparams:
                cmd += vparams.split()
            bit = (self.vbit_var.get() or "").strip()
            if bit and bit != "（不指定）":
                cmd += ["-b:v", config["bitrates"].get(bit, bit)]
        if do_audio and acodec and acodec != "default":
            aparams = config["audio_encodings"].get(acodec, "")
            if aparams:
                cmd += aparams.split()
        if not do_video and not do_audio:
            return None, False
        cmd.append(out_path)
        return cmd, do_audio

    def start_export(self):
        if not self.pieces:
            messagebox.showinfo("提示", "请先打开文件并剪辑。")
            return
        if not self.do_video.get() and not self.do_audio.get():
            messagebox.showinfo("提示", "「视频」「音频」至少要勾选一个。")
            return
        out_path = self.out_var.get().strip()
        if not out_path:
            messagebox.showinfo("提示", "请先设置输出文件路径。")
            return
        # 流复制时容器必须与编码匹配，扩展名被手工改错就自动纠正
        if self.mode_var.get() == "copy":
            want = CONTAINER_EXTS.get(self._current_fmt(), "")
            got = os.path.splitext(out_path)[1].lower()
            if want and got != want:
                fixed = os.path.splitext(out_path)[0] + want
                self.log("流复制输出容器必须用 %s（源音频编码 %s 无法装进 %s），已自动改为：%s" % (
                    want, self.sources[0].get("acodec") or "—", got or "无扩展名", fixed))
                out_path = fixed
                self.out_var.set(out_path)
        out_dir = os.path.dirname(out_path)
        if out_dir and not os.path.isdir(out_dir):
            try:
                os.makedirs(out_dir)
            except Exception as e:
                messagebox.showerror("错误", "创建输出目录失败：%s" % e)
                return
        if os.path.exists(out_path):
            if not messagebox.askyesno("确认", "文件已存在，是否覆盖？\n%s" % out_path):
                return

        self._stop_play()
        listfile = None
        if self.mode_var.get() == "copy":
            cmd, listfile = self._build_copy_cmd(out_path)
            if cmd == "mismatch":
                messagebox.showwarning(
                    "无法流复制拼接",
                    "各片段的编码参数不一致（分辨率/帧率/编码不同），无法直接拼接。\n"
                    "请改用「重编码」，或清空后只对同一素材剪辑。")
                return
            self.log("模式：流复制（不重新编码，切点对齐关键帧）")
            bad = self._copy_boundary_check()
            if bad:
                self.log("提示：有 %d 处切点不在关键帧上，流复制会从上一个关键帧开始，可能多出少量画面"
                         "（时间轴上方橙色刻度是关键帧，可先切到刻度上）。" % len(bad))
        else:
            cmd, any_audio = self._build_export_cmd(out_path)
            if cmd is None:
                messagebox.showinfo("提示", "「视频」「音频」至少要勾选一个。")
                return
        self.log("开始导出：%s" % out_path.replace(os.sep, "/"))
        self.log("命令：" + " ".join('"%s"' % c if " " in c else c for c in cmd))
        self.export_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.progress.config(value=0)
        total = max(0.01, self.total)

        def work():
            err_tail = []
            try:
                self._export_proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    universal_newlines=True, encoding="utf-8", errors="replace",
                    creationflags=CREATE_NO_WINDOW)
                tpat = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
                for line in iter(self._export_proc.stderr.readline, ""):
                    if not line:
                        break
                    err_tail.append(line.rstrip())
                    if len(err_tail) > 40:
                        err_tail.pop(0)
                    m = tpat.search(line)
                    if m:
                        sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                        pct = min(100.0, sec / total * 100.0)
                        self._ui(lambda p=pct: self.progress.config(value=p))
                rc = self._export_proc.wait()
            except Exception as e:
                self._ui(lambda: self.log("导出异常：%s" % e))
                rc = -1
            self._export_proc = None
            if listfile:
                try:
                    os.remove(listfile)
                except Exception:
                    pass
            self._ui(lambda: self._export_done(rc, out_path, err_tail))

        self._export_thread = threading.Thread(target=work, daemon=True)
        self._export_thread.start()

    def _cleanup_failed_output(self, out_path):
        """导出失败/取消时清掉半成品，避免留下 0 字节文件。"""
        try:
            if out_path and os.path.exists(out_path) and os.path.getsize(out_path) >= 0:
                os.remove(out_path)
                self.log("已删除未完成的输出文件。")
        except Exception:
            pass

    def _export_done(self, rc, out_path, err_tail):
        self.export_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED)
        if getattr(self, "_export_cancelled", False):
            self._export_cancelled = False
            self.progress.config(value=0)
            self.log("已取消导出。")
            self._cleanup_failed_output(out_path)
            return
        if rc == 0 and os.path.exists(out_path):
            self.progress.config(value=100)
            self.log("导出成功：%s" % out_path.replace(os.sep, "/"))
            if messagebox.askyesno("完成", "导出成功：\n%s\n\n是否打开所在文件夹？" % out_path):
                try:
                    subprocess.Popen('explorer /select,"%s"' % out_path.replace("/", "\\"))
                except Exception:
                    pass
        elif rc == -1:
            self.log("导出已取消或失败。")
            self._cleanup_failed_output(out_path)
        else:
            self.log("导出失败（返回码 %s）：" % rc)
            for l in err_tail[-12:]:
                self.log("  " + l)
            self._cleanup_failed_output(out_path)

    def cancel_export(self):
        if self._export_proc is not None:
            self._export_cancelled = True
            try:
                self._export_proc.kill()
            except Exception:
                pass
            self.log("正在取消导出…")

    # ================= 快捷键 =================
    def _bind_keys(self):
        top = self.winfo_toplevel()
        top.bind("<KeyPress>", self._on_key, add="+")

    def _on_key(self, event):
        if not self.app.is_edit_active():
            return
        cls = ""
        try:
            cls = event.widget.winfo_class()
        except Exception:
            pass
        if cls in ("Entry", "TEntry", "Text", "TCombobox", "Combobox", "Spinbox", "TSpinbox"):
            return
        key = event.keysym
        ctrl = bool(event.state & 0x4)

        if key == "space":
            if cls in ("TButton", "Button", "TCheckbutton", "Checkbutton", "TRadiobutton"):
                return
            self.toggle_play()
        elif key == "Left":
            if ctrl:
                return
            if event.state & 0x1:
                self._stop_play()
                self._seek_to(self.playhead - 1.0)
            else:
                self.step(-1)
        elif key == "Right":
            if ctrl:
                return
            if event.state & 0x1:
                self._stop_play()
                self._seek_to(self.playhead + 1.0)
            else:
                self.step(1)
        elif key in ("i", "I"):
            self.mark_in_at_playhead()
        elif key in ("o", "O"):
            self.mark_out_at_playhead()
        elif key in ("Delete", "BackSpace"):
            if self._selection():
                self.delete_selection()
            else:
                self.delete_selected_piece()
        elif key == "Escape":
            self.clear_marks()
        elif key == "Home":
            self.goto_start()
        elif key == "End":
            self.goto_end()
        elif key.lower() == "z" and ctrl:
            if event.state & 0x1:
                self.redo()
            else:
                self.undo()
        elif key.lower() == "y" and ctrl:
            self.redo()
        elif key in ("plus", "equal"):
            self.zoom_at(1.4)
        elif key == "minus":
            self.zoom_at(1 / 1.4)


# ---------------------------
# 6. 主窗口
# ---------------------------
class FFmpegDownloadDialog(tk.Toplevel):
    """下载 / 更新 FFmpeg 的小窗口（后台线程 + 进度条 + 可取消）。"""

    def __init__(self, master, update=False, on_done=None):
        super().__init__(master)
        self.master_app = master
        self.update_mode = update
        self.on_done = on_done
        self._cancel = threading.Event()
        self._done = False

        self.title("更新 FFmpeg" if update else "获取 FFmpeg")
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="来源：gyan.dev 官方 Windows 构建（约 115 MB）",
                  foreground="#666666").pack(anchor="w")
        self.msg = ttk.Label(body, text="正在获取版本信息…", wraplength=520, justify="left")
        self.msg.pack(anchor="w", pady=(8, 6))

        self.bar = ttk.Progressbar(body, mode="determinate", maximum=100, length=520)
        self.bar.pack(fill="x")
        self.sub = ttk.Label(body, text=" ", foreground="#666666")
        self.sub.pack(anchor="w", pady=(6, 10))

        self.btn = ttk.Button(body, text="取消", command=self._on_cancel)
        self.btn.pack(anchor="e")

        self.update_idletasks()
        w = max(560, self.winfo_reqwidth())
        h = self.winfo_reqheight()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, (sh - h) // 3))
        try:
            self.grab_set()
        except Exception:
            pass
        self.after(150, self._start)

    # ---------- 线程安全的 UI 更新 ----------
    def _ui(self, fn):
        try:
            self.after(0, fn)
        except Exception:
            pass

    def _set(self, msg=None, sub=None, pct=None):
        if msg is not None:
            self.msg.config(text=msg)
        if sub is not None:
            self.sub.config(text=sub)
        if pct is not None:
            self.bar.config(value=max(0, min(100, pct)))

    def _on_cancel(self):
        self._cancel.set()
        if self._done:
            self._close()
        else:
            self._set(msg="正在取消…")

    def _finish(self, ok, text):
        self._done = True
        self._set(msg=text, sub="", pct=100 if ok else None)
        self.btn.config(text="关闭", command=self._close)
        if ok:
            if self.on_done:
                try:
                    self.on_done()
                except Exception:
                    pass
            self.after(1200, self._close)

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    # ---------- 主流程 ----------
    def _start(self):
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        try:
            remote = self._remote_version()
            if self._cancel.is_set():
                raise RuntimeError("已取消")
            if self.update_mode and remote:
                local = ffmpeg_version()
                if local and local.split("-")[0] == remote:
                    self._ui(lambda: self._finish(
                        True, "当前已是最新版本 %s，无需更新。" % remote))
                    return

            self._ui(lambda: self._set(
                msg="正在下载 FFmpeg %s…" % (remote or ""), pct=0))
            zip_path = self._download()
            if self._cancel.is_set():
                raise RuntimeError("已取消")

            self._ui(lambda: self._set(msg="正在解压安装…", pct=100))
            self._install(zip_path)
            try:
                os.remove(zip_path)
            except Exception:
                pass

            refresh_tools()
            ver = ffmpeg_version()
            if not ver:
                raise RuntimeError("安装后仍无法运行 ffmpeg，可能被杀毒软件拦截")
            self._ui(lambda: self._finish(
                True, "已安装 FFmpeg %s（%s）" % (ver.split("-")[0], APP_DIR)))
        except Exception as e:
            msg = str(e) or e.__class__.__name__
            self._ui(lambda: self._finish(False, "失败：%s" % msg))

    def _remote_version(self):
        try:
            req = urllib.request.Request(FFMPEG_VER_URL, headers={"User-Agent": "wingwy-editor"})
            with urllib.request.urlopen(req, timeout=12) as r:
                return (r.read().decode("utf-8", "replace").strip().splitlines() or [""])[0].strip()
        except Exception:
            return ""

    def _download(self):
        fd, tmp = tempfile.mkstemp(prefix="ffmpeg_", suffix=".zip")
        os.close(fd)
        req = urllib.request.Request(FFMPEG_ZIP_URL, headers={"User-Agent": "wingwy-editor"})
        t0 = time.time()
        done = 0
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                with open(tmp, "wb") as f:
                    while True:
                        if self._cancel.is_set():
                            raise RuntimeError("已取消")
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        el = max(0.001, time.time() - t0)
                        pct = done * 100.0 / total if total else 0
                        mb = done / 1048576.0
                        tot = ("%.1f MB" % (total / 1048576.0)) if total else "?"
                        sub = "已下载 %.1f MB / %s · %.1f MB/s" % (mb, tot, mb / el)
                        self._ui(lambda p=pct, s=sub: self._set(pct=p, sub=s))
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass
            raise
        return tmp

    def _install(self, zip_path):
        # 先停掉所有在跑的 ffmpeg，否则 Windows 下文件被占用无法覆盖
        try:
            self.master_app.trim_tab.shutdown()
        except Exception:
            pass

        names = ["ffmpeg.exe", "ffprobe.exe", "ffplay.exe"]
        with zipfile.ZipFile(zip_path) as z:
            entries = z.namelist()
            installed = []
            for name in names:
                cand = [e for e in entries
                        if e.replace("\\", "/").lower().endswith("/bin/" + name)]
                if not cand:
                    cand = [e for e in entries
                            if e.replace("\\", "/").lower().endswith("/" + name)]
                if not cand:
                    continue
                target = os.path.join(APP_DIR, name)
                with z.open(cand[0]) as src:
                    data = src.read()
                for attempt in range(3):
                    try:
                        with open(target, "wb") as dst:
                            dst.write(data)
                        installed.append(name)
                        break
                    except PermissionError:
                        time.sleep(0.6)
                    except Exception:
                        break
            if "ffmpeg.exe" not in installed:
                raise RuntimeError("压缩包中未找到 ffmpeg.exe，或目标目录不可写")


class FFmpegGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("%s  v%s" % (APP_NAME, APP_VERSION))
        self._dlg = None

        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = max(1080, min(2200, sw - 80))
        h = max(720, min(1400, sh - 100))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.minsize(1000, 680)

        style = ttk.Style(self)
        for theme in ("vista", "winnative", "clam"):
            try:
                style.theme_use(theme)
                break
            except Exception:
                continue
        style.configure("TNotebook.Tab", padding=(18, 7), font=("Microsoft YaHei UI", 10))
        try:
            style.configure("Treeview", rowheight=22)
        except Exception:
            pass

        self._build_header()

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=6)

        self.convert_tab = ConvertTab(self.nb)
        self.trim_tab = TrimTab(self.nb, self)
        self.nb.add(self.convert_tab, text="  音视频转换  ")
        self.nb.add(self.trim_tab, text="  裁剪拼接  ")
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._drop = None
        self.after(250, self._setup_drop)
        self.after(120, self.refresh_ffmpeg_status)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 顶部标题栏 + FFmpeg 状态 ----------
    def _build_header(self):
        head = ttk.Frame(self)
        head.pack(fill="x", padx=12, pady=(8, 0))

        tk.Label(head, text=APP_NAME, font=("Microsoft YaHei UI", 12, "bold"),
                 fg="#1f3a93").pack(side="left")
        tk.Label(head, text="v" + APP_VERSION, font=("Microsoft YaHei UI", 9),
                 fg="#9aa1b0").pack(side="left", padx=(8, 0))

        self.ff_status = ttk.Label(head, text="正在检测 FFmpeg…", foreground="#666666")
        self.ff_status.pack(side="right", padx=(10, 0))

        self.btn_update_ff = ttk.Button(head, text="更新 FFmpeg", width=12,
                                        command=lambda: self.download_ffmpeg(True))
        self.btn_update_ff.pack(side="right", padx=4)
        self.btn_get_ff = ttk.Button(head, text="获取 FFmpeg", width=12,
                                     command=lambda: self.download_ffmpeg(False))
        self.btn_get_ff.pack(side="right", padx=4)

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=10, pady=(6, 0))

    def refresh_ffmpeg_status(self):
        """检测并刷新右上角的 FFmpeg 状态。"""
        ver, src, path = detect_ffmpeg()
        if ver:
            short = ver.split("-")[0]
            self.ff_status.config(text="FFmpeg %s（%s）✔" % (short, src), foreground="#1a7f37")
            self.btn_get_ff.config(text="获取 FFmpeg", state="normal")
            self.btn_update_ff.config(state="normal")
            try:
                self.trim_tab.log("FFmpeg %s 已就绪（%s）：%s" % (short, src, path))
            except Exception:
                pass
        else:
            self.ff_status.config(text="未检测到 FFmpeg ✘  请点「获取 FFmpeg」", foreground="#c62828")
            self.btn_get_ff.config(text="获取 FFmpeg", state="normal")
            self.btn_update_ff.config(state="disabled")
        return ver is not None

    def download_ffmpeg(self, update=False):
        """下载 / 更新 FFmpeg 到程序目录。"""
        if self._dlg is not None:
            try:
                if self._dlg.winfo_exists():
                    self._dlg.lift()
                    self._dlg.focus_force()
                    return
            except Exception:
                pass
        if os.name != "nt":
            messagebox.showinfo("提示", "自动下载仅支持 Windows，请手动安装 FFmpeg 并放到程序目录。")
            return
        try:
            if self.trim_tab._export_proc is not None:
                messagebox.showinfo("提示", "正在导出，请先等导出完成或点「取消」再更新 FFmpeg。")
                return
        except Exception:
            pass
        self._dlg = FFmpegDownloadDialog(self, update=update, on_done=self._after_ffmpeg_install)

    def _after_ffmpeg_install(self):
        self.refresh_ffmpeg_status()
        try:
            self.trim_tab.log("FFmpeg 已更新，后续任务将使用新版本。")
        except Exception:
            pass

    def _on_close(self):
        """关闭窗口前先结束所有后台 ffmpeg 进程。"""
        try:
            self.trim_tab.shutdown()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    def _setup_drop(self):
        """挂上文件拖放：把视频/音频直接拖进窗口即可。"""
        try:
            self._drop = FileDrop(self, self._on_drop_files)
            if self._drop.ok:
                self.trim_tab.log("提示：视频 / 音频文件可以直接拖进窗口。")
        except Exception:
            self._drop = None

    def _on_drop_files(self, paths):
        try:
            self.nb.select(1)
        except Exception:
            pass
        self.trim_tab._add_paths(paths, replace=not self.trim_tab.sources)

    def _on_tab_changed(self, _event=None):
        if self.nb.index(self.nb.select()) == 1:
            self.after(60, self.trim_tab._on_tab_shown)
        else:
            self.trim_tab._stop_play()

    def is_edit_active(self):
        try:
            return self.nb.index(self.nb.select()) == 1
        except Exception:
            return False


def main():
    app = FFmpegGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
