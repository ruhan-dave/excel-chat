import hashlib
import json
import os
import uuid
from difflib import SequenceMatcher
from typing import Any

import pandas as pd
import numpy as np
from dotenv import load_dotenv
from openai import OpenAI

from sheet_metadata import (
    SheetMeta, save_sheet, update_sheet_auto_description, update_sheet_schema_group,
)
from cache_service import cache_get, cache_set


class ExcelService:

    # # ChromaDB / embedding pipeline disabled — no longer used.
    # # The Pydantic AI pipeline (pipeline.py) handles retrieval directly via DataFrames.
    # @classmethod
    # def processExcel(self, excelFile):
    #     text_splitter = RecursiveCharacterTextSplitter(
    #         chunk_size=500, chunk_overlap=50,
    #         length_function=len, is_separator_regex=False,
    #     )
    #     documents = text_splitter.create_documents([excelFile])
    #     chunks = [doc.page_content for doc in documents]
    #     embeddings = self.batch_embed(chunks)
    #     return embeddings, chunks
    #
    # @classmethod
    # def batch_embed(self, texts):
    #     if not texts:
    #         return []
    #     vectorizer = TfidfVectorizer(max_features=384)
    #     tfidf_matrix = vectorizer.fit_transform(texts)
    #     return tfidf_matrix.toarray().tolist()

    # ========================================================================
    # Multi-Sheet Loading
    # ========================================================================

    @staticmethod
    def load_all_sheets(filepath: str) -> dict[str, pd.DataFrame]:
        """
        Load all sheets from an Excel file and clean each one.

        Returns:
            dict mapping sheet_name -> cleaned DataFrame.
            Sheets that fail cleaning are skipped with a warning.
        """
        raw_sheets = pd.read_excel(filepath, sheet_name=None)
        cleaned: dict[str, pd.DataFrame] = {}
        for name, df in raw_sheets.items():
            try:
                cleaned_df = ExcelService.clean_dataframe(df)
                cleaned[name] = cleaned_df
            except (ValueError, Exception) as e:
                print(f"⚠️ Skipping sheet '{name}': {e}")
        return cleaned

    @staticmethod
    def load_sheet_metadata_from_file(
        filepath: str, file_id: str, file_name: str, s3_key: str
    ) -> list[SheetMeta]:
        """
        Load all sheets from an Excel file, clean them, and create SheetMeta objects.
        Does NOT save to DB — caller is responsible for that.
        """
        cleaned = ExcelService.load_all_sheets(filepath)
        metas: list[SheetMeta] = []
        for sheet_name, df in cleaned.items():
            sheet_id = str(uuid.uuid4())
            fields = [str(f) for f in df.index.tolist()]
            years = [str(y) for y in df.columns.tolist()]
            metas.append(
                SheetMeta(
                    sheet_id=sheet_id,
                    file_id=file_id,
                    file_name=file_name,
                    sheet_name=sheet_name,
                    s3_key=s3_key,
                    fields=fields,
                    years=years,
                    row_count=len(df),
                )
            )
        return metas

    # ========================================================================
    # Schema Group Detection
    # ========================================================================

    @staticmethod
    def detect_schema_groups(sheets: list[SheetMeta], threshold: float = 0.75) -> None:
        """
        Detect which sheets share a consistent schema by fuzzy-matching their
        field names (row index labels). Mutates sheets in-place by setting
        `schema_group`.

        Two sheets are in the same group if their field name sets have a
        SequenceMatcher similarity ratio >= threshold.
        """
        if not sheets:
            return

        groups: list[list[int]] = []
        for i, sheet in enumerate(sheets):
            placed = False
            for group in groups:
                representative = sheets[group[0]]
                sim = ExcelService._field_similarity(sheet.fields, representative.fields)
                if sim >= threshold:
                    group.append(i)
                    placed = True
                    break
            if not placed:
                groups.append([i])

        for gi, group in enumerate(groups):
            group_label = f"group_{gi}" if len(group) > 1 else "unique"
            for idx in group:
                sheets[idx].schema_group = group_label

    @staticmethod
    def _field_similarity(fields_a: list[str], fields_b: list[str]) -> float:
        """Compute similarity between two field sets using difflib."""
        set_a = set(f.lower().strip() for f in fields_a)
        set_b = set(f.lower().strip() for f in fields_b)
        if not set_a or not set_b:
            return 0.0
        intersection = set_a & set_b
        union = set_a | set_b
        jaccard = len(intersection) / len(union) if union else 0.0
        return jaccard

    # ========================================================================
    # Auto-Description Generation via LLM
    # ========================================================================

    @staticmethod
    def auto_describe_sheet(sheet: SheetMeta) -> str:
        """
        Use an LLM to generate a concise plain-English description of what a sheet
        contains, based on its field names, years, and row count.
        Designed to be cheap: short prompt, max 80 output tokens.
        Checks LLM cache first to avoid duplicate calls for identical sheet profiles.
        """
        if sheet.auto_description:
            return sheet.auto_description

        load_dotenv()
        OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
        OPENROUTER_BASE_URL = os.environ.get(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
        )
        client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)

        fields_str = ", ".join(sheet.fields[:20])
        years_str = ", ".join(sheet.years)
        model = "deepseek/deepseek-v4-flash"
        prompt = (
            f"Sheet: {sheet.sheet_name} | Fields: {fields_str} | "
            f"Years: {years_str} | Rows: {sheet.row_count}\n"
            f"Describe this financial sheet in ONE sentence (max 30 words). "
            f"No preamble, just the description."
        )

        # Check cache first
        cached = cache_get(model, prompt)
        if cached:
            return cached

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=80,
            )
            result = (response.choices[0].message.content or "").strip()
            if not result:
                return ""
            # Cache the response
            cache_set(model, prompt, result)
            return result
        except Exception as e:
            print(f"⚠️ Auto-description failed for '{sheet.sheet_name}': {e}")
            return ""

    @staticmethod
    def auto_describe_all_sheets(sheets: list[SheetMeta]) -> None:
        """Generate and store auto-descriptions for sheets that don't have one yet."""
        for sheet in sheets:
            if sheet.auto_description:
                continue
            desc = ExcelService.auto_describe_sheet(sheet)
            sheet.auto_description = desc
            update_sheet_auto_description(sheet.sheet_id, desc)

    # ========================================================================
    # DataFrame Cleaning (unchanged)
    # ========================================================================

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
            if non_null_values.astype(str).str.match(r'^\d{4}(\.0)?$').all():
                year_row_index = i
                break

        if year_row_index is None:
            raise ValueError("No year row found in the dataset.")

        new_columns = df.iloc[year_row_index + 1].values.astype(str).tolist()
        new_columns = [
            col if not col.replace('.0', '').isdigit() else str(int(float(col)))
            for col in new_columns
        ]
        new_columns[0] = "category"

        df = df.iloc[year_row_index + 2:,]
        df.columns = new_columns
        df = df.fillna(-np.inf).reset_index(drop=True, inplace=False)
        df.set_index('category', inplace=True)

        return df


if __name__ == "__main__":
    sheets = ExcelService.load_all_sheets(
        "../../example_sheets/Detailed_Expense_Breakdown.xlsx"
    )
    for name, df in sheets.items():
        print(f"Sheet: {name}, shape: {df.shape}")
        print(f"  Fields: {df.index.tolist()[:5]}")
        print(f"  Years: {df.columns.tolist()}")