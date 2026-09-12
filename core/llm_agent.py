import os
import re
from typing import List, Dict, Any, Optional
from openai import OpenAI
from dotenv import load_dotenv
from core.prompt_builder import PromptBuilder


class LLMAgent:
    """
    Agent kết nối DeepSeek API để tạo phản hồi tự nhiên, thông minh
    với sự hỗ trợ của Mini-RAG PromptBuilder.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 256,
        frequency_penalty: float = 0.5,
        presence_penalty: float = 0.3,
    ):
        load_dotenv()
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.frequency_penalty = frequency_penalty
        self.presence_penalty = presence_penalty

        self.system_prompt = system_prompt or (
            "Bạn là trợ lý phản hồi tin nhắn Messenger cá nhân thay cho tôi khi tôi đang bận. "
            "Hãy trả lời tự nhiên, ngắn gọn (1-2 câu), thân thiện, dùng tiếng Việt chuẩn, không thêm markdown thừa."
        )

        if not self.api_key or self.api_key == "your_deepseek_api_key_here":
            print("[CẢNH BÁO] Chưa cấu hình DEEPSEEK_API_KEY hợp lệ trong file .env!")

        self.client = OpenAI(
            api_key=self.api_key if self.api_key else "dummy_key",
            base_url=self.base_url
        )

    def generate_reply(
        self,
        latest_message: str,
        memories: Optional[List[Dict[str, Any]]] = None,
        recent_history: Optional[List[Dict[str, Any]]] = None,
        partner_title: Optional[str] = None,
        is_group: bool = False,
    ) -> Optional[str]:
        """
        Sinh câu trả lời từ tin nhắn mới nhất, kết hợp Memory nạp từ Qdrant
        và Lịch sử gần đây từ MySQL.
        """
        if not self.api_key or self.api_key == "your_deepseek_api_key_here":
            return f"[Demo Auto-Reply] Mình đã nhận được tin: '{latest_message}'. Chút nữa mình rảnh sẽ nhắn lại nha!"

        # Xây dựng prompt hoàn chỉnh với Mini-RAG
        messages = PromptBuilder.build_prompt(
            system_persona=self.system_prompt,
            memories=memories or [],
            recent_history=recent_history,
            latest_incoming=latest_message,
            is_group=is_group,
        )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                frequency_penalty=self.frequency_penalty,
                presence_penalty=self.presence_penalty,
            )
            reply = response.choices[0].message.content.strip()
            if (reply.startswith('"') and reply.endswith('"')) or (reply.startswith("'") and reply.endswith("'")):
                reply = reply[1:-1].strip()

            incoming_sender = latest_message.split("\n")[0].strip() if is_group and "\n" in latest_message else None
            return self._sanitize_opening(reply, partner_title=partner_title, incoming_sender=incoming_sender)
        except Exception as e:
            print(f"[LỖI LLM] Không thể gọi DeepSeek API: {e}")
            return None

    def _sanitize_opening(
        self,
        reply: str,
        partner_title: Optional[str] = None,
        incoming_sender: Optional[str] = None
    ) -> str:
        """
        Lọc bỏ linh hoạt các thói quen mở đầu bằng thán từ chào hỏi lặp lại,
        và loại bỏ tên thành viên bị chèn nhầm vào dòng đầu tiên trong group chat.
        """
        if not reply:
            return reply

        # 1. Lọc bỏ nếu bot lỡ copy tên thành viên vào dòng đầu tiên (ví dụ: "Thiên Tài Ngáo Đá\n...")
        if "\n" in reply:
            parts = reply.split("\n", 1)
            first_line = parts[0].strip().strip("[]:")
            rest = parts[1].strip()
            if rest:
                if incoming_sender and first_line.lower() == incoming_sender.lower():
                    reply = rest
                elif partner_title and first_line.lower() == partner_title.lower():
                    reply = rest

        titles = ["Senpai"]
        if partner_title and partner_title.strip():
            clean_title = partner_title.strip()
            if clean_title not in titles:
                titles.append(re.escape(clean_title))

        pattern = rf"^(?:(?:{'|'.join(titles)})\s*(?:ơi|à|nè|nha)[,\.~!\s…\-–]+)+"
        cleaned = re.sub(pattern, '', reply, flags=re.IGNORECASE).strip()

        if cleaned and cleaned[0].isalpha():
            cleaned = cleaned[0].upper() + cleaned[1:]
            return cleaned

        return reply
