# Notes 模块优化与 Windows 测试

本次 Notes 改动已整合到远程 main 的 708a805 基线，开发分支为 feature/notes-improvements。保留教师扩展题库与学生优先使用题库的功能；老师 VM 尚未更新。完整整合验证记录见 README_NOTES_INTEGRATION.md。后续章节保留开发过程说明，以最新整合记录为准。

## 功能与统计口径

- 学生对自己的 Notes 答案提交错误、不完整、引用错误或其他反馈，描述 3–2000 字；每个问答允许一次反馈。
- 教师 Dashboard 右下角新增 Notes feedback & Top 10 入口。可查看原问题、原答案、引用、反馈，并标记已处理。支持近 7/30/90 天、章节、关键词和处理状态筛选，反馈每页 25 条。
- TOP10 按学生填写的 Knowledge point 字段统计，忽略大小写及多余空白；每次保存的问答计一次（包含未找到答案），另统计独立学生人数。不是自动关键词抽取，不自动合并 DNS 和 Domain Name System。历史问答没有新详情记录，不追溯统计。处理状态筛选只影响反馈列表。
- 答案按段落区分 Course notes 与 Outside course materials — supplementary explanation。课件段带引用编号，卡片展示相同编号、文件、章节、页码与原文；课外说明不带课件引用。
- Agent 未返回新结构或课件段没有合法引用时回退本地摘录。编号合法不等于引用已被自动验证真实支持结论，仍需要人工核验。

## 修改文件

核心路径相对于 HKU_CoLearning_KBot_Formal_v8.0.13/：

| 文件 | 内容 |
|---|---|
| common/models.py | 新增 NotesDetail、NotesFeedback、NotesCorrection、NotesBookmark 四张表 |
| common/notes_engine.py | 结构化提示词、分段引用、课外标记、输出校验与回退 |
| student_app/blueprints/notes.py | 保存知识点和分段，返回问答 ID，反馈接口及所有权校验 |
| student_app/templates/notes.html | 知识点输入、反馈表单、分段答案容器 |
| student_app/static/app.js | 分段显示、引用编号、反馈提交和状态 |
| teacher_app/notes_review.py（新增） | 聚合 TOP10、反馈筛选分页、教师权限与处理请求 CSRF 校验 |
| teacher_app/templates/notes_review.html（新增） | 教师查询页面，英文界面、内容自动转义 |
| teacher_app/app.py | 注册 Notes 管理蓝图 |
| teacher_app/templates/dashboard.html | 添加管理入口 |
| run_notes_windows.py（新增） | 本地三服务、独立 SQLite、测试账号，仅监听 127.0.0.1 |
| tests/test_notes_improvements.py（新增） | 权限、重复反馈、排行、处理状态、提问落库与引用测试 |

仓库根目录修改 .gitignore，新增 START_NOTES_WINDOWS.ps1 和本文档。

## Windows 启动

当前电脑可在 PowerShell 执行（已运行时不要重复启动）：

```powershell
cd C:\Users\YangJinSong\Desktop\Co_Learning_KBot_Final
.\.integration-venv\Scripts\python.exe .\KBOT\HKU_CoLearning_KBot_Formal_v8.0.13\run_notes_windows.py
```

