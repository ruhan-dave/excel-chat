import pandas as pd
from langchain_text_splitters.character import RecursiveCharacterTextSplitter
from dotenv import load_dotenv
import os
import cohere
import numpy as np


class ExcelService:

    @classmethod
    def processExcel(self, excelFile):
        """
        Processes the excel pandas,
        chunks it, embeds the chunks and returns embeddings along with chunks
        """
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=500,
            chunk_overlap = 50,
            length_function = len,
            is_separator_regex = False
        )
        documents = text_splitter.create_documents([excelFile])
        chunks = [doc.page_content for doc in documents]
        embeddings = self.batch_embed(chunks)
        return embeddings, chunks

    @classmethod
    def batch_embed(self, texts):
        load_dotenv()
        COHERE_API_KEY = os.environ.get("COHERE_KEY")
        ch = cohere.ClientV2(api_key = COHERE_API_KEY)
        all_embeddings = []
        batch_size = 96
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            response = ch.embed(
                texts = batch,
                model = "embed-english-v3.0",
                input_type = "search_document",
                embedding_types = ["float"]
            )
            all_embeddings.extend(response.embeddings.float)
        return all_embeddings


if __name__ == "__main__":
    # For now, just limited to 1 sheet
    excelPandas = pd.read_excel("../../example_sheets/Detailed_Expense_Breakdown.xlsx", sheet_name='Sheet1')
    ExcelService.processExcel(excelPandas)