# BIRD 数据来源与校验

BIRD Mini-Dev SQLite 的题目文件固定在官方仓库修订 `5b256b5a9c17478f06ed6912612e9a14d17065c8`。数据库取自官方开发集压缩包中的 `dev_20240627/dev_databases.zip`。按下表核对 SHA256 后，将题目文件和解压得到的 `dev_databases/` 放在本目录。

- [固定版本的 SQLite 题目文件](https://huggingface.co/datasets/birdsql/bird_mini_dev/resolve/5b256b5a9c17478f06ed6912612e9a14d17065c8/data/mini_dev_sqlite.jsonl)
- [BIRD 官方开发集压缩包](https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip)

## 已安装

| 文件 | 大小 (bytes) | SHA256 | 说明 |
| --- | --- | --- | --- |
| `mini_dev_sqlite.jsonl` | 471,159 | `926af2a0d1b47e5d0d15a7aa8a34d4721007f4550a881555b6a89496b54a2606` | BIRD mini-dev SQLite 子集，500 题（JSON Lines） |
| `dev_databases.zip` | 345,997,266 | `8132aacfe61ecec7198a62e93cb8512dac89b1d6b9922fe3c7bb94b13f317d8f` | 11 个 SQLite 数据库（解压至 `dev_databases/`，约 1.5 GB） |

解压后校验：`dev_databases/california_schools/california_schools.sqlite` 的
SHA256 为 `986817d793479801ed55133e55aa27e335422c0cd3866b54a3d6317b7c5f09c1`。

## 未安装（本阶段参考）

| 文件 | 大小 (bytes) | SHA256 | 说明 |
| --- | --- | --- | --- |
| `dev.zip` | 346,207,293 | `cdd6d19faeb45a23970b98d3ef6c40a87987c95459c2cf12076897a60cf5a630` | 完整 BIRD dev 集（dev.json / dev.sql / dev_databases.zip） |

## 字段说明（mini_dev_sqlite.jsonl）

每行一个 JSON 对象：`question_id`（题号）、`db_id`（数据库名）、`question`（英文问题）、
`evidence`（外部知识提示）、`SQL`（参考 SQL）、`difficulty`（simple / moderate / challenging）。

## 许可与出处

- 官网：<https://bird-bench.github.io/>
- BIRD-SQL 的许可与引用方式以官网为准。