其他电脑安装 Python 3.11 后，在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\START_NOTES_WINDOWS.ps1
```

首次创建 .notes-venv 并安装 requirements.txt。若安装中断，修复网络后重新手动安装依赖再运行。前台运行时 Ctrl+C 停止。

| 页面 | 地址 | 账号 |
|---|---|---|
| 教师 | http://127.0.0.1:15000 | notes_teacher |
| 学生 | http://127.0.0.1:18000 | notes_student |
| 教师反馈 | http://127.0.0.1:15000/notes-review | 教师登录后访问 |

两个测试账号密码均为 `NotesTest2026!`，仅用于本地测试。

启动脚本默认关闭 Coze 和向量索引，使用本地词汇检索/摘录，不调用外部模型。数据库为核心目录 data/windows-notes-test/app.db，课件也保存在该独立测试目录。真实 .env、API token、模型文件均未修改。此次直接访问学生/教师服务，不启动 Home。不要将开发启动脚本用于公网部署。

## 验收步骤

1. 教师登录，通过原有课件管理上传英文 TXT/PDF/DOCX。
2. 学生打开 Notes，选择章节，知识点填 DNS，提问 What is DNS?
3. 查看答案中的课件标记和引用编号、页码、原文。
4. 提交答案反馈，教师进入 Notes 页面查看，标记已处理，再切换 resolved 查看。
5. 多问几个知识点，核对排行、提问次数、独立学生人数和筛选功能。
6. 本地兜底只摘录课件，不生成课外补充，因此正常不会出现课外说明段。分段逻辑已用模拟 Agent 输出测试；真实 Coze 回答质量需要另行联调。

已验证：8 项新增功能及现有 Agent/迁移自动测试；实际 HTTP 上传、登录、提问、引用、反馈及教师排行联通检查。测试数据中已有 network_notes.txt 和 DNS 问答反馈。未完成 MySQL 和真实 Coze 联调，也未宣称全项目测试通过。

## 后续更新 VM

先通过 PR 合并并备份数据库。新表由现有应用启动时 db.create_all 创建，数据库账号需有建表权限；不改已有表字段。拉取后重新构建、启动 student 和 teacher。保留原 .env、数据卷、Home、Tailscale，不执行 down -v。

新详情表存知识点与分段 JSON，反馈表存状态和处理人。筛选时间采用 UTC。当前教师可查看全平台反馈；多课程租户隔离不在本次范围。

## 教师悬浮入口调整

修改 teacher_app/templates/dashboard.html：Notes feedback & Top 10 支持鼠标及触摸拖动，超过 6 像素视为拖动并阻止误跳转；普通点击仍打开页面。位置保存在当前浏览器 localStorage，刷新后保留；窗口缩小时限制在可见范围。刷新教师 Dashboard 即可使用。

## Windows 测试完成后，如何合并并部署到老师服务器

### A. 这次先处理 Windows 仓库的旧历史

当前本地 KBOT 的基线仍为 815934d；老师 VM 上传的 main 曾替换为 000973b，之后可能还有同学的新提交。不要强推，不要用 allow-unrelated-histories 硬合并。

在 Windows PowerShell 新建一份最新仓库（KBOT-next 目录必须不存在）：

```powershell
cd C:\Users\YangJinSong\Desktop\Co_Learning_KBot_Final
git clone https://github.com/song39592/KBOT.git KBOT-next
cd KBOT-next
git lfs install
git lfs pull
git switch -c feature/notes-feedback-top10
```

从旧 KBOT 复制本文“修改文件”表列出的文件，以及 START_NOTES_WINDOWS.ps1、README_NOTES_UPDATE.md 到 KBOT-next 的同名位置。不要复制 .git、真实 .env、data、虚拟环境或整个旧项目覆盖新仓库。新仓库 .gitignore 只追加一行 `.notes-venv/`，不要覆盖同学的规则。

如果远程版本中这些文件已有同学修改，先对比并合并内容，不能直接覆盖他的修改。复制后在 KBOT-next 再运行一次测试和 Windows 验收，旧目录测试成功不代表合并后的版本一定正常。

此后都在最新仓库的开发分支上开发，正常使用 git pull --ff-only 更新 main 后新建功能分支。

### B. 提交 Pull Request

```powershell
git status
git diff
```

确认差异只包含本次功能，且没有真实密码、token、运行数据后：

```powershell
git add .
git diff --cached --stat
git commit -m "Add Notes feedback, topic ranking and cited answer sections"
git push -u origin feature/notes-feedback-top10
```

到 GitHub 创建 PR，目标为 main，写明功能、测试结果、新增表和部署步骤。组员审核后合并。GitHub 合并不会自动更新服务器，下面需要部署负责人操作。

### C. 老师服务器更新前检查

远程连接老师电脑，进入 Ubuntu VM 的终端；以下全部是 Ubuntu 命令，不在 Windows PowerShell 执行。一次只安排一人部署。

```bash
cd ~/KBot_v81_Core_Home
git status --short
git branch --show-current
sudo docker compose -p kbot-v81-home ps -a
df -h /
```

工作区必须干净、分支为 main。若有未提交改动，先核对保存，不能 reset --hard。磁盘需要足够存放代码、课件、数据库和旧镜像的备份。

下面会短暂停服以保证课件和数据库备份对应。应先告知使用者，并暂停其他直接写库的任务。MySQL 容器保持运行，Tailscale 不动。

### D. 完整备份（更新前）

整段复制执行。脚本失败会停止；如果失败发生在停服之后，请先排除问题或用 `sudo docker compose -p kbot-v81-home start knowledge teacher student home-app` 恢复旧服务，不能继续部署。

```bash
bash <<'BACKUP'
set -euo pipefail
cd "$HOME/KBot_v81_Core_Home"
test -z "$(git status --porcelain)"
umask 077
stamp=$(date +%Y%m%d-%H%M%S)
backup="$HOME/kbot-backups/$stamp"
mkdir -p "$backup"
printf '%s\n' "$backup" > "$HOME/kbot-last-backup-path.txt"
git rev-parse HEAD > "$backup/commit.txt"
sudo docker compose -p kbot-v81-home ps -a > "$backup/containers.txt"
# 保存完整源目录及 .env，包含模型；排除本地测试数据、缓存与依赖环境。
tar --exclude='./data' --exclude='./.notes-venv' --exclude='./node_modules' \
  --exclude='*/__pycache__' -czf "$backup/source-and-config.tar.gz" .
