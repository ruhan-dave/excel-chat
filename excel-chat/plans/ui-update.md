# UI Update Plan: Sidebar + Threads + Long-Term Memory

**Purpose:** Redesign the frontend from a 2-tab layout (Upload/Query) to a single-view sidebar + conversation thread model. Add long-term memory so users don't re-ask questions for the same sheets. Support multi-sheet selection per thread.

---

## 1. Current State

### 1.1 Layout
- **2-tab design** (`App.tsx`): "Upload & Describe" tab and "Query" tab
- Upload tab: `SheetManager` component (file upload + sheet description editing)
- Query tab: `PromptInput` component (textarea + SSE streaming results)
- Both tabs live inside a centered `max-w-5xl` container

### 1.2 Backend
- **No sheet selection in queries** — `/query` and `/query/stream` load *all* sheets for the user (`get_all_sheets(user_id)`)
- **No conversation persistence** — each query is stateless; no history stored
- **Semantic cache** exists (per-user, embedding similarity) but is invisible to the user
- **No thread/session concept** — no way to group Q&A by context

### 1.3 Database
- `files` table: file_id, file_name, s3_key, sheet_count, user_id
- `sheets` table: sheet_id, file_id, file_name, sheet_name, fields, years, etc.
- `result_cache` table: cache_key, user_id, value, cache_type
- No `threads` or `messages` table exists

---

## 2. Target Design

