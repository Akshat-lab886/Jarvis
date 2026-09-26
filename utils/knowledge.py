import os
import logging
import threading
import time
from pypdf import PdfReader
import chromadb
from chromadb.utils import embedding_functions

logger = logging.getLogger("Jarvis.Knowledge")

# Native ML libs (torch/hnswlib) can hang on broken installs; a hang
# raises nothing, so init runs under a watchdog thread instead.
_INIT_TIMEOUT = 30

class Librarian:
    """
    The Librarian is responsible for reading, embedding, and storing knowledge in the Vault (ChromaDB).
    """
    def __init__(self):
        logger.info("Initializing Librarian (Vector Vault)...")
        self.client = None
        self.embedding_fn = None
        self.collection = None
        # ready flips True only after a successful init; lets callers
        # (auto-RAG) skip instantly instead of blocking on a broken stack
        self.ready = False

        # Hard kill-switch: on machines with broken native ML libs the
        # chromadb call HANGS WITHOUT RELEASING THE GIL, freezing every
        # thread (watchdogs can't help).  Respect the switch BEFORE any
        # heavy import.
        if os.getenv('JARVIS_DISABLE_VECTOR') == '1':
            logger.info("Librarian disabled via JARVIS_DISABLE_VECTOR "
                        "(keyword-only mode).")
            return

        outcome = {}

        def _build():
            try:
                # Cheap sentinel: torch must at least import cleanly
                # before we invest in chroma + embeddings.
                import torch  # noqa: F401
                # Persistent Client
                client = chromadb.PersistentClient(path="./knowledge_vault")

                # Lightweight, open-source embedding model (all-MiniLM-L6-v2)
                emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name="all-MiniLM-L6-v2"
                )

                collection = client.get_or_create_collection(
                    name="jarvis_knowledge",
                    embedding_function=emb_fn,
                )
                outcome['client'] = client
                outcome['embedding_fn'] = emb_fn
                outcome['collection'] = collection
                outcome['count'] = collection.count()
            except Exception as e:
                outcome['error'] = str(e)

        builder = threading.Thread(target=_build, daemon=True,
                                   name="librarian-init")
        builder.start()
        builder.join(timeout=_INIT_TIMEOUT)

        if builder.is_alive():
            logger.warning(
                "Librarian init TIMED OUT (> %ss) — vault disabled this "
                "session. (Check torch/chromadb install: "
                "JARVIS_DISABLE_VECTOR=1 silences this.)", _INIT_TIMEOUT)
        elif 'error' in outcome:
            logger.warning("Librarian Initialization Error: %s", outcome['error'])
        else:
            self.client = outcome['client']
            self.embedding_fn = outcome['embedding_fn']
            self.collection = outcome['collection']
            self.ready = True
            logger.info("Librarian initialized. Knowledge Count: %s",
                         outcome['count'])

    def read_pdf(self, file_path):
        """Extract text from PDF."""
        try:
            reader = PdfReader(file_path)
            text = ""
            for page in reader.pages:
                text += page.extract_text() + "\n"
            return text.strip()
        except Exception as e:
            return f"Error reading PDF: {str(e)}"

    def read_text(self, file_path):
        """Extract text from TXT/MD."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception as e:
            return f"Error reading text file: {str(e)}"

    def _chunk_text(self, text, chunk_size=500):
        """Split text into chunks for better embedding."""
        words = text.split()
        chunks = []
        current_chunk = []
        current_length = 0
        
        for word in words:
            current_chunk.append(word)
            current_length += len(word) + 1
            if current_length >= chunk_size:
                chunks.append(" ".join(current_chunk))
                current_chunk = []
                current_length = 0
        
        if current_chunk:
            chunks.append(" ".join(current_chunk))
        return chunks

    def ingest_file(self, file_path, store=True):
        """Read file, extracting text. If store=True, add to Vector DB."""
        if not os.path.exists(file_path):
            return f"File not found: {file_path}"
            
        filename = os.path.basename(file_path)
        _, ext = os.path.splitext(file_path)
        ext = ext.lower()
        
        # 1. Extract Text
        content = ""
        if ext == ".pdf":
            content = self.read_pdf(file_path)
        elif ext in [".txt", ".md"]:
            content = self.read_text(file_path)
        else:
            return f"Unsupported file type: {ext}"
            
        if not store or not self.collection:
            return content 

        # 2. Chunk & Store
        self.memorize_text(content, filename)
            
        return content

    def memorize_text(self, text, source_name="Unknown_Source"):
        """Manually store text in the Vault."""
        if not self.collection:
            return "Error: Vault not initialized."
            
        chunks = self._chunk_text(text)
        ids = [f"{source_name}_{int(time.time())}_{i}" for i in range(len(chunks))]
        metadatas = [{"source": source_name} for _ in range(len(chunks))]
        
        if chunks:
            logger.info("Storing %d chunks from %s...",
                        len(chunks), source_name)
            self.collection.add(
                documents=chunks,
                ids=ids,
                metadatas=metadatas
            )
        return f"Stored {len(chunks)} chunks."

    def query_vault(self, query, n_results=3):
        """Search the Vector DB for relevant context."""
        if not self.collection:
            return "Librarian Error: Vault not accessible."
            
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=n_results
            )
            # Flatten results
            documents = results['documents'][0]
            sources = results['metadatas'][0]
            
            if not documents:
                return None
                
            context = ""
            for i, doc in enumerate(documents):
                source = sources[i]['source']
                context += f"[Source: {source}] {doc}\n\n"
            
            return context.strip()
            
        except Exception as e:
            logger.warning("Vault Query Error: %s", e)
            return None

    def study_folder(self, input_folder="knowledge_input", processed_folder="knowledge_processed"):
        """Batch ingest files from input folder and move to processed."""
        import glob
        import shutil
        
        if not os.path.exists(input_folder):
            return f"Folder {input_folder} does not exist."
            
        files = glob.glob(os.path.join(input_folder, "*"))
        count = 0
        
        for file_path in files:
            if os.path.isfile(file_path):
                filename = os.path.basename(file_path)
                logger.info("Studying %s...", filename)
                
                # Ingest
                self.ingest_file(file_path)
                
                # Move to processed
                try:
                    shutil.move(file_path, os.path.join(processed_folder, filename))
                    count += 1
                except Exception as e:
                    logger.warning("Could not move %s to processed: %s",
                                   filename, e)
                    
        return f"I have finished studying {count} new documents."


if __name__ == "__main__":
    # Test
    lib = Librarian()
    # Create dummy
    with open("test_secret.txt", "w") as f:
        f.write("The secret code to the treehouse is Banana.")
    
    print("\n--- Ingesting ---")
    lib.ingest_file("test_secret.txt")
    
    print("\n--- Querying 'What is the secret code?' ---")
    res = lib.query_vault("What is the secret code?")
    print(f"Result:\n{res}")
    
    os.remove("test_secret.txt")
