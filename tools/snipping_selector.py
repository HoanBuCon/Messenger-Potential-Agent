import os
import sys
import io
import time
import yaml
import tkinter as tk
from mss import MSS
import cv2
import numpy as np
from typing import Optional, Callable

# 1. Kích hoạt Windows Per-Monitor DPI Awareness để tránh lệch / co tọa độ chuột trên đa màn hình
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor DPI Aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", "config.yaml"))


class MultiMonitorSnippingSelector:
    """
    Công cụ Snipping Tool 2 giai đoạn (2-Stage Bounding Box Selector) trên ĐA MÀN HÌNH:
    - BƯỚC 1: Kéo khoanh VÙNG ĐỌC TIN NHẮN (OCR) - chừa thanh công cụ gõ bên dưới để không bị dính chữ thừa như 'Aa'.
    - BƯỚC 2: Kéo khoanh VÙNG Ô NHẬP TIN NHẮN (Input Field) - hệ thống tự động tính toán tâm chính xác (center_x, center_y).
    
    Tính năng vượt trội:
    - Zero-flicker: Diễn ra liên tục trong cùng 1 cửa sổ trong suốt, không bị chớp giật.
    - Multi-Monitor HUD: Hiển thị thanh banner hướng dẫn nổi ở đỉnh tất cả các màn hình.
    - Hỗ trợ cả 3 cách chọn ô nhập: Kéo hình chữ nhật, Click 1 điểm, hoặc nhấn ENTER để dùng gợi ý.
    - Phím tắt Backspace / R để làm lại Bước 1 bất cứ lúc nào.
    """

    def __init__(self, on_complete_callback: Optional[Callable[[dict, dict, Optional[dict]], None]] = None):
        self.on_complete_callback = on_complete_callback
        self.root = None
        self.canvas = None

        # Trạng thái quy trình: 1 = OCR Chat, 2 = Ô nhập tin nhắn
        self.step = 1

        self.selected_region = None
        self.selected_input_box = None

        # Điểm bắt đầu kéo chuột
        self.start_root_x = None
        self.start_root_y = None

        # Quản lý các đối tượng vẽ trên Canvas
        self.current_drag_rect = None
        self.step1_rect_item = None
        self.step1_badge_items = []
        self.step2_rect_item = None
        self.step2_badge_items = []
        self.banner_items = []
        self.guide_suggest_items = []

        # Lấy thông tin Virtual Desktop và danh sách tất cả các màn hình từ MSS
        with MSS() as sct:
            v_mon = sct.monitors[0]
            self.v_left = int(v_mon["left"])
            self.v_top = int(v_mon["top"])
            self.v_width = int(v_mon["width"])
            self.v_height = int(v_mon["height"])

            self.monitors_info = [
                {
                    "left": int(m["left"]),
                    "top": int(m["top"]),
                    "width": int(m["width"]),
                    "height": int(m["height"])
                }
                for m in sct.monitors[1:]
            ]

    def _apply_windows_geometry(self, window_title: str):
        """Dùng Win32 SetWindowPos để ghim cửa sổ phủ trọn vẹn toàn bộ các màn hình kể cả tọa độ âm"""
        if sys.platform == "win32":
            try:
                user32 = ctypes.windll.user32
                user32.SetWindowPos.argtypes = [
                    wintypes.HWND, wintypes.HWND,
                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                    ctypes.c_uint
                ]
                self.root.update_idletasks()
                hwnd = user32.FindWindowW(None, window_title)
                if hwnd:
                    # HWND_TOPMOST = -1, SWP_SHOWWINDOW = 0x0040
                    user32.SetWindowPos(hwnd, -1, self.v_left, self.v_top, self.v_width, self.v_height, 0x0040)
            except Exception as e:
                print(f"[Cảnh báo SetWindowPos] {e}")

    def _clear_banners(self):
        for item in self.banner_items:
            try:
                self.canvas.delete(item)
            except Exception:
                pass
        self.banner_items = []

    def _draw_hud_banners(self, title: str, subtitle: str, hotkeys: str, accent_color: str):
        """Vẽ HUD Banner hướng dẫn trên đỉnh của TẤT CẢ các màn hình để người dùng luôn thấy"""
        self._clear_banners()

        targets = self.monitors_info if self.monitors_info else [{"left": self.v_left, "top": self.v_top, "width": self.v_width, "height": self.v_height}]

        for m in targets:
            cx = m["left"] - self.v_left + m["width"] // 2
            cy = m["top"] - self.v_top + 50

            pill_w = 460
            bg_pill = self.canvas.create_rectangle(
                cx - pill_w, cy - 36, cx + pill_w, cy + 36,
                fill="#0f172a", outline=accent_color, width=2
            )
            t1 = self.canvas.create_text(
                cx, cy - 14,
                text=title,
                fill=accent_color,
                font=("Segoe UI", 12, "bold")
            )
            t2 = self.canvas.create_text(
                cx, cy + 6,
                text=subtitle,
                fill="#f8fafc",
                font=("Segoe UI", 10)
            )
            t3 = self.canvas.create_text(
                cx, cy + 22,
                text=hotkeys,
                fill="#94a3b8",
                font=("Segoe UI", 9, "italic")
            )
            self.banner_items.extend([bg_pill, t1, t2, t3])

    def start_selection(self):
        """Khởi động cửa sổ chọn vùng toàn màn hình"""
        window_title = "Snipping_Overlay_MultiMonitor_2Stage"
        self.root = tk.Tk()
        self.root.title(window_title)
        self.root.overrideredirect(True)
        self.root.attributes("-alpha", 0.35)
        self.root.config(cursor="cross")

        self.root.geometry(f"{self.v_width}x{self.v_height}+0+0")
        self._apply_windows_geometry(window_title)

        self.canvas = tk.Canvas(
            self.root,
            width=self.v_width,
            height=self.v_height,
            bg="gray15",
            highlightthickness=0
        )
        self.canvas.pack(fill="both", expand=True)

        # Cài đặt Bước 1
        self._setup_step1()

        # Phím ESC luôn dùng để hủy
        self.root.bind("<Escape>", lambda e: self.on_cancel())

        self.root.mainloop()

    # =========================================================================
    # BƯỚC 1: CHỌN VÙNG QUÉT TIN NHẮN (OCR REGION)
    # =========================================================================
    def _setup_step1(self):
        self.step = 1
        self.selected_region = None
        self.root.config(cursor="cross")

        # Xóa các thành phần cũ nếu có
        if self.step1_rect_item:
            self.canvas.delete(self.step1_rect_item)
            self.step1_rect_item = None
        for b in self.step1_badge_items:
            self.canvas.delete(b)
        self.step1_badge_items = []

        self._draw_hud_banners(
            title="[BƯỚC 1/2] KÉO KHOANH VÙNG ĐỌC TIN NHẮN (OCR)",
            subtitle="Kéo chọn vùng lịch sử tin nhắn (chừa thanh nhập bên dưới để tránh dính chữ thừa như 'Aa')",
            hotkeys="Nhấn ESC để hủy bỏ",
            accent_color="#00ffcc"
        )

        self.canvas.bind("<ButtonPress-1>", self.on_step1_press)
        self.canvas.bind("<B1-Motion>", self.on_step1_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_step1_release)

    def on_step1_press(self, event):
        self.start_root_x = event.x_root
        self.start_root_y = event.y_root

        if self.current_drag_rect:
            self.canvas.delete(self.current_drag_rect)

        start_cx = self.start_root_x - self.v_left
        start_cy = self.start_root_y - self.v_top

        self.current_drag_rect = self.canvas.create_rectangle(
            start_cx, start_cy, start_cx, start_cy,
            outline="#00ffcc", width=3, fill="#00ffcc", stipple="gray25"
        )

    def on_step1_drag(self, event):
        if self.start_root_x is None or self.start_root_y is None or not self.current_drag_rect:
            return

        x1 = min(self.start_root_x, event.x_root) - self.v_left
        y1 = min(self.start_root_y, event.y_root) - self.v_top
        x2 = max(self.start_root_x, event.x_root) - self.v_left
        y2 = max(self.start_root_y, event.y_root) - self.v_top

        self.canvas.coords(self.current_drag_rect, x1, y1, x2, y2)

    def on_step1_release(self, event):
        if self.start_root_x is None or self.start_root_y is None:
            return

        w = abs(event.x_root - self.start_root_x)
        h = abs(event.y_root - self.start_root_y)

        if w < 40 or h < 40:
            if self.current_drag_rect:
                self.canvas.delete(self.current_drag_rect)
                self.current_drag_rect = None
            return

        abs_left = min(self.start_root_x, event.x_root)
        abs_top = min(self.start_root_y, event.y_root)

        self.selected_region = {
            "left": int(abs_left),
            "top": int(abs_top),
            "width": int(w),
            "height": int(h)
        }

        # Khóa hình chữ nhật Bước 1 với viền xanh lá cố định, bỏ stipple để nhìn rõ màn hình
        self.step1_rect_item = self.current_drag_rect
        self.current_drag_rect = None
        self.canvas.itemconfig(self.step1_rect_item, outline="#10b981", width=3, fill="", stipple="")

        # Gắn huy hiệu góc trên trái của khung chat
        cx1 = abs_left - self.v_left
        cy1 = abs_top - self.v_top

        badge_bg = self.canvas.create_rectangle(
            cx1, cy1 - 26, cx1 + 300, cy1,
            fill="#064e3b", outline="#10b981", width=1
        )
        badge_txt = self.canvas.create_text(
            cx1 + 150, cy1 - 13,
            text=f"✓ VÙNG 1: QUÉT TIN NHẮN (OCR) [{w}x{h}]",
            fill="#6ee7b7",
            font=("Segoe UI", 10, "bold")
        )
        self.step1_badge_items.extend([badge_bg, badge_txt])

        # Chuyển tiếp mượt sang Bước 2 mà KHÔNG cần đóng mở lại cửa sổ!
        self._setup_step2()

    # =========================================================================
    # BƯỚC 2: CHỌN VÙNG HOẶC ĐIỂM Ô NHẬP TIN NHẮN (INPUT BOX REGION)
    # =========================================================================
    def _setup_step2(self):
        self.step = 2
        self.root.config(cursor="cross")

        self._draw_hud_banners(
            title="[BƯỚC 2/2] KÉO KHOANH VÙNG HOẶC CLICK VÀO Ô NHẬP TIN NHẮN",
            subtitle="Kéo chọn khung nhập 'Nhắn tin...' để Bot lấy tâm chính xác (hoặc Click thẳng / Nhấn ENTER để dùng gợi ý)",
            hotkeys="[ENTER / SPACE]: Dùng vị trí gợi ý  |  [BACKSPACE / R]: Chọn lại Bước 1  |  [ESC]: Hủy",
            accent_color="#f59e0b"
        )

        # Tính toán vị trí gợi ý tự động (ngay bên dưới hoặc sát đáy vùng chat)
        reg_l = self.selected_region["left"]
        reg_t = self.selected_region["top"]
        reg_w = self.selected_region["width"]
        reg_h = self.selected_region["height"]

        auto_x = int(reg_l + reg_w / 2)
        # Giả định ô nhập nằm sát đáy vùng chat hoặc ngay dưới vùng chat 40px
        auto_y = int(reg_t + reg_h - 60)
        auto_cx = auto_x - self.v_left
        auto_cy = auto_y - self.v_top

        # Vẽ tâm gợi ý màu vàng nhạt
        for item in self.guide_suggest_items:
            self.canvas.delete(item)
        self.guide_suggest_items = []

        suggest_circ = self.canvas.create_oval(
            auto_cx - 18, auto_cy - 18, auto_cx + 18, auto_cy + 18,
            outline="#fbbf24", width=2, dash=(4, 4)
        )
        suggest_txt = self.canvas.create_text(
            auto_cx, auto_cy + 24,
            text="🎯 Vị trí gợi ý (Nhấn ENTER để chọn, hoặc kéo khoanh ô nhập thực tế)",
            fill="#fbbf24",
            font=("Segoe UI", 10, "bold")
        )
        self.guide_suggest_items.extend([suggest_circ, suggest_txt])

        # Binds cho Bước 2
        self.canvas.bind("<ButtonPress-1>", self.on_step2_press)
        self.canvas.bind("<B1-Motion>", self.on_step2_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_step2_release)

        self.root.bind("<Return>", lambda e: self.on_step2_confirm_auto(auto_x, auto_y))
        self.root.bind("<space>", lambda e: self.on_step2_confirm_auto(auto_x, auto_y))
        self.root.bind("<BackSpace>", lambda e: self.on_step2_back_to_step1())
        self.root.bind("<r>", lambda e: self.on_step2_back_to_step1())
        self.root.bind("<R>", lambda e: self.on_step2_back_to_step1())

    def on_step2_press(self, event):
        self.start_root_x = event.x_root
        self.start_root_y = event.y_root

        if self.current_drag_rect:
            self.canvas.delete(self.current_drag_rect)

        start_cx = self.start_root_x - self.v_left
        start_cy = self.start_root_y - self.v_top

        self.current_drag_rect = self.canvas.create_rectangle(
            start_cx, start_cy, start_cx, start_cy,
            outline="#f59e0b", width=3, fill="#f59e0b", stipple="gray25"
        )

    def on_step2_drag(self, event):
        if self.start_root_x is None or self.start_root_y is None or not self.current_drag_rect:
            return

        x1 = min(self.start_root_x, event.x_root) - self.v_left
        y1 = min(self.start_root_y, event.y_root) - self.v_top
        x2 = max(self.start_root_x, event.x_root) - self.v_left
        y2 = max(self.start_root_y, event.y_root) - self.v_top

        self.canvas.coords(self.current_drag_rect, x1, y1, x2, y2)

    def on_step2_release(self, event):
        if self.start_root_x is None or self.start_root_y is None:
            return

        w = abs(event.x_root - self.start_root_x)
        h = abs(event.y_root - self.start_root_y)

        # Xóa hình vẽ gợi ý
        for item in self.guide_suggest_items:
            self.canvas.delete(item)
        self.guide_suggest_items = []

        if w >= 8 and h >= 8:
            # Người dùng đã kéo một vùng hình chữ nhật quanh ô nhập
            min_x = min(self.start_root_x, event.x_root)
            min_y = min(self.start_root_y, event.y_root)
            cx = int(min_x + w / 2)
            cy = int(min_y + h / 2)

            self.selected_input_box = {
                "x": cx,
                "y": cy,
                "left": int(min_x),
                "top": int(min_y),
                "width": int(w),
                "height": int(h)
            }

            self.step2_rect_item = self.current_drag_rect
            self.current_drag_rect = None
            self.canvas.itemconfig(self.step2_rect_item, outline="#f59e0b", width=3, fill="", stipple="")

            canvas_cx = cx - self.v_left
            canvas_cy = cy - self.v_top

            # Vẽ tâm chữ thập ngay giữa vùng nhập
            cross1 = self.canvas.create_line(canvas_cx - 12, canvas_cy, canvas_cx + 12, canvas_cy, fill="#f59e0b", width=2)
            cross2 = self.canvas.create_line(canvas_cx, canvas_cy - 12, canvas_cx, canvas_cy + 12, fill="#f59e0b", width=2)
            t_circ = self.canvas.create_oval(canvas_cx - 6, canvas_cy - 6, canvas_cx + 6, canvas_cy + 6, fill="#f59e0b", outline="")

            badge = self.canvas.create_text(
                canvas_cx, canvas_cy - 22,
                text=f"✓ VÙNG 2: TÂM Ô NHẬP (X={cx}, Y={cy})",
                fill="#fbbf24",
                font=("Segoe UI", 10, "bold")
            )
            self.step2_badge_items.extend([cross1, cross2, t_circ, badge])

        else:
            # Người dùng click đơn trực tiếp vào điểm nhập
            cx = int(event.x_root)
            cy = int(event.y_root)
            self.selected_input_box = {"x": cx, "y": cy}

            if self.current_drag_rect:
                self.canvas.delete(self.current_drag_rect)
                self.current_drag_rect = None

            canvas_cx = cx - self.v_left
            canvas_cy = cy - self.v_top

            t_circ = self.canvas.create_oval(canvas_cx - 16, canvas_cy - 16, canvas_cx + 16, canvas_cy + 16, outline="#f59e0b", width=3)
            cross1 = self.canvas.create_line(canvas_cx - 10, canvas_cy, canvas_cx + 10, canvas_cy, fill="#f59e0b", width=2)
            cross2 = self.canvas.create_line(canvas_cx, canvas_cy - 10, canvas_cx, canvas_cy + 10, fill="#f59e0b", width=2)
            badge = self.canvas.create_text(
                canvas_cx, canvas_cy - 26,
                text=f"✓ VÙNG 2: TỌA ĐỘ NHẬP (X={cx}, Y={cy})",
                fill="#fbbf24",
                font=("Segoe UI", 10, "bold")
            )
            self.step2_badge_items.extend([t_circ, cross1, cross2, badge])

        self._show_success_and_finish()

    def on_step2_confirm_auto(self, auto_x: int, auto_y: int):
        """Xác nhận sử dụng tọa độ gợi ý mặc định"""
        self.selected_input_box = {"x": auto_x, "y": auto_y}

        for item in self.guide_suggest_items:
            self.canvas.delete(item)
        self.guide_suggest_items = []

        auto_cx = auto_x - self.v_left
        auto_cy = auto_y - self.v_top

        t_circ = self.canvas.create_oval(auto_cx - 16, auto_cy - 16, auto_cx + 16, auto_cy + 16, outline="#10b981", width=3)
        badge = self.canvas.create_text(
            auto_cx, auto_cy - 24,
            text=f"✓ VÙNG 2: TÂM MẶC ĐỊNH (X={auto_x}, Y={auto_y})",
            fill="#6ee7b7",
            font=("Segoe UI", 10, "bold")
        )
        self.step2_badge_items.extend([t_circ, badge])

        self._show_success_and_finish()

    def on_step2_back_to_step1(self):
        """Cho phép người dùng quay lại Bước 1 mượt mà"""
        if self.current_drag_rect:
            self.canvas.delete(self.current_drag_rect)
            self.current_drag_rect = None

        for item in self.step2_badge_items:
            self.canvas.delete(item)
        self.step2_badge_items = []

        for item in self.guide_suggest_items:
            self.canvas.delete(item)
        self.guide_suggest_items = []

        # Hủy bind bước 2
        self.root.unbind("<Return>")
        self.root.unbind("<space>")
        self.root.unbind("<BackSpace>")
        self.root.unbind("<r>")
        self.root.unbind("<R>")

        self._setup_step1()

    def _show_success_and_finish(self):
        """Hiển thị thông báo thành công và hẹn giờ lưu sau 350ms để người dùng thấy rõ cả 2 vùng"""
        self._draw_hud_banners(
            title="✓ THIẾT LẬP THÀNH CÔNG CẢ 2 VÙNG!",
            subtitle=f"Khung chat OCR: {self.selected_region['width']}x{self.selected_region['height']} | Tâm ô nhập: ({self.selected_input_box['x']}, {self.selected_input_box['y']})",
            hotkeys="Đang tự động lưu cấu hình và đóng cửa sổ...",
            accent_color="#10b981"
        )
        self.root.after(380, self.finish_and_save)

    def on_cancel(self):
        print("[!] Người dùng đã nhấn ESC hủy bỏ thao tác chọn vùng.")
        if self.root:
            self.root.destroy()
            self.root = None

    def finish_and_save(self):
        if self.root:
            self.root.destroy()
            self.root = None

        # Nghỉ 100ms để hệ điều hành giải phóng hoàn toàn cửa sổ overlay khỏi bộ đệm màn hình
        time.sleep(0.12)
        self.save_and_preview()

    def save_and_preview(self):
        if not self.selected_region or not self.selected_input_box:
            return

        print("\n" + "=" * 65)
        print(" [THIẾT LẬP VÙNG HOÀN TẤT] (MULTI-MONITOR 2-STAGE DPI SAFE)")
        print(f" - [Vùng 1 - OCR Chat]: {self.selected_region}")
        print(f" - [Vùng 2 - Ô Nhập]  : {self.selected_input_box}")
        print("=" * 65)

        # Cập nhật config.yaml
        config = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f) or {}
            except Exception:
                pass

        config["chat_region"] = self.selected_region
        config["input_box"] = self.selected_input_box

        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

        print(f" [✓] Đã lưu cấu hình vào: {CONFIG_PATH}")

        # Chụp ảnh kiểm chứng bằng ScreenCapturer chuẩn đa màn hình
        try:
            from core.screen_capturer import ScreenCapturer
            capturer = ScreenCapturer(self.selected_region)
            img_bgr = capturer.capture()
            if img_bgr is not None:
                debug_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "debug_calibration.png"))
                cv2.imwrite(debug_path, img_bgr)
                print(f" [✓] Đã lưu ảnh chụp kiểm chứng tại: {debug_path}")
        except Exception as e:
            print(f" [!] Không thể chụp ảnh kiểm chứng: {e}")

        # Callback thông báo cho GUI
        if self.on_complete_callback:
            self.on_complete_callback(self.selected_region, self.selected_input_box, None)


def main():
    print("Khởi chạy Bounding Box Snipping Tool 2 giai đoạn (DPI-Safe)...")
    selector = MultiMonitorSnippingSelector()
    selector.start_selection()


if __name__ == "__main__":
    main()
