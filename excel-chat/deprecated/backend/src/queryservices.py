from langchain_text_splitters.character import RecursiveCharacterTextSplitter
from excelservices import ExcelService
import numpy as np
# import chromadb
# from chroma_client import get_db_client
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
        # ChromaDB disabled — vector search no longer used.
        # The Pydantic AI pipeline (pipeline.py) handles retrieval directly via DataFrames.
        raise NotImplementedError(
            "getTopKDocuments is disabled (ChromaDB removed). "
            "Use the Pydantic AI pipeline in pipeline.py instead."
        )        

    
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
            model="openai/gpt-oss-120b:nitro",
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
