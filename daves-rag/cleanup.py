import shutil
import os

def clean_chroma_db():
    DB_PATH = "./chroma_db"
    if os.path.exists(DB_PATH):
        # Close any existing connections
        try:
            db = Chroma(persist_directory=DB_PATH)
            db.persist()
            del db
        except:
            pass
        # Remove the directory
        shutil.rmtree(DB_PATH)
        # Ensure the base directory exists
        os.makedirs(DB_PATH, exist_ok=True)

if __name__ == "__main__":
    clean_chroma_db()