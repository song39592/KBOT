CREATE DATABASE IF NOT EXISTS edu_qa CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE edu_qa;

CREATE TABLE IF NOT EXISTS users (
  id INT AUTO_INCREMENT PRIMARY KEY, username VARCHAR(80) NOT NULL UNIQUE, password_hash VARCHAR(256) NOT NULL,
  name VARCHAR(80) NOT NULL DEFAULT '', role VARCHAR(20) NOT NULL DEFAULT 'student', created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_users_username(username), INDEX idx_users_role(role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS student_qa_records (
  id VARCHAR(64) PRIMARY KEY, student_name VARCHAR(128) NOT NULL, chapter VARCHAR(256) NOT NULL DEFAULT '', chapter_id VARCHAR(160) NOT NULL DEFAULT '',
  question_type VARCHAR(32) NOT NULL, topic VARCHAR(256) NOT NULL DEFAULT '', question TEXT NOT NULL, answer TEXT NOT NULL,
  options_or_pairs TEXT, correct_answer TEXT, is_correct TINYINT(1) NOT NULL DEFAULT 0, score INT NOT NULL DEFAULT 0,
  ai_feedback TEXT, teacher_feedback TEXT, explanation TEXT, generation_source VARCHAR(32) NOT NULL DEFAULT 'local_fallback',
  grading_source VARCHAR(32) NOT NULL DEFAULT 'local', source VARCHAR(32) NOT NULL DEFAULT 'local_fallback', citations_json TEXT NOT NULL,
  source_document VARCHAR(255) NOT NULL DEFAULT '', source_page INT NULL, source_pages_json TEXT NOT NULL, source_excerpt TEXT,
  source_chunk_id VARCHAR(160) NOT NULL DEFAULT '', knowledge_key VARCHAR(160) NOT NULL DEFAULT '', fact_key VARCHAR(80) NOT NULL DEFAULT '',
  fact_summary VARCHAR(500) NOT NULL DEFAULT '', question_fingerprint VARCHAR(80) NOT NULL DEFAULT '', diversity_exhausted TINYINT(1) NOT NULL DEFAULT 0,
  teacher_score INT NULL, teacher_verified TINYINT(1) NOT NULL DEFAULT 0, verified_by VARCHAR(128) NOT NULL DEFAULT '',
  verified_at VARCHAR(32) NOT NULL DEFAULT '', time VARCHAR(32) NOT NULL DEFAULT '', update_time VARCHAR(32) NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_student(student_name), INDEX idx_chapter_id(chapter_id), INDEX idx_question_type(question_type),
  INDEX idx_source_chunk(source_chunk_id), INDEX idx_knowledge_key(knowledge_key), INDEX idx_fact_key(fact_key), INDEX idx_created_at(created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS teacher_question_bank (
  id VARCHAR(64) PRIMARY KEY, chapter_id VARCHAR(160) NOT NULL DEFAULT '', chapter VARCHAR(256) NOT NULL DEFAULT '',
  question_type VARCHAR(32) NOT NULL, topic VARCHAR(256) NOT NULL DEFAULT '', question TEXT NOT NULL,
  options_or_pairs TEXT NOT NULL, correct_answer TEXT NOT NULL, explanation TEXT NOT NULL,
  question_fingerprint VARCHAR(80) NOT NULL DEFAULT '', citations_json TEXT NOT NULL,
  source_document VARCHAR(255) NOT NULL DEFAULT '', source_page INT NULL, source_excerpt TEXT NOT NULL,
  source_chunk_id VARCHAR(160) NOT NULL DEFAULT '', knowledge_key VARCHAR(160) NOT NULL DEFAULT '',
  fact_key VARCHAR(80) NOT NULL DEFAULT '', fact_summary VARCHAR(500) NOT NULL DEFAULT '',
  created_by VARCHAR(128) NOT NULL DEFAULT '', is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_tqb_chapter(chapter_id), INDEX idx_tqb_type(question_type),
  INDEX idx_tqb_fp(question_fingerprint), INDEX idx_tqb_active(is_active), INDEX idx_tqb_created(created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS notes_qa_records (
  id VARCHAR(64) PRIMARY KEY, student_name VARCHAR(128) NOT NULL, chapter_id VARCHAR(160) NOT NULL DEFAULT '', chapter VARCHAR(256) NOT NULL DEFAULT '',
  question TEXT NOT NULL, answer TEXT NOT NULL, citations_json TEXT NOT NULL, insufficient_information TINYINT(1) NOT NULL DEFAULT 0,
  answering_source VARCHAR(32) NOT NULL DEFAULT 'local_fallback', source VARCHAR(32) NOT NULL DEFAULT 'local_fallback',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, INDEX idx_notes_student(student_name), INDEX idx_notes_created(created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS quiz_sessions (
  id VARCHAR(64) PRIMARY KEY, student_name VARCHAR(128) NOT NULL, chapter_id VARCHAR(160) NOT NULL DEFAULT '',
  used_knowledge_json TEXT NOT NULL, used_chunks_json TEXT NOT NULL, used_pages_json TEXT NOT NULL, used_facts_json TEXT NOT NULL,
  used_fingerprints_json TEXT NOT NULL, current_quiz_json MEDIUMTEXT NOT NULL, generation_failures_json TEXT NOT NULL,
  skipped_types_json TEXT NOT NULL, exhausted_count INT NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_session_student(student_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

