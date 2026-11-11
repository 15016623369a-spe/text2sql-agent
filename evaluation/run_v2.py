"""Run the BM25 schema-linking baseline on the same frozen development split."""
from evaluation.run_v1 import main

if __name__ == "__main__":
    raise SystemExit(main(schema_top_k=4))
