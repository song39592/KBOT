# Teacher chapter selection for question-bank imports

Upload course notes through the dashboard first. The Question bank import form loads the same Knowledge Service chapter IDs already used by student Session Setup. Select a chapter from the dropdown, upload the existing Excel template, validate, review and confirm. Labels include the source filename to distinguish similarly named chapters. The existing `course` column stores the selected chapter ID; no schema change is required.

Preview and confirmation both verify that the selected chapter still exists. The signed preview remains bound to teacher, chapter, file bytes and nonce. A deleted or forged chapter is rejected. If the Knowledge Service is unavailable, imports are blocked with a retry message while existing question listings remain visible. With no uploaded notes, the form explains that notes must be uploaded first.

Set the core `KNOWLEDGE_READ_KEY` to match the Knowledge Service, in addition to the existing admin key. Existing imports stored under old course names remain visible and are not automatically migrated. The student app and its generation behavior are unchanged by this patch.

## Included PR #3

This branch integrates PR #3 (`feature/teacher-bank-extended`, 9ad0ac6): Matching, Fill Blank and Short Answer templates, validation, preview, confirmation, detail storage and owner-scoped listing, alongside the existing TF/MCQ format. All five formats now use the chapter dropdown. The additive `teacher_bank_question_details` table is created by the existing teacher startup `db.create_all()`. This does not add student retrieval or automatic grading for the three extended types.

## Deployment

Back up the deployment and preserve real `.env` files and data volumes. After review and merge, update the checkout, verify the core read key, and rebuild/recreate only the teacher service:

```bash
sudo docker compose config --quiet
sudo docker compose build teacher
sudo docker compose up -d --no-deps teacher
sudo docker compose ps -a
```

Use the outer Compose file. Do not run `docker compose down -v`. This change does not deploy automatically or import files into the live VM. Roll back by rebuilding the previous code commit; no table or existing records need to be removed.

## Verification

Run `python -m pytest validation_tests tests/test_teacher_chapter_selection.py -q` from the core directory. Tests cover dropdown rendering, storing chapter IDs, readable preview/list labels, missing/forged/deleted chapters, Knowledge Service outages, empty chapter lists, and the existing file/owner/CSRF confirmation safeguards.

The five supplied ELEC6098 PDFs were parsed by the actual Knowledge Service and yielded five selectable chapter IDs (374 chunks total). Browser verification covered chapter selection, preview, confirmation and the resulting question list. Course files, credentials and generated knowledge stores are not included in the repository. Docker/MySQL deployment has not been run locally.

Final integrated regression: 188 passed, 5 failed, 30 subtests passed. The five failures match those individually reproduced on unchanged main df9e68f: two teacher template expectations/render-context checks, the pre-existing numeric-zero objective-answer integration failure, and two short-answer citation-count expectations. The student_app directory and student bank-first tests are byte-for-byte unchanged from main.
