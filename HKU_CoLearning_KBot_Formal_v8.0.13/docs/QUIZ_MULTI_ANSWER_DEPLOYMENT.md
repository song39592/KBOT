# Multiple-answer MCQs and quiz repairs

This cumulative deployment change is based on main d040755 (including the merged
Notes improvements). It also carries the functionality of pending PRs #4, #5 and
#6 so the teacher chapter picker, chapter-wide generation retry, approximate bank
matching and objective feedback already used on the VM are retained. Do not
apply the old PR6 files over this version.

## Teacher and student behavior

- Download the objective Excel template. Keep the existing Questions headers.
  Use `B` for one correct answer or `A,C` for multiple correct answers.
  The extra **Multi-answer example** worksheet is a copyable example, not an
  automatically imported question; copy it into Questions before importing.
- MCQ options remain 2–5. Empty/nonexistent or duplicate answer letters fail
  validation. Answers are stored as compact letters (e.g. `AC`), within the
  existing VARCHAR(8) column, so no column migration is needed.
- Single-answer MCQs use radios. Multiple-answer MCQs use checkboxes and explicit
  instructions. All and only correct choices score 100; omissions or extra
  selections score 0 and are explained. No partial MCQ credit is awarded.
- Agent questions can use `correct_indices: [0, 2]`. Legacy `correct_index`
  questions remain supported. Correct indexes are never sent in public drafts.
- Kahoot export retains its existing single-answer format. Multiple-answer
  attempts are excluded rather than silently exported with one correct answer.

## Repairs from the browser test

1. Local MCQ recovery asks direct subject/property questions and uses bounded,
   same-subject distractors instead of document-section navigation or unrelated
   true facts. It may use 2–4 coherent choices when four cannot be constructed.
2. Matching recovery rejects years, navigation fragments and labels such as
   Wikipedia definition; term/definition pairs are grouped within a category.
   Teacher template type MATCHING now reaches student retrieval; legacy MATCH
   rows remain supported.
3. Feedback remains visible until the student clicks Continue / Try another.
4. History preserves feedback, explanations, pair review, source and citations.
5. Teacher imports/listing warn about missing or apparently unrelated
   explanations. Each owned row has **Review / correct explanation** with CSRF
   protection. Teachers must correct inaccurate bank data themselves; this
   lexical warning is advisory and cannot establish semantic accuracy. Edits
   affect future questions only, preserving the audit trail of saved attempts.
6. Feedback avoids duplicate explanation paragraphs and repeated punctuation.

## Validation

- New multi-answer/quality tests: 42 passed.
- Combined objective feedback, bank-selection and multi-answer tests: 72 passed.
- Full suite: 271 passed, 30 subtests passed, 5 failed.
- Unmodified main d040755: 195 passed, 30 subtests passed, the same 5 failures.
  These are the teacher login template hash, standalone teacher template
  rendering, an HTTP flow redirect expectation and two short-answer citation
  expectations. They are baseline limitations, not green checks.
- Chrome acceptance with an isolated SQLite database: Excel preview/import of
  A,C; legacy TF scoring; simultaneous MCQ selections; missing/extra choice
  feedback; feedback surviving beyond eight seconds; persisted History review.
  Production Coze and MySQL/VM deployment were not exercised by this change.

## VM rollout

Preserve the VM's real .env files, root docker-compose.yml, named data volumes and
custom teacher login page. Make a VM/database backup and save local tracked
changes before deployment. This PR does not modify those deployment files or
login page. Never use `down -v` or overwrite the database.

Fetch the reviewed PR head to a separate local ref, then inspect its diff from
the VM branch before merging. A VM that previously merged PR4/5/6 may have
conflicts in exercise.py or app.js: use the reviewed new application versions
for those quiz files while preserving VM-only deployment configuration. Do not
select ours/theirs globally. Run `git diff --check` and ensure no unresolved
paths remain before committing. If the merge is not understood, `git merge
--abort` leaves the saved pre-merge branch available.

After the code merge, from /home/teacher1/KBOT:

```bash
sudo docker compose -p kbot-v81-home up -d --build --no-deps student teacher
sudo docker compose -p kbot-v81-home ps
sudo docker compose -p kbot-v81-home logs --tail=60 student teacher
```

Both student and teacher need rebuilding: the template/editor lives in Teacher.
No new dependency, database column change or external Agent setting is required.
Restarting only Student would leave the old teacher template in place. The Notes
changes in main are preserved in code; rollout of any other services required by
those earlier changes should follow their own integration notes.

Smoke-test a newly downloaded template with A,C, a legacy single-answer question,
a wrong multi-answer selection and History. Correct one owned bank explanation
and verify another teacher cannot edit it. Check chapter/type isolation,
case/punctuation/typo matching, blank topics and an uploaded Matching question.
Keep screenshots and logs. For rollback, return to the saved VM commit and rebuild
the same two services; retain all database and knowledge volumes.
