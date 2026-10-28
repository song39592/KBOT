# Teacher question bank: five question types

Teachers can import True/False, single-answer MCQ, Matching, Fill Blank and Short Answer through the **Question bank** entry on the teacher dashboard. All types follow download template → upload → validate → preview → confirm → list. Course filtering and pagination remain available.

This extension is based on PR #2 (`feature/teacher-question-bank`, 70ab59c). It adds the three structured types without changing the existing TF/MCQ workbook contract. Review/merge the a-stage PR before moving this extension onto main. Neither main nor the production VM is changed by this work.

## Teacher workflow

1. Sign in to Teacher and open **Question bank**.
2. Download the appropriate template. The original **Download Excel template** is for TF/MCQ; three additional links provide Matching, Fill Blank and Short Answer templates.
3. Replace the example question rows AND their linked detail rows. Keep sheet names and headers exactly as supplied. The same question ID links a Questions row to its details.
4. Enter the course name/code, select the workbook, and **Validate & preview**.
5. Fix every reported issue. Errors include worksheet name and Excel row number. No questions are saved during preview.
6. Check all questions, options/items, standard answers and rubrics, then **Confirm import**.
7. Verify the question and its structured answer appear in **Your imported questions**.

Limits: .xlsx only; 5 MB compressed / 20 MB expanded; 1,000 Questions rows and 10,000 data rows per detail sheet. Formula cells are rejected. Course is a manually entered identifier, not a new course registry. Question IDs are unique within a teacher's course. In-file IDs are compared case-insensitively; database collation may impose stricter uniqueness. Existing questions are never overwritten; any failure rolls back the whole import.

## TF and MCQ: unchanged format

The original single Questions sheet uses:

`question_id, question_type, topic, question, option_A, option_B, option_C, option_D, option_E, correct_answer, explanation`

TF: type `TF`, answer TRUE/FALSE, empty options. Excel Boolean cells are accepted. MCQ: type `MCQ`, at least two distinct nonempty options, exactly one existing answer letter A–E. E remains optional; the system never automatically adds an option.

## Matching, Fill Blank and Short Answer

These templates use a Questions sheet with:

`question_id, question_type, topic, question, reference_answer, explanation`

Question ID, type and question text are required. Topic and explanation are optional. Reference answer is required for SHORT, and must be empty for MATCHING/FILL. A structured workbook may contain one or all three types, with the corresponding detail sheets. The original TF/MCQ format is imported separately.

### Matching

Type `MATCHING`. Add a Matching sheet with:

`question_id, left_id, left_text, right_id, right_text`

Each detail row describes one correct pair, e.g. `MAT001, L1, HTTP, R1, Transfer web content`. The downloaded template contains three protocol examples.

This version supports 2–20 one-to-one pairs per question. Every ID and text is required. Left IDs, right IDs, left texts and right texts must each be unique within the question (case-insensitive). IDs start with a letter and contain at most 40 letters, digits or underscores. Pair IDs and an explicit `correct_pairs` mapping are stored, so a later student UI can shuffle items without losing the answer relationship. Multi-to-one matching, extra distractors and partial scoring are outside this upload scope.

### Fill Blank

Type `FILL`. In the question, use 1–20 unique placeholders such as `{{blank1}}` and `{{blank2}}`. Each placeholder appears once. Add a Blanks sheet with:

`question_id, blank_id, accepted_answer, ignore_case`

For `DNS resolves names to {{blank1}} addresses using port {{blank2}}.`, use rows such as:

| question_id | blank_id | accepted_answer | ignore_case |
| --- | --- | --- | --- |
| FILL001 | blank1 | IP | TRUE |
| FILL001 | blank1 | Internet Protocol | TRUE |
| FILL001 | blank2 | 53 | FALSE |

Each blank needs at least one nonempty accepted answer, up to 20. Repeat rows for alternatives. The blank ID must match a placeholder exactly. TRUE/FALSE controls case matching and must be consistent across all alternatives for one blank. Surrounding whitespace is trimmed. Duplicate alternatives under that matching rule are rejected. Stored answers are grouped per blank rather than mixed into a single string. Numeric tolerance, unit conversion, mathematical equivalence and semantic grading are not implemented.

### Short Answer

Type `SHORT`. Supply a nonempty reference answer in Questions and a Rubric sheet with:

