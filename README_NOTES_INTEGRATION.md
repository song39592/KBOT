# Notes integration — 2026-11-25

## Baseline and scope

Integrated on main `708a805918f778d9cf0a884d5cc0d3cd81ec2417` in branch `feature/notes-improvements`.
The teacher question-bank extensions and student bank-first generation remain intact.
Original local work is retained separately. Production VM and Tailscale configuration have not been changed.

Features: student answer reports; teacher feedback, corrections and Top 10; draggable dashboard shortcut; personal history and bookmarks; course/outside-course answer labels; citation page links and extracted-text highlighting; Coze/DeepSeek selection; explicit opt-in web explanation; ambiguous acronym clarification.

Four new tables (`notes_details`, `notes_feedback`, `notes_corrections`, `notes_bookmarks`) are created through the existing application/migration startup. Existing records are retained. Historical answers without NotesDetail rows do not contribute to Top 10.

## Validation

- Notes + student bank-first + teacher question-bank suites: **79 passed**.
- Updated Notes general-knowledge contract test: **1 passed**. It tests labelled outside-course answers and rejection of unlabelled legacy AI output; legacy stored-output normalization remains supported.
- Initial complete suite: **193 passed, 7 failed, 30 subtests passed**. One failure was the old Notes contract fixture, updated and verified above.
- The other **six failures were individually reproduced on unmodified main**:
  - `test_legacy_sqlite_schema_upgrade_preserves_accounts_and_history`: Windows SQLite handle remains open during temporary-directory cleanup.
  - `test_original_teacher_ui_and_registration_are_preserved`: stored template hashes no longer match main.
  - `test_representative_teacher_and_student_templates_render`: standalone Jinja fixture lacks Flask request context.
  - `test_routes_registration_login_sequential_flow_notes_and_teacher_offline`: objective-answer test gets zero score.
  - `test_page_12_gyroscope_uses_three_principles_not_application_as_fourth`: citation count differs.
  - `test_page_19_question_names_definition_and_three_lbs_types`: citation count differs.
- JavaScript syntax check passed. Comparison against local configured secrets found no matching secret values in changed text files.
- Local BGE model matches the existing LFS object and is not changed by this commit.

The complete suite is NOT green. Resolve or explicitly assess the baseline failures before production approval. This integration run used local Python/SQLite and mocked AI adapter tests; it did not revalidate live provider credentials or run the production MySQL/Docker deployment.

## Windows test

From this repository directory, run `powershell -ExecutionPolicy Bypass -File .\START_NOTES_WINDOWS.ps1` (Python 3.11 required).
Teacher: http://127.0.0.1:15000 ; student: http://127.0.0.1:18000 .
Local demo users: `notes_teacher` / `notes_student`, password `NotesTest2026!`.
This launcher uses an isolated local test database; these are not production credentials.
Stop any earlier local test service using these ports first.

For DeepSeek, copy `.env.deepseek.example` to ignored `.env.deepseek` and fill in your own key. After dependencies are installed:

```powershell
.\.notes-venv\Scripts\python.exe .\HKU_CoLearning_KBot_Formal_v8.0.13\run_notes_windows.py --ai --provider deepseek --ai-env .\.env.deepseek
```

Never commit actual `.env` files, API keys, production data, or Tailscale state.
Default answers do not search the web; the student chooses “Explain further with web sources”. Web retrieval currently uses the DeepSeek adapter. Citation highlighting is on extracted page text; the original PDF opens at its page via `#page=`, without a PDF-overlay highlight.

## Review and deployment

Review this feature branch through a PR. Do not deploy until the teacher approves and the baseline failures are assessed. See `README_NOTES_UPDATE.md` for the changed-file inventory and server source/database/knowledge/image backup and rollback commands. Keep production `.env`, volumes and Tailscale settings. Back up before pulling approved main, rebuilding images and running the existing migration service. Never use `docker compose down -v` for an update.
