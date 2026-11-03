function jsonData(form) {
  return Object.fromEntries(new FormData(form).entries());
}

class ApiError extends Error {
  constructor(message, data = {}, status = 0) {
    super(message);
    this.name = "ApiError";
    this.data = data || {};
    this.status = status;
  }
}

async function api(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body !== undefined && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const response = await fetch(url, { ...options, headers });
  let data;
  try { data = await response.json(); }
  catch (_error) { data = { ok: false, error: "The server returned an unreadable response." }; }
  if (!response.ok || !data.ok) throw new ApiError(data.error || "The request could not be completed.", data, response.status);
  return data;
}

function setFormMessage(form, text, isError = false) {
  const message = form.querySelector(".message");
  if (!message) return;
  message.textContent = text || "";
  message.classList.toggle("error-msg", Boolean(text) && isError);
  message.classList.toggle("status-message", Boolean(text) && !isError);
  message.classList.toggle("hidden", !text);
}

function bindJsonForm(selector, url) {
  const form = document.querySelector(selector);
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setFormMessage(form, "Working…");
    const submit = form.querySelector('button[type="submit"], button:not([type])');
    if (submit) submit.disabled = true;
    try {
      const data = await api(url, { method: "POST", body: JSON.stringify(jsonData(form)) });
      if (data.redirect) window.location.assign(data.redirect);
      else setFormMessage(form, "Saved.");
    } catch (error) {
      setFormMessage(form, error.message, true);
    } finally {
      if (submit) submit.disabled = false;
    }
  });
}

const logoutButton = document.querySelector("#logoutBtn");
if (logoutButton) {
  logoutButton.addEventListener("click", async () => {
    try {
      const data = await api("/logout", { method: "POST", body: "{}" });
      window.location.assign(data.redirect || "/login");
    } catch (_error) {
      window.location.assign("/login");
    }
  });
}

