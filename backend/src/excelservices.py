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
    
    @staticmethod
    def clean_dataframe(df):
        """
        Cleans a dataframe by:
        1. Identifying the row that contains year values (both integer and float).
        2. Renaming columns using the detected years.
        3. Removing metadata rows above the detected year row.
        4. Ensuring all columns have valid names.
        5. Filling NaN values with -inf.
        
        Parameters:
        df (pd.DataFrame): Raw dataframe with metadata and financial data.
        
        Returns:
        pd.DataFrame: Processed dataframe with correct column names and missing values replaced.
        """
        
        # Identify the row index where the first numeric year appears
        year_row_index = None
        for i in range(len(df)):
            non_null_values = df.iloc[i].dropna()
            if non_null_values.astype(str).str.match(r'^\d{4}(\.0)?$').all():  # Matches years like 2024 and 2024.0
                year_row_index = i
                break

        if year_row_index is None:
            raise ValueError("No year row found in the dataset.")

        # Extract column names from the identified row (convert floats to int for clean column names)
        new_columns = df.iloc[year_row_index+1].values.astype(str).tolist()
        new_columns = [col if not col.replace('.0', '').isdigit() else str(int(float(col))) for col in new_columns]

        # Ensure the first column has a proper name (e.g., "Category")
        new_columns[0] = "category"

        # Remove metadata rows above the detected year row and reset index
        df = df.iloc[year_row_index+2:,]

        # Assign new column names
        df.columns = new_columns # 

        # Fill NaN values with -inf
        df = df.fillna(-np.inf).reset_index(drop=True, inplace=False)
        df.set_index('category', inplace=True)
        
        return df


if __name__ == "__main__":
    # For now, just limited to 1 sheet
    excelPandas = pd.read_excel("../../example_sheets/Detailed_Expense_Breakdown.xlsx", sheet_name='Sheet1')
    ExcelService.processExcel(excelPandas)