import os
from pypdf import PdfReader
import chromadb
import time
from chromadb.utils import embedding_functions

class Librarian:
    """
    The Librarian is responsible for reading, embedding, and storing knowledge in the Vault (ChromaDB).
    """
    def __init__(self):
        print("Initializing Librarian (Vector Vault)...")
        try:
            # Persistent Client
            self.client = chromadb.PersistentClient(path="./knowledge_vault")
            
            # Use a lightweight, open-source embedding model
            # defaulting to all-MiniLM-L6-v2 which is standard and fast
            self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name="all-MiniLM-L6-v2" 
            )
            
            self.collection = self.client.get_or_create_collection(
                name="jarvis_knowledge",
                embedding_function=self.embedding_fn
            )
            print(f"Librarian initialized. Knowledge Count: {self.collection.count()}")
        except Exception as e:
            print(f"Librarian Initialization Error: {e}")
            self.collection = None

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
            print(f"Librarian: Storing {len(chunks)} chunks from {source_name}...")
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
            print(f"Vault Query Error: {e}")
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
                print(f"Studying {filename}...")
                
                # Ingest
                self.ingest_file(file_path)
                
                # Move to processed
                try:
                    shutil.move(file_path, os.path.join(processed_folder, filename))
                    count += 1
                except Exception as e:
                    print(f"Error moving {filename}: {e}")
                    
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