function makeNode(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function renderCitations(targetSelector, citations) {
  const target = document.querySelector(targetSelector);
  if (!target) return;
  target.replaceChildren();
  if (!Array.isArray(citations) || citations.length === 0) return;
  target.append(makeNode("h3", "Course-note references", "reference-heading"));
  citations.forEach((reference) => {
    const card = makeNode("article", undefined, "citation-card");
    const source = reference.source_file || "Course material";
    const section = reference.section || reference.chapter || "Section";
    const page = reference.page === undefined || reference.page === null ? "n/a" : reference.page;
    card.append(makeNode("strong", `${source} · ${section} · page ${page}`));
    card.append(makeNode("p", reference.excerpt || ""));
    target.append(card);
  });
}

function answerControl(quiz) {
  const form = makeNode("form", undefined, "answer-form");
  form.id = "answer-form";
  if (quiz.type === "true_false" || quiz.type === "mcq") {
    const grid = makeNode("div", undefined, "choice-grid");
    (quiz.options || []).forEach((option, index) => {
      const label = makeNode("label", undefined, "choice-btn");
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "answer";
      input.value = String(index);
      input.required = true;
      const marker = makeNode("span", String.fromCharCode(65 + index), "choice-marker");
      const text = makeNode("span", option, "choice-text");
      label.append(input, marker, text);
      grid.append(label);
    });
    form.append(grid);
  } else if (quiz.type === "matching") {
    const container = makeNode("div", undefined, "matching-form");
    (quiz.left || []).forEach((term, index) => {
      const row = makeNode("label", undefined, "matching-row");
      row.append(makeNode("strong", term, "matching-term"));
      const selectWrap = makeNode("span", undefined, "custom-select");
      const select = document.createElement("select");
      select.name = `match_${index}`;
      select.required = true;
      select.append(new Option("Choose a definition", ""));
      (quiz.right || []).forEach((definition, rightIndex) => select.append(new Option(definition, String(rightIndex))));
      selectWrap.append(select);
      row.append(selectWrap);
      container.append(row);
    });
    form.append(container);
  } else {
    const label = makeNode("label", quiz.type === "short_answer" ? "Your answer" : "Complete the blank", "field-label");
    const control = quiz.type === "short_answer" ? document.createElement("textarea") : document.createElement("input");
    control.name = "answer";
    control.required = true;
    control.autocomplete = "off";
    if (control.tagName === "TEXTAREA") control.rows = 5;
    label.append(control);
    form.append(label);
  }
  const submit = makeNode("button", "Submit answer", "btn-primary");
  submit.type = "submit";
  form.append(submit);
  return form;
}

function readStudentAnswer(form, quiz) {
  const values = new FormData(form);
  if (quiz.type === "matching") {
    return (quiz.left || []).map((_term, index) => {
      const value = values.get(`match_${index}`);
      return value === null || value === "" ? null : Number(value);
    });
  }
  return values.get("answer");
}

function formatReferenceAnswer(value) {
  if (Array.isArray(value)) return value.join("; ");
  if (value && typeof value === "object") return Object.entries(value).map(([key, item]) => `${key} → ${item}`).join("; ");
  return String(value ?? "");
}

function appendReviewRow(target, label, value, className = "") {
  if (value === undefined || value === null || String(value).trim() === "") return;
  const row = makeNode("p", undefined, className);
  row.append(makeNode("strong", `${label}: `), document.createTextNode(String(value)));
  target.append(row);
}

function resultCard(result) {
  const card = makeNode("section", undefined, `feedback-card ${result.is_correct ? "feedback-good" : "feedback-bad"}`);
  card.append(makeNode("h3", `${result.score}/100 · ${result.is_correct ? "Correct" : "Incorrect"}`));

  const review = result.review;
  if (review && typeof review === "object") {
    const reviewBlock = makeNode("div", undefined, "answer-review");
    reviewBlock.append(makeNode("h4", "Answer review"));
    appendReviewRow(reviewBlock, "Question type", review.question_type);
    appendReviewRow(reviewBlock, "Question", review.question, "answer-review-question");
    appendReviewRow(reviewBlock, "Your answer", review.student_answer);
    appendReviewRow(reviewBlock, "Result", review.result);
    appendReviewRow(reviewBlock, "Score", `${review.score}/100`);

    if (Array.isArray(review.options) && review.options.length) {
      const optionsTitle = makeNode("p", undefined, "answer-review-options-title");
      optionsTitle.append(makeNode("strong", "Options:"));
      reviewBlock.append(optionsTitle);
      const options = makeNode("ol", undefined, "answer-review-options");
      review.options.forEach((option) => {
        const item = makeNode("li", undefined, `${option.is_correct ? "answer-option-correct " : ""}${option.is_selected ? "answer-option-selected" : ""}`.trim());
        item.append(makeNode("span", `${option.label}. `, "answer-option-label"), document.createTextNode(option.text || ""));
        if (option.is_correct) item.append(makeNode("span", "Correct", "answer-option-tag"));
        if (option.is_selected) item.append(makeNode("span", "Your choice", "answer-option-tag"));
        options.append(item);
      });
      reviewBlock.append(options);
    }
    appendReviewRow(reviewBlock, review.correct_label || "Correct answer", review.correct_answer, "answer-review-correct");
    card.append(reviewBlock);
  }

  if (Array.isArray(result.key_point_results) && result.key_point_results.length) {
    const assessment = makeNode("div", undefined, "key-point-assessment");
    assessment.append(makeNode("h4", "Key-point assessment"));
    const list = makeNode("ol", undefined, "key-point-list");
    result.key_point_results.forEach((item) => {
      const row = makeNode("li");
      const credit = Number.isFinite(Number(item.credit)) ? Number(item.credit) : 0;
      row.append(
        makeNode("strong", `${credit}/100: `),
        document.createTextNode(item.comment || "No assessment comment was returned.")
      );
      list.append(row);
    });
    assessment.append(list);
    card.append(assessment);
  }

  if (result.feedback) {
    const row = makeNode("p", undefined, "feedback-detail");
    row.append(makeNode("strong", "Feedback: "), document.createTextNode(result.feedback));
    card.append(row);
  }
  const reviewAlreadyShowsAnswer = review && review.correct_answer;
  if (!reviewAlreadyShowsAnswer && result.correct_answer !== undefined && result.correct_answer !== null) {
    const row = makeNode("p", undefined, "reference-answer");
    row.append(makeNode("strong", "Reference answer: "), document.createTextNode(formatReferenceAnswer(result.correct_answer)));
    card.append(row);
  }
  if (result.explanation) {
    const row = makeNode("p", undefined, "feedback-explanation");
    row.append(makeNode("strong", "Explanation: "), document.createTextNode(result.explanation));
    card.append(row);
  }
  return card;
}

function setupExercise(quizType, autoGenerate = false, fixedTopic = "") {
  const generate = document.querySelector("#generate");
  const target = document.querySelector("#quiz");
  const status = document.querySelector("#status");
  const topicInput = document.querySelector("#topic");
  const citations = document.querySelector("#citations");
  if (!generate || !target || !status || !citations) return;

  let working = false;
  const topicValue = () => (fixedTopic || topicInput?.value || "").trim();
  const sleep = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));

  async function requestQuestion(topic) {
    // Multiple tabs or a duplicate auto-generate can reach the same server-side
    // session. The backend returns 202/pending while the original request owns
    // generation; poll for the saved result instead of starting another Agent call.
    for (let attempt = 0; attempt < 150; attempt += 1) {
      const data = await api("/api/exercise/generate", {
        method: "POST",
        body: JSON.stringify({ quiz_type: quizType, topic }),
      });
      if (!data.pending) return data;
      status.textContent = data.message || "The same question is still being generated…";
      status.className = "status-message";
      await sleep(Number(data.retry_after_ms) || 700);
    }
    throw new ApiError("Question generation did not finish within the safe wait period.", { can_skip: false }, 504);
  }

  function showSkipAction(error) {
    if (!error?.data?.can_skip) return;
    const actions = makeNode("div", undefined, "generation-recovery-actions");
    const retry = makeNode("button", "Retry generation", "btn-secondary");
    retry.type = "button";
    retry.addEventListener("click", generateQuestion);
    const skip = makeNode("button", `Skip unavailable ${document.body.dataset.quizLabel || quizType}`, "btn-secondary");
    skip.type = "button";
    skip.addEventListener("click", async () => {
      retry.disabled = true;
      skip.disabled = true;
      status.textContent = "Recording the unavailable stage and continuing…";
      status.className = "status-message";
      try {
        const result = await api("/api/exercise/skip", {
          method: "POST",
          body: JSON.stringify({ quiz_type: quizType }),
        });
        window.location.assign(result.redirect);
      } catch (skipError) {
        status.textContent = skipError.message;
        status.className = "error-msg";
        retry.disabled = false;
        skip.disabled = false;
      }
    });
    actions.append(retry, skip);
    target.replaceChildren(actions);
  }

  async function generateQuestion() {
    if (working) return;
    // Topic may be empty: backend prefers the teacher question bank, then AI.
    const topic = topicValue();
    working = true;
    generate.disabled = true;
    status.textContent = topic
      ? "Checking the teacher question bank, then generating if needed…"
      : "Checking the teacher question bank (no topic), else AI will generate…";
    status.className = "status-message";
    target.replaceChildren();
    citations.replaceChildren();
    try {
      const data = await requestQuestion(topic);
      if (topicInput) topicInput.disabled = true;
      const quiz = data.quiz;
      if (data.source_label) {
        const badge = makeNode("p", data.source_label, "quiz-source-label");
        if (data.generated_by_ai) badge.classList.add("quiz-source-ai");
        else if (data.from_question_bank) badge.classList.add("quiz-source-bank");
        target.append(badge);
      }
      target.append(makeNode("h2", quiz.question, "quiz-stem"));
      const form = answerControl(quiz);
      target.append(form);
      // References and answer keys remain hidden until after submission.
      status.textContent = data.diversity_notice || "";
      status.className = data.diversity_notice ? "status-message" : "status-message hidden";

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (working) return;
        const submit = form.querySelector('button[type="submit"]');
        const answer = readStudentAnswer(form, quiz);
        if (quiz.type === "matching" && answer.some((item) => item === null || Number.isNaN(item))) {
          status.textContent = "Match every term before submitting.";
          status.className = "error-msg";
          return;
        }
        working = true;
        if (submit) submit.disabled = true;
        try {
          const result = await api("/api/exercise/submit", {
            method: "POST",
            body: JSON.stringify({ quiz_id: data.quiz_id, answer }),
          });
          form.remove();
          target.append(resultCard(result));
          renderCitations("#citations", result.citations);
          if (result.completed) {
            status.textContent = "Quiz flow complete. You have finished all five question types.";
            status.className = "status-message";
            const actions = makeNode("div", undefined, "completion-actions");
            const history = makeNode("a", "View My History", "btn-primary");
            history.href = "/history";
            const restart = makeNode("a", "Start New Session", "btn-secondary");
            restart.href = "/exercise/setup";
            actions.append(history, restart);
            target.append(actions);
            generate.classList.add("hidden");
            return;
          }
          const nextLabel = document.body.dataset.nextLabel || "next step";
          if (result.is_correct) {
            status.textContent = "Correct. Moving to the next question type…";
          } else {
            status.textContent = `Incorrect. Generating another ${document.body.dataset.quizLabel || quizType} question…`;
          }
          status.className = "status-message";
          const continueLabel = result.is_correct ? "Continue to the next question type" : `Try another ${document.body.dataset.quizLabel || quizType} question`;
          const continueLink = makeNode("a", continueLabel, "btn-secondary feedback-continue");
          continueLink.href = result.redirect;
          target.append(continueLink);
          // Keep the reference answer and source page visible before following
          // the enforced sequence. The student may continue immediately.
          window.setTimeout(() => window.location.assign(result.redirect), 8000);
        } catch (error) {
          status.textContent = error.data?.recovery_hint ? `${error.message} ${error.data.recovery_hint}` : error.message;
          status.className = "error-msg";
          if (error.data?.can_skip) showSkipAction(error);
          else if (submit) submit.disabled = false;
        } finally {
          working = false;
        }
      });
    } catch (error) {
      status.textContent = error.data?.recovery_hint ? `${error.message} ${error.data.recovery_hint}` : error.message;
      status.className = "error-msg";
      showSkipAction(error);
      if (topicInput && !fixedTopic && !error.data?.can_skip) topicInput.disabled = false;
    } finally {
      working = false;
      generate.disabled = false;
    }
  }

  generate.addEventListener("click", generateQuestion);
  topicInput?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      generateQuestion();
    }
  });
  if (autoGenerate || fixedTopic) window.setTimeout(generateQuestion, 80);
}

function setupNotes() {
  const form = document.querySelector("#notes-form");
  const answer = document.querySelector("#notes-answer");
  if (!form || !answer) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setFormMessage(form, "Searching course notes…");
    const submit = form.querySelector('button[type="submit"]');
    if (submit) submit.disabled = true;
    try {
      const data = await api("/api/notes/ask", { method: "POST", body: JSON.stringify(jsonData(form)) });
      answer.classList.remove("hidden");
      const text = answer.querySelector(".answer-text");
      if (text) text.textContent = data.answer;
      renderCitations("#notes-citations", data.citations);
      setFormMessage(form, "");
    } catch (error) {
      setFormMessage(form, error.message, true);
    } finally {
      if (submit) submit.disabled = false;
    }
  });
}
