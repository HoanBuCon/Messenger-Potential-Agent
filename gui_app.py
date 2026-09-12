import os
import sys
import io
import re
import time
import threading
import unicodedata
import difflib
import yaml
import winsound
from datetime import datetime
from typing import Optional, List, Dict, Any

if sys.platform == "win32":
    import ctypes
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import customtkinter as ctk
from PIL import Image, ImageTk
import cv2
import numpy as np
import subprocess
from pynput import keyboard

from core.screen_capturer import ScreenCapturer
from core.ocr_engine import OCREngine
from core.message_parser import MessageParser
from core.llm_agent import LLMAgent
from core.executor import ActionExecutor
from core.memory_worker import MemoryWorker
from core.process_manager import cleanup_bot_processes, record_current_pid, remove_pid_file
from db.mysql_client import MySQLClient
from db.qdrant_client import QdrantMemoryClient
from tools.snipping_selector import MultiMonitorSnippingSelector

CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "config", "config.yaml"))


def ensure_docker_services() -> bool:
    """Kiểm tra và tự động khởi động các container MySQL & Qdrant nếu cần"""
    try:
        res = subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=os.path.dirname(__file__),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return res.returncode == 0
    except Exception:
        return False


class MessengerAgentGUI(ctk.CTk):
    """
    Giao diện điều khiển trung tâm đa màn hình cho Messenger Auto-Reply Agent:
    - Hỗ trợ di chuyển tự do qua nhiều màn hình (Multi-Monitor).
    - Hiển thị luồng hoạt động trực quan thời gian thực (OCR -> Memory -> LLM -> RPA).
    - Hỗ trợ 2 chế độ:
        1. Full-Auto (Tự động hoàn toàn)
        2. Human-in-the-loop (Người dùng duyệt, chỉnh sửa câu trả lời trước khi gửi).
    """

    def __init__(self):
        super().__init__()

        # 0. Tự động dọn dẹp tiến trình cũ và đảm bảo Docker Database hoạt động
        cleanup_bot_processes(exclude_current=True, include_gui=False)
        docker_ok = ensure_docker_services()

        # 1. Cấu hình cửa sổ
        self.title("Messenger AI Agent - Kuchiba Chisa (Kuudere Dashboard)")
        self.geometry("1240x820")
        self.minsize(1050, 700)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # 2. Dữ liệu trạng thái
        self.config = self.load_config()
        self.is_bot_running = False
        self.bot_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

        # Human-in-the-loop events & state
        self.mode_var = ctk.StringVar(value="human_in_loop")  # "full_auto" hoặc "human_in_loop"
        self.approval_event = threading.Event()
        self.current_pending_reply = None
        self.action_decision = "send"  # "send", "regenerate", "skip"
        self.is_group_chat = bool(self.config.get("conversation", {}).get("is_group", False))

        self.latest_ocr_text = "Chưa có dữ liệu"
        self.latest_memories_text = "Chưa có dữ liệu"
        self.latest_worker_text = "Chưa kích hoạt"
        self.recently_sent_replies: List[str] = []

        # 3. Khởi tạo Database & Core Modules
        self.mysql = MySQLClient()
        self.qdrant = QdrantMemoryClient()
        self.init_core_modules()

        # 4. Xây dựng giao diện
        self.build_ui()

        # 5. Kiểm tra kết nối dịch vụ & log trạng thái
        if docker_ok:
            self.log("[Docker] MySQL & Qdrant containers đã sẵn sàng.")
        self.check_database_connections()

        # 6. Global Hotkey ESC để dừng khẩn cấp
        self.init_emergency_listener()

        # 7. Bắt sự kiện tắt cửa sổ để dọn dẹp sạch tiến trình
        self.protocol("WM_DELETE_WINDOW", self.on_close_window)
        record_current_pid()

    def load_config(self) -> dict:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {}

    def init_core_modules(self):
        chat_reg = self.config.get("chat_region", {"top": 100, "left": 100, "width": 500, "height": 600})
        input_box = self.config.get("input_box", {"x": 300, "y": 700})
        det_cfg = self.config.get("detection", {})
        layout_cfg = self.config.get("layout", {})
        llm_cfg = self.config.get("llm", {})

        self.capturer = ScreenCapturer(region=chat_reg, diff_threshold=det_cfg.get("diff_threshold", 0.01))
        self.ocr_engine = OCREngine(confidence_threshold=det_cfg.get("confidence_threshold", 0.6))
        self.message_parser = MessageParser(incoming_x_ratio_max=layout_cfg.get("incoming_x_ratio_max", 0.45))
        self.llm_agent = LLMAgent(
            model=llm_cfg.get("model", "deepseek-chat"),
            system_prompt=llm_cfg.get("system_prompt"),
            temperature=llm_cfg.get("temperature", 0.7),
            max_tokens=llm_cfg.get("max_tokens", 500),
            frequency_penalty=llm_cfg.get("frequency_penalty", 0.5),
            presence_penalty=llm_cfg.get("presence_penalty", 0.3),
        )
        self.executor = ActionExecutor(input_box=input_box)
        self.memory_worker = MemoryWorker(
            mysql_client=self.mysql,
            qdrant_client=self.qdrant,
            openai_client=self.llm_agent.client,
            model=llm_cfg.get("model", "deepseek-chat")
        )

    def init_emergency_listener(self):
        def on_press(key):
            if key == keyboard.Key.esc:
                if self.is_bot_running:
                    self.after(0, self.stop_bot_action)

        self.esc_listener = keyboard.Listener(on_press=on_press)
        self.esc_listener.daemon = True
        self.esc_listener.start()

    # =========================================================================
    # GIAO DIỆN NGƯỜI DÙNG (UI LAYOUT)
    # =========================================================================
    def build_ui(self):
        # Header Bar
        header = ctk.CTkFrame(self, height=60, corner_radius=0, fg_color="#1a1c23")
        header.pack(fill="x", side="top", padx=0, pady=0)

        title_lbl = ctk.CTkLabel(
            header,
            text="🤖 KUCHIBA CHISA - MESSENGER AGENT",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#00f2fe"
        )
        title_lbl.pack(side="left", padx=20, pady=10)

        # Status Badge
        self.status_badge = ctk.CTkLabel(
            header,
            text="⚪ ĐÃ DỪNG (OCR OFF)",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#333a4d",
            corner_radius=8,
            padx=14,
            pady=4,
            text_color="#ffffff"
        )
        self.status_badge.pack(side="left", padx=10, pady=10)

        # Active Conversation Badge & Switcher Button
        conv_info = self.config.get("conversation", {"id": "conv_senpai", "partner_name": "Senpai"})
        conv_frame = ctk.CTkFrame(header, fg_color="#222631", corner_radius=8)
        conv_frame.pack(side="left", padx=15, pady=10)

        grp_tag = " [Group]" if self.is_group_chat else ""
        self.lbl_current_conv = ctk.CTkLabel(
            conv_frame,
            text=f"🏷️ [{conv_info.get('id')}] {conv_info.get('partner_name')}{grp_tag}",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#38bdf8"
        )
        self.lbl_current_conv.pack(side="left", padx=10, pady=4)

        self.btn_change_conv = ctk.CTkButton(
            conv_frame,
            text="🔄 Đổi / Tạo mới",
            command=self.open_conversation_dialog,
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="#334155",
            hover_color="#475569",
            width=90,
            height=26
        )
        self.btn_change_conv.pack(side="left", padx=(0, 6), pady=4)

        # Group Chat Mode Switch
        group_frame = ctk.CTkFrame(header, fg_color="#222631", corner_radius=8)
        group_frame.pack(side="left", padx=(0, 10), pady=10)

        self.switch_group_chat = ctk.CTkSwitch(
            group_frame,
            text="👥 Group Chat",
            command=self.on_toggle_group_chat,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#c084fc",
            progress_color="#8b5cf6",
        )
        if self.is_group_chat:
            self.switch_group_chat.select()
        else:
            self.switch_group_chat.deselect()
        self.switch_group_chat.pack(side="left", padx=10, pady=4)

        # Control Buttons
        self.btn_toggle_bot = ctk.CTkButton(
            header,
            text="▶ START (BẬT OCR)",
            command=self.toggle_bot,
            fg_color="#10b981",
            hover_color="#059669",
            font=ctk.CTkFont(size=13, weight="bold"),
            width=165,
            height=36
        )
        self.btn_toggle_bot.pack(side="right", padx=15, pady=10)

        self.btn_snip = ctk.CTkButton(
            header,
            text="🎯 CHỌN VÙNG CHAT (SNIP)",
            command=self.open_snipping_tool,
            fg_color="#3b82f6",
            hover_color="#2563eb",
            font=ctk.CTkFont(size=12, weight="bold"),
            width=170
        )
        self.btn_snip.pack(side="right", padx=8, pady=10)

        self.btn_clear_data = ctk.CTkButton(
            header,
            text="🗑 XÓA DỮ LIỆU",
            command=self.open_clear_data_modal,
            fg_color="#ef4444",
            hover_color="#dc2626",
            font=ctk.CTkFont(size=12, weight="bold"),
            width=120
        )
        self.btn_clear_data.pack(side="right", padx=8, pady=10)

        # Mode Selector (Top right)
        mode_frame = ctk.CTkFrame(header, fg_color="transparent")
        mode_frame.pack(side="right", padx=20)

        mode_lbl = ctk.CTkLabel(mode_frame, text="Chế độ:", font=ctk.CTkFont(size=12, weight="bold"))
        mode_lbl.pack(side="left", padx=5)

        self.mode_selector = ctk.CTkSegmentedButton(
            mode_frame,
            values=["Bán tự động (Human-in-loop)", "Tự động hoàn toàn (Full-Auto)"],
            command=self.on_mode_change
        )
        self.mode_selector.set("Bán tự động (Human-in-loop)")
        self.mode_selector.pack(side="left")

        # Container chia 2 cột (Trái: Pipeline Flow & Duyệt, Phải: Live Preview & Logs)
        main_container = ctk.CTkFrame(self, fg_color="transparent")
        main_container.pack(fill="both", expand=True, padx=15, pady=12)

        # ====================== CỘT TRÁI (PIPELINE & HUMAN-IN-THE-LOOP) ======================
        left_col = ctk.CTkFrame(main_container, fg_color="#181a20", corner_radius=12)
        left_col.pack(side="left", fill="both", expand=True, padx=(0, 8), pady=0)

        # Tiêu đề Cột Trái
        flow_title = ctk.CTkLabel(
            left_col,
            text="⚡ LUỒNG XỬ LÝ & DUYỆT PHẢN HỒI (WORKFLOW)",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#93c5fd"
        )
        flow_title.pack(anchor="w", padx=15, pady=(12, 6))

        # Thẻ 1: Input OCR
        card_ocr = ctk.CTkFrame(left_col, fg_color="#222631", corner_radius=8)
        card_ocr.pack(fill="x", padx=15, pady=6)
        ctk.CTkLabel(card_ocr, text="[BƯỚC 1: TIN NHẮN TỪ SENPAI (OCR)]", font=ctk.CTkFont(size=11, weight="bold"), text_color="#38bdf8").pack(anchor="w", padx=12, pady=(6, 2))
        self.lbl_ocr_msg = ctk.CTkLabel(card_ocr, text="Chưa có tin nhắn nào được ghi nhận...", font=ctk.CTkFont(size=13), text_color="#f3f4f6", wraplength=520, justify="left")
        self.lbl_ocr_msg.pack(anchor="w", padx=12, pady=(0, 8))

        # Thẻ 2: Memory Context
        card_mem = ctk.CTkFrame(left_col, fg_color="#222631", corner_radius=8)
        card_mem.pack(fill="x", padx=15, pady=6)
        ctk.CTkLabel(card_mem, text="[BƯỚC 2: KÝ ỨC ĐÃ NẠP (QDRANT & MYSQL)]", font=ctk.CTkFont(size=11, weight="bold"), text_color="#a78bfa").pack(anchor="w", padx=12, pady=(6, 2))
        self.lbl_memories = ctk.CTkLabel(card_mem, text="Chưa có ký ức được truy xuất...", font=ctk.CTkFont(size=12), text_color="#d1d5db", wraplength=520, justify="left")
        self.lbl_memories.pack(anchor="w", padx=12, pady=(0, 8))

        # Thẻ 3: PHẢN HỒI CỦA CHISA & BẢNG DUYỆT (HUMAN-IN-THE-LOOP)
        self.card_reply = ctk.CTkFrame(left_col, fg_color="#262c3d", corner_radius=10, border_width=2, border_color="#3b82f6")
        self.card_reply.pack(fill="both", expand=True, padx=15, pady=8)

        reply_header = ctk.CTkFrame(self.card_reply, fg_color="transparent")
        reply_header.pack(fill="x", padx=12, pady=(8, 4))
        ctk.CTkLabel(reply_header, text="[BƯỚC 3: CÂU TRẢ LỜI CỦA CHISA - CHO PHÉP CHỈNH SỬA]", font=ctk.CTkFont(size=12, weight="bold"), text_color="#fbbf24").pack(side="left")

        self.lbl_hitl_badge = ctk.CTkLabel(reply_header, text="HUMAN-IN-THE-LOOP", font=ctk.CTkFont(size=10, weight="bold"), fg_color="#d97706", text_color="#ffffff", corner_radius=4, padx=6, pady=1)
        self.lbl_hitl_badge.pack(side="right")

        # Textbox chỉnh sửa câu trả lời
        self.txt_reply_edit = ctk.CTkTextbox(self.card_reply, font=ctk.CTkFont(size=14), fg_color="#181a20", text_color="#f8fafc", corner_radius=8, wrap="word")
        self.txt_reply_edit.pack(fill="both", expand=True, padx=12, pady=6)
        self.txt_reply_edit.insert("1.0", "Câu trả lời của Chisa sẽ xuất hiện tại đây khi Senpai nhắn tin...")

        # Hàng nút bấm duyệt (Action Buttons)
        self.action_bar = ctk.CTkFrame(self.card_reply, fg_color="transparent")
        self.action_bar.pack(fill="x", padx=12, pady=(4, 10))

        self.btn_send_now = ctk.CTkButton(
            self.action_bar,
            text="🚀 GỬI CHO SENPAI",
            command=self.on_approve_send,
            fg_color="#10b981",
            hover_color="#059669",
            font=ctk.CTkFont(size=13, weight="bold"),
            height=36,
            width=180
        )
        self.btn_send_now.pack(side="left", padx=(0, 8))

        self.btn_regen = ctk.CTkButton(
            self.action_bar,
            text="🔄 Sinh câu khác",
            command=self.on_regenerate,
            fg_color="#3b82f6",
            hover_color="#2563eb",
            font=ctk.CTkFont(size=12, weight="bold"),
            height=36,
            width=130
        )
        self.btn_regen.pack(side="left", padx=4)

        self.btn_skip = ctk.CTkButton(
            self.action_bar,
            text="❌ Bỏ qua tin này",
            command=self.on_skip,
            fg_color="#475569",
            hover_color="#334155",
            font=ctk.CTkFont(size=12, weight="bold"),
            height=36,
            width=130
        )
        self.btn_skip.pack(side="left", padx=4)

        # Thẻ 4: Async Memory Worker
        card_worker = ctk.CTkFrame(left_col, fg_color="#222631", corner_radius=8)
        card_worker.pack(fill="x", padx=15, pady=(0, 12))
        ctk.CTkLabel(card_worker, text="[BƯỚC 4: ASYNC MEMORY & XUNG ĐỘT (CHẠY NGẦM)]", font=ctk.CTkFont(size=11, weight="bold"), text_color="#ec4899").pack(anchor="w", padx=12, pady=(6, 2))
        self.lbl_worker_status = ctk.CTkLabel(card_worker, text="Chưa kích hoạt...", font=ctk.CTkFont(size=12), text_color="#cbd5e1", wraplength=520, justify="left")
        self.lbl_worker_status.pack(anchor="w", padx=12, pady=(0, 8))

        # ====================== CỘT PHẢI (LIVE PREVIEW & LOGS) ======================
        right_col = ctk.CTkFrame(main_container, width=460, fg_color="#181a20", corner_radius=12)
        right_col.pack(side="right", fill="both", expand=False, padx=(8, 0), pady=0)

        preview_title = ctk.CTkLabel(right_col, text="📷 VÙNG THEO DÕI & LOG HOẠT ĐỘNG", font=ctk.CTkFont(size=14, weight="bold"), text_color="#93c5fd")
        preview_title.pack(anchor="w", padx=15, pady=(12, 6))

        # Khung chứa ảnh Preview Vùng Chat
        self.preview_frame = ctk.CTkFrame(right_col, height=220, fg_color="#111317", corner_radius=8)
        self.preview_frame.pack(fill="x", padx=15, pady=6)

        self.lbl_preview_img = ctk.CTkLabel(self.preview_frame, text="Chưa có ảnh chụp vùng chat\n(Nhấn 'CHỌN VÙNG CHAT' để thiết lập)", text_color="#64748b")
        self.lbl_preview_img.pack(fill="both", expand=True, padx=8, pady=8)

        # Tọa độ vùng hiện tại
        chat_reg = self.config.get("chat_region", {})
        inp_reg = self.config.get("input_box", {})
        self.lbl_coords = ctk.CTkLabel(
            right_col,
            text=f"Chat: L={chat_reg.get('left')}, T={chat_reg.get('top')}, W={chat_reg.get('width')}, H={chat_reg.get('height')} | Ô nhập: X={inp_reg.get('x')}, Y={inp_reg.get('y')}",
            font=ctk.CTkFont(size=11),
            text_color="#94a3b8"
        )
        self.lbl_coords.pack(anchor="w", padx=15, pady=(0, 4))

        # Khung Log hoạt động
        ctk.CTkLabel(right_col, text="Nhật ký hoạt động (Live Logs):", font=ctk.CTkFont(size=12, weight="bold"), text_color="#e2e8f0").pack(anchor="w", padx=15, pady=(8, 2))
        self.txt_logs = ctk.CTkTextbox(right_col, font=ctk.CTkFont(family="Consolas", size=11), fg_color="#111317", text_color="#38bdf8", corner_radius=8, wrap="word")
        self.txt_logs.pack(fill="both", expand=True, padx=15, pady=(0, 12))

        self.log("Khởi tạo Dashboard thành công. Sẵn sàng hoạt động!")
        self.load_initial_preview()

    def check_database_connections(self):
        """Kiểm tra và báo cáo trạng thái MySQL & Qdrant trực tiếp lên nhật ký"""
        try:
            convs = self.mysql.list_conversations()
            total_msgs = sum(c.get("msg_count", 0) for c in convs)
            total_mems = sum(c.get("mem_count", 0) for c in convs)
            self.log(f"[MySQL] 🟢 Kết nối thành công ({len(convs)} hội thoại, {total_msgs} tin nhắn, {total_mems} ký ức).")
        except Exception as e:
            self.log(f"[MySQL Cảnh báo] 🔴 Lỗi kết nối: {e}")

        try:
            self.log(f"[Qdrant] 🟢 Vector Collection `{self.qdrant.collection_name}` đã sẵn sàng.")
        except Exception as e:
            self.log(f"[Qdrant Cảnh báo] 🔴 Lỗi kết nối: {e}")

    def load_initial_preview(self):
        """Tải ảnh debug_calibration.png lên preview nếu có sẵn"""
        debug_path = os.path.join(os.path.dirname(__file__), "debug_calibration.png")
        if os.path.exists(debug_path):
            self.update_preview_image(debug_path)

    def update_preview_image(self, img_path_or_bgr):
        try:
            if isinstance(img_path_or_bgr, str):
                pil_img = Image.open(img_path_or_bgr)
            else:
                if np.mean(img_path_or_bgr) < 1.0:
                    return  # Bỏ qua nếu là khung hình rỗng/đen xì do màn hình chưa sẵn sàng
                img_rgb = cv2.cvtColor(img_path_or_bgr, cv2.COLOR_BGR2RGB)
                pil_img = Image.fromarray(img_rgb)

            # Resize giữ tỷ lệ khung hình
            max_w, max_h = 420, 200
            pil_img.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
            ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=pil_img.size)

            self.lbl_preview_img.configure(image=ctk_img, text="")
            self.lbl_preview_img.image = ctk_img
        except Exception as e:
            self.log(f"[Preview Error] {e}")

    def log(self, text: str):
        now = datetime.now().strftime("%H:%M:%S")
        self.txt_logs.insert("end", f"[{now}] {text}\n")
        self.txt_logs.see("end")

    def on_mode_change(self, value):
        if "Bán tự động" in value:
            self.mode_var.set("human_in_loop")
            self.card_reply.configure(border_color="#d97706")
            self.lbl_hitl_badge.configure(text="HUMAN-IN-THE-LOOP (CẦN DUYỆT)", fg_color="#d97706")
            self.btn_send_now.configure(state="normal")
            self.log("Đã chuyển sang chế độ: Bán tự động (Human-in-the-loop).")
        else:
            self.mode_var.set("full_auto")
            self.card_reply.configure(border_color="#10b981")
            self.lbl_hitl_badge.configure(text="FULL-AUTO (TỰ ĐỘNG GỬI)", fg_color="#10b981")
            self.log("Đã chuyển sang chế độ: Tự động hoàn toàn (Full-Auto).")

    def on_toggle_group_chat(self):
        """Xử lý khi người dùng bật / tắt công tắc Group Chat trên thanh Header"""
        is_active = bool(self.switch_group_chat.get())
        self.is_group_chat = is_active
        if "conversation" not in self.config:
            self.config["conversation"] = {}
        self.config["conversation"]["is_group"] = is_active

        # 1. Lưu trạng thái vào config.yaml
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.dump(self.config, f, allow_unicode=True, default_flow_style=False)
        except Exception as e:
            self.log(f"[Lỗi lưu cấu hình] {e}")

        # 2. Đồng bộ trạng thái vào MySQL
        try:
            cid = self.config["conversation"].get("id")
            if cid:
                self.mysql.update_conversation_group(cid, is_active)
        except Exception:
            pass

        # 3. Cập nhật nhãn hiển thị hội thoại hiện tại
        cid = self.config["conversation"].get("id", "conv_senpai")
        name = self.config["conversation"].get("partner_name", "Senpai")
        grp_tag = " [Group]" if self.is_group_chat else ""
        self.lbl_current_conv.configure(text=f"🏷️ [{cid}] {name}{grp_tag}")

        if is_active:
            self.log("👥 [Chế độ] Đã BẬT Group Chat. Prompt xử lý thành viên nhóm & tên người gửi đã được kích hoạt.")
        else:
            self.log("👤 [Chế độ] Đã TẮT Group Chat. Trở về chế độ trò chuyện 1-1 thông thường với Senpai.")

    # =========================================================================
    # ĐIỀU KHIỂN CHẠY BOT (BACKGROUND WORKER THREAD)
    # =========================================================================
    def toggle_bot(self):
        if self.is_bot_running:
            self.stop_bot_action()
        else:
            self.start_bot_action()

    def start_bot_action(self):
        # Kiểm tra xem đã cấu hình tọa độ chưa
        chat_reg = self.config.get("chat_region", {})
        if not chat_reg or chat_reg.get("width", 0) < 30 or chat_reg.get("height", 0) < 30:
            self.log("[!] Chưa cấu hình vùng chat! Vui lòng nhấn nút '🎯 CHỌN VÙNG CHAT (SNIP)' trước.")
            return

        # Tự động kiểm tra và bảo vệ vị trí ô nhập tin nhắn
        inp_box = self.config.get("input_box", {})
        reg_left = chat_reg.get("left", 0)
        reg_w = chat_reg.get("width", 0)
        box_x = inp_box.get("x", 0)
        if box_x < reg_left - 150 or box_x > reg_left + reg_w + 150:
            auto_x = int(reg_left + reg_w / 2)
            auto_y = int(chat_reg.get("top", 0) + chat_reg.get("height", 0) - 75)
            self.log(f"[Tự động căn chỉnh] Ô nhập lệch vị trí ({box_x}). Đã tự động gắn vào đáy khung chat ({auto_x}, {auto_y}).")
            inp_box = {"x": auto_x, "y": auto_y}
            self.config["input_box"] = inp_box
            self.executor.input_box = inp_box
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.dump(self.config, f, allow_unicode=True, default_flow_style=False)
        else:
            self.executor.input_box = inp_box

        # Dọn dẹp tiến trình cũ trước khi chạy
        cleaned = cleanup_bot_processes(exclude_current=True, include_gui=False)
        if cleaned:
            self.log(f"Đã dọn dẹp {len(cleaned)} tiến trình cũ treo (PIDs: {cleaned}).")

        self.is_bot_running = True
        self.stop_event.clear()
        self.btn_toggle_bot.configure(text="⏹ STOP (DỪNG OCR)", fg_color="#ef4444", hover_color="#dc2626")
        self.status_badge.configure(text="🟢 ĐANG QUÉT (OCR ACTIVE)", fg_color="#059669")
        self.log("[▶ START] Đã kích hoạt quét & OCR vùng chat. Đang theo dõi tin nhắn...")

        self.bot_thread = threading.Thread(target=self.bot_loop, daemon=True)
        self.bot_thread.start()

    def stop_bot_action(self):
        self.is_bot_running = False
        self.stop_event.set()
        self.approval_event.set()  # Giải phóng nếu đang chờ duyệt
        self.btn_toggle_bot.configure(text="▶ START (BẬT OCR)", fg_color="#10b981", hover_color="#059669")
        self.status_badge.configure(text="⚪ ĐÃ DỪNG (OCR OFF)", fg_color="#333a4d")
        self.log("[⏹ STOP] Đã ngắt OCR hoàn toàn. Bạn có thể thoải mái thao tác các việc khác mà không lo bot can thiệp.")

        # Dọn dẹp tiến trình treo nếu có
        cleanup_bot_processes(exclude_current=True, include_gui=False)

    def on_close_window(self):
        """Xử lý khi người dùng tắt cửa sổ: dừng bot và dọn dẹp triệt để tiến trình"""
        self.log("Đang đóng Dashboard và giải phóng toàn bộ tài nguyên...")
        self.is_bot_running = False
        self.stop_event.set()
        self.approval_event.set()
        try:
            if hasattr(self, "esc_listener") and self.esc_listener:
                self.esc_listener.stop()
        except Exception:
            pass

        cleanup_bot_processes(exclude_current=True, include_gui=False)
        remove_pid_file()
        self.destroy()

    @staticmethod
    def _strip_accents(s: str) -> str:
        nfkd = unicodedata.normalize('NFKD', s)
        return ''.join([c for c in nfkd if not unicodedata.combining(c)]).replace('đ', 'd').replace('Đ', 'D')

    def is_bot_self_echo(self, text: str) -> bool:
        """
        Kiểm tra xem tin nhắn đối phương có phải là do OCR nhận nhầm bong bóng của chính bot không.
        Chỉ coi là self-echo nếu nội dung gần như trùng khớp hoàn toàn (>= 80% similarity)
        với một trong các tin nhắn gần nhất mà bot vừa gửi.
        Tuyệt đối không bắt nhầm các câu trả lời ngắn của đối phương (ví dụ: 'Có', 'Ủa', 'Ok', 'Tôi đói').
        """
        if not text or not self.recently_sent_replies:
            return False

        clean_text = self._strip_accents(text.strip().lower())
        clean_words = [w for w in re.sub(r'[^\w\s]', ' ', clean_text).split() if w]
        if not clean_words:
            return True

        # Chỉ so sánh với 3 tin nhắn mới nhất bot vừa gửi
        for sent in self.recently_sent_replies[-3:]:
            sent_clean = self._strip_accents(sent.strip().lower())
            sent_words = [w for w in re.sub(r'[^\w\s]', ' ', sent_clean).split() if w]
            if not sent_words:
                continue

            # 1. Trùng khớp hoàn toàn (exact match)
            if clean_text == sent_clean:
                return True

            # 2. Nếu tin nhắn OCR rất ngắn (dưới 4 từ), TUYỆT ĐỐI KHÔNG coi là self-echo
            # trừ khi trùng khớp 100% với tin bot gửi (đã kiểm tra ở bước 1)
            if len(clean_words) < 4:
                continue

            # 3. Tính độ tương đồng SequenceMatcher
            ratio = difflib.SequenceMatcher(None, clean_text, sent_clean).ratio()
            if ratio >= 0.80:
                return True

            # 4. Nếu toàn bộ câu OCR trùng khớp một đoạn lớn (>= 80% độ dài tin OCR và tối thiểu 20 ký tự)
            if len(clean_text) >= 20 and clean_text in sent_clean:
                return True

        return False

    def bot_loop(self):
        conv_id = self.config.get("conversation", {}).get("id", "conv_senpai")
        partner_name = self.config.get("conversation", {}).get("partner_name", "")
        last_replied_message = None
        check_interval = self.config.get("detection", {}).get("check_interval", 1.0)

        while not self.stop_event.is_set():
            img_bgr = self.capturer.capture()
            if img_bgr is None:
                time.sleep(check_interval)
                continue

            has_changed, diff_ratio = self.capturer.has_screen_changed(img_bgr)
            if has_changed:
                # Debounce 1.2s: Chờ đối phương soạn tin xong / bong bóng typing dừng lại
                time.sleep(1.2)
                stable_img = self.capturer.capture()
                if stable_img is not None:
                    img_bgr = stable_img

                self.after(0, self.update_preview_image, img_bgr)
                ocr_items = self.ocr_engine.extract_text(img_bgr)

                if ocr_items:
                    parsed_messages = self.message_parser.parse_messages(
                        ocr_items,
                        chat_width=self.capturer.region["width"],
                        chat_height=self.capturer.region.get("height"),
                        partner_name=partner_name
                    )
                    latest_incoming = self.message_parser.get_latest_incoming_message(parsed_messages)

                    if latest_incoming:
                        # Kiểm tra chống tự đọc lại câu trả lời của chính bot
                        if self.is_bot_self_echo(latest_incoming):
                            continue

                        if latest_incoming != last_replied_message:
                            self.after(0, self.status_badge.configure, {"text": "🟡 ĐANG SUY NGHĨ", "fg_color": "#d97706"})
                            self.after(0, self.lbl_ocr_msg.configure, {"text": f"\"{latest_incoming}\""})
                            self.after(0, self.log, f"Phát hiện tin nhắn mới: '{latest_incoming}'")

                            # Nạp Memory từ Qdrant & MySQL
                            relevant_memories = self.qdrant.search_relevant_memories(conversation_id=conv_id, query_text=latest_incoming, limit=3)
                            if not relevant_memories:
                                relevant_memories = self.mysql.get_active_memories(conv_id)

                            mem_display = "\n".join([f"• [{m.get('topic')}] {m.get('fact')}" for m in relevant_memories]) if relevant_memories else "Không có ký ức liên quan trực tiếp."
                            self.after(0, self.lbl_memories.configure, {"text": mem_display})

                            recent_history = self.mysql.get_recent_history(conv_id, limit=5)

                            # Gọi LLM sinh câu trả lời
                            reply = self.llm_agent.generate_reply(
                                latest_message=latest_incoming,
                                memories=relevant_memories,
                                recent_history=recent_history,
                                partner_title=partner_name,
                                is_group=self.is_group_chat
                            )
                            if not reply:
                                self.after(0, self.log, "Lỗi: DeepSeek không sinh được câu trả lời.")
                                continue

                            # Hiển thị câu trả lời lên Textbox
                            self.current_pending_reply = reply
                            self.after(0, self.show_pending_reply, reply)

                            # KIỂM TRA CHẾ ĐỘ HOẠT ĐỘNG
                            if self.mode_var.get() == "human_in_loop":
                                # Chế độ Human-in-the-loop: Chờ người dùng duyệt!
                                self.after(0, self.status_badge.configure, {"text": "🟠 CHỜ SENPAI DUYỆT", "fg_color": "#ea580c"})
                                self.after(0, self.card_reply.configure, {"border_color": "#f59e0b"})
                                try:
                                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                                except Exception:
                                    pass

                                self.approval_event.clear()
                                self.approval_event.wait()  # Dừng chờ nút bấm

                                if self.stop_event.is_set():
                                    break

                                if self.action_decision == "send":
                                    # Lấy nội dung mới nhất từ Textbox (người dùng có thể đã sửa)
                                    final_text = self.get_edited_reply()
                                    self.after(0, self.log, f"Đang gửi tin nhắn đã duyệt: '{final_text}'")
                                    success = self.executor.send_message(final_text)
                                    if success:
                                        last_replied_message = latest_incoming
                                        self.recently_sent_replies.append(final_text.strip())
                                        self.mysql.add_chat_message(conv_id, sender="partner", content=latest_incoming)
                                        self.mysql.add_chat_message(conv_id, sender="me", content=final_text)
                                        self.after(0, self.log, "✓ Đã gửi tin nhắn thành công!")
                                        self.trigger_async_worker(conv_id, latest_incoming, final_text)

                                elif self.action_decision == "skip":
                                    self.after(0, self.log, "Đã bỏ qua tin nhắn theo yêu cầu.")
                                    last_replied_message = latest_incoming
                                    self.mysql.add_chat_message(conv_id, sender="partner", content=latest_incoming)

                            else:
                                # Chế độ Full-Auto: Tự động gửi ngay lập tức
                                self.after(0, self.status_badge.configure, {"text": "🚀 ĐANG GỬI...", "fg_color": "#10b981"})
                                time.sleep(0.3)
                                success = self.executor.send_message(reply)
                                if success:
                                    last_replied_message = latest_incoming
                                    self.recently_sent_replies.append(reply.strip())
                                    self.mysql.add_chat_message(conv_id, sender="partner", content=latest_incoming)
                                    self.mysql.add_chat_message(conv_id, sender="me", content=reply)
                                    self.after(0, self.log, f"✓ [Full-Auto] Đã gửi: '{reply}'")
                                    self.trigger_async_worker(conv_id, latest_incoming, reply)

                            self.after(0, self.status_badge.configure, {"text": "🟢 ĐANG QUÉT (SCANNING)", "fg_color": "#059669"})

            time.sleep(check_interval)

    def show_pending_reply(self, reply_text: str):
        self.txt_reply_edit.delete("1.0", "end")
        self.txt_reply_edit.insert("1.0", reply_text)

    def get_edited_reply(self) -> str:
        return self.txt_reply_edit.get("1.0", "end").strip()

    def on_approve_send(self):
        """Người dùng nhấn Gửi"""
        self.action_decision = "send"
        self.approval_event.set()

    def on_regenerate(self):
        """Người dùng nhấn Sinh lại"""
        self.log("Đang yêu cầu DeepSeek sinh lại phản hồi khác...")
        self.status_badge.configure(text="🟡 ĐANG SINH LẠI...", fg_color="#d97706")
        threading.Thread(target=self._async_regen, daemon=True).start()

    def _async_regen(self):
        conv_id = self.config.get("conversation", {}).get("id", "conv_senpai")
        ocr_text = self.lbl_ocr_msg.cget("text").strip('"')
        relevant_memories = self.qdrant.search_relevant_memories(conversation_id=conv_id, query_text=ocr_text, limit=3)
        recent_history = self.mysql.get_recent_history(conv_id, limit=5)

        partner_name = self.config.get("conversation", {}).get("partner_name", "Senpai")
        new_reply = self.llm_agent.generate_reply(
            latest_message=ocr_text,
            memories=relevant_memories,
            recent_history=recent_history,
            partner_title=partner_name,
            is_group=self.is_group_chat
        )
        if new_reply:
            self.after(0, self.show_pending_reply, new_reply)
            self.after(0, self.status_badge.configure, {"text": "🟠 CHỜ SENPAI DUYỆT", "fg_color": "#ea580c"})
            self.after(0, self.log, "Đã sinh câu trả lời mới.")

    def on_skip(self):
        """Người dùng nhấn Bỏ qua"""
        self.action_decision = "skip"
        self.approval_event.set()

    def trigger_async_worker(self, conv_id: str, incoming: str, reply: str):
        def worker():
            self.after(0, self.lbl_worker_status.configure, {"text": "Đang phân tích bóc tách sự thật ngầm..."})
            self.after(0, self.log, "[Memory Worker] Bắt đầu phân tích bóc tách sự thật ngầm...")

            def on_log_cb(msg: str):
                self.after(0, self.log, msg)

            self.memory_worker._process_memory_turn(conv_id, incoming, reply, on_log=on_log_cb)
            active_mems = self.mysql.get_active_memories(conv_id)
            status_summary = f"Đã đồng bộ MySQL & Qdrant ({len(active_mems)} facts đang có hiệu lực)."
            self.after(0, self.lbl_worker_status.configure, {"text": status_summary})

            # Tự động cập nhật hiển thị Memories trên Dashboard
            if active_mems:
                mem_display = "\n".join([f"• [{m.get('topic')}] {m.get('fact')}" for m in active_mems[-5:]])
                self.after(0, self.lbl_memories.configure, {"text": mem_display})

        threading.Thread(target=worker, daemon=True).start()

    # =========================================================================
    # TIỆN ÍCH: SNIPPING TOOL & DATA MANAGEMENT MODAL
    # =========================================================================
    def open_snipping_tool(self):
        """Mở Snipping Tool đa màn hình"""
        was_running = self.is_bot_running
        if was_running:
            self.stop_bot_action()

        self.log("Đang khởi chạy Bounding Box Snipping Tool ĐA MÀN HÌNH...")
        selector = MultiMonitorSnippingSelector(on_complete_callback=self.on_snip_completed)
        selector.start_selection()

        self.log("[✓] Đã lưu cấu hình vùng chat. Hãy nhấn nút '▶ START (BẬT OCR)' khi bạn sẵn sàng!")

    def open_conversation_dialog(self):
        """Mở hộp thoại cho phép người dùng can thiệp chọn hoặc tạo cuộc hội thoại mới"""
        try:
            from core.conversation_dialog import prompt_conversation
            chosen = prompt_conversation(parent=self)
            if chosen:
                self.config = self.load_config()
                cid = chosen["id"]
                name = chosen["partner_name"]
                is_grp = bool(chosen.get("is_group", self.config.get("conversation", {}).get("is_group", False)))
                self.is_group_chat = is_grp
                if self.is_group_chat:
                    self.switch_group_chat.select()
                else:
                    self.switch_group_chat.deselect()

                grp_tag = " [Group]" if self.is_group_chat else ""
                self.lbl_current_conv.configure(text=f"🏷️ [{cid}] {name}{grp_tag}")
                self.log(f"Đã chuyển sang cuộc hội thoại: [{cid}] {name}{grp_tag}")

                # Tải lại danh sách memories của cuộc hội thoại này
                active_mems = self.mysql.get_active_memories(cid)
                mem_display = "\n".join([f"• [{m.get('topic')}] {m.get('fact')}" for m in active_mems]) if active_mems else "Chưa có ký ức nào cho hội thoại này."
                self.lbl_memories.configure(text=mem_display)
        except Exception as e:
            self.log(f"[Lỗi hội thoại] {e}")

    def on_snip_completed(self, region: dict, input_box: dict, chosen_conv: Optional[dict] = None):
        self.config = self.load_config()
        self.capturer.update_region(region)
        self.executor.input_box = input_box
        self.lbl_coords.configure(
            text=f"Chat: L={region.get('left')}, T={region.get('top')}, W={region.get('width')}, H={region.get('height')} | Ô nhập: X={input_box.get('x')}, Y={input_box.get('y')}"
        )
        self.log(f"Đã cập nhật Vùng OCR Chat mới: {region}")
        self.log(f"Đã cập nhật Tọa độ Ô Nhập mới: {input_box}")
        self.load_initial_preview()

        if chosen_conv:
            cid = chosen_conv["id"]
            name = chosen_conv["partner_name"]
            is_grp = bool(chosen_conv.get("is_group", self.config.get("conversation", {}).get("is_group", False)))
            self.is_group_chat = is_grp
            if self.is_group_chat:
                self.switch_group_chat.select()
            else:
                self.switch_group_chat.deselect()

            grp_tag = " [Group]" if self.is_group_chat else ""
            self.lbl_current_conv.configure(text=f"🏷️ [{cid}] {name}{grp_tag}")
            self.log(f"Đã chọn cuộc hội thoại: [{cid}] {name}{grp_tag}")
        else:
            # Tự động mở hộp thoại chọn hoặc tạo cuộc hội thoại ngay sau khi snip xong
            self.after(200, self.open_conversation_dialog)

    def open_clear_data_modal(self):
        """Mở cửa sổ quản lý xóa dữ liệu"""
        modal = ctk.CTkToplevel(self)
        modal.title("Quản Lý & Xóa Dữ Liệu")
        modal.geometry("520x450")
        modal.attributes("-topmost", True)

        ctk.CTkLabel(modal, text="QUẢN LÝ DỮ LIỆU HỘI THOẠI & KÝ ỨC", font=ctk.CTkFont(size=14, weight="bold"), text_color="#ef4444").pack(pady=(15, 10))

        convs = self.mysql.list_conversations()
        conv_options = [f"{c['conversation_id']} ({c['partner_name']}) - {c['msg_count']} msgs, {c['mem_count']} facts" for c in convs]

        ctk.CTkLabel(modal, text="1. Xóa dữ liệu của 1 cuộc hội thoại cụ thể:", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=25, pady=(10, 2))

        combo = ctk.CTkComboBox(modal, values=conv_options if conv_options else ["Không có hội thoại nào"], width=380)
        combo.pack(padx=25, pady=5)

        def clear_one():
            val = combo.get()
            if not convs or "Không có" in val:
                return
            cid = val.split(" ")[0]
            self.mysql.clear_conversation_data(cid)
            self.qdrant.delete_memories_by_conversation(cid)
            self.log(f"Đã xóa dữ liệu của hội thoại: {cid}")
            modal.destroy()

        ctk.CTkButton(modal, text="Xóa Hội Thoại Này", command=clear_one, fg_color="#ea580c", width=200).pack(pady=5)

        ctk.CTkLabel(modal, text="2. Xóa dữ liệu TẤT CẢ cuộc hội thoại (giữ danh sách ID):", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=25, pady=(15, 2))

        def clear_all_data():
            self.mysql.clear_all_data_keep_conversations()
            self.qdrant.clear_all_memories()
            self.log("Đã xóa toàn bộ tin nhắn và ký ức của TẤT CẢ hội thoại!")
            modal.destroy()

        ctk.CTkButton(modal, text="Xóa Toàn Bộ Dữ Liệu", command=clear_all_data, fg_color="#dc2626", width=200).pack(pady=5)

        ctk.CTkLabel(modal, text="3. Xóa TRẮNG toàn bộ hệ thống:", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=25, pady=(15, 2))

        def clear_everything():
            self.mysql.clear_all_conversations()
            self.qdrant.clear_all_memories()
            self.log("Đã xóa trắng toàn bộ hệ thống cơ sở dữ liệu!")
            modal.destroy()

        ctk.CTkButton(modal, text="Xóa Trắng Hệ Thống", command=clear_everything, fg_color="#991b1b", width=200).pack(pady=5)


def main():
    app = MessengerAgentGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