# 给当前镜像建立独立标签，避免新版构建覆盖镜像名后无法快速回退。
: > "$backup/rollback-images.yml"
printf 'services:\n' >> "$backup/rollback-images.yml"
images=()
for service in mysql knowledge migrate teacher student home-app; do
  cid=$(sudo docker compose -p kbot-v81-home ps -a -q "$service")
  test -n "$cid"
  image=$(sudo docker inspect --format '{{.Image}}' "$cid")
  tag="kbot-rollback-$service:$stamp"
  sudo docker tag "$image" "$tag"
  images+=("$tag")
  printf '  %s:\n    image: %s\n' "$service" "$tag" >> "$backup/rollback-images.yml"
done
sudo docker image save "${images[@]}" > "$backup/images.tar"
sudo docker compose -p kbot-v81-home stop home-app teacher student knowledge
# 使用容器内已有环境变量，不在终端打印密码。
sudo docker compose -p kbot-v81-home exec -T mysql sh -c \
  'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump -uroot --single-transaction --routines --triggers --events --no-tablespaces --set-gtid-purged=OFF --databases "$MYSQL_DATABASE"' \
  > "$backup/database.sql"
test -s "$backup/database.sql"
cid=$(sudo docker compose -p kbot-v81-home ps -a -q knowledge)
mkdir "$backup/knowledge"
sudo docker cp "$cid:/app/data/knowledge/." "$backup/knowledge/"
sudo chown -R "$(id -u):$(id -g)" "$backup"
chmod -R go-rwx "$backup"
tar -tzf "$backup/source-and-config.tar.gz" >/dev/null
(cd "$backup" && sha256sum database.sql images.tar source-and-config.tar.gz > SHA256SUMS)
printf 'Backup complete: %s\n' "$backup"
BACKUP
```

备份在 `~/kbot-backups/时间戳/`，包含真实配置、账户数据和课件，必须保密，不上传公开仓库。校验和与文件非空检查不能替代恢复演练；重要上线前，应在隔离 VM 验证备份能恢复。source 备份排除了目录内 data；当前生产知识数据保存在 Compose volume，已单独复制，MySQL 已逻辑导出。

### E. 拉取、构建、迁移与启动

逐段执行，任何一步失败就停下，不要继续。

```bash
cd ~/KBot_v81_Core_Home
git pull --ff-only origin main
git lfs pull
sudo docker compose -p kbot-v81-home config --quiet
sudo docker compose -p kbot-v81-home build
```

确认构建成功后，显式运行迁移（不能只依赖之前 Exited(0) 的旧 migrate 容器）：

```bash
sudo docker compose -p kbot-v81-home up -d mysql
sudo docker compose -p kbot-v81-home run --rm --no-deps migrate
```

迁移成功后启动：

```bash
sudo docker compose -p kbot-v81-home up -d
sudo docker compose -p kbot-v81-home ps -a
sudo docker compose -p kbot-v81-home logs --tail=100 student teacher knowledge migrate
```

本次新增 notes_details、notes_feedback 表，不修改已有表字段，已有学生记录保留。未来功能若涉及删列、改类型或数据转换，必须单独编写和审核迁移，不能套用“重启即完成”的假设。

### F. 上线验证

打开 https://kbot-v81.tail2bf570.ts.net/ ，验证 Home、学生登录、课件问答、引用、反馈、教师 TOP10 和已处理筛选。MySQL/knowledge 应 healthy，常驻服务应 Up，migrate 正常完成为 Exited(0)。检查旧账号、已有课件、历史记录仍可访问。将部署时间、提交号、备份路径和结果记录在小组部署记录中。

保持项目名 kbot-v81-home 不变，避免连接到新建空数据卷；不执行 down -v、volume prune 或清理旧镜像。Tailscale 在 ~/kbot-tailscale，本次应用更新不需要重启或重新授权它。

### G. 更新失败如何回退

先保存报错日志，不要删数据库或反复强制迁移。

如果只是构建失败，旧容器尚未替换，可以先恢复旧服务：

```bash
cd ~/KBot_v81_Core_Home
sudo docker compose -p kbot-v81-home start knowledge teacher student home-app
```

注意：这只是恢复旧容器，磁盘源码可能已是新版，记录下来后再处理。

如果新容器已经替换，可用备份源代码和镜像启动旧版。以下仅适用于数据库仍兼容旧版的情况；本次只是加表，旧版通常可以忽略新表，不需要删除新表或恢复旧数据库。

```bash
backup=$(cat "$HOME/kbot-last-backup-path.txt")
cat "$backup/commit.txt"
(cd "$backup" && sha256sum -c SHA256SUMS)
mkdir "$backup/rollback-source"
tar -xzf "$backup/source-and-config.tar.gz" -C "$backup/rollback-source"
sudo docker image load -i "$backup/images.tar"
cd ~/KBot_v81_Core_Home
sudo docker compose -p kbot-v81-home stop home-app teacher student knowledge
cd "$backup/rollback-source"
sudo docker compose -p kbot-v81-home \
  -f docker-compose.yml -f "$backup/rollback-images.yml" \
  up -d --no-build --no-deps knowledge teacher student home-app
