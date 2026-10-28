# Teacher question bank: True / False and MCQ

This change adds a **Question bank** link to the teacher dashboard. It supports Excel template download, validation, preview, explicit confirmation, atomic import, course filtering and pagination. Matching, fill-in-the-blank and short-answer imports are outside this change.

## Teacher workflow

1. Log in to the teacher app and open **Question bank**.
2. Download the template and replace both example rows with your questions.
3. Enter the course name/code and select the `.xlsx` file.
4. Select **Validate & preview**. Correct any row errors before proceeding.
5. Review the question text, options, answers and explanations, then **Confirm import**.
6. Confirm the questions appear under **Your imported questions**.

The template uses these columns, in this order:

`question_id, question_type, topic, question, option_A, option_B, option_C, option_D, option_E, correct_answer, explanation`

- `question_id`, `question_type`, `question` and `correct_answer` are required; topic and explanation are optional.
- TF: `question_type=TF`, answer `TRUE` or `FALSE`, option columns empty. Excel Boolean cells are accepted.
- MCQ: `question_type=MCQ`, at least two distinct nonempty options, one answer letter A–E pointing to a nonempty option. E is optional; no option is inserted automatically.
- IDs are unique within a teacher's course; uploads never overwrite existing questions. In-file IDs are compared case-insensitively. Database collation may impose additional uniqueness restrictions, in which case the whole transaction is rejected.
- Course is a manually entered name/code, not a new course registry. Agree a common identifier with the student integration owner.
- Maximum 1,000 question rows, 5 MB compressed / 20 MB expanded. Formula cells and duplicate options are rejected.
- Every row must pass. Preview does not write questions to the database.
- Confirmation is bound to the teacher, course, exact workbook bytes and latest preview, valid for 30 minutes. Changed files require a fresh preview.

## Storage and integration boundary

`common.models.BankQuestion` adds the `teacher_bank_questions` table. The existing migration command (`python scripts/migrate_db.py`) and normal startup `db.create_all()` create it. No existing table is renamed or altered and no extra Python package is required.

Fields include owner ID, course, external question ID, type, topic, question, options JSON, standard answer, explanation, `source=teacher_upload` and timestamp. The primary key is an internal UUID. The bank is not sent to knowledge ingestion, the document vector index or teacher student-answer search.

Only teacher/admin sessions can access these routes. Lists are scoped to the logged-in teacher's own uploads; admin does not bypass ownership. All POST requests require a session CSRF token. Templates escape uploaded text.

Routes are built with `url_for`, including confirmation redirects and dashboard navigation, and work with the existing `X-Forwarded-Prefix: /teacher` proxy. Local direct route: `/question-bank/`; deployed route: `/teacher/question-bank/`.

**Student integration (wired):** `common.question_bank.pick_question` reads `BankQuestion` first (then the reserved JSON bank table). Student generate maps `TF`→`true_false` and `MCQ`→`mcq`, keeps answer keys server-side until submit, and serves bank hits before AI. Teachers should set Excel **course** to the student chapter label (e.g. `Chapter 3: Enabling Technologies`) or the internal `chapter_id`. Matching / fill-blank / short-answer remain AI-only until those types are added to the upload template.

## Verification

From the core directory:

```bash
python -m pytest validation_tests -q
python -m pytest tests validation_tests -q
```

17 new tests cover template roundtrip, invalid rows/archives, answer mapping, duplicate IDs/options, formulas, atomic import, preview binding, CSRF, ownership/role isolation, course filtering, pagination and proxy prefixes. Browser verification completed the template download → file selection → preview → confirmation → list workflow with one TF and one MCQ in an isolated in-memory database.

The full run before two additional edge-case tests: 131 passed, 5 failed, 30 subtests passed. All five failures were separately reproduced on unchanged `origin/main` (000973b): teacher template hash expectations, standalone template rendering, one objective HTTP grading scenario, and two short-answer citation-count assertions. Those existing issues are outside this feature. MySQL/Docker and a production VM deployment have not been tested.

## Deployment and rollback

Before redeployment, shut down and **clone the Ubuntu VM**, verify the clone is available, and back up the database. Do not treat this local Git clone as a VM backup. Preserve real `.env` files on the VM. Merge only after group review, then follow the existing VM pull/build/up procedure. The migration service creates the new bank table before teacher startup. Do not run `docker compose down -v`.

Rollback: deploy the previous code commit; retain the new table so uploaded questions are preserved for a later fix. No existing records need to be removed. Test Home, student login/exercises and teacher uploads after deployment.
