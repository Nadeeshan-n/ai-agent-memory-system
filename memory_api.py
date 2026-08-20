from fastapi import FastAPI
from pydantic import BaseModel
import sqlite3
from pathlib import Path
from fastapi import Query
import os
import json
from huggingface_hub import InferenceClient
import numpy as np



app = FastAPI(title="AI Agent Memory API")

DB_PATH = Path(__file__).parent / "agent_memory.db"
HF_TOKEN = os.getenv("Enter your Hugging Face API token here")  # Replace with your actual token or use environment variable

EMBEDDING_MODEL = "intfloat/multilingual-e5-large"

hf_client = InferenceClient(
    provider="hf-inference",
    api_key=HF_TOKEN
)

def generate_embedding(text, prefix=""):
    try:
        result = hf_client.feature_extraction(
            f"{prefix} {text}",
            model=EMBEDDING_MODEL
        )

        # Convert NumPy arrays to normal Python lists
        if hasattr(result, "tolist"):
            result = result.tolist()

        return result

    except Exception as e:
        print(f"Embedding generation failed: {e}")
        return None



def cosine_similarity(a, b):
    a = np.array(a)
    b = np.array(b)

    denominator = np.linalg.norm(a) * np.linalg.norm(b)

    if denominator == 0:
        return 0.0

    return float(np.dot(a, b) / denominator)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


class Conversation(BaseModel):
    user_id: str
    message_id: str | None = None
    role: str
    message: str
    whatsapp_timestamp: str | None = None


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "AI Agent Memory API"
    }


@app.post("/conversation")
def save_conversation(conversation: Conversation):

    conn = get_db()

    # Check whether this WhatsApp message was already stored
    if conversation.message_id:

        existing = conn.execute(
            """
            SELECT id
            FROM conversations
            WHERE message_id = ?
            """,
            (conversation.message_id,)
        ).fetchone()

        if existing:
            conn.close()

            return {
                "success": True,
                "duplicate": True,
                "id": existing["id"]
            }

    cursor = conn.execute(
        """
        INSERT INTO conversations
        (user_id, message_id, role, message, whatsapp_timestamp)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            conversation.user_id,
            conversation.message_id,
            conversation.role,
            conversation.message,
            conversation.whatsapp_timestamp
        )
    )

    conn.commit()

    conversation_id = cursor.lastrowid

    conn.close()

    return {
        "success": True,
        "duplicate": False,
        "id": conversation_id
    }
@app.get("/conversation/history/{user_id}")
def get_conversation_history(user_id: str, limit: int = 10):

    conn = get_db()

    rows = conn.execute(
        """
        SELECT
            id,
            user_id,
            role,
            message,
            whatsapp_timestamp,
            created_at
        FROM conversations
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, limit)
    ).fetchall()

    conn.close()

    # Reverse so the oldest message is first
    conversations = [dict(row) for row in reversed(rows)]

    return {
        "user_id": user_id,
        "count": len(conversations),
        "conversations": conversations
    }

class MemoryCreate(BaseModel):
    user_id: str
    category: str
    key: str
    value: str
    source: str = "conversation"
    importance: float = 0.5
    confidence: float = 1.0
    memory_type: str = "long_term"
    expires_at: str | None = None


