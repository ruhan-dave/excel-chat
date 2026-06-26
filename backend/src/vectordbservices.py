# import chromadb
# import uuid
# from chroma_client import get_db_client
#
# class VectorDBService:
#
#     @classmethod
#     def upload_embeddings(self, vector_embeddings, chunks):
#         """
#         Upload chunks and embeddings to vector db
#         """
#         chroma_client = get_db_client()
#         collection = chroma_client.get_or_create_collection(name="finance_docs")
#         collection.add(
#             ids=[str(uuid.uuid4()) for _ in chunks],
#             documents=chunks,
#             embeddings=vector_embeddings,
#         )
#         print(f"Added {len(chunks)} documents to chroma db")
#
#     @classmethod
#     def get_collection_count(self) -> int:
#         """Return the number of documents in the finance_docs collection."""
#         try:
#             chroma_client = get_db_client()
#             collection = chroma_client.get_collection(name="finance_docs")
#             return collection.count()
#         except Exception:
#             return 0
#
#     @classmethod
#     def clear_collection(self) -> int:
#         """Delete and recreate the finance_docs collection. Returns count of deleted docs."""
#         chroma_client = get_db_client()
#         try:
#             collection = chroma_client.get_collection(name="finance_docs")
#             count = collection.count()
#             chroma_client.delete_collection(name="finance_docs")
#             print(f"Cleared {count} documents from chroma db")
#             return count
#         except Exception:
#             return 0      