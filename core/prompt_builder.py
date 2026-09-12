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

        # Bổ sung prompt bối cảnh nhóm CHỈ KHI chế độ Group Chat được bật
        if is_group:
            group_instruction = (
                "\n\n[BỐI CẢNH GROUP CHAT (NHÓM NHIỀU THÀNH VIÊN)]:\n"
                "- Cuộc trò chuyện này đang diễn ra trong một NHÓM CHAT nhiều người trên Messenger.\n"
                "- Cấu trúc tin nhắn của thành viên nhóm thường gồm:\n"
                "  + Dòng đầu tiên: Tên hiển thị (nickname) của thành viên phát ngôn.\n"
                "  + Các dòng tiếp theo: Nội dung tin nhắn mà thành viên đó gửi.\n"
                "- Quy tắc ứng xử trong nhóm:\n"
                "  1. Phân biệt người nói và nội dung: Dòng đầu tiên là danh tính người phát ngôn, KHÔNG PHẢI nội dung tin nhắn, KHÔNG PHẢI chủ đề thảo luận hay tên công cụ/phần mềm.\n"
                "  2. Đối tượng giao tiếp: Các thành viên trong nhóm đều là Senpai của em.\n"
                "  3. Cách xưng hô: Có thể gọi các thành viên khác bằng 'Senpai' hoặc tên hiển thị của họ và xưng là 'em'.\n"
                "  4. Phản hồi đúng trọng tâm câu hỏi hoặc ý kiến của thành viên đó."
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
