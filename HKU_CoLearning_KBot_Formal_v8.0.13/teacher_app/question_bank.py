"""Authenticated teacher question-bank import with file-bound confirmation."""
import hashlib
import io
import secrets
from functools import wraps
from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file, session, url_for
from flask_login import current_user, login_required
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from openpyxl import Workbook
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from common.models import BankQuestion, BankQuestionDetails, db
from common.question_bank import HEADERS, MAX_FILE_BYTES, parse_workbook
from common.question_bank_extended import QUESTION_HEADERS, DETAIL_HEADERS

bp = Blueprint("question_bank", __name__, url_prefix="/question-bank")


@bp.before_request
def limit_upload():
    if request.method == "POST":
        if request.content_length and request.content_length > 6 * 1024 * 1024:
            return jsonify(error="Maximum request size is 6 MB."), 413
        # Flask 3.1 also enforces this for streams with no Content-Length.
        request.max_content_length = 6 * 1024 * 1024


def teacher(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role not in ("teacher", "admin"):
            abort(403)
        if request.method == "POST":
            token = session.get("bank_csrf")
            if not token or not secrets.compare_digest(request.form.get("csrf", "").encode("utf-8"), token.encode("utf-8")):
                abort(403)
        return view(*args, **kwargs)
    return wrapped


def signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="teacher-bank-import-v1")


def read_upload():
    course = request.form.get("course", "").strip()
    uploaded = request.files.get("file")
    if not course or len(course) > 160:
        raise ValueError("Enter a course name/code of 1–160 characters.")
    if not uploaded or not uploaded.filename.lower().endswith(".xlsx"):
        raise ValueError("Select an .xlsx workbook.")
    content = uploaded.stream.read(MAX_FILE_BYTES + 1)
    try:
        questions, errors = parse_workbook(content)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Invalid workbook contents. Download a fresh template and paste values only.") from exc
    existing = {q.question_id.casefold() for q in BankQuestion.query.filter_by(owner_id=current_user.id, course=course).all()}
    for question in questions:
        if question["question_id"].casefold() in existing:
            errors.append({"row": question["row"], "errors": ["This question ID already exists in this course."]})
    return course, content, questions, errors


@bp.get("/")
@teacher
def index():
    session.setdefault("bank_csrf", secrets.token_hex(32))
    page = max(1, request.args.get("page", 1, type=int))
    course = request.args.get("course", "").strip()
    query = BankQuestion.query.filter_by(owner_id=current_user.id)
    courses = [row[0] for row in db.session.query(BankQuestion.course).filter_by(owner_id=current_user.id).distinct().order_by(BankQuestion.course).all()]
    if course:
        query = query.filter_by(course=course)
    pagination = query.order_by(BankQuestion.created_at.desc(), BankQuestion.id).paginate(page=page, per_page=25, error_out=False)
    return render_template("question_bank.html", pagination=pagination, courses=courses, course=course, csrf=session["bank_csrf"])


