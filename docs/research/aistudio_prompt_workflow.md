---
date: 2025-10-10T13:45:00-0700
researcher: Codex
git_commit: 2935e4da47599d74f6ef934e25ddda06775ccd10
branch: main
repository: AIstudioProxyAPI
topic: "AIStudio prompt submission and scraping workflow"
tags: [research, codebase, playwright, streaming, aistudio]
status: complete
last_updated: 2025-10-10
last_updated_by: Codex
---
# Research: AIStudio prompt submission and scraping workflow
**Date**: 2025-10-10T13:45:00-0700
**Researcher**: Codex
**Git Commit**: 2935e4da47599d74f6ef934e25ddda06775ccd10
**Branch**: main
**Repository**: AIstudioProxyAPI

## Research Question
How does the current system submit prompts to AI Studio and scrape the resulting responses?

## Summary
- FastAPI’s `/v1/chat/completions` endpoint enqueues every request and hands off processing to a single-worker coroutine that serialises access to the Playwright-controlled browser session.
- `_process_request_refactored` orchestrates prompt preparation, parameter tuning, submission, and response handling, sourcing message text and base64 images via `prepare_combined_prompt`.
- Non-streaming responses are polled from the DOM until completion, then captured through the edit button (and clipboard as fallback); streaming mode instead relies on a local MITM proxy that parses Google’s `GenerateContent` traffic into SSE chunks.
- Post-response cleanup clears the shared stream queue and resets the chat UI to keep the Playwright session ready for the next prompt.

## Detailed Findings
### Request Intake and Queueing
- `api_utils/routes.py:169` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/api_utils/routes.py#L169) accepts `/v1/chat/completions`, generates a request ID, enqueues the payload on `server.request_queue`, and awaits the future that the worker resolves.
- `api_utils/queue_worker.py:46` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/api_utils/queue_worker.py#L46) loops over the shared queue, drops cancelled requests, acquires `processing_lock`, and invokes `_process_request_refactored`. It also runs proactive disconnect checks before and during processing.
- `api_utils/app.py:110` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/api_utils/app.py#L110) starts the optional streaming proxy process and initialises `server` globals (Playwright state, locks, queue) during application startup.

### Browser Session Setup
- `browser_utils/initialization.py:260` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/browser_utils/initialization.py#L260) connects to the remote Firefox (Camoufox) endpoint, loads stored auth when available, attaches network interception for AI Studio responses, and ensures the prompt input is visible before declaring the page ready.
- `browser_utils/model_management.py:14` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/browser_utils/model_management.py#L14) exposes helpers for aligning UI state (advanced panel, tools) and switching models via the Playwright page when `_process_request_refactored` requests a different AI Studio model.

### Prompt Preparation & Submission
- `api_utils/utils.py:244` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/api_utils/utils.py#L244) merges system/user/assistant turns into a single prompt string, collects inline tool-call metadata, and converts base64 image URLs to temporary files returned alongside the prompt.
- `browser_utils/page_controller.py:669` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/browser_utils/page_controller.py#L669) fills the prompt via DOM evaluation, uploads any images through the file chooser, waits for the Run button to enable, and submits using OS-appropriate keyboard shortcuts (with a click fallback).
- `browser_utils/page_controller.py:703` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/browser_utils/page_controller.py#L703) validates submission success by checking input emptiness, button disabled state, or new response containers before proceeding.

### Response Completion & Extraction
- `browser_utils/page_controller.py:870` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/browser_utils/page_controller.py#L870) waits for the latest response node to attach, calls `_wait_for_response_completion`, and then retrieves the final content via helper routines.
- `browser_utils/operations.py:671` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/browser_utils/operations.py#L671) polls for completion by ensuring the input is empty, the Run button is disabled, and the Edit button is visible (or heuristically after multiple passes).
- `browser_utils/operations.py:477` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/browser_utils/operations.py#L477) opens the message editor, reads `data-value` or the textarea value, then exits edit mode; `browser_utils/operations.py:588` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/browser_utils/operations.py#L588) falls back to the “Copy markdown” menu and `navigator.clipboard.readText()` if needed.

