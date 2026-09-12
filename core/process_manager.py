import os
import sys
import io
import psutil
from typing import List

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PID_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".bot.pid"))

# Danh sách các script đích cần dọn dẹp
TARGET_SCRIPTS = ["main.py", "snipping_selector.py", "calibrate.py"]


def cleanup_bot_processes(exclude_current: bool = True, include_gui: bool = False) -> List[int]:
    """
    Tìm và tiêu diệt triệt để toàn bộ tiến trình python liên quan đến bot
    đang chạy ngầm hoặc bị treo, đảm bảo giải phóng toàn bộ tài nguyên (camera, hook, port).
    
    :param exclude_current: Nếu True, không tắt tiến trình hiện tại đang gọi hàm này.
    :param include_gui: Nếu True, tắt cả các tiến trình gui_app.py khác đang chạy.
    :return: Danh sách các PID đã được dọn dẹp.
    """
    current_pid = os.getpid()
    killed_pids = []

    targets = list(TARGET_SCRIPTS)
    if include_gui:
        targets.append("gui_app.py")

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pid = proc.info["pid"]
            if exclude_current and pid == current_pid:
                continue

            name = (proc.info["name"] or "").lower()
            cmdline = proc.info.get("cmdline") or []
            cmdline_str = " ".join(cmdline).lower()

            # Kiểm tra xem có phải tiến trình python chạy các script của bot không
            if "python" in name or "python" in cmdline_str:
                is_target = any(target.lower() in cmdline_str for target in targets)
                if is_target:
                    print(f"[*] Đang dọn dẹp tiến trình treo PID={pid} ({' '.join(cmdline[-2:])})...")
                    # Tiêu diệt toàn bộ tiến trình con (process tree)
                    try:
                        parent = psutil.Process(pid)
                        for child in parent.children(recursive=True):
                            child.kill()
                        parent.kill()
                        killed_pids.append(pid)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    # Dọn dẹp file .bot.pid
    if os.path.exists(PID_FILE):
        try:
            os.remove(PID_FILE)
        except Exception:
            pass

    if killed_pids:
        print(f"[✓] Đã dọn dẹp sạch sẽ {len(killed_pids)} tiến trình treo: {killed_pids}")
    return killed_pids


def record_current_pid():
    """Ghi lại PID của tiến trình hiện tại"""
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def remove_pid_file():
    """Xóa file PID khi thoát"""
    if os.path.exists(PID_FILE):
        try:
            os.remove(PID_FILE)
        except Exception:
            pass


if __name__ == "__main__":
    print("Đang quét và dọn dẹp tiến trình...")
    cleaned = cleanup_bot_processes(exclude_current=False, include_gui=True)
    print("Hoàn tất dọn dẹp!")
