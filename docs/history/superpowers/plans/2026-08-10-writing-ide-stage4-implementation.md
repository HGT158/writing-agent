# Writing IDE Stage 4 Implementation Plan



**Goal:** Build the local FastAPI + SSE + Vue 3 writing IDE defined by architecture v1.9, including assistant-isolated project imports, multi-file editing, selection rewrites, and project-scoped Agent chat.

**Architecture:** Project content lives under `data/assistants/<assistant_id>/projects/<project_id>/`; SQLite stores isolated project/document/change-set/task metadata through MemoryStore only. FastAPI owns HTTP/SSE orchestration and reuses AgentRuntime. Vue renders a VS Code-style explorer/editor/Agent workspace and applies AI changes only after a version-checked diff confirmation.

**Tech Stack:** Python 3.13, SQLite, FastAPI, Uvicorn, Pydantic v2, Vue 3, Vite, TypeScript, CodeMirror 6, marked, lucide-vue-next, Vitest.

**Execution note:** This workspace has no Git repository, so commit steps are intentionally omitted. Execute inline; multi-agent delegation is not authorized.

---

### Task 1: Project Storage and Isolation

**Files:**
- Create: `memory/projects.py`
- Modify: `memory/store.py`
- Modify: `memory/short_term.py`
- Modify: `config/settings.py`
- Modify: `.env.example`
- Test: `tests/test_project_store.py`
- Test: `tests/test_memory_isolation.py`

- [ ] Write tests proving that projects are created under the owning assistant, same display names do not overwrite, folder/file imports copy content, path traversal is rejected, and assistant B cannot list/read/update assistant A projects.

```python
project = store.create_project("writer-a", "Novel")
document = store.get_document("writer-a", project.project_id, project.entry_document_id)
assert document.relative_path == "article.md"
with pytest.raises(ProjectNotFoundError):
    store.get_document("writer-b", project.project_id, document.document_id)
```

- [ ] Run the target tests with the mandated interpreter.

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests/test_project_store.py tests/test_memory_isolation.py -v -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent
```

- [ ] Add `projects`, `project_documents`, and `change_sets` DDL inside `memory/`; define frozen records and domain errors in `memory/projects.py`.

```python
@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    assistant_id: str
    name: str
    root_path: str
    entry_document_id: str | None

@dataclass(frozen=True)
class DocumentRecord:
    document_id: str
    project_id: str
    assistant_id: str
    relative_path: str
    version: int
    content: str | None = None
```

- [ ] Add MemoryStore methods with `assistant_id` first: `create_project`, `list_projects`, `rename_project`, `archive_project`, `import_text_project`, `import_folder_project`, `get_project_tree`, `get_document`, and `save_document`.

- [ ] Implement bounded streaming imports, strict relative-path validation, staging-directory cleanup, atomic final rename, UTF-8/UTF-8-BOM text decoding, per-document versions, and file rollback when metadata commit fails.

- [ ] Add `PROJECT_IMPORT_MAX_FILES`, `PROJECT_IMPORT_MAX_TOTAL_MB`, and `PROJECT_IMPORT_MAX_FILE_MB` to Settings and `.env.example` with 5000/512/100 defaults.

- [ ] Run project tests, then run the 6 memory-isolation red-line tests.

### Task 2: Change Sets and Runtime Editing

**Files:**
- Create: `agent/project_editing.py`
- Modify: `agent/runtime.py`
- Modify: `agent/events.py`
- Modify: `memory/projects.py`
- Modify: `memory/store.py`
- Test: `tests/test_project_editing.py`
- Test: `tests/test_runtime_project_editing.py`

- [ ] Write tests for Unicode code-point ranges, empty LLM output, assistant locks, pending/rejected/applied states, stale versions, original-text mismatch, and cross-assistant change-set access.

```python
change = store.create_change_set(
    "writer-a", project_id, document_id,
    source="selection", start=1, end=3,
    original_text="中文", replacement_text="华文", base_version=1,
)
updated = store.apply_change_set("writer-a", project_id, change.change_set_id, expected_version=1)
assert updated.version == 2
```

- [ ] Run targeted tests using the mandated Python command.

- [ ] Implement `AgentRuntime.rewrite_selection(...)` as a bounded editing entry point that acquires the existing assistant run lock, validates project/document ownership, loads persona + editing Skill, invokes the shared LLM, persists a pending change set, and emits `change_preview`.

```python
async def rewrite_selection(
    self, assistant_id: str, project_id: str, document_id: str,
    *, start: int, end: int, selected_text: str,
    instruction: str, document_version: int,
) -> ChangeSetRecord:
    ...
```

- [ ] Implement `apply_change_set` and `reject_change_set` through MemoryStore. Apply must validate ownership, pending state, version, and original snapshot before atomic file replacement and version increment.

- [ ] Add `AgentRuntime.chat_project(...)`; pure answers emit tokens, while modification responses parse structured replacements into pending change sets and never write project files directly.

- [ ] Run editing tests and rerun loop/runtime tests.

### Task 3: FastAPI and SSE

**Files:**
- Create: `api/__init__.py`
- Create: `api/main.py`
- Create: `api/models.py`
- Create: `api/dependencies.py`
- Create: `api/routes_assistants.py`
- Create: `api/routes_projects.py`
- Create: `api/routes_tasks.py`
- Create: `api/routes_articles.py`
- Create: `api/sse.py`
- Modify: `requirements.txt`
- Test: `tests/test_api_projects.py`
- Test: `tests/test_api_tasks.py`
- Test: `tests/test_api_isolation.py`

- [ ] Add FastAPI, Uvicorn, and python-multipart dependencies, then install via the mandated conda interpreter.

- [ ] Write TestClient tests for assistant listing, project CRUD/import/tree/document save, archive rules, read-only article archive, 404 isolation semantics, 409 version/lock conflicts, selection rewrite, change-set apply/reject, project chat, and SSE disconnect cleanup.

- [ ] Run targeted tests before route implementation.

- [ ] Create an application factory that owns one AgentRuntime for the process lifespan, binds only `127.0.0.1`, mounts `/api`, and serves `web/dist` only when built.

```python
def create_app(settings: Settings | None = None, runtime: AgentRuntime | None = None) -> FastAPI:
    ...
