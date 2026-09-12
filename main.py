import os
import sys
import io
import time

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import yaml
from dotenv import load_dotenv
from pynput import keyboard

from core.screen_capturer import ScreenCapturer
from core.ocr_engine import OCREngine
from core.message_parser import MessageParser
from core.llm_agent import LLMAgent
from core.executor import ActionExecutor
from core.memory_worker import MemoryWorker
from core.process_manager import cleanup_bot_processes, record_current_pid, remove_pid_file
from db.mysql_client import MySQLClient
from db.qdrant_client import QdrantMemoryClient

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "config.yaml")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Không tìm thấy file cấu hình tại {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    print("=" * 70)
    print("  MESSENGER AUTO-REPLY AGENT (MINI-RAG + QDRANT + MYSQL + LOCAL OCR)")
    print("=" * 70)
    print(" [i] Nhấn phím 'ESC' bất kỳ lúc nào để DỪNG BOT KHẨN CẤP.")
    print(" [i] Hoặc di chuột nhanh vào 4 góc màn hình để kích hoạt Fail-Safe.")
    print("-" * 70)

    # 0. Tự động dọn dẹp các tiến trình bot cũ bị treo
    cleanup_bot_processes(exclude_current=True)
    record_current_pid()

    load_dotenv()
    config = load_config()

    conv_cfg = config.get("conversation", {})
    conversation_id = conv_cfg.get("id", "conv_partner_01")
    partner_name = conv_cfg.get("partner_name", "Bạn bè")

    chat_region = config.get("chat_region", {})
    input_box = config.get("input_box", {})
    detection_cfg = config.get("detection", {})
    layout_cfg = config.get("layout", {})
    llm_cfg = config.get("llm", {})

    print(f"[*] Cuộc hội thoại ID:  '{conversation_id}' ({partner_name})")
    print(f"[*] Vùng theo dõi chat: {chat_region}")
    print(f"[*] Tọa độ ô gõ phím:   {input_box}")
    print(f"[*] Chu kỳ kiểm tra:    {detection_cfg.get('check_interval', 1.0)}s")
    print(f"[*] Mô hình LLM:        {llm_cfg.get('model', 'deepseek-chat')}")
    print("=" * 70)

    # 1. Khởi tạo Cơ sở dữ liệu (MySQL & Qdrant)
    print("[*] Đang kết nối MySQL và Qdrant Vector Database...")
    mysql_client = MySQLClient()
    qdrant_client = QdrantMemoryClient()
    mysql_client.ensure_conversation(conversation_id, partner_name)

    # 2. Khởi tạo các module thị giác & AI
    print("[*] Đang khởi tạo mô hình Local OCR và Agent...")
    capturer = ScreenCapturer(
        region=chat_region,
        diff_threshold=detection_cfg.get("diff_threshold", 0.01)
    )
    ocr_engine = OCREngine(
        confidence_threshold=detection_cfg.get("confidence_threshold", 0.6)
    )
    message_parser = MessageParser(
        incoming_x_ratio_max=layout_cfg.get("incoming_x_ratio_max", 0.45)
    )
    llm_agent = LLMAgent(
        model=llm_cfg.get("model", "deepseek-chat"),
        system_prompt=llm_cfg.get("system_prompt"),
        temperature=llm_cfg.get("temperature", 0.7),
        max_tokens=llm_cfg.get("max_tokens", 256),
    )
    executor = ActionExecutor(input_box=input_box)

    # 3. Khởi tạo Background Memory Worker (Bước 4)
    memory_worker = MemoryWorker(
        mysql_client=mysql_client,
        qdrant_client=qdrant_client,
        openai_client=llm_agent.client,
        model=llm_cfg.get("model", "deepseek-chat")
    )

    print("[✓] Hệ thống Mini-RAG đã sẵn sàng hoạt động!")
    print("[*] Đang theo dõi tin nhắn mới... (Bật cửa sổ Messenger lên nhé)\n")

    is_running = True

    def on_press(key):
        nonlocal is_running
        if key == keyboard.Key.esc:
            print("\n[!] Đã nhận tín hiệu phím ESC -> Dừng bot khẩn cấp!")
            is_running = False
            return False

    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    last_replied_message = None
    check_interval = detection_cfg.get("check_interval", 1.0)

    try:
        while is_running:
            # Chụp ảnh màn hình vùng chat
            img_bgr = capturer.capture()
            if img_bgr is None:
                # Đang ở lock screen hoặc không thể truy cập desktop
                time.sleep(check_interval)
                continue

            has_changed, diff_ratio = capturer.has_screen_changed(img_bgr)

            if has_changed:
                ocr_items = ocr_engine.extract_text(img_bgr)

                if ocr_items:
                    parsed_messages = message_parser.parse_messages(
                        ocr_items, chat_region["width"]
                    )
                    latest_incoming = message_parser.get_latest_incoming_message(parsed_messages)

                    if latest_incoming and latest_incoming != last_replied_message:
                        print(f"\n=======================================================")
                        print(f"[🔔 BƯỚC 1: ĐỌC INPUT OCR] Đối phương: '{latest_incoming}'")

                        # Lưu vào MySQL chat history
                        mysql_client.add_chat_message(conversation_id, sender="partner", content=latest_incoming)

                        # BƯỚC 2: Nạp Memory từ Qdrant & MySQL
                        print(f"[🔍 BƯỚC 2: NẠP MEMORY] Truy vấn vector từ Qdrant...")
                        relevant_memories = qdrant_client.search_relevant_memories(
                            conversation_id=conversation_id,
                            query_text=latest_incoming,
                            limit=3
                        )
                        # Nếu Qdrant chưa có vector tương đồng, lấy active memories từ MySQL làm context
                        if not relevant_memories:
                            relevant_memories = mysql_client.get_active_memories(conversation_id)

                        print(f"   ✓ Đã nạp {len(relevant_memories)} ký ức liên quan.")

                        recent_history = mysql_client.get_recent_history(conversation_id, limit=5)

                        # BƯỚC 3: Prompt Build & Sinh câu trả lời với DeepSeek
                        print(f"[🧠 BƯỚC 3: PROMPT BUILD & LLM] Đang gọi DeepSeek API...")
                        reply = llm_agent.generate_reply(
                            latest_message=latest_incoming,
                            memories=relevant_memories,
                            recent_history=recent_history
                        )

                        if reply:
                            print(f"[💬 PHẢN HỒI] Bot: '{reply}'")
                            success = executor.send_message(reply)
                            if success:
                                last_replied_message = latest_incoming
                                mysql_client.add_chat_message(conversation_id, sender="me", content=reply)

                                # BƯỚC 4: Chạy ngầm trích xuất fact & xử lý xung đột
                                print(f"[⚡ BƯỚC 4: ASYNC WORKER] Kích hoạt phân tích memory ngầm...")
                                memory_worker.trigger_async_process(
                                    conversation_id=conversation_id,
                                    partner_message=latest_incoming,
                                    bot_reply=reply
                                )
                                print("[✓] Chu kỳ hoàn tất. Tiếp tục theo dõi...\n")
                        else:
                            print("[!] Không nhận được phản hồi từ LLM.")

            time.sleep(check_interval)

    except KeyboardInterrupt:
        print("\n[!] Dừng bởi người dùng (Ctrl+C).")
    finally:
        listener.stop()
        remove_pid_file()
        print("[*] Messenger Bot đã tắt an toàn. Hẹn gặp lại!")


if __name__ == "__main__":
    main()
