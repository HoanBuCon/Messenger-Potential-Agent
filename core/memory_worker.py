import json
import re
import threading
from typing import Optional, Callable
from openai import OpenAI
from db.mysql_client import MySQLClient
from db.qdrant_client import QdrantMemoryClient


class MemoryWorker:
    """
    Background Worker: Chạy ngầm trên một luồng riêng biệt sau mỗi lượt gửi tin.
    Nhiệm vụ:
    1. Trích xuất Fact / Thông tin quan trọng từ đoạn đối thoại vừa xong.
    2. Dùng Qdrant tìm kiếm các memories cũ có ngữ nghĩa tương đồng.
    3. Phát hiện và xử lý xung đột (đổi lịch, đổi sở thích, thông tin cập nhật).
    4. Cập nhật đồng bộ vào MySQL và Qdrant.
    """

    def __init__(
        self,
        mysql_client: MySQLClient,
        qdrant_client: QdrantMemoryClient,
        openai_client: OpenAI,
        model: str = "deepseek-chat",
    ):
        self.mysql = mysql_client
        self.qdrant = qdrant_client
        self.client = openai_client
        self.model = model

    def trigger_async_process(
        self,
        conversation_id: str,
        partner_message: str,
        bot_reply: str,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        """Kích hoạt luồng chạy ngầm không làm tắc nghẽn giao diện bot"""
        thread = threading.Thread(
            target=self._process_memory_turn,
            args=(conversation_id, partner_message, bot_reply, on_log),
            daemon=True
        )
        thread.start()

    def _process_memory_turn(
        self,
        conversation_id: str,
        partner_message: str,
        bot_reply: str,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        def log(msg: str):
            print(msg)
            if on_log:
                try:
                    on_log(msg)
                except Exception:
                    pass

        try:
            prompt = f"""Bạn là chuyên gia trích xuất và quản lý bộ nhớ dài hạn cho AI.
Hãy phân tích lượt trò chuyện sau:
- Đối phương: "{partner_message}"
- Tôi: "{bot_reply}"

Yêu cầu:
1. Trích xuất các sự thật (facts), thông tin cá nhân, lịch hẹn, sở thích hoặc thay đổi quan trọng nếu có.
2. Nếu không có thông tin gì đáng lưu giữ (chỉ chào hỏi, xã giao thông thường), trả về mảng rỗng [].
3. Xuất kết quả DUY NHẤT ở định dạng JSON dạng:
[
  {{
    "topic": "schedule / preference / personal / work / general",
    "fact": "Mô tả sự thật ngắn gọn, súc tích (VD: Hẹn gặp cafe vào 3h chiều Chủ Nhật)",
    "is_update_or_conflict": true/false
  }}
]
Chỉ trả về JSON thuần túy, không kèm markdown hoặc giải thích thêm.
"""
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=500
            )

            content = response.choices[0].message.content.strip()

            # Trích xuất mảng JSON an toàn bằng Regex
            json_match = re.search(r'\[.*\]', content, re.DOTALL)
            if json_match:
                raw_json = json_match.group(0).strip()
            else:
                raw_json = content.replace("```json", "").replace("```", "").strip()

            if not raw_json or raw_json == "[]":
                log("[Memory Worker] Không có sự thật mới cần lưu trữ.")
                return

            facts_list = json.loads(raw_json)
            if not isinstance(facts_list, list) or not facts_list:
                log("[Memory Worker] Không phát hiện sự thật hợp lệ.")
                return

            for item in facts_list:
                topic = item.get("topic", "general")
                fact = item.get("fact", "").strip()
                is_conflict = item.get("is_update_or_conflict", False)

                if not fact:
                    continue

                # 1. Tìm các memory cũ trong Qdrant có độ tương đồng ngữ nghĩa cao
                related_memories = self.qdrant.search_relevant_memories(
                    conversation_id=conversation_id,
                    query_text=fact,
                    limit=2,
                    score_threshold=0.65
                )

                supersedes_id = None

                # 2. Nếu phát hiện thông tin xung đột hoặc cập nhật với memory cũ
                if related_memories and is_conflict:
                    old_mem = related_memories[0]
                    old_id = old_mem.get("memory_id")
                    if old_id:
                        supersedes_id = old_id
                        log(f"[Memory Worker] ⚡ Phát hiện cập nhật/xung đột! Memory cũ #{old_id}: '{old_mem.get('fact')}' -> Đã chuyển sang superseded.")
                        # Cập nhật MySQL
                        self.mysql.update_memory_status(old_id, status="superseded")
                        # Cập nhật Qdrant
                        self.qdrant.update_memory_status(old_id, status="superseded")

                # 3. Lưu Fact mới vào MySQL
                new_id = self.mysql.add_memory(
                    conversation_id=conversation_id,
                    topic=topic,
                    fact=fact,
                    supersedes_id=supersedes_id
                )

                # 4. Lưu Fact mới vào Qdrant
                if new_id:
                    self.qdrant.upsert_memory(
                        memory_id=new_id,
                        conversation_id=conversation_id,
                        topic=topic,
                        fact=fact,
                        status="active"
                    )
                    log(f"[Memory Worker] ✓ Đã lưu Fact mới #{new_id}: [{topic}] '{fact}'")

        except Exception as e:
            err_msg = f"[Memory Worker ERROR] Lỗi khi xử lý trích xuất memory ngầm: {e}"
            log(err_msg)
