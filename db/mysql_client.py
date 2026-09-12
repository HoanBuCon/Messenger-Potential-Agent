import os
import sys
import io
from typing import List, Dict, Any, Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import pymysql
from dotenv import load_dotenv


class MySQLClient:
    """
    Client kết nối MySQL để quản lý:
    - Bảng conversations (id cuộc hội thoại, tên đối phương)
    - Bảng chat_history (lịch sử tin nhắn vào / ra)
    - Bảng memories (ký ức dạng cấu trúc, trạng thái active / superseded)
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
    ):
        load_dotenv()
        self.host = host or os.getenv("MYSQL_HOST", "localhost")
        self.port = int(port or os.getenv("MYSQL_PORT", 3306))
        self.user = user or os.getenv("MYSQL_USER", "root")
        self.password = password or os.getenv("MYSQL_PASSWORD", "")
        self.database = database or os.getenv("MYSQL_DATABASE", "messenger_bot")
        self._init_db()

    def _get_connection(self, include_database: bool = True):
        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database if include_database else None,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True
        )

    def _init_db(self):
        """Khởi tạo database và các bảng nếu chưa có"""
        try:
            # 1. Tạo database nếu chưa tồn tại
            conn = self._get_connection(include_database=False)
            with conn.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{self.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
            conn.close()

            # 2. Tạo các bảng
            conn = self._get_connection(include_database=True)
            with conn.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS conversations (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        conversation_id VARCHAR(100) NOT NULL UNIQUE,
                        partner_name VARCHAR(255) DEFAULT 'Người quen',
                        is_group BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    );
                """)
                # Tự động thêm cột is_group nếu database đã tồn tại từ trước
                cursor.execute("""
                    SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS 
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'conversations' AND COLUMN_NAME = 'is_group';
                """, (self.database,))
                if cursor.fetchone().get("cnt", 0) == 0:
                    cursor.execute("ALTER TABLE conversations ADD COLUMN is_group BOOLEAN DEFAULT FALSE;")
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS chat_history (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        conversation_id VARCHAR(100) NOT NULL,
                        sender ENUM('partner', 'me') NOT NULL,
                        content TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        INDEX (conversation_id)
                    );
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS memories (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        conversation_id VARCHAR(100) NOT NULL,
                        topic VARCHAR(100) DEFAULT 'general',
                        fact TEXT NOT NULL,
                        status ENUM('active', 'superseded', 'invalidated') DEFAULT 'active',
                        supersedes_id INT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        INDEX (conversation_id),
                        INDEX (status)
                    );
                """)
            conn.close()
            print(f"[MySQL] Kết nối và đồng bộ cấu trúc Database `{self.database}` thành công.")
        except Exception as e:
            print(f"[MySQL - CẢNH BÁO] Chưa thể kết nối MySQL tại {self.host}:{self.port} ({e}).")

    def ensure_conversation(self, conversation_id: str, partner_name: str = "Người quen", is_group: Optional[bool] = None):
        try:
            conn = self._get_connection()
            with conn.cursor() as cursor:
                if is_group is not None:
                    cursor.execute(
                        """
                        INSERT INTO conversations (conversation_id, partner_name, is_group)
                        VALUES (%s, %s, %s)
                        ON DUPLICATE KEY UPDATE partner_name = VALUES(partner_name), is_group = VALUES(is_group), updated_at = NOW();
                        """,
                        (conversation_id, partner_name, is_group)
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO conversations (conversation_id, partner_name)
                        VALUES (%s, %s)
                        ON DUPLICATE KEY UPDATE partner_name = VALUES(partner_name), updated_at = NOW();
                        """,
                        (conversation_id, partner_name)
                    )
            conn.close()
        except Exception as e:
            print(f"[MySQL Error] ensure_conversation: {e}")

    def update_conversation_group(self, conversation_id: str, is_group: bool):
        """Cập nhật trạng thái group chat cho cuộc hội thoại"""
        try:
            conn = self._get_connection()
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE conversations SET is_group = %s, updated_at = NOW() WHERE conversation_id = %s;",
                    (is_group, conversation_id)
                )
            conn.close()
        except Exception as e:
            print(f"[MySQL Error] update_conversation_group: {e}")

    def get_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """Lấy thông tin chi tiết của 1 cuộc hội thoại"""
        try:
            conn = self._get_connection()
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT conversation_id, partner_name, is_group FROM conversations WHERE conversation_id = %s LIMIT 1;",
                    (conversation_id,)
                )
                res = cursor.fetchone()
            conn.close()
            return res
        except Exception as e:
            print(f"[MySQL Error] get_conversation: {e}")
            return None

    def add_chat_message(self, conversation_id: str, sender: str, content: str):
        try:
            conn = self._get_connection()
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO chat_history (conversation_id, sender, content) VALUES (%s, %s, %s);",
                    (conversation_id, sender, content)
                )
            conn.close()
        except Exception as e:
            print(f"[MySQL Error] add_chat_message: {e}")

    def get_recent_history(self, conversation_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        try:
            conn = self._get_connection()
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT sender, content, created_at FROM (
                        SELECT sender, content, created_at FROM chat_history
                        WHERE conversation_id = %s
                        ORDER BY id DESC LIMIT %s
                    ) sub ORDER BY created_at ASC;
                    """,
                    (conversation_id, limit)
                )
                rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            print(f"[MySQL Error] get_recent_history: {e}")
            return []

    def add_memory(self, conversation_id: str, topic: str, fact: str, supersedes_id: Optional[int] = None) -> Optional[int]:
        try:
            conn = self._get_connection()
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO memories (conversation_id, topic, fact, status, supersedes_id)
                    VALUES (%s, %s, %s, 'active', %s);
                    """,
                    (conversation_id, topic, fact, supersedes_id)
                )
                memory_id = cursor.lastrowid
            conn.close()
            return memory_id
        except Exception as e:
            print(f"[MySQL Error] add_memory: {e}")
            return None

    def update_memory_status(self, memory_id: int, status: str = "superseded"):
        try:
            conn = self._get_connection()
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE memories SET status = %s WHERE id = %s;",
                    (status, memory_id)
                )
            conn.close()
        except Exception as e:
            print(f"[MySQL Error] update_memory_status: {e}")

    def get_active_memories(self, conversation_id: str) -> List[Dict[str, Any]]:
        try:
            conn = self._get_connection()
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, topic, fact, created_at FROM memories WHERE conversation_id = %s AND status = 'active';",
                    (conversation_id,)
                )
                rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            print(f"[MySQL Error] get_active_memories: {e}")
            return []

    def list_conversations(self) -> List[Dict[str, Any]]:
        """Lấy danh sách tất cả các cuộc hội thoại cùng số lượng tin nhắn và memories"""
        try:
            conn = self._get_connection()
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT 
                        c.conversation_id, 
                        c.partner_name, 
                        COALESCE(c.is_group, FALSE) AS is_group,
                        c.updated_at,
                        (SELECT COUNT(*) FROM chat_history WHERE conversation_id = c.conversation_id) AS msg_count,
                        (SELECT COUNT(*) FROM memories WHERE conversation_id = c.conversation_id AND status = 'active') AS mem_count
                    FROM conversations c
                    ORDER BY c.updated_at DESC;
                """)
                rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            print(f"[MySQL Error] list_conversations: {e}")
            return []

    def clear_conversation_data(self, conversation_id: str) -> bool:
        """Xóa toàn bộ tin nhắn chat và memories của 1 cuộc hội thoại (giữ lại thông tin conversation)"""
        try:
            conn = self._get_connection()
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM chat_history WHERE conversation_id = %s;", (conversation_id,))
                cursor.execute("DELETE FROM memories WHERE conversation_id = %s;", (conversation_id,))
            conn.close()
            return True
        except Exception as e:
            print(f"[MySQL Error] clear_conversation_data: {e}")
            return False

    def delete_conversation(self, conversation_id: str) -> bool:
        """Xóa hoàn toàn cuộc hội thoại khỏi hệ thống (bao gồm conversation, chat_history, memories)"""
        try:
            conn = self._get_connection()
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM chat_history WHERE conversation_id = %s;", (conversation_id,))
                cursor.execute("DELETE FROM memories WHERE conversation_id = %s;", (conversation_id,))
                cursor.execute("DELETE FROM conversations WHERE conversation_id = %s;", (conversation_id,))
            conn.close()
            return True
        except Exception as e:
            print(f"[MySQL Error] delete_conversation: {e}")
            return False

    def clear_all_data_keep_conversations(self) -> bool:
        """Xóa sạch tin nhắn và memories của tất cả cuộc hội thoại (vẫn giữ danh sách conversation)"""
        try:
            conn = self._get_connection()
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM chat_history;")
                cursor.execute("DELETE FROM memories;")
            conn.close()
            return True
        except Exception as e:
            print(f"[MySQL Error] clear_all_data_keep_conversations: {e}")
            return False

    def clear_all_conversations(self) -> bool:
        """Xóa sạch toàn bộ dữ liệu hệ thống (conversations, chat_history, memories)"""
        try:
            conn = self._get_connection()
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM chat_history;")
                cursor.execute("DELETE FROM memories;")
                cursor.execute("DELETE FROM conversations;")
            conn.close()
            return True
        except Exception as e:
            print(f"[MySQL Error] clear_all_conversations: {e}")
            return False