### 2.1 Layout Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│  Header: Excel Analyst                                                │
├───────────────┬──────────────────────────────────────────────────────┤
│  Sidebar      │  Main Content Area                                    │
│  (280px)      │                                                       │
│               │  ┌──────────────────────────────────────────────┐   │
│  ┌─────────┐  │  │  Thread Title + Sheet Selector Chips         │   │
│  │ Sheets  │  │  │  [Sheet1] [Sheet2] [+]                       │   │
│  │ Section │  │  └──────────────────────────────────────────────┘   │
│  │         │  │                                                       │
│  │ ▸ file1 │  │  ┌──────────────────────────────────────────────┐   │
│  │   sheet1│  │  │  Conversation History (scrollable)            │   │
│  │   sheet2│  │  │                                               │   │
│  │ ▸ file2 │  │  │  User: What was revenue in 2022?              │   │
│  │   sheet3│  │  │  Assistant: Revenue in 2022 was $1.5M...      │   │
│  │         │  │  │                                               │   │
│  ├─────────┤  │  │  User: What's the profit margin?              │   │
│  │ Threads │  │  │  Assistant: The profit margin is 46.7%...     │   │
│  │ Section │  │  │                                               │   │
│  │         │  │  │  [Plan card] [Progress steps] [Answer]        │   │
│  │ + New   │  │  │                                               │   │
│  │ Thread  │  │  └──────────────────────────────────────────────┘   │
│  │         │  │                                                       │
│  │ ▸ Thread1│ │  ┌──────────────────────────────────────────────┐   │
│  │   Q&A.. │  │  │  [Textarea: Ask a question...]    [Send]      │   │
│  │ ▸ Thread2│ │  └──────────────────────────────────────────────┘   │
│  │   Q&A.. │  │                                                       │
│  └─────────┘  │                                                       │
└───────────────┴──────────────────────────────────────────────────────┘
```

### 2.2 Sidebar — Sheets Section (Top)

- Lists all uploaded files and their sheets
- Each file is collapsible (expand to see sheets)
- Upload button at the top of this section
- Clicking a sheet shows its metadata (fields, years, description) in a popover or inline expand
- Sheets are selectable — clicking a sheet toggles its selection state (checkbox or highlight)
- Selected sheets are highlighted and also appear as chips in the main content area
- Delete file button (trash icon) per file

### 2.3 Sidebar — Threads Section (Bottom)

- "+ New Thread" button at the top
- Each thread shows:
  - Title (auto-generated from first question or user-set)
  - Timestamp of last activity
  - Sheet count badge (e.g., "2 sheets")
  - Preview of last Q&A (truncated)
- Clicking a thread loads its conversation history in the main area
- Active thread is highlighted
- Threads can be renamed (double-click) or deleted (right-click or trash icon on hover)

### 2.4 Main Content Area — Thread View

#### Sheet Selector Bar (Top)
- Shows chips for each selected sheet in this thread
- `[Sheet1 ×] [Sheet2 ×] [+]` — click × to remove, click + to open sheet picker dropdown
- Sheets can be added/removed at any point during the conversation
- Selection persists across messages in the thread
- If user removes a sheet mid-conversation, subsequent queries won't include it (but past answers remain)

#### Conversation History (Middle, Scrollable)
- Chronological list of Q&A pairs
- Each entry shows:
  - User question (right-aligned or with user avatar)
  - Assistant response (left-aligned or with bot avatar)
  - Collapsible plan/progress details (from SSE events)
  - Timestamp
  - "Cached" badge if answer came from cache
- Auto-scroll to bottom on new message
- Smooth scroll animation

#### Query Input (Bottom)
- Textarea with Enter-to-send (Shift+Enter for newline)
- Send button
- Disabled state when no sheets are selected (show hint: "Select at least one sheet to ask a question")
- Loading state with spinner during SSE streaming

### 2.5 Empty States

- **No sheets uploaded:** Sidebar sheets section shows upload prompt. Main area shows "Upload a file to get started."
- **No threads created:** Threads section shows "Start a new thread to ask questions."
- **No sheets selected in thread:** Main area shows "Select sheets from the sidebar to begin asking questions."

---

## 3. Data Model

### 3.1 New Database Tables

```sql
CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'anonymous',
    title TEXT NOT NULL DEFAULT 'New Thread',
    created_at TEXT NOT NULL DEFAULT (datetime('isoformat')),
    updated_at TEXT NOT NULL DEFAULT (datetime('isoformat')),
    last_message_preview TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS thread_sheets (
    thread_id TEXT NOT NULL,
    sheet_id TEXT NOT NULL,
    added_at TEXT NOT NULL DEFAULT (datetime('isoformat')),
    PRIMARY KEY (thread_id, sheet_id),
    FOREIGN KEY (thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE,
    FOREIGN KEY (sheet_id) REFERENCES sheets(sheet_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'anonymous',
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    query TEXT,
    friendly_response TEXT,
    full_result TEXT,  -- JSON of complete pipeline result
    sheet_ids TEXT,    -- JSON array of sheet IDs used for this query
    cached INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('isoformat')),
    FOREIGN KEY (thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE
);
```

### 3.2 Relationships

```
User 1──* Threads 1──* Messages
                 |
                 *──* Sheets (via thread_sheets junction)
```

- A thread can reference one or more sheets (many-to-many)
- Each message stores which sheets were used (snapshot at query time)
- Messages store both the user question and the full assistant response
- Thread title auto-updates from the first user message

---

## 4. Backend API Changes

### 4.1 New Endpoints

```
# Thread CRUD
POST   /threads                    Create a new thread
       Body: { "title": "optional", "sheet_ids": ["sheet1", "sheet2"] }
       Returns: { "thread_id": "...", "title": "...", "sheet_ids": [...] }

GET    /threads                    List all threads for user
       Returns: { "threads": [{ "thread_id", "title", "updated_at", "sheet_count", "last_message_preview" }] }

GET    /threads/{thread_id}        Get thread details + messages + selected sheets
       Returns: { "thread": {...}, "messages": [...], "sheets": [...] }

PATCH  /threads/{thread_id}        Update thread (rename, add/remove sheets)
       Body: { "title": "new title" } and/or { "add_sheet_ids": [...], "remove_sheet_ids": [...] }

DELETE /threads/{thread_id}        Delete thread and all its messages

# Messages
GET    /threads/{thread_id}/messages   Get all messages in a thread
       Returns: { "messages": [{ "message_id", "role", "content", "friendly_response", "created_at", "cached" }] }

# Query with thread context
GET    /query/stream?query=...&thread_id=...&sheet_ids=sheet1,sheet2
       - If thread_id provided: store Q&A in messages table
       - If sheet_ids provided: only load those sheets (not all user sheets)
       - SSE events unchanged, but "done" event includes message_id

# Long-term memory check
GET    /threads/{thread_id}/similar?query=...
       - Check if a similar question was already asked in this thread
       - Returns: { "found": bool, "message_id": "...", "similarity": 0.92, "friendly_response": "..." }
```

### 4.2 Query Endpoint Changes

The `/query/stream` endpoint needs two new optional parameters:
- `thread_id`: If provided, the Q&A is persisted to the messages table
- `sheet_ids`: Comma-separated sheet IDs. If provided, only load those sheets instead of all user sheets

**Sheet loading change** (in `stream_generator`):
```python
# Current: loads ALL sheets for the user
all_sheet_metas = get_all_sheets(user_id=user_id)

# New: if sheet_ids provided, filter to only those
if sheet_ids:
    all_sheet_metas = [m for m in get_all_sheets(user_id=user_id) if m.sheet_id in sheet_ids_set]
else:
    all_sheet_metas = get_all_sheets(user_id=user_id)
```

**Message persistence** (after pipeline completes, in the "done" event handler):
```python
if thread_id:
    message_id = str(uuid.uuid4())
    save_message(
        message_id=message_id,
        thread_id=thread_id,
        user_id=user_id,
        role="user",
        content=query,
        friendly_response=friendly,
        full_result=json.dumps(result_dict),
        sheet_ids=json.dumps(selected_sheet_ids),
        cached=is_cached,
    )
    update_thread_preview(thread_id, query[:100])
```

### 4.3 Long-Term Memory Check

Before running the pipeline, check if a similar question was already asked in this thread:

```python
if thread_id:
    # Get all previous user messages in this thread
    previous_messages = get_thread_messages(thread_id, role="user")
    # Embed the current query
    query_embedding = embed_query(query)
    # Compare against previous questions
    for msg in previous_messages:
        prev_embedding = embed_query(msg.content)
        similarity = cosine_similarity(query_embedding, prev_embedding)
        if similarity > 0.92:
            # Found a similar previous question — return the stored answer
            return cached_response_from_message(msg.message_id)
```

This is separate from the semantic cache (which is per-user, not per-thread). The thread memory check is more precise because it only compares against questions asked about the *same set of sheets*.

---

## 5. Frontend Architecture

### 5.1 New Component Structure

```
src/
  App.tsx                          # Layout shell: header + sidebar + main
  components/
    ui/
      sidebar.tsx                  # NEW — left sidebar container
      sheet-list.tsx               # NEW — sheets section in sidebar
      thread-list.tsx              # NEW — threads section in sidebar
      thread-view.tsx              # NEW — main content area for a thread
      conversation-history.tsx     # NEW — scrollable Q&A history
      sheet-selector-bar.tsx       # NEW — sheet chips above conversation
      message-bubble.tsx           # NEW — single Q&A entry
      prompt-input.tsx             # REFACTORED — extracted from current, simplified
      file-uploader.tsx            # REFACTORED — compact upload for sidebar
      sheet-manager.tsx            # DEPRECATED — functionality split into sheet-list
      tabs.tsx                     # DEPRECATED — no longer needed
      button.tsx                   # KEEP
      input.tsx                    # KEEP
      textarea.tsx                 # KEEP
      card.tsx                     # KEEP
      backdrop.tsx                 # KEEP
      backdropWithSpinner.tsx      # KEEP
      spinner.tsx                  # KEEP
  hooks/
    useThreads.ts                  # NEW — thread CRUD + state management
    useSheets.ts                   # NEW — sheet list + selection state
    useConversation.ts             # NEW — message history + SSE streaming
  lib/
    utils.ts                       # KEEP
    api.ts                         # NEW — centralized API client
```

### 5.2 State Management

Use React's built-in state + `@tanstack/react-query` (already in dependencies) for server state:

```typescript
// useSheets hook
interface UseSheetsReturn {
  sheets: SheetInfo[];
  files: FileInfo[];
  selectedSheetIds: Set<string>;
  toggleSheet: (sheetId: string) => void;
  refetch: () => void;
  isLoading: boolean;
}

// useThreads hook
interface UseThreadsReturn {
  threads: ThreadSummary[];
  activeThreadId: string | null;
  createThread: (sheetIds: string[]) => Promise<string>;
  selectThread: (threadId: string) => void;
  deleteThread: (threadId: string) => Promise<void>;
  renameThread: (threadId: string, title: string) => Promise<void>;
}

// useConversation hook
interface UseConversationReturn {
  messages: Message[];
  sendMessage: (query: string) => void;
  isLoading: boolean;
  streamingState: StreamingState;  // status, plan, steps, friendly response
  addSheetToThread: (sheetId: string) => Promise<void>;
  removeSheetFromThread: (sheetId: string) => Promise<void>;
  threadSheets: SheetInfo[];
}
```

### 5.3 SSE Streaming Integration

The current `PromptInput` uses `EventSource` for SSE. This logic moves into `useConversation`:

```typescript
const sendMessage = (query: string) => {
  const sheetIds = Array.from(selectedSheetIds).join(",");
  const url = `${apiURL}/query/stream?query=${encodeURIComponent(query)}&thread_id=${activeThreadId}&sheet_ids=${sheetIds}`;
  const es = new EventSource(url);
  // ... same event handlers as current PromptInput ...
  // On "done" event: refetch messages to persist the new Q&A
};
```

### 5.4 Responsive Design

- **Desktop (>1024px):** Full sidebar (280px) + main content side-by-side
- **Tablet (768-1024px):** Collapsible sidebar (overlay drawer), main content full-width
- **Mobile (<768px):** Sidebar becomes bottom sheet / hamburger menu, main content full-width

---

## 6. Implementation Phases

### Phase 1: Backend — Database & API (Estimated: 3-4 hours)

1. **Add database tables** — `threads`, `thread_sheets`, `messages` in `sheet_metadata.py`
2. **Add CRUD functions** — `create_thread`, `get_threads`, `get_thread`, `update_thread`, `delete_thread`, `save_message`, `get_thread_messages`
3. **Add API endpoints** — `/threads`, `/threads/{id}`, `/threads/{id}/messages`
4. **Modify `/query/stream`** — accept `thread_id` and `sheet_ids` params, persist messages, filter sheets
5. **Add similar-question check** — `/threads/{id}/similar` or inline in query endpoint
6. **Test endpoints** — curl/Postman verification

### Phase 2: Frontend — Layout Shell (Estimated: 3-4 hours)

1. **Create `api.ts`** — centralized axios client with all endpoints
2. **Create `sidebar.tsx`** — container with two sections
3. **Create `sheet-list.tsx`** — collapsible file/sheet tree with selection
4. **Create `thread-list.tsx`** — thread list with new thread button
5. **Refactor `App.tsx`** — replace tabs with sidebar + main layout
6. **Wire up `useSheets` and `useThreads` hooks** — data fetching and state
7. **Test layout** — verify sidebar, sheet selection, thread creation

### Phase 3: Frontend — Thread View & Conversation (Estimated: 3-4 hours)

1. **Create `thread-view.tsx`** — main content area for active thread
2. **Create `sheet-selector-bar.tsx`** — chips with add/remove
3. **Create `conversation-history.tsx`** — scrollable message list
4. **Create `message-bubble.tsx`** — individual Q&A entry with collapsible details
5. **Refactor `prompt-input.tsx`** — simplify to just textarea + send, delegate SSE to `useConversation`
6. **Create `useConversation` hook** — SSE streaming + message persistence
7. **Test conversation flow** — ask question, see streaming response, verify history persists

### Phase 4: Polish & Edge Cases (Estimated: 2-3 hours)

1. **Empty states** — no sheets, no threads, no sheets selected
2. **Long-term memory** — check for similar questions before sending, show "Previously asked" badge
3. **Thread auto-titling** — set title from first question
4. **Thread rename/delete** — inline editing, confirmation dialog
5. **Responsive design** — mobile/tablet breakpoints
6. **Loading states** — skeleton loaders for sidebar lists
7. **Error handling** — network failures, SSE reconnection
8. **Scroll behavior** — auto-scroll to bottom on new message, scroll-to-top for history

### Phase 5: Backend — Query Optimization (Estimated: 1-2 hours)

1. **Sheet-specific caching** — cache key includes sheet_ids so same question on different sheets doesn't collide
2. **Thread memory pre-check** — before semantic cache, check thread's message history for exact/similar match
3. **Message search** — optional `/threads/{id}/search?q=...` endpoint

---

## 7. Migration Strategy

### 7.1 Backward Compatibility

- The `/query/stream` endpoint keeps `thread_id` and `sheet_ids` as **optional** parameters
- If not provided, behavior is identical to current (load all sheets, no persistence)
- The old `SheetManager` and `PromptInput` components remain functional until new UI is ready
- New UI components are built alongside, then `App.tsx` is swapped

### 7.2 Deployment Order

1. Deploy backend changes first (new tables, new endpoints, modified query endpoint)
2. Verify backend works with old frontend (optional params = backward compatible)
3. Deploy frontend changes (new layout)
4. Remove deprecated components (`tabs.tsx`, old `sheetmanager.tsx`)

### 7.3 Data Migration

- No existing data needs migration — new tables start empty
- Existing users start with no threads; they create threads as they ask questions
- Existing semantic cache continues to work alongside thread memory

---

## 8. Design Principles

1. **Latency first** — sidebar loads sheets/threads in parallel; SSE streaming unchanged; no blocking API calls on render
2. **Optimistic UI** — thread creation, sheet selection, and message sending show immediately, rollback on error
3. **Persistent state** — active thread ID stored in localStorage; sheet selection persisted to backend via `thread_sheets`
4. **Progressive disclosure** — plan/progress details collapsed by default in history; expand on click
5. **Keyboard friendly** — Enter to send, Tab to navigate sidebar, Ctrl+N for new thread
6. **Minimal API calls** — sheets and threads fetched once on mount, cached in react-query; mutations invalidate selectively
7. **Graceful degradation** — if thread API fails, fall back to stateless query (current behavior)

---

## 9. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| SSE URL length limit with many sheet_ids | Query fails for threads with 20+ sheets | Use POST + body for sheet_ids if URL exceeds 2000 chars; or pass thread_id only and resolve sheets server-side |
| Thread memory check adds latency | Slower first query in a thread | Only check if thread has >3 messages; limit to last 10 messages; use embedding cache |
| Sidebar clutter with many files/sheets | Poor UX with 50+ sheets | Virtual scrolling; search/filter input; collapse all by default |
| Message table grows unbounded | DB bloat over time | Auto-cleanup of threads older than 90 days (existing cleanup cron); pagination on messages endpoint |
| Concurrent sheet add/remove during streaming | Race condition | Sheet selection locked during active SSE stream; changes apply to next query |

---

**End of UI Update Plan**