@bp.get("/template")
@teacher
def template():
    book = Workbook()
    sheet = book.active
    sheet.title = "Questions"
    sheet.append(HEADERS)
    sheet.append(["TF001", "TF", "HTTP", "HTTP is stateless.", "", "", "", "", "", "TRUE", "HTTP itself does not retain session state."])
    sheet.append(["MCQ001", "MCQ", "DNS", "Which protocol resolves domain names?", "HTTP", "DNS", "SMTP", "FTP", "", "B", "DNS resolves domain names."])
    sheet.freeze_panes = "A2"
    kind = request.args.get("type", "OBJECTIVE").upper()
    if kind not in ("OBJECTIVE", "MATCHING", "FILL", "SHORT"):
        return jsonify(error="Template type must be OBJECTIVE, MATCHING, FILL or SHORT."), 400
    if kind != "OBJECTIVE":
        book.remove(sheet)
        sheet = book.create_sheet("Questions")
        sheet.append(QUESTION_HEADERS)
        if kind == "MATCHING":
            sheet.append(["MAT001", "MATCHING", "Protocols", "Match each protocol to its purpose.", "", "Each protocol has one purpose."])
            detail = book.create_sheet("Matching")
            detail.append(DETAIL_HEADERS["Matching"])
            for values in [("L1", "HTTP", "R1", "Transfer web content"), ("L2", "DNS", "R2", "Resolve domain names"), ("L3", "SMTP", "R3", "Send email")]:
                detail.append(["MAT001", *values])
        elif kind == "FILL":
            sheet.append(["FILL001", "FILL", "DNS", "DNS resolves domain names to {{blank1}} addresses using port {{blank2}}.", "", "DNS uses port 53."])
            detail = book.create_sheet("Blanks")
            detail.append(DETAIL_HEADERS["Blanks"])
            detail.append(["FILL001", "blank1", "IP", "TRUE"])
            detail.append(["FILL001", "blank1", "Internet Protocol", "TRUE"])
            detail.append(["FILL001", "blank2", "53", "FALSE"])
        else:
            sheet.append(["SA001", "SHORT", "TCP and UDP", "Explain two differences between TCP and UDP.", "TCP is connection-oriented and provides reliable ordered delivery; UDP is connectionless and does not guarantee delivery or order.", "Award points according to the rubric."])
            detail = book.create_sheet("Rubric")
            detail.append(DETAIL_HEADERS["Rubric"])
            detail.append(["SA001", "C1", "TCP is connection-oriented; UDP is connectionless.", 1])
            detail.append(["SA001", "C2", "TCP provides reliable ordered delivery; UDP does not guarantee it.", 1])
    for worksheet in book.worksheets:
        worksheet.freeze_panes = "A2"
        for column in worksheet.columns:
            worksheet.column_dimensions[column[0].column_letter].width = 24
    for col in sheet.columns:
        sheet.column_dimensions[col[0].column_letter].width = 24
    output = io.BytesIO()
    book.save(output)
    book.close()
    output.seek(0)
    return send_file(output, as_attachment=True, download_name=f"question_bank_{kind.lower()}_template.xlsx")


@bp.post("/preview")
@teacher
def preview():
    session.pop("bank_preview_nonce", None)
    try:
        course, content, questions, errors = read_upload()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    token = None
    if not errors:
        nonce = secrets.token_hex(16)
        session["bank_preview_nonce"] = nonce
        token = signer().dumps(dict(owner=current_user.id, course=course, digest=hashlib.sha256(content).hexdigest(), nonce=nonce))
    return render_template("question_bank_preview.html", course=course, questions=questions, errors=errors, token=token)


@bp.post("/confirm")
@teacher
def confirm():
    try:
        approved = signer().loads(request.form.get("preview_token", ""), max_age=1800)
    except (BadSignature, SignatureExpired):
        return jsonify(error="Preview expired or missing. Validate the file again."), 400
    try:
        course, content, questions, errors = read_upload()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if errors:
        return jsonify(error="Correct all errors and preview again.", errors=errors), 400
    if (approved.get("owner") != current_user.id or approved.get("course") != course
            or approved.get("digest") != hashlib.sha256(content).hexdigest()
            or not session.get("bank_preview_nonce")
            or approved.get("nonce") != session.get("bank_preview_nonce")):
        return jsonify(error="The file or course changed. Validate and preview again."), 400
    try:
        for question in questions:
            fields = {key: value for key, value in question.items() if key not in ("row", "details")}
            item = BankQuestion(owner_id=current_user.id, course=course, **fields)
            if "details" in question:
                item.details = BankQuestionDetails(data=question["details"])
            db.session.add(item)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(error="A question ID already exists. No questions imported."), 409
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Question bank import failed")
        return jsonify(error="Database import failed. No questions imported; please try again."), 500
    session.pop("bank_preview_nonce", None)
    return jsonify(message=f"Imported {len(questions)} questions.", count=len(questions), redirect=url_for("question_bank.index", course=course))
