from sentence_transformers import SentenceTransformer

# Load the model
model = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')

# List of sentences
sentences = ["This is an example sentence", "Each sentence is converted to an embedding"]

# Convert sentences to embeddings
embeddings = model.encode(sentences)

# Output the embeddings
print(embeddings)