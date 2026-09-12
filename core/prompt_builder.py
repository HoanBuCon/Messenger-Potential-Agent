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
    ) -> List[Dict[str, str]]:
        # 1. System message với Persona và Ký ức được nạp từ Qdrant
        system_content = system_persona.strip()

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
