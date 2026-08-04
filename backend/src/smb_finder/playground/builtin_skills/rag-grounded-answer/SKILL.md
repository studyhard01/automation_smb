---
name: rag-grounded-answer
description: Use for questions that should be answered from the local PostgreSQL vector database with explicit document evidence.
---

# RAG Grounded Answer

Use `search_rag_chunks` before answering a knowledge question that depends on local documents.

- Base the answer only on returned chunks.
- Include the file name and section or location for each material claim.
- If no useful chunk is returned, say that the local database does not contain enough evidence.
- Keep the answer concise and never invent a document, path, section, or result.