```

- [ ] Implement Pydantic request/response models with explicit `assistant_id`, `project_id`, `document_id`, `document_version`, and Unicode code-point ranges.

- [ ] Implement multipart imports using UploadFile streams and client-provided relative paths; pass all persistence work through MemoryStore.

- [ ] Implement an SSE broker that subscribes to EventBus per task, buffers bounded events, sends keepalives, replays terminal status, and unsubscribes on disconnect.

- [ ] Run API tests and rerun memory isolation.

### Task 4: Vue Application Foundation

**Files:**
- Create: `web/package.json`
- Create: `web/tsconfig.json`
- Create: `web/vite.config.ts`
- Create: `web/index.html`
- Create: `web/src/main.ts`
- Create: `web/src/App.vue`
- Create: `web/src/styles.css`
- Create: `web/src/api/client.ts`
- Create: `web/src/types.ts`
- Create: `web/src/stores/workspace.ts`
- Test: `web/src/stores/workspace.test.ts`

- [ ] Scaffold Vue 3 + TypeScript + Vite with CodeMirror 6, marked, lucide-vue-next, Vitest, jsdom, and Vue Test Utils.

- [ ] Write store tests for assistant switching, project/file loading, stale tab removal across assistants, dirty document tracking, save conflict handling, and SSE task event reduction.

- [ ] Implement the typed API client and a focused workspace composable; no component may call `fetch` directly.

- [ ] Establish restrained work-focused tokens, stable panels, keyboard focus states, responsive collapse rules, and no nested decorative cards.

- [ ] Run `npm test` and `npm run build`.

### Task 5: Explorer, Multi-Tab Editor, and Imports

**Files:**
- Create: `web/src/components/ActivityBar.vue`
- Create: `web/src/components/ProjectExplorer.vue`
- Create: `web/src/components/EditorTabs.vue`
- Create: `web/src/components/DocumentEditor.vue`
- Create: `web/src/components/MarkdownPreview.vue`
- Create: `web/src/components/ImportDialog.vue`
- Test: `web/src/components/workspace.test.ts`

- [ ] Write failing component tests for project/file imports, nested tree expansion, opening multiple tabs, dirty indicators, save/reload conflict UI, assistant switching, and Markdown preview.

- [ ] Implement file and folder pickers (`multiple` + directory relative paths), import progress/errors, project create/rename/archive controls, and keyboard-accessible explorer actions.

- [ ] Implement CodeMirror editors keyed by document id, stable tabs, Ctrl+S, preview toggle, and optimistic version saves.

- [ ] Run component tests and production build.

### Task 6: Selection Toolbar and Project Agent Panel

**Files:**
- Create: `web/src/components/SelectionToolbar.vue`
- Create: `web/src/components/ChangeDiff.vue`
- Create: `web/src/components/AgentPanel.vue`
- Create: `web/src/utils/unicodeOffsets.ts`
- Test: `web/src/components/ai-editing.test.ts`
- Test: `web/src/utils/unicodeOffsets.test.ts`

- [ ] Write tests proving CodeMirror UTF-16 offsets convert to Unicode code points, toolbar focus preserves the selected range, generation does not mutate text, accept/reject/regenerate work, stale versions show a conflict, and chat modifications require diff acceptance.

- [ ] Implement the anchored selection toolbar using CodeMirror coordinates and editor-state selection snapshots.

- [ ] Implement change preview diff with accept/reject/regenerate commands; apply updates the editor only from the server-returned new document/version.

- [ ] Implement project-scoped Agent chat with current-document and explicit-attachment context, token streaming, tool/thought event display, and change-set previews.

- [ ] Run AI-editing tests and production build.

### Task 7: Integration, Visual QA, and Documentation

**Files:**
- Create: `tests/test_api_end_to_end.py`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/phase1-architecture.md` only if implementation requires a documented correction and version bump

- [ ] Add end-to-end backend tests covering import → open → manual save → selection rewrite → preview → apply → reread, plus project chat → diff → reject.

- [ ] Run the memory isolation red line first, then all Python tests.

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests/test_memory_isolation.py -v -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent
C:\miniconda\envs\writing-agent\python.exe -m pytest tests/ -v -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent
```

- [ ] Run frontend tests, type checking, and production build.

```powershell
npm test -- --run
npm run typecheck
npm run build
```

- [ ] Start FastAPI on an available localhost port, inspect desktop and mobile layouts, verify selection toolbar/diff/chat interactions, and check browser console/network errors.

- [ ] Update README with installation, server start, imports, project editing, selection rewrite, Agent panel, limits, and build commands. Synchronize actual test counts in AGENTS and architecture status.

- [ ] Stop at the completed Stage 4 boundary and report verification evidence; do not begin later extensions.
