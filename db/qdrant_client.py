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

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.http import models
from fastembed import TextEmbedding


class QdrantMemoryClient:
    """
    Client kết nối Qdrant Vector Database:
    - Lưu trữ vector embeddings của các sự thật (memories)
    - Truy vấn tìm kiếm ngữ nghĩa (Semantic Search) phục vụ Mini-RAG
    - Hỗ trợ lọc theo conversation_id và status='active'
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        collection_name: Optional[str] = None,
    ):
        load_dotenv()
        self.host = host or os.getenv("QDRANT_HOST", "localhost")
        self.port = int(port or os.getenv("QDRANT_PORT", 6333))
        self.collection_name = collection_name or os.getenv("QDRANT_COLLECTION", "messenger_memories")

        # 1. Khởi tạo FastEmbed (Model siêu nhẹ chạy ONNX local)
        print("[Qdrant] Khởi tạo mô hình nhúng cục bộ FastEmbed (BAAI/bge-small-en-v1.5)...")
        self.embedding_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        self.vector_size = 384

        # 2. Khởi tạo Qdrant Client
        self.client = QdrantClient(host=self.host, port=self.port, timeout=10.0, check_compatibility=False)
        self._ensure_collection()

    def _ensure_collection(self):
        """Đảm bảo Collection tồn tại trong Qdrant"""
        try:
            collections = self.client.get_collections().collections
            exists = any(c.name == self.collection_name for c in collections)
            if not exists:
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=models.VectorParams(
                        size=self.vector_size,
                        distance=models.Distance.COSINE
                    )
                )
                print(f"[Qdrant] Đã tạo mới Collection: `{self.collection_name}`.")
            else:
                print(f"[Qdrant] Đã kết nối thành công tới Collection: `{self.collection_name}`.")
        except Exception as e:
            print(f"[Qdrant - CẢNH BÁO] Không thể kết nối Qdrant tại {self.host}:{self.port} ({e}).")

    def _get_embedding(self, text: str) -> List[float]:
        """Tạo vector embedding từ văn bản"""
        embeddings = list(self.embedding_model.embed([text]))
        return embeddings[0].tolist()

    def upsert_memory(
        self,
        memory_id: int,
        conversation_id: str,
        topic: str,
        fact: str,
        status: str = "active",
    ):
        """Lưu hoặc cập nhật vector memory vào Qdrant"""
        try:
            vector = self._get_embedding(fact)
            point = models.PointStruct(
                id=memory_id,
                vector=vector,
                payload={
                    "memory_id": memory_id,
                    "conversation_id": conversation_id,
                    "topic": topic,
                    "fact": fact,
                    "status": status,
                }
            )
            self.client.upsert(collection_name=self.collection_name, points=[point])
            print(f"[Qdrant] Upsert memory id={memory_id} (topic={topic}) thành công.")
        except Exception as e:
            print(f"[Qdrant Error] upsert_memory: {e}")

    def update_memory_status(self, memory_id: int, status: str = "superseded"):
        """Cập nhật trạng thái payload của một point"""
        try:
            self.client.set_payload(
                collection_name=self.collection_name,
                payload={"status": status},
                points=[memory_id]
            )
            print(f"[Qdrant] Cập nhật memory id={memory_id} -> status='{status}'")
        except Exception as e:
            print(f"[Qdrant Error] update_memory_status: {e}")

    def search_relevant_memories(
        self,
        conversation_id: str,
        query_text: str,
        limit: int = 4,
        score_threshold: float = 0.4,
    ) -> List[Dict[str, Any]]:
        """
        Tìm kiếm ngữ nghĩa các memories liên quan đến query_text.
        Bộ lọc: conversation_id tương ứng VÀ status == 'active'.
        """
        try:
            query_vector = self._get_embedding(query_text)
            query_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="conversation_id",
                        match=models.MatchValue(value=conversation_id)
                    ),
                    models.FieldCondition(
                        key="status",
                        match=models.MatchValue(value="active")
                    )
                ]
            )
            if hasattr(self.client, "query_points"):
                res = self.client.query_points(
                    collection_name=self.collection_name,
                    query=query_vector,
                    query_filter=query_filter,
                    limit=limit,
                    score_threshold=score_threshold
                )
                hits = res.points
            else:
                hits = self.client.search(
                    collection_name=self.collection_name,
                    query_vector=query_vector,
                    query_filter=query_filter,
                    limit=limit,
                    score_threshold=score_threshold
                )
            results = []
            for hit in hits:
                results.append({
                    "memory_id": hit.payload.get("memory_id"),
                    "topic": hit.payload.get("topic"),
                    "fact": hit.payload.get("fact"),
                    "score": hit.score
                })
            return results
        except Exception as e:
            print(f"[Qdrant Error] search_relevant_memories: {e}")
            return []

    def delete_memories_by_conversation(self, conversation_id: str) -> bool:
        """Xóa toàn bộ vector ký ức của một conversation_id trong Qdrant"""
        try:
            query_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="conversation_id",
                        match=models.MatchValue(value=conversation_id)
                    )
                ]
            )
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(filter=query_filter)
            )
            print(f"[Qdrant] Đã xóa toàn bộ vectors của cuộc hội thoại: `{conversation_id}`")
            return True
        except Exception as e:
            print(f"[Qdrant Error] delete_memories_by_conversation: {e}")
            return False

    def clear_all_memories(self) -> bool:
        """Xóa toàn bộ vector ký ức trong collection"""
        try:
            # Re-create collection
            self.client.delete_collection(collection_name=self.collection_name)
            self._ensure_collection()
            print(f"[Qdrant] Đã làm trống toàn bộ Collection `{self.collection_name}`.")
            return True
        except Exception as e:
            print(f"[Qdrant Error] clear_all_memories: {e}")
            return False

