import re
from typing import List, Dict, Any, Optional


class MessageParser:
    """
    Phân loại tin nhắn dựa trên vị trí Bounding Box (Trái = Đối phương, Phải = Chính mình)
    và lọc nhiễu thời gian, trạng thái đã xem/đã gửi, thanh công cụ, thông báo hệ thống Messenger,
    và bong bóng đang soạn tin nhắn (Typing Indicator).
    """

    # Mẫu regex phát hiện timestamp, nút bấm, tin rác và bong bóng gõ phím
    NOISE_PATTERNS = [
        # 1. Thời gian & ngày tháng
        r"^\d{1,2}:\d{2}(\s?(AM|PM|SA|CH))?$",
        r"^(Hôm nay|Hom nay|Hôm qua|Hom qua|Thứ [Hai|Ba|Tư|Năm|Sáu|Bảy]|Thu [Hai|Ba|Tu|Nam|Sau|Bay]|Chủ Nhật|Chu Nhat)",
        # 2. Trạng thái gửi / hoạt động
        r"^(Đã gửi|Da gui|Đã nhận|Da nhan|Đã xem|Da xem|Đang hoạt động|Dang hoat dong|Hoạt động|Hoat dong).*",
        # 3. Bong bóng đang soạn tin nhắn (3 dấu chấm ...)
        r"^(\.{1,6}|\•{1,6}|\-{1,6})$",
        r"^(Đang nhập|Dang nhap|Đang soạn|Dang soan).*",
        # 4. Thanh nhập liệu đáy màn hình Messenger (Placeholder & Icons)
        r"^(Aa|GIF)$",
        r"^(Nhắn tin|Nhan tin|Tin nhắn|Tin nhan)(\.\.\.)?$",
        # 5. Các nút bấm hệ thống & mã hóa đầu cuối
        r"^(Tìm hiểu thêm|Tim hi.*u th.*m)$",
        r".*(mã hóa đầu cuối|ma h.*a dau cuoi|bao mat bang tinh nang|bảo mật bằng tính năng).*",
        r".*(doan chat nay|đoạn chat này).*(doc, nghe|đọc, nghe|chia se|chia sẻ).*",
        r".*(cac ban c.* the goi va nhan tin|các bạn có thể gọi và nhắn tin|thoi diem doc tin nhan|thời điểm đọc tin nhắn).*",
        r".*(Ban da tao nhom|Bạn đã tạo nhóm).*",
        r".*(đã trả lời|da tra loi).*",
    ]

    def __init__(self, incoming_x_ratio_max: float = 0.45):
        """
        :param incoming_x_ratio_max: Ngưỡng tỷ lệ biên độ (nếu cần tinh chỉnh)
        """
        self.incoming_x_ratio_max = incoming_x_ratio_max

    def is_noise(self, text: str) -> bool:
        """
        Kiểm tra xem chuỗi văn bản có phải là timestamp, nút bấm, bong bóng gõ phím hoặc thông báo hệ thống không
        """
        cleaned = text.strip()
        if len(cleaned) == 0:
            return True

        # Lọc nhanh chuỗi chỉ toàn dấu chấm hoặc ký tự đặc biệt ngắn (bong bóng typing ...)
        if re.fullmatch(r"[\s\.\•\-\_\…]{1,6}", cleaned):
            return True

        for pattern in self.NOISE_PATTERNS:
            if re.search(pattern, cleaned, re.IGNORECASE):
                return True
        return False

    def parse_messages(
        self,
        ocr_items: List[Dict[str, Any]],
        chat_width: int,
        chat_height: Optional[int] = None,
        partner_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Phân loại các dòng chữ thành tin nhắn Incoming / Outgoing và ghép các dòng liền nhau.
        Đặc biệt xử lý chính xác cả các tin nhắn dài của bản thân không bị nhận nhầm thành của đối phương.
        """
        valid_items = []
        for item in ocr_items:
            text = item["text"].strip()

            # 1. Bỏ qua các dòng rác hệ thống (timestamp, encryption notice, toolbar Aa/GIF, typing ...)
            if self.is_noise(text):
                continue

            # 2. Bỏ qua header ở đỉnh màn hình nếu trùng tên đối phương
            if partner_name and item["min_y"] < 90:
                if partner_name.lower() in text.lower():
                    continue

            # 3. Bỏ qua nếu dòng chữ nằm ở đáy cùng sát mép (thanh nhập liệu rác)
            if chat_height and item["min_y"] > (chat_height - 40):
                if len(text) <= 5 or self.is_noise(text):
                    continue

            # 4. Phân loại hình học thông minh (Left-aligned vs Right-aligned):
            # Trong Messenger:
            # - Tin nhắn đối phương (partner) BẮT BUỘC neo ở lề trái: min_x < 32% chat_width và max_x < 78% chat_width.
            # - Tin nhắn của mình (me) BẮT BUỘC neo ở lề phải: min_x >= 32% chat_width HOẶC max_x >= 75% chat_width.
            is_partner = (item["min_x"] < (chat_width * 0.32)) and (item["max_x"] < (chat_width * 0.78))
            sender = "partner" if is_partner else "me"

            valid_items.append({
                **item,
                "sender": sender
            })

        if not valid_items:
            return []

        # Sắp xếp theo chiều từ trên xuống dưới (trục Y tăng dần)
        valid_items.sort(key=lambda x: x["min_y"])

        # Nhóm các dòng liền kề thuộc cùng một bong bóng chat (khoảng cách Y < 35px và cùng sender)
        merged_messages = []
        current_msg = None

        for item in valid_items:
            if current_msg is None:
                current_msg = {
                    "sender": item["sender"],
                    "texts": [item["text"]],
                    "min_y": item["min_y"],
                    "max_y": item["max_y"],
                    "min_x": item["min_x"],
                    "max_x": item["max_x"],
                }
            else:
                # Nếu cùng người gửi hoặc là dòng đuôi liền kề của tin nhắn mình (me)
                y_diff = item["min_y"] - current_msg["max_y"]
                same_bubble = (item["sender"] == current_msg["sender"]) or (
                    current_msg["sender"] == "me" and item["min_x"] >= (chat_width * 0.25)
                )
                if same_bubble and 0 <= y_diff <= 35:
                    current_msg["texts"].append(item["text"])
                    current_msg["max_y"] = max(current_msg["max_y"], item["max_y"])
                    current_msg["min_x"] = min(current_msg["min_x"], item["min_x"])
                    current_msg["max_x"] = max(current_msg["max_x"], item["max_x"])
                else:
                    merged_messages.append({
                        "sender": current_msg["sender"],
                        "text": "\n".join(current_msg["texts"]).strip(),
                        "min_y": current_msg["min_y"],
                        "max_y": current_msg["max_y"],
                    })
                    current_msg = {
                        "sender": item["sender"],
                        "texts": [item["text"]],
                        "min_y": item["min_y"],
                        "max_y": item["max_y"],
                        "min_x": item["min_x"],
                        "max_x": item["max_x"],
                    }

        if current_msg is not None:
            merged_messages.append({
                "sender": current_msg["sender"],
                "text": "\n".join(current_msg["texts"]).strip(),
                "min_y": current_msg["min_y"],
                "max_y": current_msg["max_y"],
            })

        return merged_messages

    def get_latest_incoming_message(self, merged_messages: List[Dict[str, Any]]) -> Optional[str]:
        """
        Lấy tin nhắn mới nhất nếu và chỉ nếu nó đến từ đối phương (partner).
        Nếu tin nhắn cuối cùng là của chính mình (me) -> Trả về None.
        """
        if not merged_messages:
            return None

        # Tin nhắn cuối cùng ở đáy khung chat
        last_msg = merged_messages[-1]

        if last_msg["sender"] == "partner":
            return last_msg["text"]

        return None
