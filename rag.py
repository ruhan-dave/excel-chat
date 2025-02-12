from openai import OpenAI
import openai
import os
import supabase
from time import sleep
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
print(os.environ.get("OPENAI_API_KEY"))

openai.api_key = os.environ.get("OPENAI_API_KEY")

client = OpenAI()

# Initialize supabase client
url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")

supabase_client: Client = create_client(url, key)

def insert_embedding(doc_name: str, embedding: list[float]):
    supabase_client.table("docs").upsert({"doc_name": doc_name, "embedding": embedding}).execute()
    
def get_embedding(doc_name: str) -> list[float]:
    response = supabase_client.table("docs").select("embedding").eq("doc_name", doc_name).execute()
    return response.data[0]["embedding"]

def create_embedding_openai(text: str):
    response = client.embeddings.create(input=text, model="text-embedding-3-small", encoding_format="float")
    return response.data[0].embedding

def create_embedding_from_files(files: list[str]):
    for file in files:
        with open(file, "r") as f:
            text = f.read()
            embedding = create_embedding_openai(text)
            insert_embedding(file, embedding)

def query_embedding(query: str):
    embedding = create_embedding_openai(query)
    response = supabase_client.rpc("query_documents", {"query_embedding": embedding}).execute()
    return response


def run_rag():
    # create_embedding_from_files(["docs/doc1.txt", "docs/doc2.txt"])
    response = query_embedding("dog")
    print(response)


if __name__ == "__main__":
    run_rag()