### Streaming Capture Path
- `api_utils/request_processor.py:284` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/api_utils/request_processor.py#L284) detects whether `STREAM_PORT` is enabled and, for streaming requests, builds an SSE generator that relays chunks from the stream proxy via `use_stream_response`.
- `api_utils/utils.py:57` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/api_utils/utils.py#L57) drains `server.STREAM_QUEUE`, yielding parsed dictionaries or raw strings until a `done` flag is seen, with watchdog handling for timeouts.
- `stream/proxy_server.py:200` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/stream/proxy_server.py#L200) performs HTTPS MITM for hosts matching `GenerateContent`, forwarding client/server bytes and passing intercepted responses to the queue.
- `stream/interceptors.py:54` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/stream/interceptors.py#L54) decodes chunked/gzipped Google responses, extracts accumulated `reason`, `body`, and tool-call payloads, and marks completion when chunked encoding ends.

### Post-Response Cleanup
- `api_utils/queue_worker.py:140` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/api_utils/queue_worker.py#L140) clears the stream queue and invokes `PageController.clear_chat_history` after each request to reset the UI state.
- `browser_utils/page_controller.py:534` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/browser_utils/page_controller.py#L534) handles clearing the conversation, including dealing with overlays and re-enabling “Temporary chat” mode so that subsequent submissions start from a clean slate.

## Code References
- `api_utils/routes.py:169` – `/v1/chat/completions` request lifecycle (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/api_utils/routes.py#L169)
- `api_utils/queue_worker.py:46` – Serialized worker loop and disconnect handling (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/api_utils/queue_worker.py#L46)
- `api_utils/request_processor.py:800` – Core prompt processing pipeline (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/api_utils/request_processor.py#L800)
- `api_utils/utils.py:244` – Prompt concatenation and image extraction (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/api_utils/utils.py#L244)
- `browser_utils/page_controller.py:669` – DOM submission logic for prompts (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/browser_utils/page_controller.py#L669)
- `browser_utils/operations.py:477` – Response extraction via edit/clipboard helpers (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/browser_utils/operations.py#L477)
- `api_utils/request_processor.py:284` – Streaming vs Playwright response handling switch (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/api_utils/request_processor.py#L284)
- `stream/interceptors.py:54` – Parsing intercepted AI Studio response frames (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/stream/interceptors.py#L54)

## Architecture Documentation
- Launch-time setup initialises a single Playwright page and, when configured, a local HTTPS proxy to inspect Google traffic; both components are stored in `server` globals for reuse across requests.
- A FastAPI queue plus dedicated worker ensures only one prompt is processed at a time, protecting the shared browser state while still respecting client disconnects and timeouts.
- `_process_request_refactored` acts as the orchestration layer, requesting UI adjustments, combining multi-turn prompts, submitting inputs, and delegating to either Playwright DOM scraping or the stream proxy depending on configuration.
- Non-streaming mode waits for UI cues and reads rendered Markdown directly, while streaming mode surfaces the raw response chunks captured by the MITM proxy as SSE-compatible payloads.
- After each reply the worker clears any remaining queue data and resets the AI Studio conversation so the next call starts with a fresh UI context.

## Historical Context (from thoughts/)
- No `thoughts/` directory is present in this repository at commit 2935e4da47599d74f6ef934e25ddda06775ccd10; no supplemental historical notes were found.

## Related Research
- None documented.

## Open Questions
- `api_utils/utils.py:157` (https://github.com/CJackHwang/AIstudioProxyAPI/blob/2935e4da47599d74f6ef934e25ddda06775ccd10/api_utils/utils.py#L157) implements a helper-service streaming generator, but the active pipeline does not reference it; determining whether a helper endpoint is still expected requires additional context.