```

保持同一项目名以复用原数据库和知识卷。回退启动后暂时不要在原目录执行 up，否则会再次切回新版；先统一小组的代码修复与后续部署方案。

如果迁移已经破坏旧版兼容性，必须停写，先额外保存失败后的数据库与课件，再由部署负责人按该次迁移的恢复方案导入 database.sql 并恢复对应 knowledge 备份。恢复到备份会丢失备份之后的新数据，不能未经核对直接导入或清空数据卷。建议先在隔离 VM 恢复验证后再操作生产服务器。

## 四种证据状态优化

修改 common/notes_engine.py、student_app/blueprints/notes.py、student_app/static/app.js 和 tests/test_notes_improvements.py。支持 supported（完整支持）、partial（部分支持并说明缺失）、clarify（请求澄清）、unsupported（未检索到支持证据）。通用知识用默认收起的 details 展示，用户点击后查看；这是展示开关，不是点击后才调用模型。

模型按检索片段判断证据状态，不代表穷尽全部课件，也不是自动事实核验。纯本地摘录无法判断完整性，保守标记 partial；找不到内容标记 unsupported；少量明确的含糊短句规则请求澄清。无模型时不生成通用知识。缺失信息由模型给出，本地只能提示完整性未验证。原问答 answer 保存状态说明，未新增数据库字段。

## 开启真实 AI 测试

启动脚本支持 --ai 和 --ai-env。只从指定文件读取 COZE_* 配置，仍使用独立 SQLite、测试课件和本地端口，不改真实 .env。

```powershell
cd C:\Users\YangJinSong\Desktop\Co_Learning_KBot_Final
.\.integration-venv\Scripts\python.exe .\KBOT\HKU_CoLearning_KBot_Formal_v8.0.13\run_notes_windows.py --ai --ai-env .\release\KBot_v81_Core_Home\HKU_CoLearning_KBot_Formal_v8.0.13\.env
```

启动前先停止旧的本地测试服务。--ai 开启真实 Coze 请求，会使用现有凭据及配额；未加参数仍是离线模式。2026-11-17 实测开关与 Notes 配置有效，但 Coze 调用失败，最终回答来源为 local_fallback，不能认定真实 AI 已联调成功。

## 教师纠错、历史收藏与引用定位

新增 NotesCorrection 和 NotesBookmark 两表，启动时创建，不修改已有字段。教师处理反馈时必须填写修正或解释；原答案不被覆盖，回复保留教师与时间。教师和学生均可查看回复。学生 Notes history & saved 入口支持问题搜索、20条分页、收藏筛选、收藏/取消。所有历史、收藏和引用接口按当前学生校验问答所有权。

本次修改 common/models.py、common/notes_engine.py、student_app/blueprints/notes.py、student_app/static/app.js、student_app/templates/base.html、teacher_app/notes_review.py、teacher_app/templates/notes_review.html、knowledge_service/app.py、tests/test_notes_improvements.py；新增 student_app/templates/notes_history.html、student_app/templates/notes_reference.html。

新引用保存 document_id，点击 Open source & highlight 展示引用页的解析文本并高亮匹配摘录；原文件入口对 PDF 使用 #page=页码，对 DOCX/TXT 提供下载。高亮是在解析文本中，不是在原版 PDF 页面上；未匹配时明确展示保存的摘录，旧引用没有 document_id 时不能打开原文件。文件已删除或服务不可用时显示错误，历史答案保留。DOCX/TXT 的页号可能是解析器的逻辑分段，不保证等于 Word 打印页码。

知识服务新增带读取密钥验证的文档页/文件接口，经学生后端代理，密钥不发送到浏览器。当前沿用全平台共享课程访问范围，未新增课程级 ACL。收藏请求与教师回复采用会话 CSRF 校验。14 项 Notes/迁移回归测试通过，包含学生隔离、收藏、原答案保留、教师回复、引用高亮与 HTML 转义。原版 PDF 内精确高亮及浏览器跨格式展示尚未实现。

服务器上线这次还需重新构建并更新 knowledge 服务（此前部署说明中“知识服务未改”仅适用于更早版本），与 student/teacher 一起部署。MySQL 实际迁移和真实 Coze 仍需上线前在测试环境验收。

## DeepSeek Notes 接入

新增 common/deepseek_notes.py；student_app/services.py 的 notes_router 选择供应商，notes.py 仅切换 Notes 调用；其他出题/评分仍走原 Coze。notes_engine 保存 deepseek_notes 来源，学生页面显示真实回答来源与本地回退提示。使用官方 Chat Completions JSON 模式，默认模型 deepseek-flash（可通过 DEEPSEEK_MODEL 调整），接口 https://api.deepseek.com。官方文档：https://api-docs.deepseek.com/guides/json_mode/ 。

在仓库根目录 .env.deepseek 的 DEEPSEEK_API_KEY= 后填写自己的 DeepSeek API 密钥。该文件已被 Git 忽略，不能提交或发聊天；.env.deepseek.example 是无密钥模板。

停止旧本地服务后执行：

```powershell
cd C:\Users\YangJinSong\Desktop\Co_Learning_KBot_Final
.\.integration-venv\Scripts\python.exe .\KBOT\HKU_CoLearning_KBot_Formal_v8.0.13\run_notes_windows.py --ai --provider deepseek --ai-env .\KBOT\.env.deepseek
```

真实请求会把问题与检索到的课件片段发送给 DeepSeek，并使用 API 配额。此次未填写真实密钥、未完成真实 DeepSeek 调用；15 项自动测试通过（包括适配器模拟请求）。只有页面显示 DeepSeek AI 才表明本次答案来自 DeepSeek，Local excerpts 表示回退。

部署时在核心 .env 中设置 NOTES_AI_PROVIDER=deepseek、NOTES_AI_ENABLED=true、DEEPSEEK_API_KEY、DEEPSEEK_MODEL、DEEPSEEK_BASE_URL，重建 student；不要把本地绝对路径搬到服务器。

## 完整解释与真实联网搜索

DeepSeek Notes 每次明确提问先调用官方 /v1/tools/search（最多5条，查询最长128字符），再结合课件与结果生成答案。会增加一次搜索请求及耗时/费用；问题发送到搜索服务，课件片段只发到回答接口。搜索失败/无结果时继续通用知识回答，但不标为已检索。界面默认展开补充答案，覆盖范围说明折叠放末尾；仅按用户实际提问判断课件完整性，不要求简单定义包含全部技术细节。

课件引用、AI 通用知识、带网页引用的课外解释分开标注。网页 URL 仅使用搜索 API 实际返回的 HTTP(S) 链接；模型返回的网页编号需匹配白名单。额外展示实际检索页面列表，并明确不是每个结果都被答案引用。搜索摘要不能保证结论正确，仍需核对原文。现有历史记录保留答案和分段中已引用的网页来源；未使用的搜索候选列表不单独存储。

修改 common/deepseek_notes.py、common/notes_engine.py、student_app/blueprints/notes.py、student_app/static/app.js 和测试。真实 DNSSEC 请求已确认 search_status=completed、回答来源 deepseek_notes；若模型未提供有效网页编号，内容仍标 AI 通用知识，不能冒充网页引用。参考官方：https://api-docs.deepseek.com/guides/search/ 。

## 学生自主选择联网（最新行为，取代之前每次联网）

初次 Ask Notes 默认 web_search=false，仅课件+AI通用知识，不调用搜索接口。学生看到回答后点击 Explain further with web sources，才对已回答的问题和章节发起联网进一步解释；修改输入框不会改变该按钮对应的原问题。请求期间禁止重复提交；联网失败保留原回答直到新回答成功。

答案先展示解释，来源以小字注明；课件摘录、覆盖情况、回答来源和搜索状态可展开，其他搜索结果默认折叠。引用仍然可以打开和定位。进一步解释会产生一条新问答记录，历史保留两个版本，TOP10 当前按问答次数计数，所以进一步解释也计一次。尚未做对话线程归并。

修改 common/deepseek_notes.py、student_app/services.py、student_app/blueprints/notes.py、student_app/static/app.js、student_app/templates/notes.html 与测试。16项测试通过，新增断言确认默认路径只调用回答接口、不调用搜索。未请求搜索不等于不调用云端模型：问题和课件片段仍会发送给 DeepSeek 生成解释。

按钮位置调整：Explain further with web sources 紧跟答案区域，位于课件引用详情及教师反馈表单之前。修改 student_app/static/app.js。

## 缩写与拼写保护

notes_engine.py 增强提示词：保持原词字母顺序，缺少领域背景的多义缩写优先澄清，不编造常见含义、不静默纠错。增加保守规则：未在检索片段出现的简短大写缩写（以及 dnf/dfn/xyz 小写案例）先澄清；常见网络术语允许正常回答。带明确领域的完整问题交给模型判断。主动联网仍可搜索原词，但不能擅自替换。回答若遗漏原缩写、反而解释字母重排后的缩写，会被拦下并请求澄清。

这是有限规则加提示词，并非能识别所有缩写/拼写错误。修改 common/notes_engine.py 和 tests/test_notes_improvements.py；21项测试通过，覆盖 DNF 大小写、无上下文、不调用模型及 DFN 错置。普通短单词不会仅因长度被认作缩写。
