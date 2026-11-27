"""Local-only Windows test server. Never uses the production database."""
import os
import argparse
from dotenv import dotenv_values
from pathlib import Path
import threading
from werkzeug.serving import make_server

parser = argparse.ArgumentParser()
parser.add_argument('--provider', choices=['coze','deepseek'], default='coze')
parser.add_argument('--ai', action='store_true', help='Enable real AI requests')
parser.add_argument('--ai-env', help='Read AI settings from this local env file')
args = parser.parse_args()
if args.ai_env:
    for key, value in dotenv_values(args.ai_env).items():
        if key.startswith(('COZE_', 'DEEPSEEK_', 'NOTES_AI_')) and value is not None:
            os.environ[key] = value
os.environ["NOTES_AI_PROVIDER"] = args.provider
os.environ["NOTES_AI_ENABLED"] = "true" if args.ai else "false"
root = Path(__file__).resolve().parent
state = root / "data" / "windows-notes-test"
state.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = "sqlite:///" + (state / "app.db").as_posix()
os.environ["SESSION_COOKIE_PATH"] = "/"
os.environ["SESSION_COOKIE_SECURE"] = "false"
os.environ["KBOT_ENV"] = "development"
os.environ["KNOWLEDGE_SERVICE_URL"] = "http://127.0.0.1:15100"
os.environ["KNOWLEDGE_DATA_DIR"] = str(state / "knowledge")
os.environ["KNOWLEDGE_CHROMA_ENABLED"] = "false"
os.environ["TEACHER_CHROMA_ENABLED"] = "false"
os.environ["KNOWLEDGE_ADMIN_KEY"] = "windows-local-admin"
os.environ["KNOWLEDGE_READ_KEY"] = "windows-local-read"
os.environ["APP_SECRET_KEY"] = "windows-local-test-only-secret"
os.environ["COZE_ENABLED"] = "true" if args.ai else "false"
from knowledge_service.app import create_app as knowledge_app
from teacher_app.app import create_app as teacher_app
from student_app.app import create_app as student_app
from common.models import db, User

if __name__ == "__main__":
    teacher = teacher_app({"LEGACY_TEACHER_COMPAT": False, "SEED_DEMO_TEACHER": False})
    with teacher.app_context():
        for username, role in [("notes_teacher", "teacher"), ("notes_student", "student")]:
            user = User.query.filter_by(username=username).first()
            if user is None:
                user = User(username=username, name=username, role=role)
                user.set_password("NotesTest2026!")
                db.session.add(user)
        db.session.commit()
    apps = [(knowledge_app(),15100), (teacher,15000), (student_app({"COZE_ENABLED":args.ai}),18000)]
    servers = []
    try:
        for app, port in apps:
            server = make_server("127.0.0.1", port, app, threaded=True)
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
        print("Teacher: http://127.0.0.1:15000  (notes_teacher)", flush=True)
        print("Student: http://127.0.0.1:18000  (notes_student)", flush=True)
        print("Local test password: NotesTest2026! | Ctrl+C to stop", flush=True)
        print("AI enabled: real AI requests." if args.ai else "AI disabled: local excerpts only.", flush=True)
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
