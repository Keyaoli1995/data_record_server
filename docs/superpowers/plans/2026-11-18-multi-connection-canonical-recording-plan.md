# 多连接录制规范化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变 Web 文件路径和上报协议的前提下，保存每连接原始 JSONL，并在会话关闭时生成按 `gpsTime` 排序的规范 JSONL。

**Architecture:** `DeviceSession` 保留当前主预览流，并为每个客户端维护独立原始流。关闭时调用独立的 canonicalizer，按会话时间范围过滤、稳定排序、精确去重并原子替换主文件；失败通过现有 `handler_error` 上报路径阻止 Web 将文件视为可用。

**Tech Stack:** Python 3.10/3.12、标准库 `json`、`pathlib`、`os.replace`、pytest。

---

### Task 1: Add canonicalizer tests

**Files:**
- Create: `tests/test_canonicalize.py`

- [ ] **Step 1: Write failing tests** for multi-file merge, range quarantine, stable timestamp ordering, exact duplicate removal, and invalid timestamp quarantine.
- [ ] **Step 2: Run `pytest tests/test_canonicalize.py -q` and verify failure because the module is absent.

### Task 2: Implement canonicalizer

**Files:**
- Create: `data_record_server/canonicalize.py`
- Test: `tests/test_canonicalize.py`

- [ ] **Step 1: Implement `canonicalize_jsonl_files` with explicit bounds, quarantine output, stable sort, exact-line dedupe, and atomic replacement.
- [ ] **Step 2: Run focused canonicalizer tests and verify they pass.

### Task 3: Add per-connection raw persistence

**Files:**
- Modify: `data_record_server/storage.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Add failing storage tests showing two client addresses create two raw JSONL files while the main preview file still receives both records.
- [ ] **Step 2: Run the focused tests and verify failure.
- [ ] **Step 3: Add per-client stream bookkeeping, raw path creation under `connections/`, and raw stream flush/close handling.
- [ ] **Step 4: Run storage tests and the full test suite.

### Task 4: Canonicalize on session close

**Files:**
- Modify: `data_record_server/storage.py`
- Test: `tests/test_storage.py`, `tests/test_reporting_lifecycle.py`

- [ ] **Step 1: Add a failing test proving close replaces the main file with timestamp-sorted data while retaining raw files and reporting the same relative path.
- [ ] **Step 2: Implement close-time canonicalization with a five-minute tolerance and `handler_error` on failure.
- [ ] **Step 3: Run focused lifecycle tests and then all tests.

### Task 5: Retention and documentation

**Files:**
- Modify: `data_record_server/storage.py`, `README.md`, `docs/web-reporting-operations.md`
- Test: `tests/test_storage.py`, `tests/test_deployment.py`

- [ ] **Step 1: Add a failing retention test for closed per-connection JSONL archives.
- [ ] **Step 2: Include raw JSONL and quarantine JSONL in connection archive cleanup without touching active files.
- [ ] **Step 3: Document directory layout, close-time delay, and Web compatibility.
- [ ] **Step 4: Run `pytest -q` and the deployment smoke checks.
