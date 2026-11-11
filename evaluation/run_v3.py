"""Run the BIRD benchmark with bounded SQLite execution recovery."""
from evaluation.run_v1 import main

if __name__ == "__main__":
    raise SystemExit(main(schema_top_k=4, recovery=True))
