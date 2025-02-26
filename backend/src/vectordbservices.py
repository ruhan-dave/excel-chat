import chromadb
import uuid
from chroma_client import get_db_client

class VectorDBService:

    @classmethod
    def upload_embeddings(self, vector_embeddings, chunks):
        """
        Upload chunks and embeddings to vector db
        """
        # chromadb.api.client.SharedSystemClient.clear_system_cache()
        chroma_client = get_db_client()
        collection = chroma_client.get_or_create_collection(name = "finance_docs")
        collection.add(
            ids = [str(uuid.uuid4()) for _ in chunks],
            documents = chunks,
            embeddings = vector_embeddings
        )
        print(f"Added {len(chunks)} documents to chroma db")      