#!/usr/bin/env bash
set -euo pipefail

archive_dir="$(mktemp -d)"
trap 'rm -rf "$archive_dir"' EXIT
mkdir -p data/bird

verify_sha256() {
  local checksum
  checksum="$(sha256sum "$2")"
  if [[ "${checksum%% *}" != "$1" ]]; then
    printf 'Checksum mismatch for %s\n' "$2" >&2
    return 1
  fi
  printf 'Verified %s\n' "$2"
}

curl --fail --location --retry 3 --connect-timeout 20 --max-time 300 --silent --show-error \
  'https://huggingface.co/datasets/birdsql/bird_mini_dev/resolve/5b256b5a9c17478f06ed6912612e9a14d17065c8/data/mini_dev_sqlite.jsonl' \
  --output data/bird/mini_dev_sqlite.jsonl
curl --fail --location --retry 3 --connect-timeout 20 --max-time 300 --silent --show-error \
  'https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip' --output "$archive_dir/dev.zip"

verify_sha256 '926af2a0d1b47e5d0d15a7aa8a34d4721007f4550a881555b6a89496b54a2606' data/bird/mini_dev_sqlite.jsonl
verify_sha256 'cdd6d19faeb45a23970b98d3ef6c40a87987c95459c2cf12076897a60cf5a630' "$archive_dir/dev.zip"
unzip -p "$archive_dir/dev.zip" 'dev_20240627/dev_databases.zip' > "$archive_dir/dev_databases.zip"
verify_sha256 '8132aacfe61ecec7198a62e93cb8512dac89b1d6b9922fe3c7bb94b13f317d8f' "$archive_dir/dev_databases.zip"
unzip -q -o "$archive_dir/dev_databases.zip" -d data/bird
verify_sha256 '986817d793479801ed55133e55aa27e335422c0cd3866b54a3d6317b7c5f09c1' data/bird/dev_databases/california_schools/california_schools.sqlite
