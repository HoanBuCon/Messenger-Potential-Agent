from typing import List, Dict, Any, Optional


class PromptBuilder:
    """
    Xây dựng cấu trúc Multi-Turn Chat Messages chuẩn định dạng ChatML:
    1. System message: Persona & Dynamic Memories từ Qdrant
    2. Multi-turn history: Từng lượt tin nhắn 'user' (đối phương) và 'assistant' (Chisa) tự nhiên
    3. Latest turn: Tin nhắn mới nhất của đối phương dưới dạng role 'user'
    
    Không sử dụng bất kỳ câu lệnh mồi (priming / cueing) hay hardcode nào trong nội dung tin nhắn.
    """

    @staticmethod
    def build_prompt(
        system_persona: str,
        memories: List[Dict[str, Any]],
        recent_history: Optional[List[Dict[str, Any]]],
        latest_incoming: str,
        is_group: bool = False,
    ) -> List[Dict[str, str]]:
        # 1. System message với Persona và Ký ức được nạp từ Qdrant
        system_content = system_persona.strip()

        # Bổ sung bối cảnh nhóm CHỈ KHI chế độ Group Chat được bật (chỉ bổ sung môi trường, không làm loãng Persona)
        if is_group:
            group_instruction = (
                "\n\n[BỔ SUNG BỐI CẢNH GROUP CHAT]:\n"
                "- Môi trường hội thoại: Nhóm chat Messenger có nhiều thành viên.\n"
                "- Nhận diện dữ liệu đầu vào: Dòng đầu tiên của tin nhắn OCR thường là Tên hiển thị (nickname) của thành viên phát ngôn, các dòng tiếp theo là nội dung họ nói. Nickname chỉ là danh xưng mạng xã hội, không phải nội dung câu chuyện hay chủ đề.\n"
                "- Giữ trọn vẹn bản sắc Kuchiba Chisa: Dù ở trong nhóm, em vẫn là Kuudere Havoc Resonator điềm tĩnh, sắc sảo, ít nói; luôn tự xưng là 'Em'. Tuyệt đối KHÔNG bao giờ xưng 'Anh' hay 'Tôi', không biến thành trợ lý máy móc hay nhân viên y tế/tư vấn.\n"
                "- Định dạng câu trả lời: Chỉ xuất trực tiếp lời thoại mà em muốn nói, tuyệt đối KHÔNG viết tên thành viên hay bất kỳ tiêu đề nào ở dòng đầu câu trả lời."
            )
            system_content += group_instruction

        if memories:
            memory_lines = []
            for idx, m in enumerate(memories, 1):
                topic = m.get("topic", "general")
                fact = m.get("fact", "")
                memory_lines.append(f"{idx}. [{topic}] {fact}")
            memory_section = "\n\n[MEMORIES]:\n" + "\n".join(memory_lines)
            system_content += memory_section

        messages = [
            {"role": "system", "content": system_content}
        ]

        # 2. Thêm các lượt hội thoại trước dưới dạng Multi-Turn Messages chuẩn ChatML
        if recent_history:
            for msg in recent_history:
                sender = msg.get("sender")
                role = "user" if sender == "partner" else "assistant"
                content = (msg.get("content") or "").strip()
                if content:
                    messages.append({"role": role, "content": content})

        # 3. Lượt đối thoại hiện tại: Tin nhắn mới nhất của đối phương
        messages.append({"role": "user", "content": latest_incoming.strip()})
        return messages
