import os
import sys
import re
import unicodedata
import yaml
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional, Dict, Any

from db.mysql_client import MySQLClient

CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", "config.yaml"))


def slugify_name(name: str) -> str:
    """Chuyển tên tiếng Việt có dấu thành conversation_id dạng conv_xxx"""
    if not name:
        return "conv_unknown"
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("utf-8")
    clean_slug = re.sub(r"[^a-zA-Z0-9]+", "_", normalized).strip("_").lower()
    return f"conv_{clean_slug}" if clean_slug else "conv_partner"


class ConversationDialog:
    """
    Hộp thoại cho phép người dùng can thiệp chọn cuộc hội thoại:
    - Lựa chọn 1: Chọn một cuộc hội thoại đã có từ MySQL
    - Lựa chọn 2: Tạo cuộc hội thoại mới (nhập tên đối tác, tự động sinh ID)
    """

    def __init__(self, parent=None):
        self.parent = parent
        self.mysql = MySQLClient()
        self.result: Optional[Dict[str, str]] = None
        self.root = None

    def show(self) -> Optional[Dict[str, str]]:
        if self.parent:
            self.root = tk.Toplevel(self.parent)
        else:
            self.root = tk.Tk()

        self.root.title("Cấu Hình Cuộc Hội Thoại - Messenger Bot")
        self.root.geometry("540x480")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        # Căn giữa màn hình
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() // 2) - (w // 2)
        y = (self.root.winfo_screenheight() // 2) - (h // 2)
        self.root.geometry(f"+{x}+{y}")

        self._build_ui()
        if self.parent:
            self.root.grab_set()
            self.parent.wait_window(self.root)
        else:
            self.root.mainloop()

        return self.result

    def _build_ui(self):
        # Header Banner
        header = tk.Frame(self.root, bg="#1e293b", height=60)
        header.pack(fill="x")

        tk.Label(
            header,
            text="🏷️ THIẾT LẬP CUỘC HỘI THOẠI",
            font=("Segoe UI", 13, "bold"),
            fg="#38bdf8",
            bg="#1e293b"
        ).pack(pady=(12, 2))

        tk.Label(
            header,
            text="Chọn cuộc hội thoại có sẵn hoặc tạo cuộc hội thoại mới cho phiên chat này",
            font=("Segoe UI", 9),
            fg="#94a3b8",
            bg="#1e293b"
        ).pack(pady=(0, 10))

        content = tk.Frame(self.root, padx=20, pady=15, bg="#0f172a")
        content.pack(fill="both", expand=True)

        self.choice_var = tk.StringVar(value="existing")

        # ================= OPTION 1: CHỌN HỘI THOẠI ĐÃ CÓ =================
        r1 = tk.Radiobutton(
            content,
            text="1. Chọn cuộc hội thoại đã có:",
            variable=self.choice_var,
            value="existing",
            font=("Segoe UI", 10, "bold"),
            fg="#f8fafc",
            bg="#0f172a",
            selectcolor="#1e293b",
            activebackground="#0f172a",
            activeforeground="#38bdf8",
            command=self._on_choice_changed
        )
        r1.pack(anchor="w", pady=(5, 5))

        # Lấy danh sách hội thoại từ MySQL
        convs = self.mysql.list_conversations()
        self.conv_map = {}
        conv_display_list = []

        for c in convs:
            cid = c["conversation_id"]
            name = c.get("partner_name", "Không rõ")
            msgs = c.get("msg_count", 0)
            mems = c.get("mem_count", 0)
            label = f"{name}  [{cid}] - ({msgs} tin, {mems} ký ức)"
            conv_display_list.append(label)
            self.conv_map[label] = {"id": cid, "name": name}

        # Đọc config hiện tại để chọn mặc định
        current_cid = "conv_senpai"
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                    current_cid = cfg.get("conversation", {}).get("id", "conv_senpai")
            except Exception:
                pass

        default_select = conv_display_list[0] if conv_display_list else "Không có hội thoại nào"
        for label, val in self.conv_map.items():
            if val["id"] == current_cid:
                default_select = label
                break

        self.combo_conv = ttk.Combobox(content, values=conv_display_list, width=50, state="readonly")
        if conv_display_list:
            self.combo_conv.set(default_select)
        else:
            self.combo_conv.set("Chưa có cuộc hội thoại nào trong CSDL")
            self.choice_var.set("new")  # Nếu chưa có thì chuyển sang tạo mới
        self.combo_conv.pack(anchor="w", padx=25, pady=(0, 15))

        # ================= OPTION 2: TẠO CUỘC HỘI THOẠI MỚI =================
        r2 = tk.Radiobutton(
            content,
            text="2. Tạo cuộc hội thoại mới:",
            variable=self.choice_var,
            value="new",
            font=("Segoe UI", 10, "bold"),
            fg="#f8fafc",
            bg="#0f172a",
            selectcolor="#1e293b",
            activebackground="#0f172a",
            activeforeground="#38bdf8",
            command=self._on_choice_changed
        )
        r2.pack(anchor="w", pady=(5, 5))

        new_frame = tk.Frame(content, bg="#1e293b", padx=15, pady=10)
        new_frame.pack(fill="x", padx=20, pady=(0, 15))

        tk.Label(new_frame, text="Tên người nhắn / Biệt danh:", font=("Segoe UI", 9), fg="#94a3b8", bg="#1e293b").pack(anchor="w")
        self.entry_name = tk.Entry(new_frame, font=("Segoe UI", 10), bg="#0f172a", fg="#ffffff", insertbackground="white")
        self.entry_name.pack(fill="x", pady=(2, 8))
        self.entry_name.insert(0, "Senpai")
        self.entry_name.bind("<KeyRelease>", self._auto_update_id)

        tk.Label(new_frame, text="ID cuộc hội thoại (tự sinh hoặc tùy chỉnh):", font=("Segoe UI", 9), fg="#94a3b8", bg="#1e293b").pack(anchor="w")
        self.entry_id = tk.Entry(new_frame, font=("Segoe UI", 10), bg="#0f172a", fg="#38bdf8", insertbackground="white")
        self.entry_id.pack(fill="x", pady=(2, 2))
        self.entry_id.insert(0, "conv_senpai")

        # Nút xác nhận
        btn_frame = tk.Frame(self.root, bg="#0f172a", pady=10)
        btn_frame.pack(fill="x", side="bottom")

        btn_confirm = tk.Button(
            btn_frame,
            text="✓ XÁC NHẬN & BẮT ĐẦU",
            font=("Segoe UI", 10, "bold"),
            bg="#10b981",
            fg="white",
            activebackground="#059669",
            activeforeground="white",
            padx=20,
            pady=6,
            relief="flat",
            cursor="hand2",
            command=self._on_confirm
        )
        btn_confirm.pack()

        self._on_choice_changed()

    def _auto_update_id(self, event=None):
        """Tự động sinh ID theo tên người dùng nhập"""
        name = self.entry_name.get().strip()
        slug = slugify_name(name)
        self.entry_id.delete(0, tk.END)
        self.entry_id.insert(0, slug)

    def _on_choice_changed(self):
        if self.choice_var.get() == "existing":
            self.combo_conv.config(state="readonly")
            self.entry_name.config(state="disabled")
            self.entry_id.config(state="disabled")
        else:
            self.combo_conv.config(state="disabled")
            self.entry_name.config(state="normal")
            self.entry_id.config(state="normal")

    def _on_confirm(self):
        mode = self.choice_var.get()

        if mode == "existing":
            selected_label = self.combo_conv.get()
            if not selected_label or selected_label not in self.conv_map:
                messagebox.showwarning("Cảnh báo", "Vui lòng chọn một cuộc hội thoại hợp lệ hoặc chuyển sang tạo mới!")
                return
            chosen = self.conv_map[selected_label]
            conv_id = chosen["id"]
            partner_name = chosen["name"]
        else:
            partner_name = self.entry_name.get().strip()
            conv_id = self.entry_id.get().strip()

            if not partner_name or not conv_id:
                messagebox.showwarning("Cảnh báo", "Vui lòng điền đầy đủ Tên và ID cuộc hội thoại!")
                return

        # Lưu thông tin vào MySQL
        try:
            self.mysql.ensure_conversation(conv_id, partner_name)
        except Exception as e:
            print(f"[MySQL Error] {e}")

        # Lưu vào config.yaml
        config = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f) or {}
            except Exception:
                pass

        config["conversation"] = {
            "id": conv_id,
            "partner_name": partner_name
        }

        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

        self.result = {"id": conv_id, "partner_name": partner_name}
        print(f"[✓] Đã thiết lập cuộc hội thoại hiện tại: [{conv_id}] {partner_name}")

        self.root.destroy()


def prompt_conversation(parent=None) -> Optional[Dict[str, str]]:
    """Hàm tiện ích gọi nhanh hộp thoại chọn/tạo hội thoại"""
    dialog = ConversationDialog(parent=parent)
    return dialog.show()


if __name__ == "__main__":
    res = prompt_conversation()
    print("Kết quả:", res)