`question_id, criterion_id, criterion, points`

Each question needs 1–20 criteria. Criterion IDs are unique per question and follow the same 40-character ID rule as Matching. Criterion text is required. Points are positive decimal values with up to two decimal places; each criterion and the calculated total must not exceed 1000. Total score is computed from the criteria, not entered independently.

The downloaded TCP/UDP example has two 1-point criteria. The reference answer, criteria, individual points and total are all previewed and stored. This feature does **not** enable AI grading or exact-string grading of short answers; reliable grading is a separate deliverable.

## Routes and storage

Existing endpoints are retained (GET for the first two, POST for the last two):

- `/teacher/question-bank/`
- `/teacher/question-bank/template` (original TF/MCQ)
- `/teacher/question-bank/preview`
- `/teacher/question-bank/confirm`

Additional template query values: `?type=MATCHING`, `?type=FILL`, `?type=SHORT`. Local direct paths omit `/teacher`. `url_for` preserves the existing proxy prefix.

Teacher/admin authentication, owner-scoped lists, session CSRF checks and escaped display apply to every type. Confirmation binds the exact workbook bytes, teacher, course and latest preview, and expires after 30 minutes. Editing answers or course after preview requires a fresh preview. Existing answers are not exposed to other teachers or students.

`common.models.BankQuestion` continues to store the shared fields. A new additive `BankQuestionDetails` model creates `teacher_bank_question_details`, keyed by the bank's internal UUID. This avoids changing existing bank columns or packing structured answers into the 8-character objective-answer column. Structured questions have empty `options` and `correct_answer`; their private payload lives in `question.details.data`:

- MATCHING: `schema_version`, `pairs` (stable left/right IDs and texts), `correct_pairs`.
- FILL: `schema_version`, `blanks` (ID, accepted_answers, ignore_case, trim_whitespace).
- SHORT: `schema_version`, `reference_answer`, `rubric` (ID, criterion, decimal points as strings), `max_score` (decimal string).

`question_type` values are `TF`, `MCQ`, `MATCHING`, `FILL`, `SHORT`; source remains `teacher_upload`. The existing migration service and startup `db.create_all()` create the new table, preserving old rows. There are no new packages or configuration values.

The bank remains separate from course knowledge and is never ingested into the document vector index or teacher student-answer index. Student bank-first retrieval and grading are unchanged, and there is no dependency on the teammate's proposed `/question_bank/items` API. Later student integration must map type names, authorize courses and keep all standard answers server-side until submission.

## Verification

Run from the core directory:

```bash
python -m pytest validation_tests -q
python -m pytest tests validation_tests -q
```

54 question-bank tests pass: the original 17 and 37 extension cases. They cover workbook roundtrips, one-to-one pairing, orphan/missing detail rows, multi-blank answers, case settings, malformed placeholders, reference/rubric validation, decimal limits, formula rejection, mixed structured workbooks, file-bound confirmation, additive table creation preserving an existing TF row, escaping and ownership isolation. A corrupt-sheet input is checked as a 400 validation response.

A full run before the final two extension cases: 168 passed, 5 failed, 30 subtests passed. The five failures are the same previously reproduced on unchanged main 000973b: teacher template hash expectations, standalone template rendering, one HTTP objective-grading scenario and two short-answer citation-count assertions. No new full-suite failures appeared. After the final error-handling change, all 54 targeted tests passed.

Browser acceptance completed: all template links visible; a mixed workbook with one Matching, one two-blank Fill Blank and one rubric-based Short Answer was uploaded, previewed, confirmed, and displayed correctly in the owner-filtered list. The sample used an isolated local in-memory database. Docker/MySQL deployment and production VM operation have not been tested.

## Deployment and rollback

Before redeployment, shut down and **clone the Ubuntu VM**, verify the clone exists, and back up the database. A local Git clone is not a VM backup. Preserve real `.env` files and follow the team's pull/build/up procedure only after review and merge. Never run `docker compose down -v`.

The existing migrate service creates both bank tables before Teacher startup. Rollback to earlier code can retain the additive tables and uploaded data. Earlier TF/MCQ-only UI will not display the new structured answer details; those data remain available when the extension is restored. Test Home, existing student login/exercises, teacher TF/MCQ upload and the three new upload flows after any deployment.
