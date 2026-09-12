import sys
import time
import pyautogui
import pyperclip
from typing import Optional, Dict

if sys.platform == "win32":
    import ctypes


class ActionExecutor:
    """
    Thực hiện hành vi gửi tin nhắn an toàn qua Clipboard (hỗ trợ tiếng Việt tuyệt đối)
    và Win32 / PyAutoGUI trên môi trường đa màn hình.
    """

    def __init__(self, input_box: Optional[Dict[str, int]] = None, cooldown_seconds: float = 2.0):
        self.input_box = input_box
        self.cooldown_seconds = cooldown_seconds
        self.last_send_time = 0.0

        # Tắt FAILSAFE của PyAutoGUI để hỗ trợ click đa màn hình mà không bị crash
        pyautogui.FAILSAFE = False

    def send_message(self, text: str) -> bool:
        """
        Copy nội dung vào Clipboard, click ô chat (nếu có tọa độ), paste Ctrl+V và nhấn Enter.
        """
        now = time.time()
        if now - self.last_send_time < self.cooldown_seconds:
            print(f"[EXECUTOR] Đang trong thời gian cooldown ({self.cooldown_seconds}s), tạm hoãn gửi.")
            return False

        if not text or not text.strip():
            return False

        try:
            # 1. Click vào ô nhập tin nhắn để chuyển focus chính xác sang Messenger
            if self.input_box and self.input_box.get("x") is not None and self.input_box.get("y") is not None:
                tx = int(self.input_box["x"])
                ty = int(self.input_box["y"])

                if sys.platform == "win32":
                    try:
                        ctypes.windll.user32.SetCursorPos(tx, ty)
                    except Exception:
                        pass

                # Sử dụng PyAutoGUI (SendInput API) để click thực sự vào ô nhập
                pyautogui.moveTo(tx, ty)
                time.sleep(0.08)
                pyautogui.click(tx, ty)
                time.sleep(0.25)

            # 2. Copy nội dung vào clipboard
            pyperclip.copy(text.strip())
            time.sleep(0.12)

            # 3. Paste bằng Ctrl + V
            pyautogui.hotkey("ctrl", "v")
            # Chờ Messenger DOM hoàn tất render văn bản nhiều dòng từ clipboard
            time.sleep(0.35)

            # 4. Nhấn Enter để gửi
            pyautogui.press("enter")
            self.last_send_time = time.time()

            print(f"[EXECUTOR] Đã gửi thành công: '{text.strip()}'")
            return True

        except Exception as e:
            print(f"[LỖI EXECUTOR] Thất bại khi gửi tin nhắn: {e}")
            return False
