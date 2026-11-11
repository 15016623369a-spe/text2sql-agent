"""Prepare a local safetensors copy of the pinned public BGE-M3 model."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from app.dense_schema import DENSE_ROOT, MODEL_ID, MODEL_MANIFEST, MODEL_REVISION, MODEL_SHA256

_CONFIG_FILES = (
    "config.json", "config_sentence_transformers.json", "modules.json", "sentence_bert_config.json",
    "sentencepiece.bpe.model", "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json",
    "1_Pooling/config.json",
)
_WEIGHTS = "pytorch_model.bin"


def prepare_model(root: Path, source: Path | None = None) -> Path:
    from safetensors.torch import save_file

    target = root / "bge-m3"
    marker = target / "rag-model.json"
    if target.exists():
        if marker.is_file() and json.loads(marker.read_text()) == MODEL_MANIFEST and (target / "model.safetensors").is_file():
            return target
        raise ValueError(f"Model directory already exists with unexpected files: {target}")

    if source is None:
        from huggingface_hub import snapshot_download

        source = Path(snapshot_download(
            repo_id=MODEL_ID, revision=MODEL_REVISION, allow_patterns=[*_CONFIG_FILES, _WEIGHTS],
        ))

    import torch

    with (source / _WEIGHTS).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != MODEL_SHA256:
        raise ValueError("BGE-M3 checkpoint SHA256 did not match the pinned public file")

    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        output = Path(directory) / "bge-m3"
        output.mkdir()
        for name in _CONFIG_FILES:
            dst = output / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, dst)
        tensors = torch.load(source / _WEIGHTS, map_location="cpu", weights_only=True, mmap=True)
        save_file(tensors, output / "model.safetensors", metadata={"format": "pt"})
        (output / "rag-model.json").write_text(json.dumps(MODEL_MANIFEST, indent=2) + "\n")
        output.rename(target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dense-root", type=Path, default=DENSE_ROOT)
    parser.add_argument("--source", type=Path, help="Use an already downloaded pinned model directory")
    args = parser.parse_args()
    try:
        print(prepare_model(args.dense_root, args.source))
    except (ImportError, OSError, ValueError) as exc:
        parser.exit(1, f"BGE-M3 preparation failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
