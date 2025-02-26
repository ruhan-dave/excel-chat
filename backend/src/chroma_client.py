from chromadb import PersistentClient

# At the module level
_db_client = None

def get_db_client():
    DB_PATH = "./db"
    global _db_client
    if _db_client is None:
        _db_client = PersistentClient(DB_PATH)
    return _db_client


if __name__ == "__main__":
    get_db_client()