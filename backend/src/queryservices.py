from langchain_text_splitters.character import RecursiveCharacterTextSplitter
from excelservices import ExcelService
import numpy as np
import chromadb
from chroma_client import get_db_client
from dotenv import load_dotenv
import os
from openai import OpenAI


class QueryService:

    PREAMBLE = """
    ## Task & Context
    You give answers to user's financial questions with precision, based on document chunks you receive.
    You will be equipped with the text or excel tables and text to formulate your answer. 
    You should focus on serving the user's needs as best you can, which can be wide-ranging.

    ## Style Guide
    Unless the user asks for a different style of answer, you should answer in full sentences, using proper grammar and spelling.
    """
    TEMPERATURE = 0.3


    @classmethod
    def getTopKDocuments(self, query, k):
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=500,
            chunk_overlap = 50,
            length_function = len,
            is_separator_regex = False
        )
        documents = text_splitter.create_documents([query])
        subqueries = [doc.page_content for doc in documents]
        subquery_embeddings = ExcelService.batch_embed(subqueries)
        # chromadb.api.client.SharedSystemClient.clear_system_cache()
        chroma_client = get_db_client()
        collection = chroma_client.get_collection(name = "finance_docs")
        all_documents = collection.get(include = ["embeddings", "documents"])
        embeddings = all_documents['embeddings']

        similarities = []
        for chunk_embedding in embeddings:
            subquery_scores = [self.cosineSimilarity(subquery_embedding, chunk_embedding) for subquery_embedding in subquery_embeddings]
            similarities.append(np.mean(subquery_scores))

        similarities = np.array(similarities)
        top_indices = similarities[::-1].argsort()[:k]
        documents = all_documents['documents']
        relevant_documents = [documents[i] for i in top_indices]
        return relevant_documents        

    
    @classmethod
    def cosineSimilarity(self, a, b):
        return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

    @classmethod
    def promptLLMWithContext(self, query, relevant_documents):
        # Build context from relevant documents and inject into the prompt
        context = "\n\n".join([
            f"Document chunk {i}:\n{doc}" for i, doc in enumerate(relevant_documents)
        ])

        load_dotenv()
        OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
        OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)
        response = client.chat.completions.create(
            model="deepseek/deepseek-v4-flash",
            messages=[
                {"role": "system", "content": self.PREAMBLE + "\n\nUse the following documents to answer the user's question:\n" + context},
                {"role": "user", "content": query}
            ],
            temperature=self.TEMPERATURE
        )
        answer = response.choices[0].message.content
        return answer

if __name__ == "__main__":
    query = "What was our Expense in 2017 and 2018??"
    relevant_documents = QueryService.getTopKDocuments(query, 18)
    answer = QueryService.promptLLMWithContext(query, relevant_documents)
    print(answer)
