"""Optional local BGE-M3 schema retrieval backed by Chroma."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

from app.column_notes import load_column_notes
from app.config import PROJECT_ROOT
from app.schema_linking import _tokens

if TYPE_CHECKING:
    from app.schema_linking import SchemaIndex

MODEL_ID = "BAAI/bge-m3"
MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
MODEL_SHA256 = "b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38"
MODEL_MANIFEST = {"model": MODEL_ID, "revision": MODEL_REVISION, "source_sha256": MODEL_SHA256}
VERSION = "bge-m3-chroma-1"
DENSE_ROOT = PROJECT_ROOT / "data" / "bird" / ".retrieval"
DENSE_RANK_K = 4
_MAX_SEQUENCE_LENGTH = 512
_EMBEDDING_BATCH = 8
_INSERT_BATCH = 48
_COLUMN_HITS = 32


@dataclass(frozen=True)
class SchemaDocument:
    table: str
    column: str
    text: str

    @property
    def metadata(self) -> dict[str, str]:
        return {"table": self.table, "column": self.column, "kind": "column" if self.column else "table"}


def schema_documents(db_path: Path, index: SchemaIndex) -> list[SchemaDocument]:
    descriptions = {
        path.stem.casefold(): path
        for path in (db_path.parent / "database_description").glob("*.csv")
    }
    docs = []
    for table in sorted(index.ddl):
        description = descriptions.get(table.casefold())
        notes = load_column_notes(description) if description is not None else {}
        table_text = f"Table {table} ({' '.join(_tokens(table))})"
        columns = []
        for column in sorted(index.columns[table]):
            note = notes.get(column.strip().casefold())
            meaning = f". Meaning: {note.meaning[:320]}" if note and note.meaning else ""
            encoding = f". Documented values: {note.encoding[:200]}" if note and note.encoding else ""
            label = f"{column} ({' '.join(_tokens(column))}){meaning}"
            columns.append(SchemaDocument(table, column, f"{table_text}. Column {label}{encoding}"))
        summary = " | ".join(document.text.removeprefix(f"{table_text}. Column ") for document in columns)
        docs.append(SchemaDocument(table, "", f"{table_text}. Columns: {summary}"))
        docs.extend(columns)
    return docs


def index_name(docs: list[SchemaDocument]) -> str:
    material = [VERSION, MODEL_REVISION, _MAX_SEQUENCE_LENGTH,
                [[doc.metadata, doc.text] for doc in docs]]
    digest = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return "schema-" + digest[:48]


def best_tables(results: list[dict], limit: int) -> list[str]:
    best = {}
    for result in results:
        for metadata, distance in zip(result["metadatas"][0], result["distances"][0], strict=True):
            table = metadata["table"]
            best[table] = min(distance, best.get(table, distance))
    return sorted(best, key=lambda table: (best[table], table))[:limit]


class DenseSchemaIndex:
    def __init__(self, root: Path = DENSE_ROOT):
        import chromadb
        from chromadb.config import Settings
        from sentence_transformers import SentenceTransformer

        root = root.resolve()
        model = root / "bge-m3"
        marker = model / "rag-model.json"
        if not marker.is_file() or json.loads(marker.read_text()) != MODEL_MANIFEST:
            raise ValueError(f"Prepare the pinned BGE-M3 model first: python -m evaluation.prepare_dense --dense-root {root}")
        if not (model / "model.safetensors").is_file():
            raise ValueError(f"Missing BGE-M3 safetensors in {model}")
        self._root = root
        self._client = chromadb.PersistentClient(
            path=str(root / "chroma"), settings=Settings(anonymized_telemetry=False),
        )
        self._device = "cpu"
        self._model = SentenceTransformer(
            str(model), device=self._device, local_files_only=True, trust_remote_code=False,
            model_kwargs={"use_safetensors": True},
        )
        self._model.max_seq_length = _MAX_SEQUENCE_LENGTH
        self._lock = Lock()
        self._collections = {}

    def metadata(self) -> dict:
        return {
            "version": VERSION, **MODEL_MANIFEST, "device": self._device,
            "store": "Chroma PersistentClient", "distance": "cosine",
            "max_sequence_length": _MAX_SEQUENCE_LENGTH, "dense_rank_k": DENSE_RANK_K,
        }

    def _encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(
            texts, batch_size=_EMBEDDING_BATCH, normalize_embeddings=True,
            convert_to_numpy=True, show_progress_bar=False,
        )
        return vectors.astype("float32", copy=False).tolist()

    def _collection(self, db_path: Path, index: SchemaIndex):
        key = db_path.resolve()
        if key not in self._collections:
            docs = schema_documents(db_path, index)
            collection = self._client.get_or_create_collection(
                name=index_name(docs), embedding_function=None, configuration={"hnsw": {"space": "cosine"}},
            )
            if collection.count() != len(docs):
                for offset in range(0, len(docs), _INSERT_BATCH):
                    batch = docs[offset:offset + _INSERT_BATCH]
                    texts = [doc.text for doc in batch]
                    collection.upsert(
                        ids=[str(i) for i in range(offset, offset + len(batch))],
                        documents=texts, metadatas=[doc.metadata for doc in batch],
                        embeddings=self._encode(texts),
                    )
                if collection.count() != len(docs):
                    raise RuntimeError("Incomplete Chroma schema index")
            tables = len(index.ddl)
            self._collections[key] = (collection, tables, len(docs) - tables)
        return self._collections[key]

    def rank(self, db_path: Path, index: SchemaIndex, query: str) -> list[str]:
        import fcntl

        if not index.ddl or not query.strip():
            return []
        with self._lock, (self._root / ".chroma.lock").open("a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            collection, tables, columns = self._collection(db_path, index)
            vector = self._encode([query])
            results = [
                collection.query(
                    query_embeddings=vector, where={"kind": kind}, n_results=count,
                    include=["metadatas", "distances"],
                )
                for kind, count in (("table", min(tables, DENSE_RANK_K)), ("column", min(columns, _COLUMN_HITS)))
                if count
            ]
        return best_tables(results, DENSE_RANK_K)
