import os
import sys
import io
import time
import numpy as np
import cv2
from PIL import ImageGrab
import mss
from typing import Tuple, Optional

if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass


class ScreenCapturer:
    """
    Chụp ảnh màn hình vùng chat an toàn đa màn hình (Multi-Monitor Safe):
    1. MSS direct grab
    2. MSS Virtual Desktop Grab & Crop (chống lỗi boundary màn hình phụ)
    3. PIL.ImageGrab với all_screens=True
    4. Cơ chế từ chối ảnh đen xì (Black frame rejection) bảo vệ preview & OCR.
    """

    def __init__(self, region: dict, diff_threshold: float = 0.01):
        self.region = {
            "top": int(region.get("top", 0)),
            "left": int(region.get("left", 0)),
            "width": int(region.get("width", 500)),
            "height": int(region.get("height", 500)),
        }
        self.diff_threshold = diff_threshold
        self.last_gray_frame: Optional[np.ndarray] = None
        self.last_valid_bgr: Optional[np.ndarray] = None

    def capture(self) -> Optional[np.ndarray]:
        """
        Chụp ảnh vùng chỉ định và trả về ảnh numpy BGR (chuẩn OpenCV).
        Nếu màn hình bị khóa, bị che hoặc trả về ảnh đen xì, trả về None.
        """
        # Phương án 1: Dùng MSS trực tiếp với context manager riêng biệt
        try:
            with mss.mss() as sct:
                screenshot = sct.grab(self.region)
                img = np.array(screenshot)
                img_bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                if np.mean(img_bgr) > 1.5:
                    self.last_valid_bgr = img_bgr
                    return img_bgr
        except Exception:
            pass

        # Phương án 2: Dùng MSS chụp Virtual Desktop (Monitor 0) rồi Crop trong RAM
        try:
            with mss.mss() as sct:
                v_mon = sct.monitors[0]
                v_shot = sct.grab(v_mon)
                v_img = np.array(v_shot)

                rel_x = self.region["left"] - v_mon["left"]
                rel_y = self.region["top"] - v_mon["top"]
                rel_w = self.region["width"]
                rel_h = self.region["height"]

                cropped = v_img[rel_y:rel_y + rel_h, rel_x:rel_x + rel_w]
                if cropped.size > 0:
                    img_bgr = cv2.cvtColor(cropped, cv2.COLOR_BGRA2BGR)
                    if np.mean(img_bgr) > 1.5:
                        self.last_valid_bgr = img_bgr
                        return img_bgr
        except Exception:
            pass

        # Phương án 3: Fallback sang PIL ImageGrab với all_screens=True
        try:
            bbox = (
                self.region["left"],
                self.region["top"],
                self.region["left"] + self.region["width"],
                self.region["top"] + self.region["height"]
            )
            pil_img = ImageGrab.grab(bbox=bbox, all_screens=True)
            img_rgb = np.array(pil_img)
            img_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
            if np.mean(img_bgr) > 1.5:
                self.last_valid_bgr = img_bgr
                return img_bgr
        except Exception:
            pass

        # Nếu tất cả các phương án đều thất bại hoặc trả về ảnh đen xì (do màn hình bị khóa / minimize)
        return None

    def has_screen_changed(self, current_bgr: Optional[np.ndarray]) -> Tuple[bool, float]:
        """
        So sánh khung hình hiện tại với khung hình trước đó để phát hiện thay đổi.
        :return: (is_changed, diff_ratio)
        """
        if current_bgr is None:
            return False, 0.0

        current_gray = cv2.cvtColor(current_bgr, cv2.COLOR_BGR2GRAY)

        if self.last_gray_frame is None:
            self.last_gray_frame = current_gray
            return True, 1.0

        diff = cv2.absdiff(self.last_gray_frame, current_gray)
        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        non_zero_count = cv2.countNonZero(thresh)
        total_pixels = current_gray.shape[0] * current_gray.shape[1]
        diff_ratio = non_zero_count / float(total_pixels)

        is_changed = diff_ratio >= self.diff_threshold
        if is_changed:
            self.last_gray_frame = current_gray

        return is_changed, diff_ratio

    def update_region(self, new_region: dict):
        self.region = {
            "top": int(new_region.get("top", 0)),
            "left": int(new_region.get("left", 0)),
            "width": int(new_region.get("width", 500)),
            "height": int(new_region.get("height", 500)),
        }
        self.last_gray_frame = None