@app.post("/memory")
@app.post("/memory")
def save_memory(memory: MemoryCreate):

    conn = get_db()

    # --------------------------------------------------
    # 1. Check exact key match FIRST
    # --------------------------------------------------

    existing = conn.execute(
        """
        SELECT
            id,
            value,
            importance,
            confidence,
            memory_type,
            expires_at
        FROM memories
        WHERE user_id = ?
          AND category = ?
          AND key = ?
        """,
        (
            memory.user_id,
            memory.category,
            memory.key
        )
    ).fetchone()

    # --------------------------------------------------
    # 2. Same key + same value = DUPLICATE
    # --------------------------------------------------

    if existing and existing["value"] == memory.value:

        conn.close()

        return {
            "success": True,
            "action": "DUPLICATE",
            "duplicate": True,
            "consolidated": False,
            "id": existing["id"]
        }

    # --------------------------------------------------
    # 3. Generate embedding only when needed
    # --------------------------------------------------

    memory_text = (
        f"{memory.category} "
        f"{memory.key.replace('_', ' ')} "
        f"{memory.value}"
    )

    embedding = generate_embedding(memory_text)

    if embedding is None:

        conn.close()

        return {
            "success": False,
            "error": "embedding_unavailable",
            "message": "Could not generate memory embedding"
        }

    embedding_json = json.dumps(embedding)

    # --------------------------------------------------
    # 4. Existing key + different value = UPDATE
    # --------------------------------------------------

    if existing:

        conn.execute(
            """
            UPDATE memories
            SET value = ?,
                importance = ?,
                confidence = ?,
                source = ?,
                embedding = ?,
                memory_type = ?,
                expires_at = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                memory.value,
                memory.importance,
                memory.confidence,
                memory.source,
                embedding_json,
                memory.memory_type,
                memory.expires_at,
                existing["id"]
            )
        )

        conn.commit()
        conn.close()

        return {
            "success": True,
            "action": "UPDATE",
            "duplicate": False,
            "consolidated": False,
            "id": existing["id"]
        }

    # --------------------------------------------------
    # 5. Check semantic duplicates
    # --------------------------------------------------

    rows = conn.execute(
        """
        SELECT
            id,
            category,
            key,
            value,
            importance,
            confidence,
            updated_at,
            embedding
        FROM memories
        WHERE user_id = ?
          AND category = ?
          AND embedding IS NOT NULL
        """,
        (
            memory.user_id,
            memory.category
        )
    ).fetchall()

    duplicate = None
    highest_similarity = 0.0

    for row in rows:

        if row["key"] != memory.key:
            continue

        try:
            existing_embedding = json.loads(row["embedding"])

            similarity = cosine_similarity(
                embedding,
                existing_embedding
            )

            if similarity > highest_similarity:
                highest_similarity = similarity
                duplicate = row

        except Exception:
            continue

    # --------------------------------------------------
    # 6. Consolidate semantic duplicate
    # --------------------------------------------------

    if duplicate and highest_similarity >= 0.90:

        keep_id = duplicate["id"]

        importance = max(
            memory.importance,
            duplicate["importance"]
        )

        confidence = max(
            memory.confidence,
            duplicate["confidence"]
        )

        conn.execute(
            """
            UPDATE memories
            SET value = ?,
                importance = ?,
                confidence = ?,
                source = ?,
                embedding = ?,
                memory_type = ?,
                expires_at = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                memory.value,
                importance,
                confidence,
                memory.source,
                embedding_json,
                memory.memory_type,
                memory.expires_at,
                keep_id
            )
        )

        conn.commit()
        conn.close()

        return {
            "success": True,
            "action": "CONSOLIDATE",
            "duplicate": True,
            "consolidated": True,
            "similarity": highest_similarity,
            "id": keep_id
        }

    # --------------------------------------------------
    # 7. Create completely new memory
    # --------------------------------------------------

    cursor = conn.execute(
        """
        INSERT INTO memories
        (
            user_id,
            category,
            key,
            value,
            source,
            importance,
            confidence,
            embedding,
            memory_type,
            expires_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory.user_id,
            memory.category,
            memory.key,
            memory.value,
            memory.source,
            memory.importance,
            memory.confidence,
            embedding_json,
            memory.memory_type,
            memory.expires_at
        )
    )

    conn.commit()

    memory_id = cursor.lastrowid

    conn.close()

    return {
        "success": True,
        "action": "CREATE",
        "duplicate": False,
        "consolidated": False,
        "id": memory_id
    }
@app.get("/memory/{user_id}")
def get_memories(user_id: str):

    conn = get_db()

    rows = conn.execute(
        """
        SELECT
            id,
            user_id,
            category,
            key,
            value,
            source,
            importance,
            confidence,
            memory_type,
            expires_at,
            created_at,
            updated_at
        FROM memories
        WHERE user_id = ?
            AND (
            expires_at IS NULL
            OR expires_at > CURRENT_TIMESTAMP
                )   
        ORDER BY (importance * confidence) DESC, updated_at DESC
        """,
    
        (user_id,)
    ).fetchall()

    conn.close()

    memories = [dict(row) for row in rows]

    return {
        "user_id": user_id,
        "count": len(memories),
        "memories": memories
    }


@app.get("/memory/{user_id}/relevant")
def get_relevant_memories(
    user_id: str,
    query: str = Query(...)
):

    # Generate embedding for the user's question
    query_embedding = generate_embedding(
        f"query: {query}",
        prefix=""
    )
    if query_embedding is None:
        return {
            "user_id": user_id,
            "query": query,
            "count": 0,
            "memories": [],
            "error": "embedding_unavailable"
    }

    conn = get_db()

    rows = conn.execute(
        """
        SELECT
            id,
            user_id,
            category,
            key,
            value,
            source,
            importance,
            confidence,
            embedding,
            memory_type,
            expires_at,
            created_at,
            updated_at
        FROM memories
        WHERE user_id = ?
          AND (
          expires_at IS NULL
          OR expires_at > CURRENT_TIMESTAMP
          )
        
        """,
        (user_id,)
    ).fetchall()

    conn.close()

    results = []

    for row in rows:

        try:
            if not row["embedding"]:
                continue

            memory_embedding = json.loads(row["embedding"])

    # Make sure the stored embedding is usable
            if not isinstance(memory_embedding, list):
                continue

            if len(memory_embedding) == 0:
                continue

            similarity = cosine_similarity(
                query_embedding,
                memory_embedding
            )

            memory = dict(row)

    # Don't return the huge vector to n8n
            memory.pop("embedding", None)

            memory["similarity"] = float(similarity)

    # Combine semantic relevance with memory quality
            memory["relevance_score"] = (
                float(similarity)
                * float(row["importance"])
                * float(row["confidence"])
            )

            results.append(memory)
        except Exception as e:

            print(
                f"Skipping invalid embedding for "
                f"memory id={row['id']}: {e}"
            )

            continue

    # Highest relevance first
    results.sort(
        key=lambda x: x["relevance_score"],
        reverse=True
    )

    # Semantic retrieval thresholds
    MIN_SIMILARITY = 0.82
    MIN_MARGIN = 0.04

    if not results:
        return {
            "user_id": user_id,
            "query": query,
            "count": 0,
            "memories": []
        }

# Find the strongest semantic match
    semantic_results = sorted(
        results,
        key=lambda x: x["similarity"],
        reverse=True
    )

    best = semantic_results[0]

    second_similarity = (
        semantic_results[1]["similarity"]
        if len(semantic_results) > 1
        else 0.0
    )

    margin = best["similarity"] - second_similarity

    print(
        f"\n--- RETRIEVAL ---"
        f"\nBest: {best['key']}"
        f"\nSimilarity: {best['similarity']:.4f}"
        f"\nSecond: {second_similarity:.4f}"
        f"\nMargin: {margin:.4f}"
    )

# Now rank memories by final quality/relevance
    results.sort(
        key=lambda x: x["relevance_score"],
        reverse=True
)
# Reject weak or ambiguous matches
 

# Return the best memory
# Return all memories that are both:
# 1. Similar enough to the query
# 2. Close enough to the best result

    MAX_RESULTS = 5
    MIN_SIMILARITY = 0.80
    MIN_RELATIVE_SCORE = 0.90

    query_lower = query.lower()

    interest_query = any(
        phrase in query_lower
        for phrase in [
            "my interests",
            "my interest",
            "what am i interested in",
            "what do i like",
            "things i like",
            "what are my hobbies",
            "my hobbies"
        ]
    )

    about_me_query = any(
        phrase in query_lower
        for phrase in [
            "about me",
            "what do you know about me",
            "what do you remember about me",
            "what do you know about me so far"
        ]
    )

    broad_query = interest_query or about_me_query

    selected = []

    for memory in results:

        similarity = memory["similarity"]
        relevance_score = memory["relevance_score"]

        if interest_query:

        # Interest questions should include
        # interests and preferences.
            if memory["category"] not in [
                "interest",
                "preference",
                "hobby",
                "skill"
            ]:
                continue

            if similarity >= 0.72 and relevance_score >= 0.30:
                selected.append(memory)

        elif about_me_query:

        # General "about me" questions can include
        # any reasonably relevant memory.
            if similarity >= 0.72 and relevance_score >= 0.25:
                selected.append(memory)

        else:

        # Specific questions remain strict.
            if similarity < MIN_SIMILARITY:
                continue

            if relevance_score < 0.30:
                continue

            if similarity < best["similarity"] * MIN_RELATIVE_SCORE:
                continue

            selected.append(memory)

        if len(selected) >= MAX_RESULTS:
            break
    return {
        "user_id": user_id,
        "query": query,
        "count": len(selected),
        "memories": selected
    }
    

@app.delete("/memory/{user_id}/{category}/{key}")
def delete_memory(user_id: str, category: str, key: str):

    conn = get_db()

    cursor = conn.execute(
        """
        DELETE FROM memories
        WHERE user_id = ?
          AND category = ?
          AND key = ?
        """,
        (user_id, category, key)
    )

    conn.commit()

    deleted = cursor.rowcount

    conn.close()

    return {
        "success": True,
        "deleted": deleted > 0,
        "count": deleted
    }
@app.post("/memory/{user_id}/generate-embeddings")
def generate_missing_embeddings(user_id: str):

    conn = get_db()

    rows = conn.execute(
        """
        SELECT
            id,
            category,
            key,
            value
        FROM memories
        WHERE user_id = ?
          AND embedding IS NULL
        """,
        (user_id,)
    ).fetchall()

    updated = 0

    for row in rows:

        memory_text = (
            f"{row['category']} "
            f"{row['key'].replace('_', ' ')} "
            f"{row['value']}"
        )

        embedding = generate_embedding(memory_text)

        embedding_json = json.dumps(embedding)

        conn.execute(
            """
            UPDATE memories
            SET embedding = ?
            WHERE id = ?
            """,
            (
                embedding_json,
                row["id"]
            )
        )

        updated += 1

    conn.commit()
    conn.close()

    return {
        "success": True,
        "updated": updated
    }

@app.post("/memory/check-duplicate")
def check_memory_duplicate(memory: MemoryCreate):

    memory_text = (
        f"{memory.category} "
        f"{memory.key.replace('_', ' ')} "
        f"{memory.value}"
    )

    new_embedding = generate_embedding(memory_text)

    conn = get_db()

    rows = conn.execute(
        """
        SELECT
            id,
            category,
            key,
            value,
            importance,
            confidence,
            embedding
        FROM memories
        WHERE user_id = ?
          AND embedding IS NOT NULL
        """,
        (memory.user_id,)
    ).fetchall()

    conn.close()

    matches = []

    for row in rows:

        existing_embedding = json.loads(row["embedding"])

        similarity = cosine_similarity(
            new_embedding,
            existing_embedding
        )

        if similarity >= 0.90:
            matches.append({
                "id": row["id"],
                "category": row["category"],
                "key": row["key"],
                "value": row["value"],
                "similarity": similarity
            })

    matches.sort(
        key=lambda x: x["similarity"],
        reverse=True
    )

    return {
        "duplicate": len(matches) > 0,
        "matches": matches
    }

@app.post("/memory/consolidate")
def consolidate_memory(
    user_id: str,
    keep_id: int,
    remove_id: int
):
    conn = get_db()

    keep = conn.execute(
        """
        SELECT *
        FROM memories
        WHERE id = ? AND user_id = ?
        """,
        (keep_id, user_id)
    ).fetchone()

    remove = conn.execute(
        """
        SELECT *
        FROM memories
        WHERE id = ? AND user_id = ?
        """,
        (remove_id, user_id)
    ).fetchone()

    if not keep or not remove:
        conn.close()

        return {
            "success": False,
            "error": "Memory not found"
        }

    # Keep the higher quality values
    importance = max(
        keep["importance"],
        remove["importance"]
    )

    confidence = max(
        keep["confidence"],
        remove["confidence"]
    )

    # Keep the newer value
    value = (
        remove["value"]
        if remove["updated_at"] > keep["updated_at"]
        else keep["value"]
    )

    conn.execute(
        """
        UPDATE memories
        SET value = ?,
            importance = ?,
            confidence = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            value,
            importance,
            confidence,
            keep_id
        )
    )

    conn.execute(
        """
        DELETE FROM memories
        WHERE id = ? AND user_id = ?
        """,
        (
            remove_id,
            user_id
        )
    )

    conn.commit()
    conn.close()

    return {
        "success": True,
        "kept_id": keep_id,
        "removed_id": remove_id,
        "consolidated": True
    }

@app.delete("/memory/cleanup/expired")
def cleanup_expired_memories():

    conn = get_db()

    cursor = conn.execute(
        """
        DELETE FROM memories
        WHERE expires_at IS NOT NULL
          AND expires_at <= CURRENT_TIMESTAMP
        """
    )

    conn.commit()

    deleted = cursor.rowcount

    conn.close()

    return {
        "success": True,
        "deleted": deleted
    }
def cleanup_expired_memories():
    conn = get_db()

    cursor = conn.execute(
        """
        DELETE FROM memories
        WHERE expires_at IS NOT NULL
          AND expires_at <= CURRENT_TIMESTAMP
        """
    )

    deleted_count = cursor.rowcount

    conn.commit()
    conn.close()

    return deleted_count

@app.delete("/memory/{user_id}/cleanup")
def cleanup_user_memories(user_id: str):

    conn = get_db()

    cursor = conn.execute(
        """
        DELETE FROM memories
        WHERE user_id = ?
          AND expires_at IS NOT NULL
          AND expires_at <= CURRENT_TIMESTAMP
        """,
        (user_id,)
    )

    deleted_count = cursor.rowcount

    conn.commit()
    conn.close()

    return {
        "success": True,
        "deleted": deleted_count
    }